"""One durable gate followed by optional Runtime research and one QQ body."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from harness.core.inference import MemoryEvidenceEntry, TokenUsage, ResponseSemanticContext
from harness.core.models import CharacterSnapshot
from harness.qq.memory import QQVerifiedProfile, QQVisualObservation
from harness.qq.policy import QQLatencyOptions
from harness.qq import semantic_alignment as alignment
from harness.core.expression import EXPRESSION_VERSION, ResponseExpressionContext, qq_context as expression_qq_context, mode_for as expression_mode_for
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace, classify_error
from harness.qq.turn_checkpoints import QQTurnCheckpointStore, QQTurnStateError, binding_identity, digest


def _json(value):
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else value


def _expression_binding(settings):
    if settings.reply_internalization_mode == "off" and settings.reply_internalization_conversations == []:
        return None
    return {"version": EXPRESSION_VERSION, "mode": settings.reply_internalization_mode,
            "conversations": settings.reply_internalization_conversations}


def _generation_binding(settings, preset):
    return digest({**({"expression": _expression_binding(settings)} if _expression_binding(settings) is not None else {}),
                   "model": settings.llm_model, "endpoint": settings.llm_base_url,
                   "model_version": settings.model_version, "prompt_version": settings.prompt_version,
                   "persona": preset.persona.model_dump(mode="json"), "emotion_mode": settings.emotion_mode,
                   "validator_enabled": settings.validator_enabled, "semantic_version": alignment.VERSION,
                   "semantic_mode": settings.qq_semantic_alignment_mode,
                   "semantic_conversations": settings.qq_semantic_alignment_conversations})


def get_qq_turn_preparation(*, memory_db, turn_binding):
    store = QQTurnCheckpointStore(memory_db)
    row = store.get(turn_binding)
    if row is None:
        return None
    if row["status"] != "completed" and datetime.now(timezone.utc) >= datetime.fromisoformat(turn_binding["deadline_at"]):
        store.terminate(turn_binding, "expired")
        raise QQTurnStateError("qq_turn_expired")
    if row["status"] not in {"prepared", "completed"}:
        raise QQTurnStateError("qq_turn_" + row["status"] if row["status"] in {"cancelled", "expired", "interrupted"}
                               else "qq_turn_in_progress_or_interrupted")
    return row["public"]


def get_qq_turn_checkpoint(*, memory_db, prepare_id):
    return QQTurnCheckpointStore(memory_db).load(prepare_id)


def prepare_qq_turn(*, turn_binding: dict, web_capabilities: dict | None = None,
                   used_sources_provider=None, request_validator=None, **kwargs) -> dict:
    from harness.qq import decision as qq

    path = kwargs["memory_db"]
    for key in ("group_id", "self_member_id", "trigger_member_id"):
        if turn_binding.get(key) != kwargs.get(key):
            raise QQTurnStateError("qq_turn_binding_conflict")
    previous = get_qq_turn_preparation(memory_db=path, turn_binding=turn_binding)
    if previous is not None:
        return previous
    if datetime.now(timezone.utc) >= datetime.fromisoformat(turn_binding["deadline_at"]):
        raise QQTurnStateError("qq_turn_expired")
    store = QQTurnCheckpointStore(path)
    store.reserve(turn_binding)
    try:
        preparation = {
            "binding": turn_binding, "web_capabilities": web_capabilities or {"enabled": False},
            "used_sources_provider": used_sources_provider,
            "request_validator": request_validator,
            "request": {key: _json(kwargs[key]) for key in (
                "group_id", "self_member_id", "trigger_member_id", "request", "query", "member_ids",
                "idle_intent", "homeostatic_interoception") if key in kwargs}}
        result = qq.decide_qq_reply(**kwargs, _turn_preparation=preparation)
        if isinstance(result, dict):
            return result
        if request_validator is not None:
            request_validator()
        return store.complete(turn_binding, result.model_dump(mode="json"), used_sources=preparation.get("used_sources"))
    except QQTurnStateError:
        store.terminate(turn_binding, "cancelled")
        raise
    except BaseException:
        store.interrupt(turn_binding)
        raise


def checkpoint_gate(*, preparation, path, settings, preset, result, choice, raw_gate, messages,
                    payload, context, selection, receipts, partial_receipts, shared_owner, summary,
                    emotion_guidance, emotion_on, new_ids, trigger_latest_id, usage, trace, timings,
                    body_selection, body_extra_messages, body_evidence_refs, vision_evidence=()):
    from harness.qq import decision as qq

    evidence = qq.build_qq_memory_evidence(payload=body_selection.payload, trusted_context=context,
        current_observation_ids=qq.current_qq_burst_ids(trusted_context=context,
            new_observation_ids=set(new_ids), trigger_record_id=trigger_latest_id),
        receipts=receipts, partial_receipts=partial_receipts, shared_owner=shared_owner)
    evidence.extend(vision_evidence)
    semantic_mode = alignment.mode_for(settings, group_id=result.group_id, self_member_id=result.self_member_id)
    semantics = alignment.task_context(mode=semantic_mode, audience=qq.conversation_audience(result.group_id),
        trigger_ids=set(payload.get("current_request_observation_ids") or []) | ({trigger_latest_id} if trigger_latest_id else set()),
        choice=choice, entries=evidence)
    policy = QQLatencyOptions.from_settings(settings)
    expression = expression_qq_context(settings, group_id=result.group_id, self_member_id=result.self_member_id,
        plan=choice.reply_plan, style_window=payload.get("delivered_style_window"))
    source_records = {"qq_visual_profiles": {}, "qq_visual_observations": {}}
    for member in context.members:
        for profile in member.profiles:
            if profile.record_id in body_selection.profile_ids:
                source_records["qq_visual_profiles"][profile.record_id] = profile.model_dump(mode="json")
        for item in member.observations:
            if item.record_id in body_selection.observation_ids:
                source_records["qq_visual_observations"][item.record_id] = item.model_dump(mode="json")
    web_plan = choice.web_plan if preparation["web_capabilities"].get("enabled") else None
    snapshot = {
        "format": "qq-prepared-turn-v1", "binding": preparation["binding"], "request": preparation["request"],
        "policy": policy.model_dump(), "policy_version": policy.version,
        "generation_binding": _generation_binding(settings, preset),
        "expression_binding": _expression_binding(settings),
        "response_expression": _json(expression),
        "context_token_budget": settings.context_token_budget,
        "input_character_budget": settings.qq_input_token_budget * 2,
        "prompt_version": result.prompt_version,
        "response_semantics": _json(semantics),
        "gate_body_text": choice.model_dump_json() if semantic_mode == "enforce" or (expression and expression.mode == "enforce") else raw_gate,
        "messages": messages, "raw_gate": raw_gate, "choice": choice.model_dump(mode="json"),
        "selected_payload": payload, "source_records": source_records,
        "body_selected_payload": body_selection.payload,
        "body_extra_messages": body_extra_messages,
        "body_evidence_refs": body_evidence_refs,
        "presentation": {"gate_observation_ids": list(selection.observation_ids),
            "body_observation_ids": list(body_selection.observation_ids),
            "body_new_observation_ids": sorted(set(new_ids) & set(body_selection.observation_ids)),
            "processed_source_event_ids": list(preparation["binding"].get("source_event_ids") or [])},
        "used_sources": preparation.get("used_sources"),
        "shared_context": shared_owner.context if shared_owner else {},
        "derived_memory": shared_owner.derived_memory if shared_owner else None,
        "source_hashes": {table: {key: digest(value) for key, value in records.items()}
                          for table, records in source_records.items()},
        "memory_evidence": [entry.model_dump(mode="json") for entry in evidence],
        "emotion_guidance": _json(emotion_guidance) if emotion_on else None,
        "homeostatic_interoception": _json(summary),
        "character_snapshot": _json(shared_owner.memory.get_state(now=result.created_at)) if shared_owner else None,
        "decision": result.model_dump(mode="json"), "usage": usage.model_dump(mode="json"),
        "trace": trace.snapshot(), "timings": timings,
        "web_plan": _json(web_plan), "web_plan_invalid": choice._web_plan_invalid,
    }
    from harness.production.metering import current_production_context
    production = current_production_context()
    if production is not None:
        snapshot["production_context"] = production
    public = {"kind": "prepared", "decision_id": result.decision_id,
        "expires_at": preparation["binding"]["deadline_at"],
        "prepared_revision": preparation["binding"].get("prepared_revision"),
        "policy": policy.model_dump(), "policy_version": policy.version,
        "web_plan": _json(web_plan), "web_plan_invalid": choice._web_plan_invalid}
    if preparation.get("request_validator") is not None:
        preparation["request_validator"]()
    return QQTurnCheckpointStore(path).prepared(preparation["binding"], snapshot, public)


def _verify_source_records(path, snapshot):
    from harness.qq import decision as qq

    group_id = snapshot["binding"]["group_id"]
    with closing(qq._connect(Path(path))) as db:
        # Fixed table names only; no model- or caller-supplied SQL identifiers.
        for table in ("qq_visual_profiles", "qq_visual_observations"):
            for record_id, expected in snapshot["source_hashes"][table].items():
                row = db.execute(f"SELECT group_id,record_json FROM {table} WHERE record_id=?", (record_id,)).fetchone()
                if row is None or row["group_id"] != group_id:
                    raise QQTurnStateError("qq_turn_source_changed")
                model = QQVerifiedProfile if table == "qq_visual_profiles" else QQVisualObservation
                try:
                    value = model.model_validate_json(row["record_json"]).model_dump(mode="json")
                except ValueError as exc:
                    raise QQTurnStateError("qq_turn_source_changed") from exc
                # New optional schema fields normalize identically on both
                # sides; an old row without analysis_ref means null, not a
                # changed image. A real artifact/content mutation still fails.
                if value["record_id"] != record_id or value["group_id"] != group_id or digest(value) != expected:
                    raise QQTurnStateError("qq_turn_source_changed")


def resume_qq_response(*, memory_db, prepare_id, snapshot_hash, turn_binding: dict,
                       runtime_web_context: dict | None = None, shared_owner=None,
                       deadline: QQDeadline | None = None, trace: QQTurnTrace | None = None,
                       config_path=None, preset_path=None, used_sources_validator=None, request_validator=None):
    from harness.qq import decision as qq
    from harness.cognitive.web import normalize_web_context

    if prepare_id != binding_identity(turn_binding):
        raise QQTurnStateError("qq_turn_binding_conflict")
    store = QQTurnCheckpointStore(memory_db)
    row = store.get(turn_binding)
    if row is None:
        raise QQTurnStateError("qq_turn_not_found")
    if row["snapshot_hash"] != snapshot_hash:
        raise QQTurnStateError("qq_turn_snapshot_conflict")
    snapshot = row.get("snapshot")
    if snapshot is None:
        raise QQTurnStateError("qq_turn_not_prepared")
    current_settings, _ = ((shared_owner.settings, shared_owner.preset) if shared_owner is not None
                          else qq._load_configuration(config_path, preset_path))
    if snapshot.get("expression_binding") != _expression_binding(current_settings):
        raise QQTurnStateError("qq_turn_configuration_changed")
    from harness.production.metering import _scope
    active_scope = _scope.get()
    frozen_production = snapshot.get("production_context")
    if (active_scope is None) != (frozen_production is None):
        raise QQTurnStateError("qq_turn_configuration_changed")
    if active_scope is not None:
        if frozen_production.get("epoch") != active_scope.context.get("epoch"):
            raise QQTurnStateError("qq_turn_cancelled")
        # Resume uses the same body/affect/audience snapshot as its gate.
        # Budget authorization is still checked live for every physical call.
        active_scope.context = frozen_production
    web_context = normalize_web_context(runtime_web_context) if runtime_web_context is not None else None
    if web_context and not snapshot["web_plan"] and any(
        source.get("origin") != "user_link" for source in web_context.get("sources", [])
    ):
        raise QQTurnStateError("qq_turn_unplanned_web_context")
    resume_hash = digest({"snapshot_hash": snapshot_hash, "web_context": web_context})
    if row["status"] == "completed":
        if row["resume_hash"] != resume_hash:
            raise QQTurnStateError("qq_turn_resume_conflict")
        # A cached body may not have been delivered. Retrying it still releases
        # its evidence, so withdrawal must fence replay as well as generation.
        if request_validator is not None:
            request_validator()
        _verify_source_records(memory_db, snapshot)
        if snapshot.get("used_sources") is not None:
            if used_sources_validator is None:
                raise QQTurnStateError("qq_turn_evidence_validator_missing")
            used_sources_validator(snapshot["used_sources"])
        result = qq.QQReplyDecision.model_validate(row["public"]["decision"])
        result = result.model_copy(update={"new_decision": False, "replayed": True})
        return qq._deadline_override(result, deadline)
    remaining = (datetime.fromisoformat(turn_binding["deadline_at"]) - datetime.now(timezone.utc)).total_seconds()
    if remaining <= 0:
        store.terminate(turn_binding, "expired")
        raise QQTurnStateError("qq_turn_expired")
    if deadline is None:
        deadline = QQDeadline.after(remaining)
    else:
        deadline = QQDeadline(min(deadline.deadline_mono, deadline.clock() + remaining), deadline.clock,
                              deadline.downstream_reserved_seconds)
    deadline.check()
    policy = QQLatencyOptions.model_validate(snapshot["policy"])
    if policy.version != snapshot["policy_version"]:
        raise QQTurnStateError("qq_turn_policy_corrupt")
    settings, preset = ((shared_owner.settings, shared_owner.preset) if shared_owner is not None
                        else qq._load_configuration(config_path, preset_path))
    if _generation_binding(settings, preset) != snapshot["generation_binding"]:
        raise QQTurnStateError("qq_turn_configuration_changed")
    expected_prompt = qq.PRIVATE_PROMPT_VERSION if qq.conversation_scope(turn_binding["group_id"])["kind"] == "private" else qq.PROMPT_VERSION
    expression_mode = expression_mode_for(settings, f"{turn_binding['self_member_id']}/{turn_binding['group_id']}")
    if expression_mode != "off":
        expected_prompt += ":" + EXPRESSION_VERSION + ":" + expression_mode
    semantic_mode = alignment.mode_for(settings, group_id=turn_binding["group_id"], self_member_id=turn_binding["self_member_id"])
    if semantic_mode != "off":
        expected_prompt += ":" + alignment.VERSION + ":" + semantic_mode
    if snapshot["prompt_version"] != expected_prompt:
        raise QQTurnStateError("qq_turn_prompt_changed")
    try:
        if request_validator is not None:
            request_validator()
        _verify_source_records(memory_db, snapshot)
        if snapshot.get("used_sources") is not None:
            if used_sources_validator is None:
                raise QQTurnStateError("qq_turn_evidence_validator_missing")
            used_sources_validator(snapshot["used_sources"])
    except QQTurnStateError:
        store.terminate(turn_binding, "cancelled")
        raise
    settings = settings.model_copy(update={**policy.model_dump(),
        "context_token_budget": snapshot["context_token_budget"],
        "max_reply_segments": 3, "continuation_enabled": False})
    claimed = store.claim_resume(turn_binding, snapshot_hash, resume_hash)
    if claimed["status"] == "completed":
        # A concurrent owner may have completed while this caller checked the
        # frozen sources. The atomic claim decides whether a model may run.
        result = qq.QQReplyDecision.model_validate(claimed["public"]["decision"])
        return qq._deadline_override(result.model_copy(update={"new_decision": False, "replayed": True}), deadline)
    trace = trace or QQTurnTrace()
    result = qq.QQReplyDecision.model_validate(snapshot["decision"])
    trace.bind_decision(result.decision_id)
    usage = TokenUsage.model_validate(snapshot["usage"])
    choice_model = qq._ProductionGateDecision if "used_memory_ids" in snapshot["choice"] else qq._GateDecision
    choice = choice_model.model_validate(snapshot["choice"])
    timings = dict(snapshot["timings"])
    started = trace.clock()
    raw_llm = llm = None
    try:
        deadline.check()
        body_payload = {"assessment": choice.model_dump(mode="json")}
        if web_context is not None:
            body_payload["web_context"] = web_context
        elif snapshot["web_plan_invalid"]:
            body_payload["web_context"] = {"sources": [], "failures": [{"code": "web_plan_invalid"}], "status": "unavailable"}
        body_text = qq._checked_model_payload(body_payload, trace=trace, stage="persona",
                                              max_characters=snapshot["input_character_budget"])
        prefix = [*snapshot["messages"], {"role": "assistant", "content": snapshot.get("gate_body_text", snapshot["raw_gate"])},
                  *snapshot.get("body_extra_messages", [])]
        # Check the actual complete user/assistant wire payload, not the large
        # internal DTO. Web never displaces a mandatory current observation.
        actual = sum(len(item["content"]) for item in prefix if item["role"] != "system") + len(body_text)
        if actual > snapshot["input_character_budget"]:
            raise qq._QQContextBudgetExceeded(qq._CONTEXT_BUDGET_REASON)
        raw_llm = shared_owner.llm if shared_owner is not None else qq.build_llm_client(settings)
        llm = qq._QQScopedLLM(raw_llm, deadline=deadline, trace=trace, timeout=settings.llm_timeout,
                             policy=policy, request_validator=request_validator,
                             reasoning_options=qq.deepseek_low_reasoning_options(settings))
        llm.stage_name = "persona"
        llm.stable_messages = snapshot["messages"][:-1]
        entries = [MemoryEvidenceEntry.model_validate(item) for item in snapshot["memory_evidence"]]
        with trace.stage("persona"), qq.trace_scope(phase="qq_persona"):
            final = qq._prepare_group_speech(settings=settings, preset=preset, llm=llm, payload=body_text,
                evidence_refs=snapshot.get("body_evidence_refs", result.evidence_refs), group_id=result.group_id,
                shared_memory=shared_owner.memory if shared_owner else None,
                homeostatic_interoception=snapshot["homeostatic_interoception"],
                emotion_guidance=snapshot["emotion_guidance"], memory_evidence_entries=entries,
                runtime_web_context=body_payload.get("web_context"),
                response_expression=(ResponseExpressionContext.model_validate(snapshot["response_expression"])
                                     if snapshot.get("response_expression") else None),
                current_speaker_id=f"qq:member:{result.trigger_member_id}" if result.trigger_member_id else None,
                response_semantics=(ResponseSemanticContext.model_validate(snapshot["response_semantics"])
                                    if snapshot.get("response_semantics") else None),
                prefix_messages=prefix, character_snapshot=(CharacterSnapshot.model_validate(snapshot["character_snapshot"])
                                                           if snapshot["character_snapshot"] else None))
        usage = usage.add(final.usage)
        deadline.check()
        if request_validator is not None:
            request_validator()
        _verify_source_records(memory_db, snapshot)
        if snapshot.get("used_sources") is not None:
            used_sources_validator(snapshot["used_sources"])
        if llm.failed_error_type is not None:
            raise RuntimeError("QQ persona call failed")
        if alignment.rejected(final):
            raise alignment.SemanticReplyRejected()
        if not final.text.strip() or len(final.text) > 8000 or (final.validation is not None and final.validation.verdict != "pass"):
            raise qq.QQDraftError("The prepared reply failed validation.")
        segments, reply = qq._prepared_group_segments(final, trace, reply_style=choice.reply_style)
        presentation = snapshot.get("presentation", {})
        body_ids = presentation.get("body_observation_ids", result.source_record_ids)
        result = result.model_copy(update={"action": "speak", "reply": reply, "reply_segments": segments,
            "source_record_ids": body_ids, "evidence_refs": snapshot.get("body_evidence_refs", result.evidence_refs),
            "seen_observation_record_ids": ([] if result.initiated_by != "message" else
                sorted(set(result.seen_observation_record_ids) | set(body_ids))),
            "new_observation_record_ids": presentation.get("body_new_observation_ids", result.new_observation_record_ids),
            "outcome": "ready", "expression_diagnostics": final.validation.expression if final.validation else None,
            "style_diagnostics": {**qq.style_diagnostics(reply, segments=len(segments),
                detail_level=choice.reply_plan.detail_level if choice.reply_plan else "brief"),
                **result.style_diagnostics, "body_presented_observations": len(body_ids),
                "web_plan_invalid": snapshot["web_plan_invalid"]}})
    except alignment.SemanticReplyRejected:
        result = alignment.guard_wait(result, trace)
    except QQTurnStateError:
        # Revoked/corrected evidence is not an ordinary model or web failure.
        store.terminate(turn_binding, "cancelled")
        raise
    except Exception as exc:
        category = llm.failed_error_type if llm and llm.failed_error_type else classify_error(exc)
        expired = isinstance(exc, QQDeadlineExpired) or deadline.expired
        result = qq.apply_error_fallback(result, error_type="deadline_expired" if expired else category,
            decision_source="expired" if expired else "model_error", reason="正文准备未完成，本轮未产生可发送的模型正文。")
    finally:
        if shared_owner is None and raw_llm is not None and callable(getattr(raw_llm, "close", None)):
            raw_llm.close()
    timings["persona"] = round(max(0.0, trace.clock() - started) * 1000, 3)
    timings["total"] = timings.get("gate", 0) + timings["persona"]
    gate_calls = snapshot["trace"]["counters"].get("gate_model_calls", 0)
    result = qq._deadline_override(result, deadline)
    result = qq.QQReplyDecision.model_validate(result.model_copy(update={"usage": usage,
        "completed_at": datetime.now(timezone.utc), "timings_ms": timings,
        "model_call_counts": {"gate": gate_calls, "persona": trace.snapshot()["counters"].get("persona_model_calls", 0),
            **({"vision": snapshot["trace"]["counters"]["vision_model_calls"]}
               if snapshot["trace"]["counters"].get("vision_model_calls") else {})}}).model_dump())
    try:
        if request_validator is not None:
            request_validator()
        store.complete(turn_binding, result.model_dump(mode="json"))
    except QQTurnStateError:
        store.terminate(turn_binding, "cancelled")
        raise
    trace.add_counter(f"decision_outcome_{result.outcome}")
    final = qq._deadline_override(result, deadline)
    if final is not result:
        store.finalize_expired(turn_binding, final.model_dump(mode="json"))
    return final
