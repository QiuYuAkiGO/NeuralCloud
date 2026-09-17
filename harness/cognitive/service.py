"""Private Cognitive service and diagnostic event stream."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

from fastapi import Header, HTTPException
from fastapi.responses import StreamingResponse
from gui.debug import (
    CandidatePatch,
    EpisodicPatch,
    PersonaPatch,
    PsychPatch,
    RelationshipPatch,
    SemanticPatch,
    TuningPatch,
    apply_candidate,
    apply_episodic,
    apply_persona,
    apply_psych,
    apply_relationship,
    apply_semantic,
    apply_tuning,
)
from gui.persist import Mode, apply_loaded_tuning, default_config_path, load_gui_settings, persist_gui_settings
from gui.presets import DEFAULT_PRESET_ID, PresetLibrary
from gui.service import HarnessChatService
from harness.llm.client import list_endpoint_models
from harness.runtime.adapters import PipelineCognitiveAdapter
from harness.runtime.rpc import create_domain_service_app
from harness.runtime.ownership import SharedDomainLease, require_development, resolve_control_mode, require_service_token
from harness.cognitive.shared_memory import CognitiveSharedMemory, SharedMemoryCompiler
from harness.contracts.envelope import DomainCommandResult
from infra.trace.serialize import serialize_stage
from infra.trace.request_spans import trace_scope
from infra.trace.diagnostics import diagnostic_scope

ROOT = Path(__file__).resolve().parents[2]
HOST = os.environ.get("NC_COGNITIVE_HOST", "127.0.0.1")
PORT = int(os.environ.get("NC_COGNITIVE_PORT", "8771"))


class CognitiveEventObserver:
    def __init__(self) -> None:
        self.run_id: str | None = None
        self._subscribers: set[tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]] = set()

    def bind(self, run_id: str) -> None:
        self.run_id = run_id

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        self._broadcast(
            {
                "type": "final" if kind == "final" else "stage",
                "run_id": self.run_id,
                "stage": kind,
                "latency_ms": payload.get("latency_ms", 0.0),
                "data": serialize_stage(kind, result),
                "meta": {key: value for key, value in payload.items() if key != "latency_ms"},
            }
        )

    def emit_state(self, snapshot: dict[str, Any], run_id: str | None = None) -> None:
        self._broadcast({"type": "state", "run_id": run_id or self.run_id, "data": snapshot})

    def emit_error(self, message: str, run_id: str | None = None) -> None:
        self._broadcast({"type": "error", "run_id": run_id or self.run_id, "data": {"message": message}})

    def subscribe(self) -> tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]:
        item: tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]] = (
            asyncio.get_running_loop(),
            asyncio.Queue(maxsize=256),
        )
        self._subscribers.add(item)
        return item

    def unsubscribe(self, item: tuple[asyncio.AbstractEventLoop, asyncio.Queue[dict[str, Any]]]) -> None:
        self._subscribers.discard(item)

    def _broadcast(self, event: dict[str, Any]) -> None:
        for loop, queue in list(self._subscribers):
            def put(target: asyncio.Queue[dict[str, Any]] = queue) -> None:
                if target.full():
                    try:
                        target.get_nowait()
                    except asyncio.QueueEmpty:
                        pass
                target.put_nowait(event)

            try:
                loop.call_soon_threadsafe(put)
            except RuntimeError:
                self._subscribers.discard((loop, queue))


class CognitiveServiceHost:
    def __init__(self, *, config_path: Path, presets_dir: Path, state_path: Path) -> None:
        self.config_path = config_path
        self.control_mode = resolve_control_mode()
        isolation = os.environ.get("NC_ISOLATED_DEVELOPMENT", "0")
        if isolation not in {"0", "1"}:
            raise ValueError("invalid_development_isolation")
        self.owner_lease = SharedDomainLease("cognition", state_path,
            control_mode=self.control_mode, isolated=isolation == "1").acquire()
        try:
            self._initialize(config_path=config_path, presets_dir=presets_dir, state_path=state_path)
        except BaseException:
            self.owner_lease.release()
            raise

    def _initialize(self, *, config_path: Path, presets_dir: Path, state_path: Path) -> None:
        self.observer = CognitiveEventObserver()
        persisted = load_gui_settings(config_path)
        library = PresetLibrary(working_dir=presets_dir)
        open_ids = [item for item in persisted.open_preset_ids if library.exists(item)]
        active = persisted.active_preset_id if library.exists(persisted.active_preset_id) else DEFAULT_PRESET_ID
        if active not in open_ids:
            open_ids.insert(0, active)
        configured_mode = os.environ.get("NC_MODE", persisted.mode)
        if configured_mode not in {"demo", "live", "ollama"}:
            raise ValueError(f"unsupported NC_MODE: {configured_mode}")
        self.chat = HarnessChatService(
            observer=self.observer,
            mode=cast(Mode, configured_mode),
            base_url=persisted.base_url,
            model=persisted.model,
            api_key=persisted.api_key,
            aux_base_url=persisted.aux_base_url,
            aux_model=persisted.aux_model,
            aux_api_key=persisted.aux_api_key,
            validator_enabled=persisted.validator_enabled,
            user_name=persisted.user_name,
            character_name=persisted.character_name,
            library=library,
            active_preset_id=active,
            open_preset_ids=open_ids or [active],
        )
        apply_loaded_tuning(self.chat.settings, persisted)
        self.chat.pipeline.compiler = SharedMemoryCompiler(self.chat.settings)
        qq_config = os.environ.get("NC_QQ_SESSION_CONFIG")
        qq_settings = None
        if qq_config:
            from harness.qq.session import QQSessionSettings
            qq_settings = QQSessionSettings.model_validate_json(Path(qq_config).read_text(encoding="utf-8"))
        self.shared = CognitiveSharedMemory(self,
            Path(os.environ.get("NC_SHARED_MEMORY_DB", str(state_path.parent / "shared-memory.sqlite3"))),
            qq_settings=qq_settings)
        self.adapter = PipelineCognitiveAdapter(
            self.chat.pipeline,
            state_path=state_path,
            autonomous_log_dir=Path(
                os.environ.get(
                    "NC_COGNITIVE_AUTONOMOUS_LOG_DIR",
                    str(state_path.parent / "autonomous"),
                )
            ),
            default_session_id=self.chat.active_session_id,
            persist_memory=self._flush_committed_memory,
            context_provider=self.shared.local_context,
            archive_local=self.shared.archive_local,
            archive_action=self.shared.archive_action,
        )
        from harness.runtime.emotion import shared_observation_input
        self.adapter.emotion_input_provider = lambda command: shared_observation_input(self, command)
        self._emotion_mode = self.chat.settings.emotion_mode
        manifest_path = os.environ.get("NC_COGNITIVE_MANIFEST_PATH") or os.environ.get("NC_RUNTIME_MANIFEST_PATH")
        if manifest_path:
            from harness.research.manifest import load_experiment_manifest

            manifest = load_experiment_manifest(Path(manifest_path))
            self._emotion_mode = manifest.emotion_mode
            self.chat.settings.emotion_mode = manifest.emotion_mode
            if manifest.emotion_mode == "on":
                self.chat.settings.prompt_version = manifest.prompt_version
            if manifest.grounded_agency is not None:
                self.adapter.bind_research_manifest(manifest)
        self.adapter._publish_snapshot()
        if self._emotion_mode == "on" and self.chat.mode != "demo":
            from harness.llm.client import aux_model_distinct
            if not aux_model_distinct(self.chat.settings):
                raise ValueError("emotion_mode=on requires a distinct configured Qwen auxiliary endpoint")
        self._control_lock = asyncio.Lock()
        from harness.cognitive.qq_memory import QQMemoryWorker
        self.qq_memory = QQMemoryWorker(self, control_path=state_path.parent / "qq-memory.control.json")
        from harness.cognitive.daily_revision import DailyRevisionWorker
        self.daily_revision = DailyRevisionWorker(self,
            config_path=Path(os.environ.get("NC_DAILY_REVISION_CONFIG", str(state_path.parent / "daily-revision.config.json"))),
            queue_path=state_path.parent / "daily-revision.sqlite3")
        self._close_task = None

    @property
    def command_tasks(self):
        return self.adapter.command_tasks

    @property
    def research_tasks(self):
        return self.adapter.research_tasks

    async def health(self):
        health = await self.adapter.health()
        return health.model_copy(update={"detail": {**health.detail, "qq_memory": self.qq_memory.status(),
            "daily_revision": self.daily_revision.status(),
            "qq_turn_commands": ["PrepareQQTurn", "ResumeQQResponse"]}})

    async def get_snapshot(self):
        from harness.qq.policy import QQLatencyOptions, effective_configuration
        snapshot = await self.adapter.get_snapshot()
        return snapshot.model_copy(update={"payload": {**snapshot.payload,
            "qq_latency_policy": QQLatencyOptions.from_settings(self.chat.settings).model_dump(),
            "effective_configuration": effective_configuration(self.chat.settings),
            "qq_turn_commands": ["PrepareQQTurn", "ResumeQQResponse"]}})

    async def prepare_qq_turn(self, command):
        from harness.cognitive.qq_turn import run
        return await run(self, command, resume=False)

    async def resume_qq_response(self, command):
        from harness.cognitive.qq_turn import run
        return await run(self, command, resume=True)

    async def prepare_response(self, command):
        return await self.adapter.prepare_response(command)

    async def observe_emotion(self, command):
        source = command.payload.get("source_payload") or {}
        if command.payload.get("source_command_type") not in {"PrepareQQResponse", "PrepareQQTurn"}:
            return await self.adapter.observe_emotion(command)
        from harness.qq.telemetry import QQTurnTrace, qq_trace_scope
        origin = source.get("trace") or {}
        trace = QQTurnTrace(event_id=origin.get("event_id"))
        if origin.get("trace_id"):
            trace.trace_id = origin["trace_id"]
        with qq_trace_scope(trace), trace.stage("emotion.observe"):
            result = await self.adapter.observe_emotion(command)
        if isinstance(result.result, dict):
            return result.model_copy(update={"result": {**result.result, "qq_emotion_trace": trace.snapshot()}})
        return result

    async def prepare_web_step(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="web_prepare", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._prepare_web_step(command))

    async def _prepare_web_step(self, command):
        from harness.cognitive.web import prepare, WebSelectionError

        adapter = self.adapter
        async with adapter._lock:
            replay = adapter._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(adapter._persist_state)
                return replay
            rejected = adapter._preflight(command)
            if rejected is not None:
                adapter._remember(command, rejected)
                await asyncio.to_thread(adapter._persist_state)
                return rejected
            try:
                if command.command_type != "PrepareWebStep":
                    raise WebSelectionError("web_command_type_mismatch")
                payload = await prepare(self, command)
                adapter._cursor = max(adapter._cursor, command.receive_seq)
                adapter._event_id = command.event_id
                result = DomainCommandResult[dict](command_id=command.command_id,
                    command_type=command.command_type, status="applied",
                    request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                    acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                    result=payload, completed_at=datetime.now(timezone.utc))
            except Exception as exc:
                error = str(exc) if isinstance(exc, WebSelectionError) else (
                    "web_step_expired" if isinstance(exc, TimeoutError) else "web_step_prepare_failed")
                result = adapter._failure(command, error)
            adapter._remember(command, result)
            await asyncio.to_thread(adapter._persist_state)
            return result

    async def prepare_qq_response(self, command):
        """Prepare through the same lock/ledger; only QQ's body commits delivery."""
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="qq_prepare", span_id=command.command_id,
                         qq_trace_id=(command.payload.get("trace") or {}).get("trace_id")):
            return await self.adapter._run_command(lambda: self._prepare_qq_response(command))

    async def _prepare_qq_response(self, command):
        from harness.qq.decision import decide_qq_reply
        from harness.qq.shared import QQSharedPrepareRequest
        from harness.qq.telemetry import QQDeadline, QQTurnTrace, qq_trace_scope
        from harness.runtime.qq_turns import _source

        adapter = self.adapter
        trace = QQTurnTrace()
        waiting = trace.clock()
        async with adapter._lock:
            trace.record("cognitive.lock_wait", started_mono=waiting)
            replay = adapter._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(adapter._persist_state)
                return replay
            rejected = adapter._preflight(command)
            if rejected is not None:
                return rejected
            try:
                if command.command_type != "PrepareQQResponse" or self.shared.qq_settings is None:
                    raise ValueError("qq_shared_owner_not_configured")
                qq_payload = dict(command.payload)
                qq_payload.pop("emotion_context_ref", None)
                qq_payload.pop("emotion_revision", None)
                runtime_goal_context = qq_payload.pop("runtime_goal_context", None)
                runtime_web_context = qq_payload.pop("runtime_web_context", None)
                emotion_trace = qq_payload.pop("qq_emotion_trace", None)
                frozen_policy = qq_payload.pop("qq_frozen_policy", None)
                request = QQSharedPrepareRequest.model_validate(qq_payload)
                if request.homeostatic_interoception is not None:
                    from harness.runtime.adapters import _validated_homeostatic_interoception
                    _validated_homeostatic_interoception(command)
                if runtime_goal_context is not None:
                    from harness.contracts.goals import QQGoalContext
                    runtime_goal_context = QQGoalContext.model_validate(runtime_goal_context)
                configured = os.environ.get("NC_QQ_WATCHER_CONFIG")
                status_file = getattr(self.shared.qq_settings, "watcher_status_file", None)
                watcher = Path(configured) if configured else (status_file.parent / "watcher.json" if status_file else None)
                await asyncio.to_thread(_source, self.shared.qq_settings, watcher, request)
                from harness.compiler.compiler import command_emotion_deadline

                remaining = (command_emotion_deadline(self, command, request.deadline_at)
                             - datetime.now(timezone.utc)).total_seconds()
                deadline = QQDeadline.after(max(0, remaining))
                deadline.check()
                trace.event_id = request.trace.event_id
                trace.first_seen_at = request.trace.first_seen_at
                trace.observation_sequence = request.trace.observation_sequence
                trace.trace_id = request.trace.trace_id
                if emotion_trace is not None:
                    trace.merge_prior_phase(emotion_trace)
                from harness.qq.policy import QQLatencyOptions, settings_with_policy
                policy = (QQLatencyOptions.model_validate(frozen_policy) if frozen_policy is not None else
                    QQLatencyOptions.from_settings(self.chat.settings).for_conversation(
                        group_id=request.group_id, self_member_id=request.self_member_id))
                trace.bind_configuration(settings_with_policy(self.chat.settings, policy))
                with qq_trace_scope(trace):
                    with trace.stage("shared_memory"):
                        binding = await asyncio.to_thread(self.shared.qq_binding,
                            group_id=request.group_id, member_id=request.trigger_member_id, query=request.query)
                    from dataclasses import replace
                    binding = replace(binding, settings=settings_with_policy(binding.settings, policy))
                    deadline.check()
                    values = request.model_dump(exclude={"trace", "deadline_at", "transport",
                        "request_id", "revision", "source_event_ids", "authority_anchor"})
                    # Keep validated DTOs for identity and state evidence.
                    values.update(verified_self_aliases=request.verified_self_aliases,
                        idle_intent=request.idle_intent, homeostatic_interoception=request.homeostatic_interoception)
                    emotion_guidance = await adapter.emotion_guidance(command)
                    if emotion_guidance is not None:
                        values["emotion_guidance"] = emotion_guidance
                    if runtime_goal_context is not None:
                        values["goal_context"] = runtime_goal_context
                    if runtime_web_context is not None:
                        values["runtime_web_context"] = runtime_web_context
                    state_summary = request.homeostatic_interoception
                    if state_summary is None and request.idle_intent is not None:
                        state_summary = request.idle_intent.homeostatic_interoception
                    state_json = state_summary.model_dump(mode="json") if state_summary is not None else None
                    profile_ref = state_summary.profile_ref if state_summary is not None else None
                    profile_suffix = profile_ref.rsplit("@", 1)[-1] if profile_ref else ""
                    with diagnostic_scope(channel="qq", transport=request.transport,
                        conversation_kind="private" if request.group_id.startswith("private:") else "group",
                        homeostasis_revision=state_summary.snapshot_revision if state_summary is not None else None,
                        profile_ref=profile_ref,
                        profile_revision=int(profile_suffix) if profile_suffix.isdigit() else None,
                        summary_hash=hashlib.sha256(json.dumps(state_json, ensure_ascii=False, sort_keys=True,
                            separators=(",", ":")).encode("utf-8")).hexdigest() if state_json is not None else None,
                        persona_hash=hashlib.sha256(json.dumps(binding.preset.persona.model_dump(mode="json"),
                            ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()):
                        decision = await asyncio.to_thread(decide_qq_reply,
                            memory_db=self.shared.qq_settings.memory_db, **values,
                            deadline=deadline, trace=trace, shared_owner=binding)
                    self.qq_memory.note_recall(decision)
                    adapter._cursor = max(adapter._cursor, command.receive_seq)
                    adapter._event_id = command.event_id
                    result = DomainCommandResult[dict](command_id=command.command_id,
                        command_type=command.command_type, status="applied",
                        request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                        acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                        result={"decision": decision.model_dump(mode="json"), "trace": trace.snapshot()},
                        completed_at=datetime.now(timezone.utc))
            except Exception as exc:
                # No prompts, source text, tokens or paths in service failures.
                result = adapter._failure(command, "qq_shared_prepare_failed:" + type(exc).__name__)
            adapter._remember(command, result)
            with qq_trace_scope(trace), trace.stage("persistence.command"):
                await asyncio.to_thread(adapter._persist_state)
            if isinstance(result.result, dict):
                # Outcome is already durable. Completing diagnostics must not
                # add another synchronous full-state commit to this request.
                result = result.model_copy(update={"result": {**result.result, "trace": trace.snapshot()}})
                adapter._ledger[(command.command_type, command.idempotency_key)] = result
            return result

    async def assess_autonomous(self, command):
        return await self.adapter.assess_autonomous(command)

    async def prepare_capability_call(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="capability_prepare", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._capability_command(command))

    async def prepare_goal_decision(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="goal_prepare", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._capability_command(command, goal_decision=True))

    async def _capability_command(self, command, *, goal_decision=False):
        from harness.cognitive import capabilities
        from harness.contracts.capability_calls import CapabilitySelectionRequest, PreparedCapabilityChoice
        from harness.contracts.decisions import PreparedResponse
        from harness.core.inference import FinalResponse
        from harness.core.models import InteractionEvent
        from harness.runtime.adapters import _PreparedEntry

        adapter = self.adapter
        async with adapter._lock:
            replay = adapter._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(adapter._persist_state)
                return replay
            rejected = adapter._preflight(command)
            if rejected is not None:
                adapter._remember(command, rejected)
                await asyncio.to_thread(adapter._persist_state)
                return rejected
            try:
                expected_type = "PrepareGoalDecision" if goal_decision else "PrepareCapabilityCall"
                if command.command_type != expected_type:
                    raise capabilities.CapabilitySelectionError("capability_command_type_mismatch")
                request_payload = {key: value for key, value in command.payload.items()
                    if key not in {"emotion_context_ref", "emotion_revision"}}
                if goal_decision:
                    from harness.cognitive import goals
                    from harness.contracts.goals import GoalDecisionRequest, PreparedGoalDecision
                    payload = await goals.prepare(self, command)
                    selected = PreparedGoalDecision.model_validate(payload["decision"]).capability_choice
                    request = GoalDecisionRequest.model_validate(request_payload)
                else:
                    payload = await capabilities.prepare(self, command)
                    selected = PreparedCapabilityChoice.model_validate(payload["decision"])
                    request = CapabilitySelectionRequest.model_validate(request_payload)
                offer = next((item for item in request.offers if item.offer_id == selected.choice.offer_id), None)
                if offer is not None and offer.channel == "desktop":
                    goal = capabilities.compile_desktop_choice(selected.choice, offer,
                        as_of=command.issued_at if goal_decision else None)
                    prepared = PreparedResponse(
                        prepare_id="prepare:" + selected.decision_id,
                        event_id=command.event_id, correlation_id=command.correlation_id,
                        session_id=self.chat.active_session_id, decision=goal,
                        read_versions=command.version_vector or adapter._version_vector(),
                        prepared_at=selected.prepared_at, expires_at=selected.deadline_at,
                        model_version=self.chat.settings.model_version, prompt_version=self.chat.settings.prompt_version,
                    )
                    # This private retrieval marker is durable for evidence binding
                    # only. Capability commits never turn it into a user utterance.
                    event = InteractionEvent(event_id=command.event_id, timestamp=command.issued_at,
                        channel="internal", speaker="character", raw_text="", session_id=prepared.session_id)
                    final = FinalResponse(response_id="response:" + selected.decision_id, text="", path="fast",
                        situation_id=request.opportunity_id, event_id=command.event_id)
                    adapter._prepared[prepared.prepare_id] = _PreparedEntry(
                        event=event, final=final, prepared=prepared, capability=True)
                    payload["prepared_response"] = prepared.model_dump(mode="json")
                adapter._cursor = max(adapter._cursor, command.receive_seq)
                adapter._event_id = command.event_id
                result = DomainCommandResult[dict](command_id=command.command_id,
                    command_type=command.command_type, status="applied",
                    request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                    acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                    result=payload, completed_at=datetime.now(timezone.utc))
            except Exception as exc:
                error = exc.code if isinstance(exc, capabilities.CapabilitySelectionError) else (
                    ("goal_decision_expired" if goal_decision else "capability_selection_expired") if isinstance(exc, TimeoutError) else
                    ("goal_prepare_failed" if goal_decision else "capability_prepare_failed"))
                result = adapter._failure(command, error)
            adapter._remember(command, result)
            await asyncio.to_thread(adapter._persist_state)
            return result

    async def prepare_board_entry(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="board_prepare", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._board_command(command, commit=False))

    async def commit_board_entry(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="board_commit", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._board_command(command, commit=True))

    async def prepare_diary_entry(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="diary_prepare", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._diary_command(command, commit=False))

    async def commit_diary_entry(self, command):
        with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                         command_id=command.command_id, phase="diary_commit", span_id=command.command_id):
            return await self.adapter._run_command(lambda: self._diary_command(command, commit=True))

    async def _board_command(self, command, *, commit):
        from harness.cognitive import board

        adapter = self.adapter
        diagnostic = board.BoardDiagnostics()
        async with adapter._lock:
            replay = adapter._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(adapter._persist_state)
                return replay
            rejected = adapter._preflight(command)
            if rejected is not None:
                return rejected
            try:
                expected = "CommitBoardEntry" if commit else "PrepareBoardEntry"
                if command.command_type != expected:
                    raise ValueError("board_command_type_mismatch")
                payload = await (board.commit(self, command, diagnostic=diagnostic) if commit
                                 else board.prepare(self, command, diagnostic=diagnostic))
                if commit:
                    adapter._revision += 1
                    adapter._component_revisions["memory"] = adapter._revision
                adapter._cursor = max(adapter._cursor, command.receive_seq)
                adapter._event_id = command.event_id
                result = DomainCommandResult[dict](command_id=command.command_id,
                    command_type=command.command_type, status="applied",
                    request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                    acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                    result=payload, completed_at=datetime.now(timezone.utc))
            except Exception as exc:
                result = adapter._failure(command, "board_shared_" + ("commit" if commit else "prepare")
                                          + "_failed:" + diagnostic.error_code(exc))
                result = result.model_copy(update={"result": {"diagnostic": diagnostic.metadata()}})
            adapter._remember(command, result)
            await asyncio.to_thread(adapter._persist_state)
            return result

    async def _diary_command(self, command, *, commit):
        from harness.cognitive import diary

        adapter = self.adapter
        diagnostic = diary.DiaryDiagnostics()
        async with adapter._lock:
            replay = adapter._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(adapter._persist_state)
                return replay
            rejected = adapter._preflight(command)
            if rejected is not None:
                return rejected
            try:
                expected = "CommitDiaryEntry" if commit else "PrepareDiaryEntry"
                if command.command_type != expected:
                    raise ValueError("diary_command_type_mismatch")
                payload = await (diary.commit(self, command, diagnostic=diagnostic) if commit
                                 else diary.prepare(self, command, diagnostic=diagnostic))
                if commit:
                    adapter._revision += 1
                    adapter._component_revisions["memory"] = adapter._revision
                adapter._cursor = max(adapter._cursor, command.receive_seq)
                adapter._event_id = command.event_id
                result = DomainCommandResult[dict](command_id=command.command_id,
                    command_type=command.command_type, status="applied",
                    request_hash=command.effective_request_hash(), committed_revision=adapter._revision,
                    acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                    result=payload, completed_at=datetime.now(timezone.utc))
            except Exception as exc:
                result = adapter._failure(command, "diary_shared_" + ("commit" if commit else "prepare")
                                          + "_failed:" + diagnostic.error_code(exc))
                result = result.model_copy(update={"result": {"diagnostic": diagnostic.metadata()}})
            adapter._remember(command, result)
            await asyncio.to_thread(adapter._persist_state)
            return result

    async def commit_interaction(self, command):
        return await self.adapter.commit_interaction(command)

    async def pause_maintenance(self, *, timeout: float) -> bool:
        return await self.adapter.pause_maintenance(timeout=timeout)

    async def resume_maintenance(self) -> None:
        await self.adapter.resume_maintenance()
        self.daily_revision.wake.set()

    async def close(self) -> None:
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        try:
            await self.daily_revision.close()
            await self.qq_memory.close()
            await self.adapter.close()
        finally:
            self.owner_lease.release()

    async def start(self) -> None:
        self.owner_lease.acquire()
        try:
            await asyncio.to_thread(self.shared.sync_qq)
            await asyncio.to_thread(self.shared.sync_local)
            await self.qq_memory.start()
            await self.daily_revision.start()
        except BaseException:
            self.owner_lease.release()
            raise

    async def ui_snapshot(self) -> dict[str, Any]:
        self._sync_transcript()
        return {**self.chat.status(), **self.shared.ui(), "qq_memory": self.qq_memory.status(),
                "daily_revision": self.daily_revision.status()}

    async def control(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        body = payload or {}
        # These authenticated maintenance operations schedule work immediately.
        # They must not hold _control_lock while awaiting a provider or acquire
        # it twice during a rollback's atomic commit.
        if operation.startswith("daily_revision."):
            action = operation.removeprefix("daily_revision.")
            if action == "status" and not body:
                return self.daily_revision.status(details=True)
            if action == "history" and not (set(body) - {"limit"}):
                return {"runs": self.daily_revision.history(**body)}
            if action == "diff" and set(body) == {"run_id"}:
                return self.daily_revision.diff(body["run_id"])
            if action == "configure":
                return self.daily_revision.configure(body)
            if action in {"run", "dry_run"} and not (set(body) - {"day"}):
                return await self.daily_revision.run_now(**body, dry_run=action == "dry_run")
            if action == "retry" and set(body) == {"run_id"}:
                return await self.daily_revision.retry(body["run_id"])
            if action == "rollback" and set(body) == {"run_id"}:
                return await self.daily_revision.rollback(body["run_id"])
            raise ValueError("daily_revision_control_parameters_invalid")
        async with self._control_lock:
            if operation == "snapshot":
                return await self.ui_snapshot()
            if operation == "presets.list":
                return self.chat.presets_payload()
            if operation == "debug.state":
                return {**self.chat.debug_state(), **self.shared.ui(),
                        "qq_memory": self.qq_memory.status(details=True)}
            if operation == "qq_memory.status":
                return self.qq_memory.status(details=True)
            if operation == "qq_memory.configure":
                return self.qq_memory.configure(body)
            if operation == "qq_memory.preview":
                if (not {"group_id", "query"} <= set(body)
                        or set(body) - {"group_id", "query", "member_id"}
                        or not all(isinstance(body[key], str) for key in body)):
                    raise ValueError("qq_memory_preview_parameters_invalid")
                async with self.adapter._lock:
                    return self.qq_memory.recall(group_id=body["group_id"],
                        query=body["query"], member_id=body.get("member_id"))
            if operation == "qq_memory.import":
                from harness.cognitive.qq_memory_import import import_backfill
                if (not {"output_dir", "expected_preset_sha256"} <= set(body)
                        or set(body) - {"output_dir", "expected_preset_sha256", "expected_owner_state_sha256"}):
                    raise ValueError("qq_memory_import_parameters_invalid")
                return await import_backfill(self, **body)
            if operation == "models":
                url = str(body.get("base_url") or self.chat.settings.llm_base_url)
                key = self.chat.settings.llm_api_key or "not-needed"
                if url.rstrip("/") != self.chat.settings.llm_base_url.rstrip("/"):
                    require_development("models.custom_endpoint", control_mode=self.control_mode)
                    # A configured endpoint's credential never follows a custom URL.
                    key = "not-needed"
                names = await asyncio.to_thread(
                    list_endpoint_models, url, key
                )
                return {"base_url": url, "models": names}
            require_development(operation, control_mode=self.control_mode)
            self._apply_mutation(operation, body)
            self.chat.pipeline.compiler = SharedMemoryCompiler(self.chat.settings)
            self.adapter.pipeline = self.chat.pipeline
            self._bind_emotion_pipeline()
            self.adapter.default_session_id = self.chat.active_session_id
            self.chat.flush_active()
            self._persist()
            snapshot = await self.ui_snapshot()
            self.observer.emit_state(snapshot)
            return snapshot if not operation.startswith("debug.") else self.chat.debug_state()

    def _bind_emotion_pipeline(self) -> None:
        self.chat.settings.emotion_mode = self._emotion_mode
        if self._emotion_mode == "on":
            self.chat.settings.prompt_version = "qwen-emotion-v1"
        self.adapter.emotion_service.settings = self.chat.settings
        self.adapter.emotion_service.llm = self.chat.llm
        self.chat.pipeline.emotion_service = self.adapter.emotion_service

    def _apply_mutation(self, operation: str, body: dict[str, Any]) -> None:
        if operation == "presets.open":
            self.chat.open_preset(str(body["preset_id"]))
        elif operation == "presets.apply":
            self.chat.apply_preset(str(body["preset_id"]))
        elif operation == "presets.new":
            self.chat.new_preset(body.get("from_id"))
        elif operation == "presets.restore" or operation == "reset":
            self.chat.restore_active()
        elif operation == "presets.delete":
            self.chat.delete_preset(str(body["preset_id"]))
        elif operation == "speakers":
            self.chat.set_speakers(user_name=body.get("user_name"), character_name=body.get("character_name"))
        elif operation == "config":
            self.chat.set_speakers(user_name=body.get("user_name"), character_name=body.get("character_name"))
            self.chat.rebuild(
                mode=body.get("mode"),
                base_url=body.get("base_url"),
                model=body.get("model"),
                api_key=body.get("api_key"),
                aux_base_url=body.get("aux_base_url"),
                aux_model=body.get("aux_model"),
                aux_api_key=body.get("aux_api_key"),
                validator_enabled=body.get("validator_enabled"),
                qq_latency=body.get("qq_latency"),
            )
            self.adapter.pipeline = self.chat.pipeline
        elif operation == "debug.relationship":
            apply_relationship(self.chat.memory, RelationshipPatch.model_validate(body))
        elif operation == "debug.psych":
            apply_psych(self.chat.memory, PsychPatch.model_validate(body))
        elif operation == "debug.persona":
            patch = PersonaPatch.model_validate(body)
            apply_persona(self.chat.memory, patch)
            if patch.name is not None:
                self.chat.set_speakers(character_name=patch.name)
        elif operation == "debug.episodic":
            apply_episodic(self.chat.memory, EpisodicPatch.model_validate(body))
        elif operation == "debug.semantic":
            apply_semantic(self.chat.memory, SemanticPatch.model_validate(body))
        elif operation == "debug.candidate":
            apply_candidate(self.chat.memory, CandidatePatch.model_validate(body))
        elif operation == "debug.tuning":
            apply_tuning(self.chat.settings, TuningPatch.model_validate(body), memory=self.chat.memory)
        else:
            raise ValueError(f"unsupported cognitive control operation: {operation}")

    def _sync_transcript(self) -> None:
        self.chat.transcript = self.chat.pipeline.session_store.recent(self.chat.active_session_id)

    def _flush_committed_memory(self) -> None:
        self._sync_transcript()
        self.chat.flush_active()
        self.shared.sync_local()

    def _persist(self) -> None:
        persist_gui_settings(
            mode=self.chat.mode,
            settings=self.chat.settings,
            path=self.config_path,
            user_name=self.chat.user_name,
            character_name=self.chat.character_name,
            active_preset_id=self.chat.active_preset_id,
            open_preset_ids=self.chat.open_preset_ids,
        )


def create_app():
    token = require_service_token(os.environ.get("NC_SERVICE_TOKEN", "development-service-token"), name="cognitive")
    config_path = default_config_path()
    presets_dir = Path(os.environ.get("NC_GUI_PRESETS", str(ROOT / "data" / "presets")))
    state_path = Path(os.environ.get("NC_COGNITIVE_STATE_PATH", str(ROOT / "data" / "cognitive" / "state.json")))
    host = CognitiveServiceHost(config_path=config_path, presets_dir=presets_dir, state_path=state_path)
    app = create_domain_service_app(host, service_token=token, service_name="cognitive")

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        if authorization != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid service token")

    @app.get("/v1/ui/snapshot")
    async def ui_snapshot(authorization: str | None = Header(default=None)) -> dict[str, Any]:
        await authenticate(authorization)
        return await host.ui_snapshot()

    @app.post("/v1/ui/control")
    async def ui_control(payload: dict[str, Any], authorization: str | None = Header(default=None)) -> dict[str, Any]:
        await authenticate(authorization)
        try:
            return await host.control(str(payload.get("operation") or ""), dict(payload.get("payload") or {}))
        except PermissionError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/events/stream")
    async def events(authorization: str | None = Header(default=None)) -> StreamingResponse:
        await authenticate(authorization)
        subscription = host.observer.subscribe()

        async def generate():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(subscription[1].get(), timeout=15.0)
                        yield f"event: cognitive.ui\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                host.observer.unsubscribe(subscription)

        return StreamingResponse(generate(), media_type="text/event-stream")

    app.state.host = host
    app.router.add_event_handler("startup", host.start)
    app.router.add_event_handler("shutdown", host.close)
    return app


def main() -> None:
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=PORT, reload=False)


if __name__ == "__main__":
    main()
