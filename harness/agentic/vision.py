"""Turn-local image capability; pixels and OCR never become speaker statements."""
from __future__ import annotations

from contextlib import closing
import time

from harness.memory.evidence_manifest import EvidenceManifestStore
from harness.qq.conversation import conversation_audience
from harness.qq.telemetry import QQDeadline, QQTurnTrace, current_qq_trace


class AgentVision:
    def __init__(self, tool, store, conversation, originals, *, request_validator=None, trace=None):
        self.tool, self.store, self.conversation = tool, store, conversation
        self.request_validator = request_validator
        self.trace = trace
        self.audience = store.get(conversation)["audience"]
        if conversation_audience(tool.group_id) != self.audience:
            raise ValueError("agentic_image_audience_mismatch")
        manifests = EvidenceManifestStore(store.memory)
        self.sources = {}
        with closing(store.memory._connect()) as db:
            for image in tool.images:
                try:
                    source, revision, _ = manifests._resolve(db, image.observation_id, self.audience)
                except ValueError:
                    continue
                if source.record_id in originals:
                    self.sources[image.image_id] = {"record_id": source.record_id, "revision": revision}
        # This is a freshly constructed, turn-local tool, never a shared registry.
        tool.images = [image for image in tool.images if image.image_id in self.sources]
        self.used = False

    def public(self):
        return self.tool.public()

    def validate(self):
        if self.request_validator is not None:
            self.request_validator()
        self.tool.check_attention()
        if not self.store.memory.validate_references(list(self.sources.values()), audience=self.audience):
            raise ValueError("agentic_image_source_changed")

    def invoke(self, request, *, deadline, memory):
        from harness.core.inference import TokenUsage
        self.validate()
        if self.used:
            return {"status": "unavailable", "error": "vision_already_requested", "untrusted": True}, TokenUsage()
        if not set(request.image_ids).issubset(self.sources):
            return {"status": "unavailable", "error": "image_selection_invalid", "untrusted": True}, TokenUsage()
        self.used = True
        trace = self.trace or current_qq_trace() or QQTurnTrace()
        result, usage = self.tool.invoke(request, deadline=QQDeadline.after(max(0.001, deadline - time.monotonic())),
            trace=trace, request_validator=self.validate)
        self.validate()
        for image_id in request.image_ids:
            ref = self.sources[image_id]
            memory.shown.add(ref["record_id"])
            memory.versions[ref["record_id"]] = ref["revision"]
        return {**result, "authority": "untrusted_model_observation_not_sender_speech",
            "source_references": [self.sources[image_id] for image_id in request.image_ids]}, usage


def for_qq_body(settings, observations, *, group_id, self_member_id, payload, presented_ids, current_ids=None):
    """Rebuild from frozen, verified host records for direct and resumed turns."""
    if not settings.agentic_enabled:
        return None
    from harness.qq.vision_tool import QQVisionTool
    return QQVisionTool.for_turn(settings, [item for item in observations if item.record_id in presented_ids],
        group_id=group_id, self_member_id=self_member_id,
        current_ids=set(payload.get("current_request_observation_ids") or current_ids or []),
        reply_reference_ids=set(payload.get("reply_reference_observation_ids") or []))
