"""Runtime orchestration for one gate, optional research and one QQ body."""
from __future__ import annotations

import time

from harness.qq.policy import QQLatencyOptions
from harness.runtime.models import utc_now
from harness.runtime.qq_web import execute_qq_web, web_capabilities


class QQTurnRejected(ValueError):
    """A single stale or invalid turn cannot fault the whole Runtime."""


def policy(supervisor, request=None):
    selected = QQLatencyOptions.model_validate(supervisor._cognitive_snapshot.payload.get("qq_latency_policy") or {})
    return selected if request is None else selected.for_conversation(
        group_id=request.group_id, self_member_id=request.self_member_id)


def validate_commands(supervisor):
    advertised = supervisor._cognitive_snapshot.payload.get("qq_turn_commands", [])
    if (not {"PrepareQQTurn", "ResumeQQResponse"}.issubset(advertised)
            or not callable(getattr(supervisor.cognitive, "prepare_qq_turn", None))
            or not callable(getattr(supervisor.cognitive, "resume_qq_response", None))):
        raise ValueError("qq_turn_commands_unavailable")


def _merged_trace(first, second, *, web_ms):
    """Cognitive snapshots share an owner clock; Runtime's web duration is explicit."""
    if (first.get("trace_id"), first.get("event_id")) != (second.get("trace_id"), second.get("event_id")):
        raise ValueError("qq_turn_trace_binding_changed")
    shift = max(0.0, (second["first_seen_mono"] - first["first_seen_mono"]) * 1000)
    stages = list(first["stages"])
    # Inter-service gap includes RPC/dispatch overhead; do not label it all web.
    gap_start = first["elapsed_ms"]
    gap_end = max(gap_start, shift)
    stages.append({"name": "runtime.web", "outcome": "ok", "error_type": None,
        "start_offset_ms": gap_start, "end_offset_ms": min(gap_end, gap_start + web_ms),
        "duration_ms": min(gap_end - gap_start, web_ms)})
    for stage in second["stages"]:
        stages.append({**stage, "start_offset_ms": stage["start_offset_ms"] + shift,
                       "end_offset_ms": stage["end_offset_ms"] + shift})
    counters = dict(first["counters"])
    for key, value in second["counters"].items():
        counters[key] = counters.get(key, 0) + value
    return {**second, "first_seen_mono": first["first_seen_mono"], "stages": stages[:256],
            "elapsed_ms": shift + second["elapsed_ms"], "counters": counters}


async def prepare(supervisor, event, request):
    validate_commands(supervisor)
    frozen = policy(supervisor, request)
    key = f"cognitive:qq-prepare-turn:{event.event_id}"
    supervisor._sync_command_index()
    payload = request.model_dump(mode="json")
    if key not in supervisor._command_intent_seqs:
        summary = supervisor._response_homeostatic_interoception()
        if summary is not None and summary["salience"] == "critical":
            raise ValueError("critical_homeostasis_before_qq")
        if request.idle_intent is not None and (summary is None or
                request.idle_intent.homeostatic_interoception.model_dump(mode="json") != summary):
            raise ValueError("qq_idle_homeostasis_changed")
        payload.update(homeostatic_interoception=summary, qq_frozen_policy=frozen.model_dump(),
            qq_turn_runtime={"runtime_instance_id": supervisor.runtime_instance_id,
                             "manifest_revision": supervisor.manifest.revision},
            qq_web_capabilities=await web_capabilities(supervisor, event, request, policy=frozen))
        goal = supervisor.goals.qq_context(request) if supervisor.goals.enabled else None
        if goal is not None:
            payload["runtime_goal_context"] = goal
    envelope = supervisor._make_command(event, command_type="PrepareQQTurn", idempotency_key=key,
                                       expected_revision=None, payload=payload)
    envelope = envelope.model_copy(update={"deadline": min(envelope.deadline, request.deadline_at)})
    supervisor._journal_homeostatic_route(event=event, command_id=envelope.command_id,
        target="qq_response", summary=envelope.payload.get("homeostatic_interoception"))
    prepared_result = await supervisor._invoke_domain(service="cognitive", envelope=envelope,
        method=supervisor.cognitive.prepare_qq_turn, event=event)
    prepared = prepared_result.result
    if not isinstance(prepared, dict) or prepared.get("kind") not in {"completed", "prepared"}:
        raise ValueError("qq_turn_prepare_result_invalid")
    if prepared["kind"] == "completed":
        prepared["trace"]["counters"].update(web_requested=0, web_used=0)
        return prepared_result.model_copy(update={"result": {"decision": prepared["decision"], "trace": prepared["trace"]}})
    binding = prepared.get("turn_binding") or {}
    if (binding.get("event_id") != event.event_id or binding.get("group_id") != request.group_id
            or binding.get("self_member_id") != request.self_member_id
            or binding.get("deadline_at") != request.deadline_at.isoformat()
            or binding.get("runtime_instance_id") != supervisor.runtime_instance_id):
        raise ValueError("qq_turn_prepared_binding_invalid")
    frozen_payload = envelope.payload
    started = time.monotonic()
    context = await execute_qq_web(supervisor, event, request, prepared,
                                  capabilities=frozen_payload["qq_web_capabilities"])
    web_ms = (time.monotonic() - started) * 1000
    prepared["trace"]["counters"].update(
        web_requested=int(bool((prepared.get("web_plan") or {}).get("steps"))),
        web_used=int(bool(context and context.get("evidence_refs"))))
    body_payload = {**frozen_payload, "prepare_id": prepared["prepare_id"],
        "snapshot_hash": prepared["snapshot_hash"], "turn_binding": binding,
        "emotion_context_ref": binding.get("emotion_context_ref"), "emotion_revision": binding.get("emotion_revision"),
        "runtime_web_context": context}
    # Frozen policy goes with the preparation, even if GUI settings change.
    body_payload["qq_frozen_policy"] = prepared["policy"]
    remaining = (request.deadline_at - utc_now()).total_seconds()
    if remaining <= 0:
        raise TimeoutError("qq_turn_expired")
    resumed = supervisor._make_command(event, command_type="ResumeQQResponse",
        idempotency_key=f"cognitive:qq-resume:{prepared['prepare_id']}", expected_revision=None, payload=body_payload)
    resumed = resumed.model_copy(update={"deadline": min(resumed.deadline, request.deadline_at)})
    result = await supervisor._invoke_domain(service="cognitive", envelope=resumed,
        method=supervisor.cognitive.resume_qq_response, event=event)
    if not isinstance(result.result, dict) or set(result.result) != {"decision", "trace"}:
        raise ValueError("qq_turn_resume_result_invalid")
    return result.model_copy(update={"result": {**result.result,
        "trace": _merged_trace(prepared["trace"], result.result["trace"], web_ms=web_ms)}})
