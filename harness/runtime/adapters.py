"""Transport-neutral domain service ports and in-process reference adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal, Protocol, TypeVar

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from harness.contracts.decisions import (
    ActionOutcomeEvidence,
    AutonomousAssessment,
    CommitEvidence,
    DeferDecision,
    DecisionEvidence,
    DeliveryEvidence,
    GoalProposal,
    NoActionDecision,
    PreparedResponse,
    ReplyDecision,
)
from harness.cognitive.autonomous import (
    assessment_id_for,
    load_assessment_record,
    run_or_load_assessment,
)
from harness.cognitive.action_planner import ActionDecisionPlanner
from harness.contracts.envelope import (
    CONTRACT_SCHEMA_VERSION,
    DomainCommandEnvelope,
    DomainCommandResult,
    VersionVector,
)
from harness.files import replace_path
from harness.core.error_notice import ERROR_FALLBACK_REPLY, ERROR_FALLBACK_SEND_SECONDS
from harness.core.inference import ContextSegment, FinalResponse, InteroceptiveState, SessionTurn
from harness.core.models import EpisodicEvent, InteractionEvent
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.pipeline.pipeline import InferencePipeline
from harness.runtime.cognitive_store import CognitiveStateStore, DELETED, TrackedDict, write_checkpoint
from harness.qq.telemetry import measure_stage
from infra.trace.request_spans import trace_scope

_CommandResult = TypeVar("_CommandResult")


class DomainSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CONTRACT_SCHEMA_VERSION
    service: str
    revision: int = Field(ge=0)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    acknowledged_event_id: str | None = None
    component_revisions: dict[str, int] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class ServiceHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    healthy: bool = True
    schema_version: str = CONTRACT_SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class CognitiveServiceClient(Protocol):
    async def observe_emotion(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def appraise_affect(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def read_affect_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    async def health(self) -> ServiceHealth: ...

    async def get_snapshot(self) -> DomainSnapshot: ...

    async def assess_text_interruption(self, request) -> dict[str, Any]: ...

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]: ...

    async def prepare_capability_call(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def prepare_goal_decision(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def prepare_web_step(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def assess_autonomous(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[AutonomousAssessment]: ...

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def pause_maintenance(self, *, timeout: float) -> bool: ...

    async def resume_maintenance(self) -> None: ...

    async def ui_snapshot(self) -> dict[str, Any]: ...

    async def control(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class HomeostasisServiceClient(Protocol):
    async def health(self) -> ServiceHealth: ...

    async def get_snapshot(self) -> DomainSnapshot: ...

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_appraisal(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def assimilate_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]: ...

    async def read_traces(
        self, *, since: str | None = None, until: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


class _PreparedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: InteractionEvent
    final: FinalResponse
    prepared: PreparedResponse
    autonomous_assessment_id: str | None = None
    autonomous_record_ref: str | None = None
    autonomous_memory_ref: str | None = None
    research: bool = False
    capability: bool = False
    production: bool = False
    production_autonomous: bool = False


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _validated_homeostatic_interoception(
    command: DomainCommandEnvelope[dict[str, Any]],
) -> HomeostaticInteroceptiveSummary | None:
    profile_marker = command.payload.get("homeostatic_profile_ref")
    require_summary = False
    if profile_marker is not None:
        if not isinstance(profile_marker, str) or not profile_marker.strip():
            raise ValueError("homeostatic_profile_ref must be a non-empty string")
        _, separator, revision_text = profile_marker.rpartition("@")
        if not separator:
            raise ValueError("homeostatic_profile_ref must end with @<revision>")
        try:
            profile_revision = int(revision_text)
        except ValueError as exc:
            raise ValueError(
                "homeostatic_profile_ref must end with an integer revision"
            ) from exc
        if profile_revision < 1:
            raise ValueError("homeostatic_profile_ref revision must be positive")
        require_summary = profile_revision >= 3

    raw = command.payload.get("homeostatic_interoception")
    if raw is None:
        if require_summary:
            raise ValueError(
                "homeostatic interoception is required for profile revision 3 or later"
            )
        return None
    summary = HomeostaticInteroceptiveSummary.model_validate(raw)
    if profile_marker is not None and summary.profile_ref != profile_marker:
        raise ValueError(
            "homeostatic interoception profile mismatch: "
            f"summary={summary.profile_ref}, marker={profile_marker}"
        )
    if command.version_vector is None:
        raise ValueError("homeostatic interoception requires a version vector")
    expected_revision = command.version_vector.homeostasis_state
    if summary.snapshot_revision != expected_revision:
        raise ValueError(
            "homeostatic interoception revision mismatch: "
            f"summary={summary.snapshot_revision}, version_vector={expected_revision}"
        )
    if summary.schema_version == "homeostatic-interoception-v1" and summary.salience == "critical":
        raise ValueError("critical homeostatic state must pause before cognitive invocation")
    return summary


class PipelineCognitiveAdapter:
    """Idempotent PrepareResponse/CommitInteraction boundary for M2 Pipeline.

    The optional state file persists the command ledger, prepared results,
    revisions, acknowledged cursor and Session projection. Memory durability is
    still owned by the configured ``MemoryService`` backend.
    """

    def __init__(
        self,
        pipeline: InferencePipeline,
        *,
        character_id: str = "character",
        state_path: Path | str | None = None,
        autonomous_log_dir: Path | str | None = None,
        default_session_id: str = "sess_default",
        action_planner: ActionDecisionPlanner | None = None,
        persist_memory: Callable[[], None] | None = None,
        context_provider: Callable[[InteractionEvent], list[ContextSegment]] | None = None,
        archive_local: Callable[..., Any] | None = None,
        archive_action: Callable[..., Any] | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.character_id = character_id
        self.state_path = Path(state_path) if state_path is not None else None
        if autonomous_log_dir is not None:
            self.autonomous_log_dir = Path(autonomous_log_dir)
        elif self.state_path is not None:
            self.autonomous_log_dir = self.state_path.parent / "autonomous"
        else:
            self.autonomous_log_dir = Path("data/cognitive/autonomous")
        self.default_session_id = default_session_id
        self.action_planner = action_planner or ActionDecisionPlanner()
        self._persist_memory = persist_memory
        self._context_provider = context_provider
        self._archive_local = archive_local
        self._archive_action = archive_action
        self._lock = asyncio.Lock()
        self._affect_lock = asyncio.Lock()
        from harness.runtime.background import ThreadLane

        self._affect_thread = ThreadLane("nc-affect")
        self._revision = 0
        self._cursor = 0
        self._event_id: str | None = None
        self._component_revisions = {
            "world": 0,
            "relationship": 0,
            "memory": 0,
            "session": 0,
            "maintenance": 0,
        }
        self._ledger: dict[tuple[str, str], DomainCommandResult[Any]] = TrackedDict()
        self._request_hashes: dict[tuple[str, str], str] = TrackedDict()
        self._prepared: dict[str, _PreparedEntry] = TrackedDict()
        self._research_manifest = None
        self._research_manifest_hash: str | None = None
        self.research_traces: dict[str, dict[str, Any]] = TrackedDict()
        self.production_affect: dict[str, dict[str, Any]] = TrackedDict()
        self._production_session_locks: dict[str, asyncio.Lock] = {}
        # Backend choice is fixed for this owner's lifetime. A restart performs
        # the durable authority handoff when the compatibility flag changes.
        self._incremental_enabled = bool(getattr(pipeline.settings, "cognitive_incremental_persistence_enabled", False))
        self._state_store: CognitiveStateStore | None = None
        self._persistence_lock = threading.RLock()
        self._persisted_session_store = pipeline.session_store
        self._persisted_session_keys: set[str] = set()
        self.research_tasks: set[asyncio.Task] = set()
        self.command_tasks: set[asyncio.Task] = set()
        self._closing = False
        self._close_task: asyncio.Task | None = None
        self._maintenance_paused = False
        from harness.emotion import EmotionService

        self.emotion_service = EmotionService(pipeline.settings, pipeline.llm, persist=self._persist_emotion_state)
        self.pipeline.emotion_service = self.emotion_service
        self.emotion_input_provider = None
        self._load_state()
        self._legacy_initial_state = (json.dumps(self._legacy_state_payload(), ensure_ascii=False)
            if not self._incremental_enabled and self.state_path is not None and not self.state_path.exists() else None)
        self._publish_snapshot()

    async def health(self) -> ServiceHealth:
        snapshot = self._committed_snapshot
        return ServiceHealth(
            service="cognitive",
            revision=snapshot.revision,
            acknowledged_receive_seq=snapshot.acknowledged_receive_seq,
            detail={"maintenance_paused": self._maintenance_paused,
                    "commands_pending": sum(not task.done() for task in self.command_tasks),
                    "emotion_mode": snapshot.payload.get("emotion_mode", "off")},
        )

    async def get_snapshot(self) -> DomainSnapshot:
        return self._committed_snapshot.model_copy(deep=True)

    def _publish_snapshot(self) -> None:
        # Readers see a complete durable projection even while a worker is
        # updating memory, flushing the preset, or writing the command ledger.
        self._committed_snapshot = DomainSnapshot(
            service="cognitive",
            revision=self._revision,
            acknowledged_receive_seq=self._cursor,
            acknowledged_event_id=self._event_id,
            component_revisions=dict(self._component_revisions),
            payload={"prepared_count": len(self._prepared), **({
                "emotion_mode": self.pipeline.settings.emotion_mode,
                "default_session_id": self.default_session_id,
            } if self.pipeline.settings.emotion_mode != "off" else {})},
        )

    async def _run_command(
        self, operation: Callable[[], Awaitable[_CommandResult]],
    ) -> _CommandResult:
        if self._closing:
            raise RuntimeError("cognitive service is closing")
        task = asyncio.create_task(operation())
        self.command_tasks.add(task)

        def completed(done: asyncio.Task) -> None:
            self.command_tasks.discard(done)
            if not done.cancelled():
                done.exception()

        task.add_done_callback(completed)
        # The operation, not its caller, owns the state lock. HTTP timeouts
        # cannot release it while to_thread still mutates the same domain.
        return await asyncio.shield(task)

    async def observe_emotion(self, command):
        async def operation():
            async with self._lock:
                replay = self._lookup_replay(command)
                if replay is not None:
                    await asyncio.to_thread(self._persist_state)
                    return replay
                rejected = self._preflight(command)
                if rejected is not None:
                    return rejected
                if (command.command_type != "ObserveEmotion" or self.pipeline.settings.emotion_mode == "off"
                        or self._research_manifest is not None):
                    return self._failure(command, "emotion observation is disabled", status="rejected")
                from harness.runtime.emotion import observation_input

                homeostasis = command.payload.get("homeostasis") or {}
                if (command.version_vector is not None and homeostasis.get("snapshot_revision")
                        != command.version_vector.homeostasis_state):
                    return self._failure(command, "emotion snapshot revision mismatch", status="rejected")
                request_key = (command.command_type, command.idempotency_key)
                request_hash = command.effective_request_hash()
                reserved_hash = self._request_hashes.get(request_key)
                if reserved_hash is not None and reserved_hash != request_hash:
                    return self._failure(command, "emotion reservation request conflict", status="conflict")
                self._request_hashes[request_key] = request_hash
                await asyncio.to_thread(self._persist_state)
                # Persisting the reservation can consume a short remaining QQ
                # window. Do not invoke its source reader on an expired command.
                rejected = self._preflight(command)
                if rejected is not None:
                    self._remember(command, rejected)
                    await asyncio.to_thread(self._persist_state)
                    return rejected
                provider = self.emotion_input_provider or (lambda value: observation_input(self, value))
                origin = (command.payload.get("source_payload") or {}).get("trace") or {}
                qq_trace = ({"qq_trace_id": origin["trace_id"]} if origin.get("trace_id")
                            and command.payload.get("source_command_type") in {"PrepareQQResponse", "PrepareQQTurn"} else {})
                with trace_scope(correlation_id=command.correlation_id, event_id=command.event_id,
                                 command_id=command.command_id, phase="emotion_appraisal", **qq_trace):
                    observation = self.emotion_service.existing_input(command.payload["context_id"])
                    if observation is None:
                        from harness.qq.direct_source import DirectQQSourceExpired

                        try:
                            observation = await asyncio.to_thread(provider, command)
                        except DirectQQSourceExpired:
                            # Source expiry can race the preflight above. Only
                            # that typed error, after our original command also
                            # expired, is a deadline result; all other source or
                            # persistence failures must remain visible failures.
                            if not command.is_expired(now=_utc_now()):
                                raise
                            rejected = self._failure(command, "command deadline expired", status="rejected")
                            self._remember(command, rejected)
                            await asyncio.to_thread(self._persist_state)
                            return rejected
                    frame = await asyncio.to_thread(self.emotion_service.observe, observation)
                self._cursor = max(self._cursor, command.receive_seq)
                self._event_id = command.event_id
                result = DomainCommandResult[dict](command_id=command.command_id,
                    command_type=command.command_type, status="applied",
                    request_hash=command.effective_request_hash(), committed_revision=self._revision,
                    acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                    result={"emotion_context_ref": frame.context_id, "emotion_revision": frame.revision},
                    completed_at=_utc_now())
                self._remember(command, result)
                await asyncio.to_thread(self._persist_state)
                return result
        return await self._run_command(operation)

    async def read_affect_snapshot(self, payload: dict[str, Any]) -> dict[str, Any]:
        if (self.pipeline.settings.emotion_mode == "off" or self._research_manifest is not None):
            scope = str(payload.get("scope") or "")
            return {"status": "off", "revision": 0, "frame": None, "guidance": {"text": "", "status": "off"},
                    "window_hash": "", "source_ids": [], "degraded": True, "scope": scope}
        as_of = payload["as_of"]
        if isinstance(as_of, str):
            as_of = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        persona = self.pipeline.memory.get_persona()
        return await asyncio.to_thread(
            self.emotion_service.read_snapshot, scope=payload["scope"], as_of=as_of,
            persona=persona, homeostasis=payload.get("homeostasis") or {})

    async def appraise_affect(self, command):
        async def operation():
            # One affect writer preserves causal stimulus order, independently
            # of the foreground command queue. The shared command lock covers
            # only reservation and final ledger publication, never model I/O.
            async with self._affect_lock:
                return await self._appraise_affect(command)
        return await self._run_command(operation)

    async def _appraise_affect(self, command):
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(self._persist_state)
                return replay
            rejected = self._preflight(command)
            if rejected is not None:
                return rejected
            if (command.command_type != "AppraiseAffect" or self.pipeline.settings.emotion_mode == "off"
                    or self._research_manifest is not None):
                return self._failure(command, "emotion observation is disabled", status="rejected")
            from harness.runtime.emotion import observation_input

            homeostasis = command.payload.get("homeostasis") or {}
            if (command.version_vector is not None and homeostasis.get("snapshot_revision")
                    != command.version_vector.homeostasis_state):
                return self._failure(command, "emotion snapshot revision mismatch", status="rejected")
            request_key = (command.command_type, command.idempotency_key)
            request_hash = command.effective_request_hash()
            reserved_hash = self._request_hashes.get(request_key)
            if reserved_hash is not None and reserved_hash != request_hash:
                return self._failure(command, "emotion reservation request conflict", status="conflict")
            self._request_hashes[request_key] = request_hash
            await asyncio.to_thread(self._persist_state)
        provider = self.emotion_input_provider or (lambda value: observation_input(self, value))
        with trace_scope(event_id=command.event_id, command_id=command.command_id, phase="emotion_appraisal"):
            observation = await self._affect_thread.run(self.emotion_service.existing_input, command.payload["context_id"])
            dropped = []
            if observation is None:
                observation = await self._affect_thread.run(provider, command)
                observation, dropped = await self._affect_thread.run(self.emotion_service.drop_conflicting_new_sources, observation)
            frame = await self._affect_thread.run(self.emotion_service.observe, observation)
            guidance = await self._affect_thread.run(self.emotion_service.guidance, frame)
        async with self._lock:
            if command.receive_seq >= self._cursor:
                self._event_id = command.event_id
            self._cursor = max(self._cursor, command.receive_seq)
            result = DomainCommandResult[dict](command_id=command.command_id,
                command_type=command.command_type, status="applied",
                request_hash=command.effective_request_hash(), committed_revision=self._revision,
                acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                result={"accepted_ids": self.emotion_service.accepted_new_sources(frame.context_id),
                        "dropped_conflict_ids": dropped, "emotion_revision": frame.revision,
                        "frame_status": frame.status, "guidance_status": guidance.status,
                        "appraisal_ms": frame.diagnostics.get("elapsed_ms"),
                        "guidance_ms": guidance.diagnostics.get("elapsed_ms")},
                completed_at=_utc_now())
            self._remember(command, result)
            await asyncio.to_thread(self._persist_state)
            return result

    async def emotion_guidance(self, command):
        if self.pipeline.settings.agentic_enabled:
            return None
        reference = command.payload.get("emotion_context_ref")
        if reference is None or self.pipeline.settings.emotion_mode == "off":
            return None
        from harness.llm.deadline import call_before_deadline

        observation = self.emotion_service.existing_input(reference)
        if observation is None:
            raise ValueError("emotion_context_not_committed")
        if command.command_type in {"PrepareQQResponse", "PrepareQQTurn"}:
            from harness.qq.conversation import conversation_audience
            expected_scope = conversation_audience(command.payload["group_id"])
        elif command.command_type == "PrepareBoardEntry":
            expected_scope = "board:" + str(command.payload["board_id"])
        else:
            expected_scope = "local:" + str(command.payload.get("session_id") or self.default_session_id)
        if observation.scope != expected_scope:
            raise ValueError("emotion_guidance_scope_mismatch")
        # Cache the second model result before main generation, including when
        # generation fails. Reading it never adds a new emotional stimulus.
        options = {}
        if command.command_type in {"PrepareQQResponse", "PrepareQQTurn"}:
            from harness.qq.policy import QQLatencyOptions
            from harness.qq.semantic_alignment import mode_for
            frozen = command.payload.get("qq_frozen_policy")
            policy = QQLatencyOptions.model_validate(frozen) if frozen is not None else self.pipeline.settings
            if mode_for(policy, group_id=command.payload["group_id"],
                        self_member_id=command.payload.get("self_member_id")) == "enforce":
                options["expression_only"] = True
        guidance = await asyncio.to_thread(call_before_deadline, command.deadline,
            self.emotion_service.guidance, reference, **options)
        if guidance.revision != command.payload.get("emotion_revision"):
            raise ValueError("emotion_guidance_revision_mismatch")
        return guidance

    def _emotion_model_deadline(self, command):
        if self.pipeline.settings.agentic_enabled:
            return command.deadline
        if self.pipeline.settings.emotion_mode == "off":
            return None
        reference = command.payload.get("emotion_context_ref")
        observation = self.emotion_service.existing_input(reference) if reference else None
        original = observation.deadline_at if observation is not None else None
        return min(command.deadline, original) if original is not None else command.deadline

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]:
        with trace_scope(
            correlation_id=command.correlation_id, event_id=command.event_id,
            command_id=command.command_id, phase="prepare", span_id=command.command_id,
        ):
            if command.payload.get("mode") == "grounded_agency_decision":
                if self._closing:
                    raise RuntimeError("cognitive service is closing")
                return await self._prepare_response(command)
            return await self._run_command(lambda: self._prepare_response(command))

    async def assess_text_interruption(self, request):
        from harness.cognitive.interruption import InterruptionAssessor
        if self._maintenance_paused or self._closing:
            raise RuntimeError("interruption_owner_paused")
        if getattr(self, "_interruption_assessor", None) is None:
            self._interruption_assessor = InterruptionAssessor(self.pipeline.llm)
        return await self._interruption_assessor.assess(request)

    async def _prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]:
        if command.payload.get("production_context") is not None:
            from harness.cognitive.production import prepare
            return await prepare(self, command)
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(self._persist_state)
                return DomainCommandResult[PreparedResponse].model_validate(replay.model_dump(mode="json"))
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[PreparedResponse].model_validate(rejected.model_dump(mode="json"))

            payload = command.payload
            if payload.get("mode") == "grounded_agency_decision":
                return await self._prepare_grounded(command)
            observer = getattr(self.pipeline, "observer", None)
            bind = getattr(observer, "bind", None)
            if callable(bind):
                bind(str(payload.get("call_id") or command.correlation_id))
            mode = str(payload.get("mode") or "")
            source_time = payload.get("source_time") or command.issued_at
            autonomous = mode == "autonomous"
            reconsideration = mode == "reconsideration"
            internal = autonomous or reconsideration
            session_id = str(payload.get("session_id") or self.default_session_id)
            try:
                homeostatic_interoception = _validated_homeostatic_interoception(command)
                emotion_guidance = await self.emotion_guidance(command)
            except Exception as exc:
                result = self._failure(command, f"invalid homeostatic interoception: {exc}")
                self._remember(command, result)
                await asyncio.to_thread(self._persist_state)
                return DomainCommandResult[PreparedResponse].model_validate(
                    result.model_dump(mode="json")
                )
            assessment = None
            if autonomous:
                try:
                    assessment = self._load_speak_assessment(payload)
                except Exception as exc:
                    result = self._failure(command, f"invalid autonomous assessment: {exc}")
                    self._remember(command, result)
                    await asyncio.to_thread(self._persist_state)
                    return DomainCommandResult[PreparedResponse].model_validate(
                        result.model_dump(mode="json")
                    )
            event = InteractionEvent(
                event_id=command.event_id,
                timestamp=source_time,
                channel=str(payload.get("channel") or "chat"),
                speaker=str(payload.get("speaker") or ("runtime" if internal else "user")),
                modality="text",
                raw_text=str(payload.get("text") or ("（主动表达准备）" if internal else "")),
                session_id=session_id,
            )
            interoception_payload = payload.get("interoception")
            interoception: InteroceptiveState | None = None
            context_segments: list[ContextSegment] = []
            if payload.get("text_interruption_context"):
                context_segments.append(ContextSegment(kind="text_interruption",
                    text=json.dumps(payload["text_interruption_context"], ensure_ascii=False),
                    why="Continuation after a new message; delivered text must not be repeated"))
            if self._context_provider is not None:
                context_segments.extend(await asyncio.to_thread(self._context_provider, event))
            runtime_web_context = payload.get("runtime_web_context")
            if isinstance(interoception_payload, dict):
                if "resource_label" in interoception_payload:
                    cognitive_payload = dict(interoception_payload)
                    source_revision = cognitive_payload.pop("source_revision", None)
                    if "evidence_refs" not in cognitive_payload:
                        if not isinstance(source_revision, int) or source_revision < 0:
                            raise ValueError(
                                "redacted interoception requires a non-negative source_revision"
                            )
                        cognitive_payload["evidence_refs"] = [
                            f"body-state:{source_revision}"
                        ]
                    interoception = InteroceptiveState.model_validate(cognitive_payload)
                elif "resource_level" in interoception_payload:
                    level = float(interoception_payload["resource_level"])
                    trend = float(interoception_payload["resource_trend"])
                    if level <= 0.1:
                        semantic_level = "critical"
                    elif level < 0.3:
                        semantic_level = "low"
                    elif level < 0.7:
                        semantic_level = "nominal"
                    elif level < 0.98:
                        semantic_level = "high"
                    else:
                        semantic_level = "full"
                    source_revision = int(interoception_payload["source_revision"])
                    interoception = InteroceptiveState(
                        resource_label="internal_resource",
                        level=semantic_level,
                        trend=max(-1.0, min(1.0, trend)),
                        time_to_action_limit_seconds=interoception_payload.get(
                            "estimated_time_to_limit_seconds"
                        ),
                        confidence=float(interoception_payload.get("confidence", 1.0)),
                        captured_at=source_time,
                        evidence_refs=[f"body-state:{source_revision}"],
                    )
            try:
                if runtime_web_context is not None:
                    from harness.cognitive.web import web_context_segments
                    context_segments.extend(web_context_segments(runtime_web_context))
                terminal_decision = None
                terminal_context = payload.get("basic_terminal") if mode == "basic_terminal_task" else None
                if isinstance(terminal_context, dict) and terminal_context.get("enabled"):
                    from harness.cognitive.basic_terminal import BasicTerminalActionPlanner

                    if terminal_context.get("error"):
                        terminal_decision = ReplyDecision(text=str(terminal_context["error"]), segments=[str(terminal_context["error"])])
                    else:
                        terminal_decision = await asyncio.to_thread(
                            BasicTerminalActionPlanner(self.pipeline.llm).decide,
                            str(terminal_context.get("text") or event.raw_text),
                            terminal_app_id=terminal_context["terminal_app_id"],
                            provider_intents=terminal_context.get("provider_intents") or [],
                            permitted_intents=terminal_context["permitted_intents"],
                            observation=terminal_context.get("observation"),
                            history=terminal_context.get("history") or [],
                            task_id=terminal_context["task_id"],
                            step=terminal_context["step"],
                            max_steps=terminal_context["max_steps"],
                        )
                    if isinstance(terminal_decision, DeferDecision):
                        message = f"终端任务未完成：{terminal_decision.reason}"
                        terminal_decision = ReplyDecision(text=message, segments=[message])
                    if isinstance(terminal_decision, ReplyDecision) and not terminal_decision.segments:
                        terminal_decision = terminal_decision.model_copy(update={"segments": [terminal_decision.text]})
                    if terminal_decision is not None:
                        event = event.model_copy(update={
                            "event_id": str(terminal_context["root_event_id"]),
                            "raw_text": str(terminal_context.get("text") or event.raw_text),
                        })
                if terminal_decision is not None:
                    text = terminal_decision.text if isinstance(terminal_decision, ReplyDecision) else ""
                    final = FinalResponse(
                        text=text, segments=[text] if text else [], path="fast",
                        situation_id=f"terminal:{command.event_id}", event_id=event.event_id,
                    )
                    if text and observer is not None:
                        observer.on_stage("final", final, {"latency_ms": 0.0})
                elif autonomous and assessment is not None:
                    from harness.llm.deadline import call_before_deadline

                    final = await asyncio.to_thread(
                        call_before_deadline,
                        self._emotion_model_deadline(command),
                        self.pipeline.prepare_autonomous,
                        event_id=command.event_id,
                        assessment_id=assessment.assessment_id,
                        assessment_text=assessment.body,
                        record_ref=assessment.record_ref,
                        homeostasis=dict(payload.get("homeostasis") or {}),
                        policies=[dict(item) for item in (payload.get("policies") or [])],
                        session_id=session_id,
                        channel=str(payload.get("channel") or "chat"),
                        now=command.issued_at,
                        call_id=str(payload.get("call_id") or command.correlation_id),
                        interoception=interoception,
                        homeostatic_interoception=homeostatic_interoception,
                        **({"emotion_guidance": emotion_guidance} if emotion_guidance is not None else {}),
                    )
                else:
                    from harness.llm.deadline import call_before_deadline

                    generation_deadline = self._emotion_model_deadline(command)
                    if runtime_web_context is not None:
                        generation_deadline = generation_deadline or command.deadline
                        if payload.get("runtime_web_deadline_at") is not None:
                            original_deadline = datetime.fromisoformat(str(payload["runtime_web_deadline_at"]).replace("Z", "+00:00"))
                            generation_deadline = min(generation_deadline, original_deadline)
                    final = await asyncio.to_thread(
                        call_before_deadline,
                        generation_deadline,
                        self.pipeline.prepare,
                        event,
                        now=command.issued_at,
                        call_id=str(payload.get("call_id") or command.correlation_id),
                        context_segments=context_segments,
                        interoception=interoception,
                        homeostatic_interoception=homeostatic_interoception,
                        **({"emotion_guidance": emotion_guidance} if emotion_guidance is not None else {}),
                    )
                now = _utc_now()
                read_versions = command.version_vector or self._version_vector()
                decision: ReplyDecision | GoalProposal | NoActionDecision | DeferDecision
                if terminal_decision is not None:
                    decision = terminal_decision
                elif reconsideration:
                    decision = NoActionDecision(
                        reason="continue current goal after interoceptive reconsideration"
                    )
                else:
                    planned = self.action_planner.decide(payload, mode=mode)
                    if planned is not None:
                        decision = planned
                    elif final.text.strip():
                        decision = ReplyDecision(
                            text=final.text,
                            segments=list(final.segments) or [final.text],
                            channel=str(payload.get("channel") or "chat"),
                        )
                    else:
                        decision = NoActionDecision(
                            reason="cognitive pipeline produced no reply"
                        )
                prepared = PreparedResponse(
                    event_id=command.event_id,
                    correlation_id=command.correlation_id,
                    session_id=event.session_id,
                    decision=decision,
                    read_versions=read_versions,
                    prepared_at=now,
                    expires_at=command.deadline,
                    model_version=self.pipeline.settings.model_version,
                    prompt_version=self.pipeline.settings.prompt_version,
                )
            except Exception as exc:
                if self.pipeline.settings.agentic_enabled or internal or mode in {"basic_terminal_task", "grounded_agency_decision"}:
                    result = self._failure(command, str(exc))
                    self._remember(command, result)
                    await asyncio.to_thread(self._persist_state)
                    return DomainCommandResult[PreparedResponse].model_validate(result.model_dump(mode="json"))
                text = ERROR_FALLBACK_REPLY
                now = _utc_now()
                final = FinalResponse(
                    text=text, segments=[text], path="fast",
                    situation_id=f"error-fallback:{command.event_id}", event_id=command.event_id,
                )
                if observer is not None:
                    try:
                        observer.on_stage("final", final, {"latency_ms": 0.0, "error_fallback": True})
                    except Exception:
                        pass
                decision = ReplyDecision(
                    text=text, segments=[text], channel=str(payload.get("channel") or "chat"),
                )
                read_versions = command.version_vector or self._version_vector()
                fallback_expiry = now + timedelta(seconds=ERROR_FALLBACK_SEND_SECONDS)
                if command.deadline > fallback_expiry:
                    fallback_expiry = command.deadline
                prepared = PreparedResponse(
                    event_id=command.event_id,
                    correlation_id=command.correlation_id,
                    session_id=event.session_id,
                    decision=decision,
                    read_versions=read_versions,
                    prepared_at=now,
                    expires_at=fallback_expiry,
                    model_version=self.pipeline.settings.model_version,
                    prompt_version=self.pipeline.settings.prompt_version,
                )

            self._prepared[prepared.prepare_id] = _PreparedEntry(
                event=event,
                final=final,
                prepared=prepared,
                autonomous_assessment_id=assessment.assessment_id if assessment else None,
                autonomous_record_ref=assessment.record_ref if assessment else None,
                autonomous_memory_ref=(
                    (str(payload.get("internal_memory_ref") or "") or None)
                    if assessment
                    else None
                ),
            )
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[PreparedResponse](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=command.receive_seq,
                result=prepared,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            await asyncio.to_thread(self._persist_state)
            return result

    def bind_research_manifest(self, manifest) -> None:
        if manifest.grounded_agency is None:
            raise ValueError("a grounded agency manifest is required")
        digest = manifest.content_hash()
        if self._research_manifest_hash is not None and digest != self._research_manifest_hash:
            raise ValueError("cognitive research manifest drift: create an isolated state directory")
        self._research_manifest = manifest
        self._research_manifest_hash = digest

    async def _prepare_grounded(self, command):
        from harness.cognitive.grounded_agency import GroundedAgencyDecisionModel
        from harness.core.inference import SituationActor, SituationState, SituationTrigger
        from harness.core.models import CharacterSnapshot
        from harness.research.grounded_agency.hashes import content_hash
        from harness.research.grounded_agency.manifest import GroundedAgencyRuntimeOptions
        from harness.research.interoception.contracts import OpaqueInteroceptiveObservation
        from harness.runtime.experimental_decision import ExperimentalDecisionCompiler

        try:
            manifest = self._research_manifest
            if manifest is None or command.payload.get("grounded_manifest_hash") != manifest.content_hash():
                raise ValueError("research command does not match the bound manifest")
            ga = manifest.grounded_agency
            options = ga.runtime or GroundedAgencyRuntimeOptions()
            payload = command.payload
            observations = tuple(OpaqueInteroceptiveObservation.model_validate(item) for item in payload.get("opaque_interoception", []))
            if observations and ga.sensor_transform is None:
                raise ValueError("opaque observations supplied to a non-opaque condition")
            additional_segments: list[ContextSegment] = []
            for raw_segment in payload.get("research_context", []):
                segment = ContextSegment.model_validate(raw_segment)
                if segment.kind not in {"body_memory", "persistent_goals", "entity_observation", "entity_memory"}:
                    raise ValueError("unsupported research context segment")
                if ga.memory_treatment == "none" and segment.kind == "body_memory":
                    raise ValueError("body memory is not visible in this treatment")
                if segment.kind == "persistent_goals" and (ga.agency is None or not ga.agency.enabled):
                    raise ValueError("persistent goals are disabled in this treatment")
                if segment.kind.startswith("entity_"):
                    if ga.open_social is None or not ga.open_social.enabled:
                        raise ValueError("entity context is disabled in this treatment")
                    if segment.kind == "entity_memory" and ga.open_social.entity_memory == "none":
                        raise ValueError("entity memory is not visible in this treatment")
                additional_segments.append(segment)
            if ga.semantic_homeostasis and payload.get("interoception") is not None:
                from harness.compiler.compiler import _interoception_text

                summary = {**payload["interoception"], "evidence_refs": ["semantic-comparator"]}
                additional_segments.append(ContextSegment(kind="semantic_comparator", text=_interoception_text(summary)))
            situation = SituationState(
                trigger=SituationTrigger(ref=command.event_id), utterance=str(payload.get("text") or ""),
                actor=SituationActor(), snapshot=CharacterSnapshot(persona=self.pipeline.memory.get_persona()),
                world=self.pipeline.memory.stores.world.latest(),
            )
            context = self.pipeline.compiler.compile(
                situation, opaque_interoception=observations, opaque_token_budget=options.opaque_token_budget,
                additional_segments=additional_segments,
                required_user_context=True,
                required_additional_context=True,
            )
            messages = context.render_messages()
            self.pipeline.compiler.validate_final_messages(messages, manifest=manifest)
            usage = None
            text = ""
            if ga.action_selection == "model_structured":
                result = await self._call_research_worker(
                    GroundedAgencyDecisionModel(self.pipeline.llm, max_repairs=options.max_decision_repairs).decide,
                    messages, manifest_intents=options.permitted_intents,
                    provider_intents=payload.get("provider_intents", ()), opaque=ga.is_opaque,
                    rules_version=ga.neutrality_rules_version, rules_digest=ga.neutrality_rules_digest,
                )
                decision = ExperimentalDecisionCompiler().compile(
                    result.decision, manifest_intents=options.permitted_intents,
                    provider_intents=payload.get("provider_intents", ()), decision_id=command.event_id,
                )
                usage = result.usage
                trace = result.model_dump(mode="json")
            elif ga.action_selection == "engineered_argmin":
                policies = payload.get("policies") or []
                if policies and all("experimental_decision" in item for item in policies):
                    from harness.cognitive.decision_parser import ExperimentalDecisionParser

                    selected = min(policies, key=lambda item: (item["rank"], str(item.get("policy_id", ""))))
                    parsed = ExperimentalDecisionParser(options.permitted_intents).parse(selected["experimental_decision"])
                    decision = ExperimentalDecisionCompiler().compile(
                        parsed, manifest_intents=options.permitted_intents,
                        provider_intents=payload.get("provider_intents", ()), decision_id=command.event_id,
                    )
                    trace = {"decision": parsed.model_dump(mode="json"), "messages": messages,
                             "input_context_hash": content_hash(messages), "selected_policy_id": selected.get("policy_id")}
                else:
                    decision = self.action_planner.decide(payload, mode="autonomous") or NoActionDecision(reason="no engineered candidate")
                    trace = {"decision": decision.model_dump(mode="json"), "messages": messages, "input_context_hash": content_hash(messages)}
            else:
                response = await self._call_research_worker(self.pipeline.character._complete, messages, stream=False)
                text = response.text
                usage = response.usage
                if ga.report_channel == "normal" and text.strip():
                    decision = ReplyDecision(text=text, segments=[text], channel=str(payload.get("channel") or "chat"))
                else:
                    decision = NoActionDecision(reason="research report channel has no delivery")
                trace = {"decision": decision.model_dump(mode="json"), "messages": messages,
                         "input_context_hash": content_hash(messages), "raw_output_hash": content_hash(text)}
            event = InteractionEvent(
                event_id=command.event_id, timestamp=payload.get("source_time") or command.issued_at,
                raw_text=str(payload.get("text") or ""), session_id=str(payload.get("session_id") or "grounded-agency"),
                channel=str(payload.get("channel") or "runtime"), speaker="user",
            )
            prepared = PreparedResponse(
                prepare_id="prepare-ga-" + content_hash([command.event_id, command.effective_request_hash()])[:24],
                event_id=command.event_id, correlation_id=command.correlation_id, session_id=event.session_id,
                decision=decision, read_versions=command.version_vector or self._version_vector(),
                prepared_at=_utc_now(), expires_at=command.deadline,
                model_version=manifest.model_version, prompt_version=manifest.prompt_version,
            )
            final = FinalResponse(
                text=text, path="fast", situation_id=situation.situation_id, context_id=context.context_id,
                event_id=event.event_id, **({"usage": usage} if usage is not None else {}),
            )
            self._prepared[prepared.prepare_id] = _PreparedEntry(event=event, final=final, prepared=prepared, research=True)
            trace.update(
                manifest_hash=manifest.content_hash(), artifact_identity=ga.artifact_identity.model_dump(mode="json"),
                observation_hashes=[content_hash(item) for item in observations], prepare_id=prepared.prepare_id,
                compiled_decision=decision.model_dump(mode="json"),
                research_context=[segment.model_dump(mode="json") for segment in additional_segments],
            )
            self.research_traces[command.event_id] = trace
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            output = DomainCommandResult[PreparedResponse](
                command_id=command.command_id, command_type=command.command_type, status="applied",
                request_hash=command.effective_request_hash(), committed_revision=self._revision,
                acknowledged_event_id=command.event_id, acknowledged_receive_seq=self._cursor,
                result=prepared, completed_at=_utc_now(),
            )
        except Exception as exc:
            output = self._failure(command, str(exc))
        self._remember(command, output)
        self._persist_state()
        return output

    async def _call_research_worker(self, function, *args, **kwargs):
        if self._maintenance_paused:
            raise RuntimeError("research calls are paused")
        if any(not task.done() for task in self.research_tasks):
            raise RuntimeError("previous research model worker is still running")
        worker = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self.research_tasks.add(worker)

        def completed(task):
            self.research_tasks.discard(task)
            if not task.cancelled():
                # A timed-out command no longer awaits this task. Retrieve an
                # eventual exception without treating it as a committed result.
                task.exception()

        worker.add_done_callback(completed)
        # Cancelling a command cannot stop its underlying model thread. Keep
        # the actual worker visible to Runtime until the thread has returned.
        return await asyncio.shield(worker)

    async def assess_autonomous(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[AutonomousAssessment]:
        with trace_scope(
            correlation_id=command.correlation_id, event_id=command.event_id,
            command_id=command.command_id, phase="assessment", span_id=command.command_id,
        ):
            return await self._run_command(lambda: self._assess_autonomous(command))

    async def _assess_autonomous(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[AutonomousAssessment]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                self._persist_state()
                return DomainCommandResult[AutonomousAssessment].model_validate(
                    replay.model_dump(mode="json")
                )
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[AutonomousAssessment].model_validate(
                    rejected.model_dump(mode="json")
                )

            payload = command.payload
            session_id = str(payload.get("session_id") or self.default_session_id)
            channel = str(payload.get("channel") or "chat")
            assessment_id = assessment_id_for(command.idempotency_key)
            try:
                homeostatic_interoception = _validated_homeostatic_interoception(command)
                emotion_guidance = await self.emotion_guidance(command)
                from harness.llm.deadline import call_before_deadline

                run = await asyncio.to_thread(
                    call_before_deadline,
                    self._emotion_model_deadline(command),
                    run_or_load_assessment,
                    pipeline=self.pipeline,
                    log_dir=self.autonomous_log_dir,
                    assessment_id=assessment_id,
                    evaluated_at=command.issued_at,
                    session_id=session_id,
                    channel=channel,
                    homeostasis=dict(payload.get("homeostasis") or {}),
                    policies=[dict(item) for item in (payload.get("policies") or [])],
                    homeostatic_interoception=homeostatic_interoception,
                    **({"emotion_guidance": emotion_guidance} if emotion_guidance is not None else {}),
                )
                memory_ref: str | None = None
                if run.decision == "speak":
                    memory_ref = f"reflection_{assessment_id.removeprefix('assessment_')}"
                    if self.pipeline.memory.stores.episodic.get(memory_ref) is None:
                        reflection = InteractionEvent(
                            event_id=memory_ref,
                            timestamp=run.evaluated_at,
                            channel="internal",
                            speaker="character",
                            modality="text",
                            raw_text=run.body,
                            session_id=session_id,
                            evidence=[f"autonomous-text:{run.record_ref}"],
                        )
                        await asyncio.to_thread(
                            self.pipeline.memory.record_event,
                            reflection,
                            generate_insight=False,
                            curate=False,
                            event_type="internal_reflection",
                            now=run.evaluated_at,
                        )
                        if self._persist_memory is not None:
                            await asyncio.to_thread(self._persist_memory)
                        self._revision += 1
                        self._component_revisions["memory"] = self._revision
                assessment = AutonomousAssessment(
                    assessment_id=run.assessment_id,
                    decision=run.decision,
                    record_ref=run.record_ref,
                    evaluator=run.evaluator,
                    internal_memory_ref=memory_ref,
                    session_id=session_id,
                    channel=channel,
                    evaluated_at=run.evaluated_at,
                    model_version=run.model_version,
                    prompt_version=run.prompt_version,
                )
            except Exception as exc:
                result = self._failure(command, str(exc))
                self._remember(command, result)
                self._persist_state()
                return DomainCommandResult[AutonomousAssessment].model_validate(
                    result.model_dump(mode="json")
                )

            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[AutonomousAssessment](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result=assessment,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            self._persist_state()
            return result

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        with trace_scope(
            correlation_id=command.correlation_id, event_id=command.event_id,
            command_id=command.command_id, phase="commit", span_id=command.command_id,
        ):
            return await self._run_command(lambda: self._commit_interaction(command))

    async def _commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                await asyncio.to_thread(self._persist_state)
                return DomainCommandResult[dict[str, Any]].model_validate(replay.model_dump(mode="json"))
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[dict[str, Any]].model_validate(rejected.model_dump(mode="json"))

            prepare_id = str(command.payload.get("prepare_id") or "")
            entry = self._prepared.get(prepare_id)
            if entry is None:
                result = self._failure(command, f"unknown prepare_id: {prepare_id}", status="rejected")
                self._remember(command, result)
                return result
            try:
                evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence = TypeAdapter(
                    CommitEvidence
                ).validate_python(command.payload.get("evidence"))
            except Exception as exc:
                error = "capability_invalid_commit_evidence" if entry.capability else f"invalid commit evidence: {exc}"
                result = self._failure(command, error, status="rejected")
                self._remember(command, result)
                if entry.capability:
                    await asyncio.to_thread(self._persist_state)
                return result
            if evidence.prepare_id != prepare_id:
                result = self._failure(command, "evidence prepare_id mismatch", status="rejected")
                self._remember(command, result)
                return result

            if entry.capability:
                decision = entry.prepared.decision
                valid_decision_end = (isinstance(evidence, DecisionEvidence)
                    and evidence.recorded_at >= entry.prepared.prepared_at)
                valid_action_end = (isinstance(evidence, ActionOutcomeEvidence) and isinstance(decision, GoalProposal)
                    and evidence.goal_id == decision.goal_id and evidence.observed_at >= entry.prepared.prepared_at
                    and (not evidence.success or bool(evidence.evidence_refs))
                    and all(ref.strip() for ref in evidence.evidence_refs))
                if not (valid_decision_end or valid_action_end):
                    result = self._failure(command, "capability_commit_requires_bound_action_evidence", status="rejected")
                    self._remember(command, result)
                    await asyncio.to_thread(self._persist_state)
                    return result
            await asyncio.to_thread(self._commit_entry, entry, evidence)
            if self._persist_memory is not None:
                await asyncio.to_thread(self._persist_memory)
            self._revision += 1
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            if not entry.capability:
                self._component_revisions["session"] = self._revision
            if not entry.capability or isinstance(evidence, ActionOutcomeEvidence) and evidence.evidence_refs:
                self._component_revisions["memory"] = self._revision
            payload = {
                "prepare_id": prepare_id,
                "evidence_kind": evidence.evidence_kind,
                "revision": self._revision,
            }
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result=payload,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            await asyncio.to_thread(self._persist_state)
            return result

    async def pause_maintenance(self, *, timeout: float) -> bool:
        self._maintenance_paused = True
        agent = getattr(self.pipeline.memory, "agentic", None)
        if agent is not None:
            agent.pause_background()
        deadline = asyncio.get_running_loop().time() + timeout
        judge_jobs = getattr(getattr(self, "_interruption_assessor", None), "jobs", set())
        workers = tuple(task for task in self.research_tasks | self.command_tasks | judge_jobs if not task.done())
        if workers:
            _, pending = await asyncio.wait(workers, timeout=max(0, timeout))
            if pending:
                return False
            timeout = max(0, deadline - asyncio.get_running_loop().time())
        drain = getattr(self.pipeline.memory, "drain_maintenance", None)
        if not callable(drain):
            return True
        # Thread/task scheduling can exceed a 10ms research-test budget even when
        # there is no remaining maintenance work. Let drain_maintenance's own
        # timeout govern; keep a small outer ceiling so a stuck drain cannot hang.
        wait_for = timeout + 1.0
        try:
            if inspect.iscoroutinefunction(drain):
                result = await asyncio.wait_for(drain(timeout=timeout), timeout=wait_for)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(drain, timeout=timeout),
                    timeout=wait_for,
                )
            return bool(result)
        except Exception:
            return False

    async def resume_maintenance(self) -> None:
        self._maintenance_paused = False
        resume = getattr(self.pipeline.memory, "resume_maintenance", None)
        if callable(resume):
            if inspect.iscoroutinefunction(resume):
                await resume()
            else:
                await asyncio.to_thread(resume)

    async def close(self) -> None:
        if self._close_task is None:
            self._closing = True
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self) -> None:
        judge_jobs = getattr(getattr(self, "_interruption_assessor", None), "jobs", set())
        workers = tuple(self.research_tasks | self.command_tasks | judge_jobs)
        if workers:
            await asyncio.gather(*workers, return_exceptions=True)
        await self._affect_thread.close()
        async with self._lock:
            await asyncio.to_thread(self._flush_and_close)

    def _flush_and_close(self) -> None:
        if self._persist_memory is not None:
            self._persist_memory()
        self._persist_state()
        if self._incremental_enabled and self._state_store is not None:
            self._state_store.checkpoint(force=True)
        if self._state_store is not None:
            self._state_store.close()
        close = getattr(self.pipeline.memory, "close", None)
        if callable(close):
            close()

    def _commit_entry(
        self,
        entry: _PreparedEntry,
        evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence,
    ) -> None:
        if entry.research:
            # Research evidence is maintained separately from legacy relationship enrichment.
            return
        if entry.capability:
            # The authenticated Runtime supplies independently verified action
            # evidence. No incoming chat, persona inference, need satisfaction or
            # relationship update follows from merely completing a capability.
            if isinstance(evidence, DecisionEvidence):
                return
            if not isinstance(evidence, ActionOutcomeEvidence):
                raise ValueError("capability_commit_requires_bound_action_evidence")
            if not evidence.evidence_refs:
                if evidence.success:
                    raise ValueError("capability_commit_requires_bound_action_evidence")
                return
            decision = entry.prepared.decision
            if not isinstance(decision, GoalProposal) or evidence.goal_id != decision.goal_id:
                raise ValueError("capability_commit_requires_bound_action_evidence")
            event_id = f"{entry.prepared.prepare_id}:action_outcome"
            intent_id = decision.embodied_action.intent.intent_id if decision.embodied_action else (
                decision.policy_id if decision.policy_id.startswith("production:rest:") else "unknown")
            if self._archive_action is not None:
                self._archive_action(event_id=event_id, session_id=entry.prepared.session_id,
                                     intent_id=intent_id, evidence=evidence)
            if self.pipeline.memory.stores.episodic.get(event_id) is None:
                # Store a fixed observation of the verifier outcome rather than
                # copying arbitrary provider/model summary prose into fact text.
                fact = f"身体能力 {intent_id} 的独立核验结果：{'通过' if evidence.success else '未通过'}。"
                record = EpisodicEvent(event_id=event_id, time=evidence.observed_at,
                    participants=["character"], summary=fact, objective_fact=fact,
                    character_action=intent_id, outcome="verified_success" if evidence.success else "verified_failure",
                    importance=0.4, emotional_valence=0.0,
                    evidence=list(evidence.evidence_refs), supporting_evidence=list(evidence.evidence_refs),
                    channel="body", event_type="capability_action_outcome", session_id=entry.prepared.session_id)
                self.pipeline.memory.record_event(record, generate_insight=False, curate=False, now=evidence.observed_at)
            return
        event = entry.event
        session = self.pipeline.session_store
        if entry.autonomous_assessment_id is None and not entry.production_autonomous:
            if self._archive_local is not None and event.channel == "chat":
                self._archive_local(event_id=event.event_id, session_id=event.session_id, role="user",
                                    text=event.raw_text, occurred_at=event.timestamp, display_name=event.speaker)
            if not session.contains_event(event.session_id, event.event_id):
                session.append(
                    event.session_id,
                    "user",
                    event.raw_text,
                    speaker=event.speaker,
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                )
            if self.pipeline.memory.stores.episodic.get(event.event_id) is None:
                # A structured action has already been planned, approved, executed,
                # and independently verified before its outcome reaches this commit.
                # Re-running persona insight and curator LLM calls here adds no
                # decision value, can exceed the Runtime command deadline, and used
                # to leave the supervisor trying to pause while Cognitive was still
                # blocked in synchronous enrichment.  Keep the objective audit event,
                # but reserve enrichment for conversational deliveries.
                enrich = not self.pipeline.settings.agentic_enabled and not entry.production and not isinstance(evidence, ActionOutcomeEvidence)
                self.pipeline.memory.record_event(
                    event,
                    generate_insight=enrich,
                    curate=enrich,
                    now=event.timestamp,
                )

        if isinstance(evidence, DeliveryEvidence) and evidence.success:
            from harness.agentic.service import enabled_service
            agent = enabled_service(self.pipeline)
            if agent is not None:
                agent.acknowledge_delivery(entry.final, evidence.external_ref or "delivery:" + evidence.delivery_id)
            if not entry.production and self.pipeline.settings.emotion_mode != "off":
                from harness.emotion import EmotionMessage

                self.emotion_service.record_delivery([
                    EmotionMessage(source_id=f"{entry.final.response_id}:s{index}",
                        scope="local:" + event.session_id, role="character", text=segment,
                        timestamp=evidence.delivered_at, speaker=self.pipeline._speaker_label("character"))
                    for index, segment in enumerate(evidence.delivered_segments, start=1)
                ])
            for index, segment in enumerate(evidence.delivered_segments, start=1):
                segment_event_id = f"{entry.final.response_id}:s{index}"
                if self._archive_local is not None and event.channel == "chat":
                    self._archive_local(event_id=segment_event_id, session_id=event.session_id, role="assistant",
                                        text=segment, occurred_at=evidence.delivered_at,
                                        display_name=self.pipeline._speaker_label("character"))
                if not session.contains_event(event.session_id, segment_event_id):
                    session.append(
                        event.session_id,
                        "character",
                        segment,
                        speaker=self.pipeline._speaker_label("character"),
                        event_id=segment_event_id,
                        timestamp=evidence.delivered_at,
                    )
            reply_event_id = f"{entry.final.response_id}:delivered"
            reply = InteractionEvent(
                event_id=reply_event_id,
                timestamp=evidence.delivered_at,
                channel=event.channel,
                speaker="character",
                modality=event.modality,
                raw_text="\n\n".join(evidence.delivered_segments),
                session_id=event.session_id,
                evidence=[evidence.external_ref or f"delivery:{evidence.delivery_id}"],
            )
            if self.pipeline.memory.stores.episodic.get(reply_event_id) is None:
                self.pipeline.memory.record_event(
                    reply,
                    generate_insight=False,
                    curate=False,
                    now=evidence.delivered_at,
                )

    def _load_speak_assessment(self, payload: dict[str, Any]):
        assessment_id = str(payload.get("assessment_id") or "")
        record_ref = str(payload.get("record_ref") or "")
        if not assessment_id or not record_ref:
            raise ValueError("assessment_id and record_ref are required")
        root = self.autonomous_log_dir.resolve()
        target = Path(record_ref).resolve()
        if not target.is_relative_to(root):
            raise ValueError("record_ref is outside the autonomous log directory")
        run = load_assessment_record(target)
        if run.assessment_id != assessment_id:
            raise ValueError("assessment_id does not match record")
        if run.decision != "speak":
            raise ValueError("only SPEAK assessments can prepare autonomous output")
        return run

    def _lookup_replay(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any] | None:
        key = (command.command_type, command.idempotency_key)
        previous = self._ledger.get(key)
        if previous is None:
            return None
        if self._request_hashes[key] != command.effective_request_hash():
            return self._failure(command, "idempotency key reused with different request", status="conflict")
        return previous.model_copy(
            update={"status": "idempotent_replay" if previous.status in {"applied", "idempotent_replay"}
                    else previous.status, "idempotency_hit": True}
        )

    def _preflight(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any] | None:
        if command.is_expired(now=_utc_now()):
            return self._failure(command, "command deadline expired", status="rejected")
        if command.expected_revision is not None and command.expected_revision != self._revision:
            return self._failure(
                command,
                f"expected revision {command.expected_revision}, current {self._revision}",
                status="conflict",
            )
        return None

    def _failure(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        error: str,
        *,
        status: Literal["failed", "rejected", "conflict"] = "failed",
    ) -> DomainCommandResult[Any]:
        return DomainCommandResult[Any](
            command_id=command.command_id,
            command_type=command.command_type,
            status=status,
            request_hash=command.effective_request_hash(),
            error=error,
            completed_at=_utc_now(),
        )

    def _remember(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        result: DomainCommandResult[Any],
    ) -> None:
        key = (command.command_type, command.idempotency_key)
        self._ledger[key] = result
        self._request_hashes[key] = command.effective_request_hash()

    def _version_vector(self) -> VersionVector:
        return VersionVector(
            cognitive_world=self._component_revisions["world"],
            cognitive_relationship=self._component_revisions["relationship"],
            cognitive_memory=self._component_revisions["memory"],
            cognitive_session=self._component_revisions["session"],
            cognitive_maintenance=self._component_revisions["maintenance"],
        )

    def _persist_emotion_state(self) -> None:
        # A background emotion commit must not checkpoint or publish an
        # unrelated foreground command that is still constructing its result.
        if self.state_path is None:
            return
        with self.emotion_service.persistence_guard(), self._persistence_lock:
            versions, rows = self.emotion_service.persistence_changes()
            if self._incremental_enabled:
                assert self._state_store is not None
                self._state_store.commit(rows)
                self._state_store.checkpoint()
            else:
                # Replace only emotion in the last durable JSON projection.
                # Other owner fields may be mid-command on the foreground lane.
                payload = json.loads(self.state_path.read_text(encoding="utf-8")) if self.state_path.exists() else json.loads(self._legacy_initial_state)
                payload["emotion"] = self.emotion_service.snapshot()
                write_checkpoint(self.state_path, payload)
            self.emotion_service.acknowledge_persistence(versions)

    def _persist_state(self) -> None:
        if self.state_path is None:
            self._publish_snapshot()
            return
        # Emotion callbacks already hold their RLock. Always acquire in that
        # order so a concurrent checkpoint cannot invert the owner lock order.
        with self.emotion_service.persistence_guard(), self._persistence_lock:
            if self._incremental_enabled:
                self._persist_incremental()
            else:
                self._persist_legacy_state()

    def _persist_incremental(self) -> None:
        assert self._state_store is not None
        with measure_stage("persistence.project"):
            changes, maps, versions, emotion_versions, session, session_versions, session_rows = self._incremental_changes()
        self._state_store.commit(changes)
        for name, values in maps.items():
            values.acknowledge(versions[name])
        self.emotion_service.acknowledge_persistence(emotion_versions)
        session.acknowledge_persistence(session_versions)
        for key, turns in session_rows.items():
            if turns is None:
                self._persisted_session_keys.discard(key)
            else:
                self._persisted_session_keys.add(key)
        self._persisted_session_store = session
        self._publish_snapshot()
        self._state_store.checkpoint()

    def _incremental_changes(self):
        maps = {"request_hashes": self._request_hashes, "ledger": self._ledger,
                "prepared": self._prepared, "research_traces": self.research_traces,
                "production_affect": self.production_affect}
        versions = {name: values.dirty_versions() for name, values in maps.items()}
        changes = {"owner_meta": {"state": {
            "revision": self._revision, "cursor": self._cursor, "event_id": self._event_id,
            "component_revisions": self._component_revisions,
            "research_manifest_hash": self._research_manifest_hash}}}
        for name, values in maps.items():
            rows = changes[name] = {}
            for key in versions[name]:
                encoded = f"{key[0]}\0{key[1]}" if name in {"request_hashes", "ledger"} else key
                value = values.get(key, DELETED)
                rows[encoded] = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        emotion_versions, emotion_rows = self.emotion_service.persistence_changes()
        changes.update(emotion_rows)
        session = self.pipeline.session_store
        session_versions, session_rows = session.persistence_changes()
        if session is not self._persisted_session_store:
            # GUI settings can replace the pipeline and its SessionStore. This
            # rare replacement is a full session projection, never a hot save.
            session_rows = {key: None for key in self._persisted_session_keys}
            session_rows.update(session.snapshot())
        changes["sessions"] = {key: DELETED if turns is None else [turn.model_dump(mode="json") for turn in turns]
                               for key, turns in session_rows.items()}
        return changes, maps, versions, emotion_versions, session, session_versions, session_rows

    def _persist_legacy_state(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        with measure_stage("persistence.project"):
            payload = self._legacy_state_payload()
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            with measure_stage("persistence.serialize"):
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            with measure_stage("persistence.fsync"):
                handle.flush()
                os.fsync(handle.fileno())
        with measure_stage("persistence.replace"):
            replace_path(temporary, self.state_path)
        self._acknowledge_loaded_state()
        self._legacy_initial_state = None
        self._publish_snapshot()

    def _legacy_state_payload(self):
        return {
            "revision": self._revision,
            "cursor": self._cursor,
            "event_id": self._event_id,
            "component_revisions": self._component_revisions,
            "request_hashes": {f"{key[0]}\0{key[1]}": value for key, value in self._request_hashes.items()},
            "ledger": {
                f"{key[0]}\0{key[1]}": value.model_dump(mode="json") for key, value in self._ledger.items()
            },
            "prepared": {key: value.model_dump(mode="json") for key, value in self._prepared.items()},
            "research_manifest_hash": self._research_manifest_hash,
            "research_traces": self.research_traces,
            "production_affect": self.production_affect,
            "emotion": self.emotion_service.snapshot(),
            "sessions": {
                key: [turn.model_dump(mode="json") for turn in turns]
                for key, turns in self.pipeline.session_store.snapshot().items()
            },
        }
    def _load_state(self) -> None:
        if self.state_path is None:
            return
        sidecar = self.state_path.with_suffix(self.state_path.suffix + ".sqlite3")
        if self._incremental_enabled or sidecar.exists():
            self._state_store = CognitiveStateStore(self.state_path)
            payload = self._state_store.activate(enabled=self._incremental_enabled)
        elif self.state_path.exists():
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        else:
            return
        self._revision = int(payload.get("revision", 0))
        self._cursor = int(payload.get("cursor", 0))
        self._event_id = payload.get("event_id")
        self._research_manifest_hash = payload.get("research_manifest_hash")
        self.research_traces.update(payload.get("research_traces") or {})
        self.production_affect.update(payload.get("production_affect") or {})
        self.emotion_service.load(payload.get("emotion") or {})
        self._component_revisions.update(payload.get("component_revisions") or {})
        for encoded, request_hash in (payload.get("request_hashes") or {}).items():
            self._request_hashes[tuple(encoded.split("\0", 1))] = request_hash
        for encoded, result in (payload.get("ledger") or {}).items():
            self._ledger[tuple(encoded.split("\0", 1))] = DomainCommandResult[Any].model_validate(result)
        self._prepared.update({
            key: _PreparedEntry.model_validate(value)
            for key, value in (payload.get("prepared") or {}).items()
        })
        sessions = {
            key: [SessionTurn.model_validate(turn) for turn in turns]
            for key, turns in (payload.get("sessions") or {}).items()
        }
        self.pipeline.session_store.restore(sessions)
        self._persisted_session_keys = set(sessions)
        self._acknowledge_loaded_state()
        # A smaller configured window normalizes restored sessions. Keep that
        # change dirty so later increasing the window cannot resurrect turns
        # that a successful save was expected to have discarded.
        session = self.pipeline.session_store
        for key, turns in sessions.items():
            if len(turns) > session.window:
                session.replace(key, session.recent(key))

    def _acknowledge_loaded_state(self) -> None:
        for values in (self._request_hashes, self._ledger, self._prepared, self.research_traces, self.production_affect):
            values.acknowledge(values.dirty_versions())
        self.emotion_service.acknowledge_persistence(dict(self.emotion_service._persistence_dirty))
        session = self.pipeline.session_store
        session.acknowledge_persistence(session.persistence_changes()[0])


class PassiveHomeostasisAdapter:
    """Passive monotonic reference service used until the V2 engine is supplied."""

    def __init__(
        self,
        *,
        policies: list[dict[str, Any]] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self._revision = 0
        self._cursor = 0
        self._event_id: str | None = None
        self._policies = list(policies or [])
        self.state_path = Path(state_path) if state_path is not None else None
        self._ledger: dict[tuple[str, str], tuple[str, DomainCommandResult[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._load_state()

    async def health(self) -> ServiceHealth:
        async with self._lock:
            return ServiceHealth(
                service="homeostasis",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
            )

    async def get_snapshot(self) -> DomainSnapshot:
        async with self._lock:
            return DomainSnapshot(
                service="homeostasis",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                acknowledged_event_id=self._event_id,
                component_revisions={"state": self._revision},
                payload={"policies": self._policies},
            )

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def assimilate_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_observation(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]:
        return list(self._policies)

    async def read_traces(
        self, *, since: str | None = None, until: str | None = None, limit: int = 5000
    ) -> list[dict[str, Any]]:
        del since, until, limit
        return []

    async def close(self) -> None:
        self._persist_state()

    async def _apply(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            key = (command.command_type, command.idempotency_key)
            request_hash = command.effective_request_hash()
            previous = self._ledger.get(key)
            if previous is not None:
                if previous[0] != request_hash:
                    return DomainCommandResult[dict[str, Any]](
                        command_id=command.command_id,
                        command_type=command.command_type,
                        status="conflict",
                        request_hash=request_hash,
                        error="idempotency key reused with different request",
                        completed_at=_utc_now(),
                    )
                replay = previous[1].model_copy(
                    update={"status": "idempotent_replay", "idempotency_hit": True}
                )
                self._persist_state()
                return replay
            if command.is_expired(now=_utc_now()):
                return DomainCommandResult[dict[str, Any]](
                    command_id=command.command_id,
                    command_type=command.command_type,
                    status="rejected",
                    request_hash=request_hash,
                    error="command deadline expired",
                    completed_at=_utc_now(),
                )
            if command.expected_revision is not None and command.expected_revision != self._revision:
                return DomainCommandResult[dict[str, Any]](
                    command_id=command.command_id,
                    command_type=command.command_type,
                    status="conflict",
                    request_hash=request_hash,
                    error=f"expected revision {command.expected_revision}, current {self._revision}",
                    completed_at=_utc_now(),
                )
            self._revision += 1
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=request_hash,
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result={"revision": self._revision, "policies": list(self._policies)},
                completed_at=_utc_now(),
            )
            self._ledger[key] = (request_hash, result)
            self._persist_state()
            return result

    def _persist_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": self._revision,
            "cursor": self._cursor,
            "event_id": self._event_id,
            "policies": self._policies,
            "ledger": {
                f"{key[0]}\0{key[1]}": {
                    "request_hash": request_hash,
                    "result": result.model_dump(mode="json"),
                }
                for key, (request_hash, result) in self._ledger.items()
            },
        }
        temporary = self.state_path.with_name(f".{self.state_path.name}.{os.getpid()}.{os.urandom(3).hex()}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        replace_path(temporary, self.state_path)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._revision = int(payload.get("revision", 0))
        self._cursor = int(payload.get("cursor", 0))
        self._event_id = payload.get("event_id")
        if "policies" in payload:
            self._policies = list(payload["policies"])
        for encoded, entry in (payload.get("ledger") or {}).items():
            key = tuple(encoded.split("\0", 1))
            self._ledger[key] = (
                entry["request_hash"],
                DomainCommandResult[dict[str, Any]].model_validate(entry["result"]),
            )
