"""Freeze trusted emotion observations before every semantic expression command."""
from __future__ import annotations

import hashlib

from harness.runtime.models import RuntimeEventType


EXPRESSION_COMMANDS = frozenset({
    "PrepareResponse", "AssessAutonomous", "PrepareQQResponse", "PrepareQQTurn", "PrepareBoardEntry",
    "PrepareCapabilityCall", "PrepareGoalDecision",
})


def project_homeostasis(payload: dict, revision: int) -> dict:
    """Keep all seven factors, without sending model matrices or hidden truth."""
    state = payload.get("snapshot", payload)
    summary = state.get("interoceptive_summary") or payload.get("interoceptive_summary") or {}
    if state.get("watchdog") or summary.get("salience") == "critical":
        raise ValueError("critical homeostasis must pause before emotion observation")
    profile = payload.get("profile") or {}
    factors = {}
    for name, factor in (state.get("dimensions") or {}).items():
        value = {key: factor[key] for key in ("value", "target_band", "precision", "velocity") if key in factor}
        bounds = (profile.get("dimensions", {}).get(name) or {}).get("hard_bounds")
        if bounds is not None:
            value["hard_bounds"] = bounds
            low, high = value["target_band"]
            current = value["value"]
            distance = low - current if current < low else current - high if current > high else 0.0
            span = low - bounds[0] if current < low else bounds[1] - high
            value["normalized_deviation"] = min(1.0, max(0.0, distance / span if span > 0 else float(distance > 0)))
        factors[name] = value
    return {"snapshot_revision": revision, "profile_ref": summary.get("profile_ref"),
        "salience": summary.get("salience", "nominal"), "dimensions": factors,
        "dominant_needs": summary.get("dominant_needs", [])}


async def with_emotion_context(supervisor, event, envelope):
    """A nested, journaled observation, sharing the original turn deadline.

    Legacy QQ preparation reads a previously committed affect snapshot;
    Runtime submits its new stimuli through AffectWorker. The staged QQ
    pipeline retains its frozen ObserveEmotion context across prepare/resume.
    """
    if (envelope.command_type not in EXPRESSION_COMMANDS
            or envelope.command_type == "PrepareQQResponse"
            or getattr(supervisor.manifest, "emotion_mode", "off") == "off"
            or supervisor.manifest.grounded_agency is not None
            or envelope.payload.get("mode") == "grounded_agency_decision"):
        return envelope
    return await attach_emotion_context(supervisor, event, envelope)


async def attach_emotion_context(supervisor, event, envelope):
    """Journal ObserveEmotion and attach the frozen reference to ``envelope``."""
    # Only a previously journaled command may supply a frozen reference.
    supervisor._sync_command_index()
    if envelope.idempotency_key in supervisor._command_intent_seqs and envelope.payload.get("emotion_context_ref"):
        return envelope
    method = getattr(supervisor.cognitive, "observe_emotion", None)
    if method is None:
        raise ValueError("cognitive service does not support ObserveEmotion")
    source = dict(envelope.payload)
    source.pop("emotion_context_ref", None)
    source.pop("emotion_revision", None)
    source.pop("emotion_snapshot", None)
    if envelope.command_type == "PrepareResponse":
        source.setdefault("source_time", event.source_time)
    if envelope.command_type in {"PrepareQQResponse", "PrepareQQTurn"}:
        group = str(source["group_id"])
        scope = "qq:" + group if group.startswith("private:") else "qq:group:" + group
    elif envelope.command_type == "PrepareBoardEntry":
        scope = "board:" + str(source["board_id"])
    else:
        # The Cognitive owner resolves its active local session, which Runtime
        # deliberately does not duplicate in configuration.
        scope = "local:" + str(source.get("session_id")
            or supervisor._cognitive_snapshot.payload.get("default_session_id") or "@active")
    identity = hashlib.sha256((event.event_id + "\0" + scope).encode()).hexdigest()
    context_id = "emotion:" + identity
    observation = supervisor._make_command(event, command_type="ObserveEmotion",
        idempotency_key=context_id, expected_revision=None,
        payload={"context_id": context_id, "scope": scope,
            "source_command_type": envelope.command_type, "source_payload": source,
            "external_input": event.event_type == RuntimeEventType.USER_MESSAGE,
            "homeostasis": project_homeostasis(supervisor._homeostasis_snapshot.payload,
                                                supervisor._homeostasis_snapshot.revision)})
    observation = observation.model_copy(update={"deadline": min(observation.deadline, envelope.deadline)})
    observed = await supervisor._invoke_domain(service="cognitive", envelope=observation, method=method,
        event=event, degrade_on_failure=True)
    result = observed.result or {}
    if result.get("emotion_context_ref") != context_id:
        raise ValueError("emotion observation context binding mismatch")
    # Other channels can commit after the Runtime cached its last snapshot.
    # Build the new preparation against the state actually read after the
    # observation, while retaining CAS and the original homeostasis/deadline.
    from harness.runtime.capabilities import before_deadline
    snapshot = await before_deadline(supervisor.cognitive.get_snapshot(), envelope.deadline)
    supervisor._validate_snapshot(snapshot, "cognitive")
    if snapshot.revision < supervisor._cognitive_snapshot.revision:
        raise ValueError("emotion_cognitive_revision_regressed")
    supervisor._cognitive_snapshot = snapshot
    updates = {"request_hash": None}
    if envelope.expected_revision is not None:
        updates["expected_revision"] = snapshot.revision
    if envelope.version_vector is not None:
        updates["version_vector"] = envelope.version_vector.model_copy(update={
            key: value for key, value in supervisor.version_vector.model_dump().items()
            if key.startswith("cognitive_")})
    if envelope.command_type in {"PrepareQQResponse", "PrepareQQTurn"} and result.get("qq_emotion_trace") is not None:
        source["qq_emotion_trace"] = result["qq_emotion_trace"]
    return envelope.model_copy(update={**updates, "payload": {**source,
        "emotion_context_ref": context_id, "emotion_revision": result["emotion_revision"]}})


def observation_input(adapter, command):
    """Construct local/board inputs only from trusted Runtime commands."""
    from harness.emotion import EmotionInput, EmotionMessage

    payload = command.payload
    source = payload.get("source_payload") or {}
    scope = payload["scope"]
    session_id = str(source.get("session_id") or adapter.default_session_id)
    if scope == "local:@active":
        scope = "local:" + session_id
    elif scope.startswith("local:"):
        frozen_session = scope.removeprefix("local:")
        if source.get("session_id") and session_id != frozen_session:
            raise ValueError("emotion_local_session_scope_mismatch")
        session_id = frozen_session
    messages = []
    new_ids = []
    if scope.startswith("local:"):
        for turn in adapter.pipeline.session_store.recent(session_id):
            if turn.event_id and turn.timestamp:
                messages.append(EmotionMessage(source_id=turn.event_id, scope=scope, role=turn.role,
                    text=turn.text, timestamp=turn.timestamp, speaker=turn.speaker))
        if payload.get("external_input") and str(source.get("text") or "").strip():
            messages = [item for item in messages if item.source_id != command.event_id]
            messages.append(EmotionMessage(source_id=command.event_id, scope=scope, role="user",
                text=str(source["text"]), timestamp=source.get("source_time") or command.issued_at,
                speaker=str(source.get("speaker") or "user")))
            new_ids.append(command.event_id)
    # A board request is an attributed user observation; previously generated
    # board text is never presented as a new external emotional stimulus.
    elif scope.startswith("board:") and source.get("trigger") == "user_request" and source.get("request_text"):
        messages.append(EmotionMessage(source_id=command.event_id, scope=scope, role="user",
            text=source["request_text"], timestamp=command.issued_at, speaker="user"))
        new_ids.append(command.event_id)
    return EmotionInput(context_id=payload["context_id"], scope=scope, as_of=command.issued_at,
        persona=adapter.pipeline.memory.get_persona(), homeostasis=payload.get("homeostasis") or {},
        messages=messages, new_source_ids=new_ids, deadline_at=command.deadline)


def shared_observation_input(host, command):
    """QQ sources are re-read and verified inside the Cognitive owner."""
    if getattr(command, "command_type", None) == "AppraiseAffect":
        return affect_observation_input(host, command)
    if command.payload.get("source_command_type") not in {"PrepareQQResponse", "PrepareQQTurn"}:
        return observation_input(host.adapter, command)
    import os
    from pathlib import Path
    from harness.emotion import EmotionInput
    from harness.qq.emotion_window import build_qq_emotion_messages
    from harness.qq.shared import QQSharedPrepareRequest
    from harness.runtime.qq_turns import _source

    source = dict(command.payload["source_payload"])
    for key in ("runtime_goal_context", "runtime_web_context", "emotion_context_ref", "emotion_revision",
                "qq_turn_runtime", "qq_frozen_policy", "qq_web_capabilities", "qq_emotion_trace", "emotion_snapshot"):
        source.pop(key, None)
    request = QQSharedPrepareRequest.model_validate(source)
    settings = host.shared.qq_settings
    if settings is None:
        raise ValueError("emotion QQ owner is not configured")
    configured = os.environ.get("NC_QQ_WATCHER_CONFIG")
    status = getattr(settings, "watcher_status_file", None)
    watcher = Path(configured) if configured else status.parent / "watcher.json" if status else None
    _source(settings, watcher, request)
    protocol_state_db = None
    if request.transport == "onebot":
        from harness.qq.direct_config import DirectQQSettings
        from harness.qq.reply_requests import validate_reply_request

        protocol_state_db = DirectQQSettings.read(Path(os.environ["NC_QQ_DIRECT_CONFIG"])).state_db
        # The DTO only checks shape. Bind every supplement to the current
        # request revision and immutable protocol evidence before treating it
        # as new external stimulus. The original anchor is separately checked
        # by _source above; this also rechecks revision after reading proofs.
        validate_reply_request(protocol_state_db, request, memory_config=settings)
    scope, messages, new_ids = build_qq_emotion_messages(memory_db=settings.memory_db,
        group_id=request.group_id, self_member_id=request.self_member_id,
        as_of=command.issued_at, trigger_event_id=request.trace.event_id if request.idle_intent is None else None,
        trigger_member_id=request.trigger_member_id, protocol_state_db=protocol_state_db,
        required_event_ids=request.source_event_ids)
    return EmotionInput(context_id=command.payload["context_id"], scope=scope, as_of=command.issued_at,
        persona=host.chat.memory.get_persona(), homeostasis=command.payload.get("homeostasis") or {},
        messages=messages, new_source_ids=new_ids, deadline_at=min(command.deadline, request.deadline_at))


def affect_observation_input(host, command):
    """Re-verify each merged trigger after the QQ turn; never trust Runtime text."""
    import os
    from pathlib import Path
    from harness.emotion import EmotionInput
    from harness.qq.emotion_window import build_qq_emotion_messages
    from harness.qq.shared import QQSharedPrepareRequest
    from harness.runtime.qq_turns import _source

    payload = command.payload
    settings = host.shared.qq_settings
    if settings is None:
        raise ValueError("emotion QQ owner is not configured")
    configured = os.environ.get("NC_QQ_WATCHER_CONFIG")
    status = getattr(settings, "watcher_status_file", None)
    watcher = Path(configured) if configured else status.parent / "watcher.json" if status else None
    protocol_state_db = None
    transport = payload["transport"]
    if transport == "onebot":
        from harness.qq.direct_config import DirectQQSettings

        protocol_state_db = DirectQQSettings.read(Path(os.environ["NC_QQ_DIRECT_CONFIG"])).state_db
    pairs = []
    for trigger in payload.get("triggers") or []:
        request = QQSharedPrepareRequest.model_validate({
            "transport": transport, "group_id": payload["group_id"],
            "self_member_id": payload["self_member_id"], "trigger_member_id": trigger["member_id"],
            "deadline_at": trigger["deadline_at"],
            "trace": {"trace_id": trigger["trace_id"], "event_id": trigger["event_id"],
                      "first_seen_at": trigger["first_seen_at"],
                      "first_seen_mono": trigger.get("first_seen_mono", 0)},
        })
        _source(settings, watcher, request, completed_replay=True)
        pairs.append((trigger["event_id"], trigger["member_id"]))
    scope, messages, new_ids = build_qq_emotion_messages(
        memory_db=settings.memory_db, group_id=payload["group_id"],
        self_member_id=payload["self_member_id"], as_of=command.issued_at,
        triggers=pairs, protocol_state_db=protocol_state_db)
    if payload.get("scope") not in {None, scope}:
        raise ValueError("emotion_affect_scope_mismatch")
    return EmotionInput(context_id=payload["context_id"], scope=scope, as_of=command.issued_at,
        persona=host.chat.memory.get_persona(), homeostasis=payload.get("homeostasis") or {},
        messages=messages, new_source_ids=new_ids, deadline_at=command.deadline, isolated=True)
