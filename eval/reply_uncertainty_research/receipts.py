"""Research-only host failure admission, persisted through the existing checkpoint.

Only the actual fixture invocation can produce a receipt. Never extract one from
assistant plans, user text, OCR, or arbitrary messages.
"""

import hashlib
import json


def failure_entries(receipts, *, group_id, presented_ids):
    from harness.core.inference import MemoryEvidenceEntry
    from harness.qq.conversation import conversation_audience

    entries = []
    audience = conversation_audience(group_id)
    for receipt in receipts:
        if receipt["group_id"] != group_id:
            continue
        if not receipt["sources"] or not all(s["observation_id"] in presented_ids for s in receipt["sources"]):
            continue
        code = receipt["result"].get("error")
        if receipt["result"].get("status") != "unavailable" or not isinstance(code, str) or not code:
            continue
        fingerprint = hashlib.sha256(json.dumps(receipt, sort_keys=True).encode()).hexdigest()
        entries.append(
            MemoryEvidenceEntry(
                source_id="qq-image-failure:" + fingerprint,
                text=code,
                audience=audience,
                source_channel="qq_image_execution_failure",
                source_conversation_id=audience,
                source_refs=[
                    ref for source in receipt["sources"] for ref in (source["observation_id"], source["evidence_ref"])
                ],
                status="retained_fact",
            )
        )
    return entries


def install():
    from copy import deepcopy
    from harness.qq import prepared_turn, vision_tool

    receipts = []
    original_invoke = vision_tool.QQVisionTool.invoke
    original_checkpoint = prepared_turn.checkpoint_gate

    def invoke(tool, request, **kwargs):
        result, usage = original_invoke(tool, request, **kwargs)
        selected = [i for i in tool.images if i.image_id in request.image_ids]
        if result.get("status") == "unavailable" and len(selected) == len(request.image_ids):
            receipts.append(
                {"group_id": tool.group_id, "sources": [i.public() for i in selected], "result": deepcopy(result)}
            )
        return result, usage

    def checkpoint(**kwargs):
        entries = failure_entries(
            receipts, group_id=kwargs["result"].group_id, presented_ids=set(kwargs["body_selection"].observation_ids)
        )
        return original_checkpoint(**(kwargs | {"vision_evidence": [*kwargs.get("vision_evidence", ()), *entries]}))

    vision_tool.QQVisionTool.invoke = invoke
    prepared_turn.checkpoint_gate = checkpoint
