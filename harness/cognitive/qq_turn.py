"""One appraisal/guidance/gate preparation and a separately resumable QQ body."""
from __future__ import annotations

import asyncio
from contextlib import nullcontext
from datetime import datetime, timezone
import hashlib
import json

from harness.contracts.envelope import DomainCommandResult
from harness.qq.failures import failure_detail
from harness.qq.policy import QQLatencyOptions, settings_with_policy
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQDeadline, QQTurnTrace, qq_trace_scope
from harness.runtime.qq_sources import verified_qq_source
from infra.trace.request_spans import trace_scope


_INTERNAL = {"emotion_context_ref", "emotion_revision", "runtime_goal_context", "runtime_web_context",
             "qq_turn_runtime", "qq_frozen_policy", "qq_web_capabilities", "prepare_id", "snapshot_hash",
             "turn_binding", "qq_emotion_trace", "production_context"}


def _hash(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _request(payload):
    return QQSharedPrepareRequest.model_validate({key: value for key, value in payload.items() if key not in _INTERNAL})


def _binding(command, request, source):
    runtime = command.payload.get("qq_turn_runtime") or {}
    if set(runtime) != {"runtime_instance_id", "manifest_revision"} or not all(runtime.values()):
        raise ValueError("qq_turn_runtime_binding_missing")
    if (runtime["runtime_instance_id"], runtime["manifest_revision"]) != (
            command.runtime_instance_id, command.manifest_revision):
        raise ValueError("qq_turn_runtime_binding_changed")
    return {**runtime, "event_id": command.event_id, "transport": request.transport,
        "group_id": request.group_id, "self_member_id": request.self_member_id,
        "trigger_member_id": request.trigger_member_id, "deadline_at": request.deadline_at.isoformat(),
        "request_id": request.request_id or command.event_id, "request_revision": request.revision or 1,
        "source_event_ids": request.source_event_ids or [request.trace.event_id],
        "authority_anchor": request.authority_anchor or request.trace.event_id,
        "source_digest": source["digest"], "request_digest": _hash(request.model_dump(mode="json")),
        "emotion_context_ref": command.payload.get("emotion_context_ref"),
        "emotion_revision": command.payload.get("emotion_revision")}


def _resume_owner(host, request):
    from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
    preset = host.chat.library.load(host.chat.active_preset_id)
    preset = preset.model_copy(update={"persona": host.chat.memory.get_persona()})
    settings = host.chat.settings.model_copy(update={"record_interaction": False, "call_log_enabled": False,
        "continuation_enabled": False, "max_reply_segments": 3, "aux_llm_preload": False})
    agent = getattr(host, "agentic", None)
    return SharedQQBinding(settings, preset, agent.llm if agent is not None and agent.enabled else host.chat.llm,
        ScopedQQMemoryView(host.chat.memory, group_id=request.group_id, member_id=request.trigger_member_id), {}, ())


def _production_checkpoint_fence(host, command, payload, *, runtime=True):
    """Cached domain results cannot bypass current source or control authority."""
    from harness.cognitive.production import validate_runtime_fence
    from harness.qq.prepared_evidence import validate_used_sources
    from harness.qq.turn_checkpoints import QQTurnCheckpointStore, QQTurnStateError
    request = _request(command.payload)
    source = verified_qq_source(request, host.shared.qq_settings)
    store = host.shared.production_store
    # Autonomous opportunities are validated typed intents, not utterances.
    # Their recalled evidence and orientation are still fenced below.
    current_refs = []
    if request.idle_intent is None:
        source_id = store.ingest_qq(source["source"], memory_db=host.shared.qq_settings.memory_db)
        current_refs.append({"record_id": source_id, "revision": 0})
    current = {"version": "qq-used-sources-v2", "group_id": request.group_id,
        "owner_id": store.owner_id, "preset_id": host.chat.active_preset_id,
        "shared": [], "derived": [], "production": current_refs}
    validate_used_sources(host, current)
    prepare_id = payload.get("prepare_id") or command.payload.get("prepare_id")
    if not prepare_id:
        raise QQTurnStateError("qq_turn_not_found")
    checkpoint = QQTurnCheckpointStore(host.shared.qq_settings.memory_db).load(prepare_id)
    binding = _binding(command, request, source)
    if checkpoint["binding"] != binding:
        raise QQTurnStateError("qq_turn_request_changed")
    snapshot = checkpoint.get("snapshot") or {}
    manifest = snapshot.get("used_sources") or checkpoint.get("used_sources")
    if manifest is not None:
        validate_used_sources(host, manifest)
    agent = getattr(host, "agentic", None)
    if agent is not None and agent.enabled:
        from harness.qq.conversation import conversation_audience
        decision = payload.get("decision") or {}
        agent.validate_evidence(decision.get("agentic_evidence"), audience=conversation_audience(request.group_id))
    if runtime:
        validate_runtime_fence(host.adapter, command.payload["production_context"])


async def run(host, command, *, resume: bool):
    from harness.qq.attention import ATTENTION_DENIALS, require_attention
    group = str(command.payload.get("group_id", ""))
    try:
        revision = require_attention(group, received_at=(command.payload.get("trace") or {}).get("first_seen_at"))
        result = await _run_attended(host, command, resume=resume, attention_revision=revision)
        require_attention(group, revision=revision)
        return result
    except ValueError as exc:
        if str(exc) not in ATTENTION_DENIALS:
            raise
        return host.adapter._failure(command, str(exc), status="rejected").model_copy(
            update={"result": failure_detail(exc)})


async def _run_attended(host, command, *, resume, attention_revision):
    from harness.qq.attention import processing_evidence
    phase = "qq_resume" if resume else "qq_prepare_turn"
    with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                     command_id=command.command_id, phase=phase, span_id=command.command_id,
                     attention_context="qq:" + str(command.payload.get("group_id", "")),
                     attention_revision=attention_revision,
                     attention_evidence_refs=processing_evidence(command.payload),
                     qq_trace_id=(command.payload.get("trace") or {}).get("trace_id")):
        from harness.production.metering import production_call_scope
        context = command.payload.get("production_context")
        if context is not None:
            host.chat.settings.production_memory_enabled = True
            await host.recover_production_memory()
        async def operation():
            from harness.agentic.metering import command_scope
            agent = getattr(host, 'agentic', None)
            foreground = agent is not None and agent.enabled and not command.payload.get('idle_intent')
            with (agent.priority.foreground_turn() if foreground else nullcontext()), command_scope(host, command), production_call_scope(context, command_id=command.command_id,
                    autonomous=bool((context or {}).get("autonomous", False)),
                    meter=getattr(host.adapter, "production_meter", None)):
                if (agent is not None and agent.enabled and command.payload.get('idle_intent') and
                        host.chat.settings.agentic_regulation.mode == 'on'):
                    from harness.agentic.background import run as run_background
                    return await run_background(agent, lambda: _run_locked(host, command, resume=resume))
                return await _run_locked(host, command, resume=resume)
        return await host.adapter._run_command(operation)


async def _run_locked(host, command, *, resume):
    from harness.qq.decision import get_qq_turn_preparation, prepare_qq_turn, resume_qq_response
    from harness.qq.prepared_evidence import capture_used_sources, validate_used_sources
    adapter = host.adapter
    # Start measuring before contention, independent of another process's monotonic epoch.
    trace = QQTurnTrace()
    waiting = trace.clock()
    production = command.payload.get("production_context") is not None
    agent = getattr(host, "agentic", None)
    protected = production or (agent is not None and agent.enabled)
    operation_lock = (adapter._production_session_locks.setdefault("qq:" + str(command.payload.get("group_id")), asyncio.Lock())
                      if protected else adapter._lock)
    async with operation_lock:
        trace.record("cognitive.lock_wait", started_mono=waiting)
        async with adapter._lock if protected else nullcontext():
            replay = adapter._lookup_replay(command)
            if replay is not None and not protected:
                return replay
            rejected = adapter._preflight(command)
            if rejected is not None:
                adapter._remember(command, rejected)
                await asyncio.to_thread(adapter._persist_state)
                return rejected
            frozen_persona = _hash(host.chat.memory.get_persona().model_dump(mode="json")) if protected else None
            frozen_preset = host.chat.active_preset_id
        if replay is not None:
            if replay.status in {"applied", "idempotent_replay"} and isinstance(replay.result, dict):
                try:
                    await asyncio.to_thread(_production_checkpoint_fence, host, command, replay.result, runtime=production)
                except ValueError:
                    return adapter._failure(command, "qq_turn_used_source_changed", status="rejected")
            return replay
        with qq_trace_scope(trace):
            try:
                expected = "ResumeQQResponse" if resume else "PrepareQQTurn"
                if command.command_type != expected or host.shared.qq_settings is None:
                    raise ValueError("qq_turn_owner_unavailable")
                request = _request(command.payload)
                trace.trace_id, trace.event_id = request.trace.trace_id, request.trace.event_id
                trace.first_seen_at = request.trace.first_seen_at
                trace.observation_sequence = request.trace.observation_sequence
                if not resume and command.payload.get("qq_emotion_trace") is not None:
                    trace.merge_prior_phase(command.payload["qq_emotion_trace"])
                frozen = QQLatencyOptions.model_validate(command.payload["qq_frozen_policy"])
                settings = settings_with_policy(host.chat.settings, frozen)
                trace.bind_configuration(settings)
                if request.homeostatic_interoception is not None:
                    from harness.runtime.adapters import _validated_homeostatic_interoception
                    _validated_homeostatic_interoception(command)
                with trace.stage("source.verify"):
                    source = await asyncio.to_thread(verified_qq_source, request, host.shared.qq_settings)
                    if agent is not None and agent.enabled and settings.agentic_regulation.mode == 'on':
                        from harness.runtime.qq_sources import protocol_wake
                        source['native_wake'] = await asyncio.to_thread(protocol_wake, request, source['source'])
                binding = _binding(command, request, source)
                def validate_current_request(*, runtime=True):
                    from harness.qq.turn_checkpoints import QQTurnStateError
                    try:
                        current = verified_qq_source(request, host.shared.qq_settings)
                    except ValueError as exc:
                        raise QQTurnStateError("qq_turn_request_changed") from exc
                    if current["digest"] != binding["source_digest"]:
                        raise QQTurnStateError("qq_turn_request_changed")
                    if protected and (host.chat.active_preset_id != frozen_preset or
                            _hash(host.chat.memory.get_persona().model_dump(mode="json")) != frozen_persona):
                        raise QQTurnStateError("qq_turn_configuration_changed")
                    if production and runtime:
                        from harness.cognitive.production import validate_runtime_fence
                        try:
                            validate_runtime_fence(adapter, command.payload["production_context"])
                        except Exception as exc:
                            raise QQTurnStateError("qq_turn_cancelled") from exc
                from harness.compiler.compiler import command_emotion_deadline
                remaining = (command_emotion_deadline(host, command, request.deadline_at)
                             - datetime.now(timezone.utc)).total_seconds()
                from harness.qq.production_deadline import downstream_reserve
                deadline = QQDeadline.after(max(0, remaining),
                    downstream_reserved_seconds=downstream_reserve(command) if production else 0.0)
                deadline.check()
                if resume:
                    if command.payload.get("turn_binding") != binding:
                        raise ValueError("qq_turn_resume_binding_changed")
                    owner = _resume_owner(host, request)
                    from dataclasses import replace
                    owner = replace(owner, settings=settings_with_policy(owner.settings, frozen))
                    # Do not recall memory or ask either emotion model here.
                    decision = await asyncio.to_thread(resume_qq_response,
                        memory_db=host.shared.qq_settings.memory_db,
                        prepare_id=command.payload["prepare_id"], snapshot_hash=command.payload["snapshot_hash"],
                        turn_binding=binding, runtime_web_context=command.payload.get("runtime_web_context"),
                        used_sources_validator=lambda manifest: validate_used_sources(host, manifest),
                        request_validator=validate_current_request,
                        shared_owner=owner, deadline=deadline, trace=trace)
                    host.qq_memory.note_recall(decision)
                    payload = {"decision": decision.model_dump(mode="json"), "trace": trace.snapshot()}
                else:
                    previous = await asyncio.to_thread(get_qq_turn_preparation,
                        memory_db=host.shared.qq_settings.memory_db, turn_binding=binding)
                    if previous is not None:
                        if protected:
                            await asyncio.to_thread(_production_checkpoint_fence, host, command, previous, runtime=production)
                        prepared = previous
                    else:
                        with trace.stage("shared_memory"):
                            owner = await asyncio.to_thread(host.shared.qq_binding, group_id=request.group_id,
                                member_id=request.trigger_member_id, query=request.query)
                        # Rebuild only the immutable wrapper to freeze this round's policy.
                        from dataclasses import replace
                        owner = replace(owner, settings=settings_with_policy(owner.settings, frozen))
                        deadline.check()
                        await asyncio.to_thread(validate_current_request)
                        if production:
                            from harness.cognitive.production import appraise_qq
                            from harness.production.metering import _scope
                            current = _scope.get()
                            current.context = await appraise_qq(host, command, request, source, owner)
                            # This experience may have corrected a recalled belief.
                            owner = await asyncio.to_thread(host.shared.qq_binding, group_id=request.group_id,
                                member_id=request.trigger_member_id, query=request.query)
                            owner = replace(owner, settings=settings_with_policy(owner.settings, frozen))
                            guidance = None
                        else:
                            with trace.stage("emotion.guidance"):
                                guidance = await adapter.emotion_guidance(command)
                        values = request.model_dump(exclude={"trace", "deadline_at", "transport",
                            "request_id", "revision", "source_event_ids", "authority_anchor"})
                        values.update(verified_self_aliases=request.verified_self_aliases,
                            idle_intent=request.idle_intent, homeostatic_interoception=request.homeostatic_interoception,
                            emotion_guidance=guidance)
                        goal = command.payload.get("runtime_goal_context")
                        if goal is not None:
                            from harness.contracts.goals import QQGoalContext
                            values["goal_context"] = QQGoalContext.model_validate(goal)
                        prepared = await asyncio.to_thread(prepare_qq_turn,
                            memory_db=host.shared.qq_settings.memory_db, **values,
                            _request_source_event_ids=(None if request.idle_intent is not None else
                                (getattr(request, "source_event_ids", None) or [request.trace.event_id])),
                            _request_authority_record_id=(source["source"]["record_id"]
                                if request.request_id is not None else None),
                            _reply_reference_record_ids=source.get("reply_reference_record_ids", []),
                            turn_binding=binding, web_capabilities=command.payload.get("qq_web_capabilities"),
                            native_wake=source.get('native_wake', False),
                            used_sources_provider=lambda selected: capture_used_sources(host, selected),
                            request_validator=validate_current_request,
                            deadline=deadline, trace=trace, shared_owner=owner)
                    payload = {**prepared, "turn_binding": binding, "trace": trace.snapshot()}
                    if prepared["kind"] == "completed":
                        from harness.qq.decision import QQReplyDecision
                        host.qq_memory.note_recall(QQReplyDecision.model_validate(prepared["decision"]))
                result = DomainCommandResult[dict](command_id=command.command_id, command_type=command.command_type,
                    status="applied", request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                    acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                    result=payload, completed_at=datetime.now(timezone.utc))
            except Exception as exc:
                from harness.qq.turn_checkpoints import QQTurnStateError
                code = str(exc) if isinstance(exc, (QQTurnStateError, ValueError)) and str(exc) in {
                    "qq_turn_expired", "qq_turn_cancelled", "qq_turn_interrupted", "qq_turn_binding_conflict",
                    "qq_turn_source_changed", "qq_turn_snapshot_corrupt", "qq_turn_snapshot_conflict",
                    "qq_turn_resume_conflict", "qq_turn_in_progress_or_interrupted", "qq_turn_configuration_changed",
                    "qq_required_reference_missing", "qq_required_reference_invalid", "qq_current_source_missing",
                    "qq_current_authority_invalid", "qq_turn_runtime_binding_missing", "qq_turn_runtime_binding_changed",
                    "qq_turn_resume_binding_changed",
                    "qq_turn_request_changed",
                } else "qq_turn_rejected:" + type(exc).__name__
                result = adapter._failure(command, code, status="rejected").model_copy(
                    update={"result": failure_detail(exc)})
            if protected and result.status == "applied":
                try:
                    await asyncio.to_thread(validate_current_request)
                    await asyncio.to_thread(_production_checkpoint_fence, host, command, result.result, runtime=production)
                except ValueError:
                    result = adapter._failure(command, "qq_turn_request_changed", status="rejected")
            async with adapter._lock if protected else nullcontext():
                if protected and result.status == "applied":
                    try:
                        validate_current_request(runtime=False)
                        _production_checkpoint_fence(host, command, result.result, runtime=False)
                        rejected = adapter._preflight(command)
                        if rejected is not None:
                            result = rejected
                    except ValueError:
                        result = adapter._failure(command, "qq_turn_request_changed", status="rejected")
                adapter._cursor = max(adapter._cursor, command.receive_seq)
                adapter._event_id = command.event_id
                adapter._remember(command, result)
                with trace.stage("persistence.command"):
                    await asyncio.to_thread(adapter._persist_state)
            if isinstance(result.result, dict):
                # The durable command keeps its original outcome; add completed
                # persistence timing only to the returned diagnostic snapshot.
                result = result.model_copy(update={"result": {**result.result, "trace": trace.snapshot()}})
                adapter._ledger[(command.command_type, command.idempotency_key)] = result
            return result
