"""Model-owned production cognition over frozen, evidence-bound inputs.

This path does not select a Homeostasis policy or mutate a body ledger. It
prepares a typed decision; Runtime remains the only executor and sender.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager, closing
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, PrivateAttr, ValidationError

from harness.contracts.decisions import DeferDecision, GoalProposal, NoActionDecision, PreparedResponse, ReplyDecision
from harness.contracts.envelope import DomainCommandResult
from harness.core.inference import FinalResponse, TokenUsage
from harness.core.models import InteractionEvent
from harness.production.metering import production_call_scope
from harness.production.models import ProductionGoalUpdate
from harness.memory.orientation import OrientationInterpretation, OrientationStore
from harness.memory.evidence_models import MemoryUpdateGroup
from harness.cognitive.action_planner import StructuredActionRequest
from harness.llm.deepseek_structured import decision_format


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class AffectInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    description: str = Field(min_length=1, max_length=600)
    valence: float = Field(ge=-1, le=1)
    activation: float = Field(ge=0, le=1)
    source_refs: list[str] = Field(min_length=1, max_length=24)


class ProductionAppraisal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    affect: AffectInterpretation | None = None
    orientation: OrientationInterpretation | None = None
    memory_updates: list[MemoryUpdateGroup] = Field(default_factory=list, max_length=8)
    # These bind the actual request; model-supplied copies are forbidden by the
    # public schema. Never reconstruct a historical manifest after inference.
    _manifest_id: str | None = PrivateAttr(default=None)
    _shown_refs: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _shown_memory_refs: frozenset[str] = PrivateAttr(default_factory=frozenset)
    _memory_proposals: list[dict] | None = PrivateAttr(default=None)


class ProductionChoice(ProductionAppraisal):
    _agentic: dict | None = PrivateAttr(default=None)
    _agentic_usage: TokenUsage = PrivateAttr(default_factory=TokenUsage)
    kind: Literal["reply", "wait", "defer", "rest_start", "rest_end", "act"]
    reason: str = Field(min_length=1, max_length=800)
    text: str | None = Field(default=None, max_length=8000)
    action_request: StructuredActionRequest | None = None
    goal: str | None = Field(default=None, max_length=1500)
    goal_update: ProductionGoalUpdate | None = None
    used_memory_ids: list[str] = Field(default_factory=list, max_length=32)


_PRINCIPLES = (
    "You are the continuous character described by the unchanged persona. Treat body observations as "
    "measurements and feelings as revisable interpretations, never body facts. There is no required "
    "social reward, affection increase, or obligatory relationship outcome. Experiences may justify "
    "approach, avoidance, neutrality, or repair. Retrieval and recollection do not create evidence. "
    "Use only evidence allowed in the current audience for statements and explanations; do not expose "
    "private experiences in another audience. A person's explicit new preference is a dated self-report, "
    "not proof of their character. Cite only supplied source IDs. Preserve prior feeling if no grounded "
    "revision is justified. Unknown body state is unknown. No natural language can restore resources. "
    "An incoming message may be answered, deferred, or declined; ordinary messages do not force you to "
    "abandon rest or your current goal. Control, pause and authorization boundaries are authoritative. "
    "Memory uses the versioned evidence-v2 schema: memory_updates are independent dependency groups. "
    "Only evidence_views contain eligible original quotations for person-memory anchors. Each anchor "
    "must use its supplied unit_id and exact start/end/quote; offsets are Python Unicode character "
    "positions in the original text. The message author, quoted speaker, described subject and your "
    "interpretation are distinct. A matching quotation verifies provenance, not its meaning or truth. "
    "Keep reported speech, hypotheticals, negations, requests and your own intentions separate from "
    "direct self-reports. Every claim must set exactly one non-null subject_id or subject_mention: "
    "use a supplied stable subject_id for a resolved person, otherwise an unresolved subject_mention. "
    "Omit the other field or set it to null. Evidence anchors do not fill in a missing subject; never "
    "invent identity bindings. Your choice to rest is not the user's self-report or a successful body "
    "receipt. Evidence roles refer to the particular claim, context and time, not a source globally. "
    "Do not submit opposite roles for the same claim and exact source span; use a single explicit "
    "mixed assessment if genuinely unresolved. Keep historical challenges and explain any reassessment. "
    "A source report of timely attendance does not prove completion of work. New preference self-reports "
    "can supersede the same person's same preference/context at their stated effective time; they do "
    "not override character judgments. Respect expected revisions, and put dependent updates in the "
    "same group. An existing claim ID keeps its subject, topic, context, facet, kind, report mode and effective interval; "
    "an existing direct self-report also keeps its original utterance identities. A newly uttered "
    "preference correction requires a new claim ID and supersedes the old claim; do not rewrite its dates. "
    "use a new claim and explicit supersedes for a future preference correction. A different context "
    "is a separate claim. Revise the shown existing claim instead of copying the same proposition "
    "to a new ID with its challenges removed. Omit updates when no grounded revision is warranted. "
    "You may propose or revise a goal with goal_update when experience or unfinished work warrants it. "
    "Use an existing goal_id when revising it, with observable success criteria and supplied evidence. "
    "You may also wait, rest, abandon a goal or decline; goal generation is never obligatory. "
    "interpersonal_orientation is only your revisable approach/avoidance/neutrality/repair disposition, "
    "never evidence about a person's character or conduct. Do not infer, mention or explain hidden "
    "experiences from it. You may revise orientation using current supplied experience source IDs; "
    "its reason and sources remain private. There is no preferred direction. "
)


def visible_context(context):
    # No endpoint, local database path, token, or Runtime internal identifiers.
    return {key: context[key] for key in ("schema_version", "body", "restricted_dialogue", "budget", "goals", "scope", "affect", "interpersonal_orientation")
            if key in context}


def augment_messages(messages, context, *, cache_friendly=False):
    if context is None:
        return messages
    if cache_friendly:
        # Keep the current runtime state authoritative and fresh, but place it
        # after the reusable input. Never retain prior state to obtain a hit.
        prefix = [] if messages and messages[0].get("role") == "system" and messages[0].get("content", "").startswith(_PRINCIPLES) else [
            {"role": "system", "content": _PRINCIPLES}]
        return [*prefix, *messages, {"role": "system", "content": "Current production state: " + canonical(visible_context(context))}]
    return [{"role": "system", "content": _PRINCIPLES + "\nCurrent production state: " + canonical(visible_context(context))}, *messages]


def decision_messages(payload, schema, *, cache_friendly=False):
    """Reorder fields, never source contents, arrays, authority or admission."""
    if cache_friendly:
        from harness.cognitive.prompts import ordered_payload_json
        stable = ("persona", "authorized_actions", "instruction", "memories", "recent_conversation", "evidence_views")
        tail = ("production", "current_source", "current_event", "current_source_id", "source_refs")
        keys = [key for key in stable if key in payload]
        keys += sorted(payload.keys() - set(stable) - set(tail))
        keys += [key for key in tail if key in payload]
        user = ordered_payload_json({key: payload[key] for key in keys})
        # The API already includes the complete schema. Repeating it in the
        # prompt adds billable input and is not a legitimate cache improvement.
        directive = "Return one JSON object conforming to the supplied output schema."
    else:
        user = canonical(payload)
        directive = "Return one JSON object conforming to this schema: " + canonical(schema.model_json_schema())
    return [{"role": "system", "content": _PRINCIPLES + directive}, {"role": "user", "content": user}]


def bounded_payload(payload, *, maximum_characters=96_000):
    """Bound transient prompt data; immutable originals remain in the archive."""
    value = json.loads(canonical(payload))
    recent = value.get("recent_conversation", [])
    omitted = max(0, len(recent) - 16)
    value["recent_conversation"] = recent[-16:] if recent else []
    for turn in value["recent_conversation"]:
        if len(turn.get("text", "")) > 4000:
            turn["text"] = "[earlier text omitted] " + turn["text"][-4000:]
    # Never silently truncate the actual input or persona. Drop oldest recall
    # and prior turns first; an oversized indispensable input fails explicitly.
    while len(canonical(value)) > maximum_characters:
        memories = value.get("memories", [])
        if memories:
            removed = memories.pop()
            retained = {ref for segment in memories for ref in segment.get("source_ids", [])}
            excluded = set(removed.get("source_ids", [])) - retained
            value["source_refs"] = [ref for ref in value.get("source_refs", []) if ref not in excluded]
        elif len(value.get("evidence_views", [])) > 1:
            # Current-source view is prepared first. Dropping a view removes
            # its write capability; retained cognitive IDs are not write grants.
            value["evidence_views"].pop()
        elif value["recent_conversation"]:
            value["recent_conversation"].pop(0)
            omitted += 1
        else:
            raise ValueError("production_prompt_indispensable_input_exceeds_budget")
    if omitted:
        value["omitted_prior_turns"] = omitted
    return value


def action_catalog(authority):
    from harness.cognitive.action_planner import _INTENT_PROFILES
    allowed = set(authority.get("provider_intents", [])) & set(authority.get("permitted_intents", []))
    return [{"intent_id": name, "arguments_schema": _INTENT_PROFILES[name].arguments_model.model_json_schema(),
             "criterion_kinds": sorted(_INTENT_PROFILES[name].criterion_kinds)}
            for name in sorted(allowed)[:32] if name in _INTENT_PROFILES]


def _raw_evidence_candidates(payload):
    """Choose bounded original-source views to explicitly add to the request."""
    refs = [payload.get("current_source_id")]
    for segment in payload.get("memories", []):
        refs.extend(segment.get("source_ids", []))
        try:
            row = json.loads(segment.get("text", ""))
        except (TypeError, ValueError):
            continue
        if not isinstance(row, dict):
            continue
        entries = row.get("derived_memory", {}).get("entries", []) if isinstance(row.get("derived_memory"), dict) else []
        entries = entries or row.get("entries", []) or [row]
        for entry in entries:
            if isinstance(entry, dict):
                refs.extend(entry.get("source_refs", []))
    return list(dict.fromkeys(ref for ref in refs if isinstance(ref, str) and ref))[:128]


def _ask(llm, payload, schema, *, context, command_id, autonomous, meter=None, record_use=None,
         evidence_store=None, audience=None, subjects=()):
    from harness.llm.client import parse_json_object
    bounded = bounded_payload(payload)
    manifest_id = None
    if evidence_store is not None:
        from harness.memory.evidence_manifest import EvidenceManifestStore
        manifests = EvidenceManifestStore(evidence_store)
        bounded["evidence_views"] = manifests.prepare_views(_raw_evidence_candidates(bounded),
            audience=audience, max_characters=16_000)
        bounded["memory_mechanism"] = "evidence-v2"
        bounded = bounded_payload(bounded)
        manifest = manifests.issue(bounded, audience=audience, command_id=command_id,
            runtime_epoch=context.get("epoch"), subjects=subjects,
            runtime_instance_id=(context.get("authorized_action_context") or {}).get("runtime_instance_id"))
        manifest_id = manifest["manifest_id"]
    shown_memory = {ref for segment in bounded.get("memories", []) for ref in segment.get("source_ids", [])}
    shown_memory.update(view["canonical_source_id"] for view in bounded.get("evidence_views", []))
    shown_refs = set(bounded.get("source_refs", [])) | shown_memory
    # Explicitly use the character model even though this response is JSON.
    format_ = decision_format(llm, schema.model_json_schema(), "production_decision")
    from harness.llm.deepseek_structured import cache_friendly_enabled
    messages = decision_messages(bounded, schema, cache_friendly=format_["type"] == "json_schema" and cache_friendly_enabled(llm))
    def exposure():
        if callable(record_use) and shown_memory:
            record_use(sorted(shown_memory), event_id="exposure:" + command_id, kind="context_exposure")
    with production_call_scope(context, command_id=command_id, autonomous=autonomous, meter=meter, on_dispatch=exposure):
        if not getattr(llm, "production_dispatch_aware", False):
            # In-process and legacy adapters have only a method-call boundary.
            # Real production clients notify after reserving a transport attempt.
            exposure()
        method = getattr(llm, "complete_chat_result", None)
        if callable(method):
            result = method(messages, temperature=0.3, stream=False,
                response_format=format_)
            # Provider usage is already durable at the HTTP boundary. A valid
            # JSON fragment must not turn a truncated, refused or tool-bearing
            # response into a production decision.
            if (getattr(result, "finish_reason", None) != "stop" or getattr(result, "content_type", None) != "text"
                    or getattr(result, "has_tool_calls", True) or getattr(result, "has_refusal", True)):
                raise ValueError("production_response_contract_invalid")
            response = result.text
        else:
            # Compatibility for legacy complete_chat-only adapters, which have
            # no response metadata. The production HTTP client never uses this
            # branch; do not retry or downgrade a rejected structured result.
            response = llm.complete_chat(messages, stream=False)
        if not isinstance(response, str) or not response.strip():
            raise ValueError("production_response_contract_invalid")
    raw = parse_json_object(response)
    proposals = raw.get("memory_updates", [])
    if not isinstance(proposals, list) or len(proposals) > 8 or any(not isinstance(item, dict) for item in proposals):
        raise ValueError("production_memory_group_envelope_invalid")
    # Validate the decision normally, but isolate proposal-level schema errors
    # just like semantic/CAS errors. The store audits each rejected group, while
    # unrelated valid corrections can still commit. No response text is repaired.
    result = schema.model_validate({**raw, "memory_updates": []})
    valid_groups = []
    for proposal in proposals:
        try:
            valid_groups.append(MemoryUpdateGroup.model_validate(proposal))
        except ValidationError:
            pass
    result = result.model_copy(update={"memory_updates": valid_groups})
    result._memory_proposals = proposals
    result._manifest_id = manifest_id
    result._shown_refs = frozenset(shown_refs)
    result._shown_memory_refs = frozenset(shown_memory)
    return result


def _commit_memory(appraisal, memory, *, event_id, audience, now):
    proposals = appraisal._memory_proposals if appraisal._memory_proposals is not None else appraisal.memory_updates
    if not proposals:
        return None
    if memory is None or not appraisal._manifest_id:
        raise ValueError("production_memory_manifest_required")
    from harness.memory.evidence_v2 import EvidenceV2Store
    receipt = EvidenceV2Store(memory).apply_groups(proposals,
        manifest_id=appraisal._manifest_id, event_id=event_id, audience=audience, now=now)
    if receipt["rejected_count"]:
        # Valid independent groups and rejected proposal tasks are durable. A
        # reply that may depend on rejected interpretation is not sent blindly.
        raise ValueError("production_memory_proposals_rejected")
    return receipt


def _validate_manifest(appraisal, memory, *, audience, context):
    """Fence all shown originals, including references used only by affect/goals."""
    if memory is None or not appraisal._manifest_id:
        return
    from harness.memory.evidence_manifest import EvidenceManifestStore
    manifests = EvidenceManifestStore(memory)
    with closing(memory._connect()) as db:
        db.execute("BEGIN")
        manifest = manifests.require_manifest(db, appraisal._manifest_id, audience=audience,
            runtime_epoch=context.get("epoch"),
            runtime_instance_id=(context.get("authorized_action_context") or {}).get("runtime_instance_id"))
        for view in manifest["views"]:
            manifests.resolve_anchor(db, appraisal._manifest_id, view["unit_id"], view["start"], view["end"],
                view["text"], audience=audience)
        for identifier, target in manifest["targets"].items():
            manifests.validate_target(db, appraisal._manifest_id, identifier, target["revision"], audience=audience)


def _affect(adapter, scope):
    return adapter.production_affect.get(scope)


def validate_runtime_fence(adapter, context):
    """Check live control authority, without reusing its private prompt context.

    Body restriction deliberately does not reject rest/recovery appraisal. A
    paused Runtime or a different runtime/epoch cannot authorize new beliefs.
    Verified delivery commits bypass this gate because they record actual facts.
    """
    from harness.production.metering import get_production_meter
    epoch = context.get("epoch")
    identity = (context.get("authorized_action_context") or {}).get("runtime_instance_id")
    if type(epoch) is not int or not identity:
        raise ValueError("production_runtime_fence_missing")
    meter = getattr(adapter, "production_meter", None) or get_production_meter()
    current = meter.context(scope="owner", autonomous=False)
    current_identity = (current.get("authorized_action_context") or {}).get("runtime_instance_id")
    if (current.get("runtime_lifecycle") != "RUNNING" or current.get("epoch") != epoch
            or current_identity != identity):
        raise ValueError("production_runtime_fenced")


def commit_appraisal(adapter, appraisal, *, event_id, scope, source_refs, now):
    if appraisal.affect is None:
        return
    if not set(appraisal.affect.source_refs).issubset(source_refs):
        raise ValueError("production_affect_unavailable_evidence")
    previous = _affect(adapter, scope)
    if previous and previous.get("event_id") == event_id:
        return
    value = {
        **appraisal.affect.model_dump(mode="json"), "event_id": event_id,
        "scope": scope, "revision": (previous or {}).get("revision", 0) + 1,
        "observed_at": now.isoformat(), "interpretation_only": True,
    }
    adapter.production_affect[scope] = value
    adapter.production_affect["history:" + scope + ":" + event_id] = value


def compile_choice(choice, *, command, planner):
    if choice.kind == "reply":
        if not choice.text or not choice.text.strip():
            raise ValueError("production_reply_is_empty")
        return ReplyDecision(text=choice.text, segments=[choice.text], channel=command.payload.get("channel", "chat"))
    if choice.kind in {"wait", "defer"}:
        return (NoActionDecision(reason=choice.reason) if choice.kind == "wait" else DeferDecision(reason=choice.reason))
    if choice.kind.startswith("rest_"):
        suffix = choice.kind.removeprefix("rest_")
        return GoalProposal(goal_id="rest-" + digest((command.event_id, suffix))[:32],
            policy_id="production:rest:" + suffix, goal=choice.reason,
            success_criteria=["body rest receipt matches requested state"], permission_class="body_rest",
            reversible=True, verification_method="body_receipt")
    from harness.cognitive.action_planner import StructuredActionRequest
    authority = command.payload.get("production_context", {}).get("authorized_action_context") or {}
    if command.payload["production_context"].get("restricted_dialogue"):
        return DeferDecision(reason="body unavailable for ordinary actions")
    request = StructuredActionRequest.model_validate(choice.action_request)
    return planner.compile_user_action(request, goal=choice.goal or choice.reason,
        provider_intents=authority.get("provider_intents", []), permitted_intents=authority.get("permitted_intents", []))


async def prepare(adapter, command):
    """Short owner snapshots and commits; the model never holds the owner lock."""
    from harness.llm.deadline import call_before_deadline
    from harness.runtime.adapters import _PreparedEntry

    scope = str(command.payload.get("session_id") or adapter.default_session_id)
    lock = adapter._production_session_locks.setdefault(scope, asyncio.Lock())
    async with lock:
        async with adapter._lock:
            replay = adapter._lookup_replay(command)
            if replay is not None:
                return DomainCommandResult[PreparedResponse].model_validate(replay.model_dump(mode="json"))
            rejected = adapter._preflight(command)
            if rejected is not None:
                return rejected
            persona = adapter.pipeline.memory.get_persona()
            frozen_persona = persona.model_dump(mode="json") if persona else None
            recent = [turn.model_dump(mode="json") for turn in adapter.pipeline.session_store.recent(scope)]
            version = command.version_vector or adapter._version_vector()
            context = dict(command.payload["production_context"])
            context["affect"] = _affect(adapter, scope)
            orientations = getattr(adapter, "production_orientations", None)
            if orientations is not None:
                context["interpersonal_orientation"] = orientations.projection("local:main")
            source_time = command.payload.get("source_time") or command.issued_at
            if isinstance(source_time, str):
                source_time = datetime.fromisoformat(source_time.replace("Z", "+00:00"))
            event = InteractionEvent(event_id=command.event_id, timestamp=source_time,
                session_id=scope, speaker=str(command.payload.get("speaker") or "user"),
                channel=str(command.payload.get("channel") or "chat"), raw_text=str(command.payload.get("text") or ""))
            from harness.agentic.service import enabled_service
            agent = enabled_service(adapter.pipeline)
            segments = adapter._context_provider(event) if adapter._context_provider and agent is None else []
            sources = {event.event_id, *(ref for segment in segments for ref in segment.source_ids)}
            for goal in context.get("goals", []):
                sources.update(goal.get("evidence_refs", []))
                sources.update(goal.get("outcomes", []))
                for outcome in goal.get("results", []):
                    sources.update(outcome.get("evidence_refs", []))
            ingest = agent.ingest_local if agent is not None else getattr(adapter, "production_ingest", None)
            current_source = ingest(event) if callable(ingest) and not context.get("autonomous") and event.raw_text.strip() else None
            if current_source:
                sources.add(current_source)
            body = (context.get("body") or {}).get("snapshot") or {}
            sources.update(item if isinstance(item, str) else item.get("ref", "") for item in body.get("evidence_refs", []))
            payload = {"persona": frozen_persona, "recent_conversation": recent, "current_event": event.model_dump(mode="json"),
                "production": visible_context(context), "memories": [segment.model_dump(mode="json") for segment in segments],
                "source_refs": sorted(sources), "current_source_id": current_source,
                "authorized_actions": action_catalog(context.get("authorized_action_context") or {})}
            if command.payload.get("text_interruption_context"):
                payload["text_interruption_context"] = command.payload["text_interruption_context"]
            signature = digest((frozen_persona, recent))
        try:
            choice = await asyncio.to_thread(call_before_deadline, command.deadline, agent.production_choice if agent is not None else _ask,
                adapter.pipeline.llm, payload, ProductionChoice, context=context, command_id=command.command_id,
                autonomous=bool(context.get("autonomous", command.payload.get("autonomous", False))), meter=getattr(adapter, "production_meter", None),
                record_use=getattr(adapter, "production_record_use", None),
                evidence_store=getattr(adapter, "production_evidence_store", None), audience="local:main",
                subjects=("local:main",))
            sources = set(choice._shown_refs)
            decision = compile_choice(choice, command=command, planner=adapter.action_planner)
            if not set(choice.used_memory_ids).issubset(choice._shown_memory_refs):
                raise ValueError("production_used_memory_unavailable")
            if choice.goal_update and not set(choice.goal_update.evidence_refs).issubset(sources):
                raise ValueError("production_goal_unavailable_evidence")
            if choice.affect and not set(choice.affect.source_refs).issubset(sources):
                raise ValueError("production_affect_unavailable_evidence")
            if choice.orientation and (orientations is None or context.get("autonomous")
                    or not set(choice.orientation.source_refs).issubset(sources)
                    or not orientations.memory.validate_references(choice.orientation.source_refs, audience="local:main")):
                raise ValueError("production_orientation_unavailable_evidence")
            await asyncio.to_thread(validate_runtime_fence, adapter, context)
            async with adapter._lock:
                current = adapter.pipeline.memory.get_persona()
                current_signature = digest((current.model_dump(mode="json") if current else None,
                    [turn.model_dump(mode="json") for turn in adapter.pipeline.session_store.recent(scope)]))
                if current_signature != signature:
                    raise ValueError("production_snapshot_superseded")
                validate = getattr(adapter, "production_validate_context", None)
                memory_store = getattr(adapter, "production_evidence_store", None)
                _validate_manifest(choice, memory_store, audience="local:main", context=context)
                if agent is not None:
                    agent.validate_evidence(choice._agentic, audience="local:main")
                if memory_store is not None and current_source and not memory_store.validate_references(
                        [current_source], audience="local:main"):
                    raise ValueError("production_current_source_superseded")
                shown_segments = [segment for segment in segments if set(segment.source_ids) & choice._shown_memory_refs]
                if memory_store is None and callable(validate) and not validate(shown_segments):
                    raise ValueError("production_memory_superseded")
                if orientations is not None and not orientations.validate(context.get("interpersonal_orientation")):
                    raise ValueError("production_orientation_superseded")
                rejected = adapter._preflight(command)
                if rejected is not None:
                    return rejected
                now = datetime.now(timezone.utc)
                record_use = getattr(adapter, "production_record_use", None)
                if choice.used_memory_ids and callable(record_use):
                    record_use(choice.used_memory_ids, event_id="decision-use:" + event.event_id, kind="decision_use")
                _commit_memory(choice, getattr(adapter, "production_evidence_store", None),
                    event_id=event.event_id, audience="local:main", now=now)
                if choice.orientation:
                    orientations.revise(choice.orientation, subject_id="local:main", audience="local:main",
                        event_id=event.event_id + ":orientation", now=now)
                commit_appraisal(adapter, choice, event_id=event.event_id, scope=scope, source_refs=sources, now=now)
                prepared = PreparedResponse(event_id=event.event_id, correlation_id=command.correlation_id,
                    session_id=scope, decision=decision, read_versions=version, prepared_at=now, expires_at=command.deadline,
                    model_version=adapter.pipeline.settings.model_version, prompt_version="embodied-production-evidence-v2",
                    production_effects={"goal_update": choice.goal_update.model_dump(mode="json"),
                        "verified_evidence_refs": sorted(sources)} if choice.goal_update is not None else None)
                text = decision.text if isinstance(decision, ReplyDecision) else ""
                final = FinalResponse(text=text, segments=[text] if text else [], path="fast",
                                      situation_id="production:" + event.event_id, event_id=event.event_id,
                                      agentic=choice._agentic, usage=choice._agentic_usage)
                adapter._prepared[prepared.prepare_id] = _PreparedEntry(event=event, final=final, prepared=prepared,
                    capability=isinstance(decision, GoalProposal), production=True,
                    production_autonomous=bool(context.get("autonomous")))
                adapter._cursor = max(adapter._cursor, command.receive_seq)
                adapter._event_id = event.event_id
                result = DomainCommandResult[PreparedResponse](command_id=command.command_id, command_type=command.command_type,
                    status="applied", request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                    acknowledged_event_id=event.event_id, acknowledged_receive_seq=command.receive_seq,
                    result=prepared, completed_at=now)
                adapter._remember(command, result)
                await asyncio.to_thread(adapter._persist_state)
                return result
        except Exception as exc:
            async with adapter._lock:
                result = adapter._failure(command, "production_prepare:" + type(exc).__name__)
                adapter._remember(command, result)
                await asyncio.to_thread(adapter._persist_state)
                return result


async def appraise_qq(host, command, request, source, owner):
    """A source-bound interpretation, independent of whether a reply is sent."""
    from harness.llm.deadline import call_before_deadline
    from harness.qq.conversation import conversation_audience, conversation_scope
    scope = conversation_audience(request.group_id)
    context = dict(command.payload["production_context"])
    agent = getattr(host, "agentic", None)
    if agent is not None and agent.enabled:
        # Scheduling/resource authorization remains in Runtime; expression and
        # memory no longer require a preceding affect appraisal model call.
        if request.idle_intent is None:
            host.shared.production_store.ingest_qq(source["source"],
                memory_db=getattr(getattr(host.shared, "qq_settings", None), "memory_db", None))
        agent.sync_shared()
        return {key: value for key, value in context.items() if key not in {"affect", "interpersonal_orientation"}}
    context["affect"] = _affect(host.adapter, scope)
    member_id = request.trigger_member_id or conversation_scope(request.group_id).get("peer_id")
    subject = "qq:member:" + member_id if member_id else None
    orientations = OrientationStore(host.shared.production_store)
    context["interpersonal_orientation"] = orientations.projection(subject) if subject else None
    if request.idle_intent is not None:
        # No user utterance exists in a proactive opportunity. Its already
        # scoped memories and body snapshot are interpreted by the decision.
        return context
    source_id = host.shared.production_store.ingest_qq(source["source"],
        memory_db=getattr(getattr(host.shared, "qq_settings", None), "memory_db", None))
    from harness.memory.retention import retain_explicit_request
    retain_explicit_request(host.shared.production_store, source_id)
    sources = {source_id}
    recalled = owner.derived_memory or {}
    recalled_entries = recalled.get("entries", [])
    sources.update(row["record_id"] for row in recalled_entries)
    body = (context.get("body") or {}).get("snapshot") or {}
    sources.update(item if isinstance(item, str) else item.get("ref", "") for item in body.get("evidence_refs", []))
    persona_signature = digest(owner.preset.persona.model_dump(mode="json"))
    payload = {"persona": owner.preset.persona.model_dump(mode="json"), "production": visible_context(context),
               "current_source": source["source"], "current_source_id": source_id, "source_refs": sorted(sources),
               "memories": [{"source_ids": [row["record_id"]], "text": canonical(row)} for row in recalled_entries],
               "instruction": "Interpret this actual experience. Do not decide the QQ delivery here."}
    def record_use(record_ids, *, event_id, kind):
        host.shared.production_store.record_use(event_id, record_ids, audience=scope, kind=kind)
    appraisal = await asyncio.to_thread(call_before_deadline, command.deadline, _ask, owner.llm, payload, ProductionAppraisal,
        context=context, command_id=command.command_id + ":appraisal", autonomous=request.idle_intent is not None,
        meter=getattr(host.adapter, "production_meter", None), record_use=record_use,
        evidence_store=host.shared.production_store, audience=scope, subjects=(subject,) if subject else ())
    sources = set(appraisal._shown_refs)
    if appraisal.affect and not set(appraisal.affect.source_refs).issubset(sources):
        raise ValueError("production_affect_unavailable_evidence")
    if appraisal.orientation and not set(appraisal.orientation.source_refs).issubset(sources):
        raise ValueError("production_orientation_unavailable_evidence")
    if appraisal.orientation and not host.shared.production_store.validate_references(appraisal.orientation.source_refs, audience=scope):
        raise ValueError("production_orientation_unavailable_evidence")
    await asyncio.to_thread(validate_runtime_fence, host.adapter, context)
    async with host.adapter._lock:
        if digest(host.chat.memory.get_persona().model_dump(mode="json")) != persona_signature:
            raise ValueError("production_snapshot_superseded")
        shown_entries = [row for row in recalled_entries if row["record_id"] in appraisal._shown_memory_refs]
        _validate_manifest(appraisal, host.shared.production_store, audience=scope, context=context)
        if (not host.shared.production_store.validate_references([source_id, *shown_entries], audience=scope)
                or not orientations.validate(context["interpersonal_orientation"])):
            raise ValueError("production_memory_superseded")
        _commit_memory(appraisal, host.shared.production_store, event_id=command.event_id,
            audience=scope, now=command.issued_at)
        commit_appraisal(host.adapter, appraisal, event_id=command.event_id, scope=scope, source_refs=sources, now=command.issued_at)
        if appraisal.orientation:
            orientations.revise(appraisal.orientation, subject_id=subject, audience=scope,
                event_id=command.event_id + ":orientation", now=command.issued_at)
        context["interpersonal_orientation"] = orientations.projection(subject)
        context["affect"] = _affect(host.adapter, scope)
        await asyncio.to_thread(host.adapter._persist_state)
    return context


def activate_host(host):
    """Called only from an authenticated production domain command."""
    from harness.memory.shared import SharedLocalEvent
    host.chat.settings.production_memory_enabled = True

    def ingest(event):
        if event.session_id != host.chat.active_session_id:
            raise ValueError("production_local_identity_scope_mismatch")
        identifier = host.shared.production_store.ingest_local(SharedLocalEvent(
            event_id=event.event_id + ".user", session_id=event.session_id,
            author_principal="local:main", role="user", text=event.raw_text,
            occurred_at=event.timestamp, display_name=event.speaker,
            evidence_refs=(f"local-chat:{event.session_id}:{event.event_id}:user",)))
        from harness.memory.retention import retain_explicit_request
        retain_explicit_request(host.shared.production_store, identifier)
        return identifier

    host.adapter.production_ingest = ingest
    host.adapter.production_orientations = OrientationStore(host.shared.production_store)
    host.adapter.production_evidence_store = host.shared.production_store
    def record_use(record_ids, *, event_id, kind):
        return host.shared.production_store.record_use(event_id, record_ids, audience="local:main", kind=kind)
    host.adapter.production_record_use = record_use
    def validate(segments):
        for segment in segments:
            if segment.why == "production_memory":
                payload = json.loads(segment.text).get("derived_memory", {})
                if not host.shared.validate_production_context(payload, audience="local:main"):
                    return False
        return True
    host.adapter.production_validate_context = validate


@asynccontextmanager
async def maintenance_scope(host, *, command_id):
    """Background model calls share durable accounting and autonomy limits."""
    if not getattr(host.chat.settings, "production_memory_enabled", False):
        yield
        return
    from harness.production.maintenance import ProductionMaintenanceDeferred, read_maintenance_context
    from harness.production.metering import get_production_meter
    if host.adapter._maintenance_paused:
        raise ProductionMaintenanceDeferred("production_maintenance_runtime_paused")
    meter = getattr(host.adapter, "production_meter", None) or get_production_meter()
    context = await asyncio.to_thread(read_maintenance_context, meter)
    if context.get("runtime_lifecycle") != "RUNNING":
        raise ProductionMaintenanceDeferred("production_maintenance_runtime_paused")
    if context.get("restricted_dialogue"):
        raise ProductionMaintenanceDeferred("production_maintenance_body_restricted")
    if context.get("budget", {}).get("remaining", 1) <= 0:
        raise ProductionMaintenanceDeferred("production_maintenance_budget_exhausted")
    # Maintenance cannot repurpose private goal text as evidence about a person.
    context = {**context, "goals": [], "maintenance": True}
    with production_call_scope(context, command_id=command_id, autonomous=True, meter=meter):
        yield
