"""Persistent event-driven Runtime Supervisor."""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import TypeAdapter

from harness.body.mistyai import MistyBodyClient, MistyBodyMonitor
from harness.contracts.body import BodyStateSnapshot as LegacyBodyStateSnapshot
from harness.contracts.decisions import (
    ActionOutcomeEvidence,
    AutonomousAssessment,
    CharacterDecision,
    DecisionEvidence,
    DeferDecision,
    DeliveryEvidence,
    GoalProposal,
    NoActionDecision,
    PreparedResponse,
    ReplyDecision,
)
from harness.contracts.envelope import (
    CONTRACT_SCHEMA_VERSION,
    DomainCommandEnvelope,
    DomainCommandResult,
    VersionVector,
)
from harness.core.error_notice import ERROR_FALLBACK_SEND_SECONDS
from harness.core.ids import new_id
from harness.core.inference import InteroceptiveState as CognitiveInteroceptiveState
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
    EmbodimentState,
    EmbodimentStatus,
)
from harness.embodiment.contracts import (
    BodyStateSnapshot as EmbodimentBodyStateSnapshot,
    HomeostaticObservation as EmbodimentHomeostaticObservation,
    InteroceptiveState as EmbodimentInteroceptiveState,
)
from harness.embodiment.guards import BodyStateGuard
from harness.embodiment.manager import EmbodimentManager
from harness.research.manifest import ExperimentManifest
from harness.runtime.action import ActionCoordinator, DeliveryBackend, InMemoryDeliveryBackend
from harness.runtime.adapters import (
    CognitiveServiceClient,
    DomainSnapshot,
    HomeostasisServiceClient,
    ServiceHealth,
)
from harness.runtime.checkpoint import CheckpointStore, RuntimeCheckpoint
from harness.runtime.journal import JournalUnavailableError, RuntimeJournal, RuntimeJournalRecord
from harness.runtime.locking import InstanceLockError, SingleInstanceLock
from harness.runtime.models import (
    CONTROL_EVENT_TYPES,
    PAUSED_RECORD_ONLY_TYPES,
    ActionBackendResult,
    ActionExecution,
    ActionExecutionState,
    AgencyState,
    DeliveryResult,
    EventDispatchResult,
    LifecycleTransition,
    RuntimeEvent,
    RuntimeEventPriority,
    RuntimeEventType,
    RuntimeHealth,
    RuntimeLifecycle,
    RuntimeStatus,
    WatchdogAction,
    WatchdogDecision,
    utc_now,
)
from harness.runtime.perception import PauseObservationBuffer, PerceptionGuard
from harness.runtime.queue import PriorityEventQueue, QueueCapacityError, QueueClosedError
from harness.runtime.scheduler import RealTimeScheduler
from harness.runtime.watchdog import RuntimeWatchdog


class RuntimeStateError(RuntimeError):
    pass


class DomainCommandError(RuntimeError):
    pass


class LateDomainResultError(DomainCommandError):
    pass


class UnknownDeliveryStateError(DomainCommandError):
    pass


RuntimeListener = Callable[[str, dict[str, Any]], Awaitable[None] | None]


_TRANSITIONS: dict[RuntimeLifecycle, frozenset[RuntimeLifecycle]] = {
    RuntimeLifecycle.BOOTING: frozenset({RuntimeLifecycle.RECOVERING, RuntimeLifecycle.FAULTED}),
    RuntimeLifecycle.RECOVERING: frozenset(
        {RuntimeLifecycle.RUNNING, RuntimeLifecycle.PAUSED, RuntimeLifecycle.FAULTED}
    ),
    RuntimeLifecycle.RUNNING: frozenset(
        {RuntimeLifecycle.PAUSING, RuntimeLifecycle.STOPPING, RuntimeLifecycle.FAULTED}
    ),
    RuntimeLifecycle.PAUSING: frozenset(
        {RuntimeLifecycle.PAUSED, RuntimeLifecycle.STOPPING, RuntimeLifecycle.FAULTED}
    ),
    RuntimeLifecycle.PAUSED: frozenset(
        {RuntimeLifecycle.RUNNING, RuntimeLifecycle.STOPPING, RuntimeLifecycle.FAULTED}
    ),
    RuntimeLifecycle.STOPPING: frozenset({RuntimeLifecycle.STOPPED, RuntimeLifecycle.FAULTED}),
    RuntimeLifecycle.FAULTED: frozenset({RuntimeLifecycle.RECOVERING, RuntimeLifecycle.STOPPING}),
    RuntimeLifecycle.STOPPED: frozenset(),
}

_HOMEOSTATIC_COGNITIVE_RESERVED_PAYLOAD_KEYS = frozenset(
    {"homeostatic_interoception", "homeostatic_profile_ref", "interoception",
     "opaque_interoception", "grounded_manifest_hash", "provider_intents", "research_context", "basic_terminal",
     "web_context", "runtime_web_context", "runtime_web_deadline_at", "offers",
     "emotion_context_ref", "emotion_revision"}
)


class RuntimeIngress:
    def __init__(self, supervisor: RuntimeSupervisor) -> None:
        self.supervisor = supervisor
        self._sequence_lock = asyncio.Lock()

    async def accept(
        self,
        *,
        event_type: RuntimeEventType | str,
        priority: RuntimeEventPriority | int,
        payload: dict[str, Any] | None = None,
        source: str = "external",
        source_time: datetime | None = None,
        event_id: str | None = None,
        correlation_id: str | None = None,
        idempotency_key: str | None = None,
    ) -> RuntimeEvent | None:
        event_kind = RuntimeEventType(event_type)
        if not self.supervisor.accepts_event(event_kind):
            raise RuntimeStateError(
                f"runtime in {self.supervisor.lifecycle} does not accept {event_kind.value}"
            )
        async with self._sequence_lock:
            self.supervisor._receive_seq += 1
            receive_seq = self.supervisor._receive_seq
        identifier = event_id or new_id("event")
        received_at = utc_now()
        event = RuntimeEvent(
            event_id=identifier,
            receive_seq=receive_seq,
            correlation_id=correlation_id or identifier,
            event_type=event_kind,
            priority=RuntimeEventPriority(priority),
            source=source,
            source_time=source_time or received_at,
            received_at=received_at,
            idempotency_key=idempotency_key or f"event:{identifier}",
            manifest_revision=self.supervisor.manifest.revision,
            payload=payload or {},
        )
        self.supervisor._append_journal(
            "event_accepted",
            event=event,
            data={"event": event.model_dump(mode="json")},
        )
        if event.event_type == RuntimeEventType.USER_MESSAGE:
            # Make user preemption effective as soon as the ingress record is
            # durable.  Publishing and queue insertion both await, so deferring
            # this bookkeeping until accept() returns would leave a running
            # autonomous reply eligible while an async observer is handling the
            # already-accepted user event.
            self.supervisor._pending_user_ingress_event_ids.add(event.event_id)
            self.supervisor._remember_user_event(event)
            if self.supervisor._basic_terminal is not None:
                self.supervisor._basic_terminal.interrupt_for_user(event)
            self.supervisor._cancel_pending_autonomous(
                reason="new user activity superseded autonomous output",
                event=event,
            )
        await self.supervisor._publish("event.accepted", event.model_dump(mode="json"))

        if (
            self.supervisor.lifecycle in {RuntimeLifecycle.PAUSING, RuntimeLifecycle.PAUSED}
            and event.event_type in PAUSED_RECORD_ONLY_TYPES
        ):
            self.supervisor.pause_observations.add(event)
            self.supervisor._append_journal(
                "event_deferred_paused",
                event=event,
                data={"sequence_range": event.sequence_range},
            )
            return event
        queued = await self.supervisor.queue.put(event)
        if event.event_type == RuntimeEventType.USER_MESSAGE:
            # Queue insertion has now produced an authoritative outcome: the
            # normal user path is visible to eligibility checks, or an
            # explicitly low-priority malformed ingress was dropped.
            self.supervisor._pending_user_ingress_event_ids.discard(event.event_id)
        shed_events = self.supervisor.queue.take_shedding_events()
        for shed in shed_events:
            self.supervisor._append_journal(
                "event_dropped",
                event=shed,
                data={
                    "reason": "queue_load_shedding",
                    "event_type": shed.event_type.value,
                    "source": shed.source,
                    "sequence_range": shed.sequence_range,
                    "count": shed.coalesced_count,
                },
            )
            await self.supervisor._publish(
                "queue.dropped",
                {
                    "event_id": shed.event_id,
                    "receive_seq": shed.receive_seq,
                    "sequence_range": shed.sequence_range,
                },
            )
        if not queued:
            return None
        return event


def _qq_error_fallback_payload(payload: Any) -> bool:
    decision = payload.get("decision") if isinstance(payload, dict) else None
    return isinstance(decision, dict) and decision.get("outcome") == "error_fallback"


class RuntimeSupervisor:
    def __init__(
        self,
        *,
        manifest: ExperimentManifest,
        cognitive: CognitiveServiceClient,
        homeostasis: HomeostasisServiceClient,
        state_dir: Path | str,
        character_id: str = "character",
        runtime_instance_id: str | None = None,
        journal: RuntimeJournal | None = None,
        checkpoint_store: CheckpointStore | None = None,
        queue: PriorityEventQueue | None = None,
        scheduler: RealTimeScheduler | None = None,
        action_coordinator: ActionCoordinator | None = None,
        embodiment_manager: EmbodimentManager | None = None,
        body_state_guard: BodyStateGuard | None = None,
        delivery_backend: DeliveryBackend | None = None,
        watchdog: RuntimeWatchdog | None = None,
        command_timeout_seconds: float | None = None,
        action_timeout_seconds: float = 120.0,
        maintenance_drain_seconds: float = 5.0,
        body_client: MistyBodyClient | None = None,
        grounded_sensor_transform: Callable[[Any], Any] | None = None,
        research_clock: Callable[[], datetime] | None = None,
        body_belief_service: Any | None = None,
        agency_service: Any | None = None,
        social_world_service: Any | None = None,
        research_world_signature: Callable[[], str] | None = None,
    ) -> None:
        self.manifest = manifest
        self.cognitive = cognitive
        self.homeostasis = homeostasis
        self.character_id = character_id
        self.runtime_instance_id = runtime_instance_id or new_id("runtime")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._grounded_binding = manifest.bind_condition() if manifest.grounded_agency else None
        self._opaque_store = None
        self._grounded_body = None
        self._grounded_sensor_transform = grounded_sensor_transform
        self._research_clock = research_clock or utc_now
        self._research_world_signature = research_world_signature
        self._research_memory = None
        self._agency_service = None
        self._social_world_service = None
        self._social_appraisal_tasks: set[asyncio.Task] = set()
        self._agency_decision_bindings: dict[str, dict[str, str]] = {}
        if manifest.grounded_agency is not None:
            from harness.research.grounded_agency.manifest import GroundedAgencyRuntimeOptions

            self._grounded_options = manifest.grounded_agency.runtime or GroundedAgencyRuntimeOptions()
            bind = getattr(cognitive, "bind_research_manifest", None)
            if callable(bind):
                bind(manifest)
            if manifest.grounded_agency.sensor_transform is not None:
                from harness.research.interoception.store import OpaqueInteroceptionStore

                self._opaque_store = OpaqueInteroceptionStore(
                    self.state_dir / "opaque-interoception.sqlite3", manifest.grounded_agency,
                    history_limit=self._grounded_options.history_limit,
                    max_tokens=self._grounded_options.opaque_token_budget,
                    bin_count=self._grounded_options.sensor_bins,
                )
                if body_belief_service is None:
                    from harness.body_belief import BodyBeliefAppraiser, BodyBeliefService, BodyBeliefStore

                    llm = getattr(getattr(cognitive, "pipeline", None), "llm", None)
                    treatment = manifest.grounded_agency.memory_treatment
                    if treatment == "body_causal_beliefs" and llm is None:
                        raise ValueError("body_causal_beliefs requires an explicit appraisal service for remote Cognitive")
                    body_belief_service = BodyBeliefService(
                        BodyBeliefStore(self.state_dir / "body-beliefs.sqlite3", profile=manifest.grounded_agency.body_belief_profile),
                        appraiser=BodyBeliefAppraiser(llm) if treatment == "body_causal_beliefs" else None,
                        memory_treatment=treatment,
                        objective_history_visible=self._grounded_options.objective_history_visible,
                    )
                from harness.runtime.research_memory import ResearchMemoryBridge

                self._research_memory = ResearchMemoryBridge(body_belief_service, context_key=manifest.environment)
            agency = manifest.grounded_agency.agency
            if agency is not None and agency.enabled:
                from harness.agency import AgencyService

                llm = getattr(getattr(cognitive, "pipeline", None), "llm", None)
                if agency_service is None and llm is None:
                    raise ValueError("long-horizon agency requires an explicit model service for remote Cognitive")
                self._agency_service = agency_service or AgencyService(
                    self.state_dir / "agency.sqlite3", llm, config=agency, clock=self._research_clock,
                    permitted_intents=self._grounded_options.permitted_intents,
                )
            social = manifest.grounded_agency.open_social
            if social is not None and social.enabled:
                from harness.social_world import SocialWorldService

                self._social_world_service = social_world_service or SocialWorldService(
                    self.state_dir / "entity-memory.sqlite3", social,
                    session_id=manifest.experiment_id,
                )
        self.journal = journal or RuntimeJournal(
            self.state_dir / "journal",
            runtime_instance_id=self.runtime_instance_id,
            character_id=character_id,
            manifest_revision=manifest.revision,
        )
        # Seek metadata only: command payloads remain in the journal's bounded
        # tail/on disk. New idempotency keys must not scan all historical data.
        self._command_intent_seqs: dict[str, int] = {}
        self._prepare_intent_seqs: dict[str, tuple[int, int]] = {}
        self._command_result_seqs: dict[str | None, int] = {}
        self._qq_turn_event_seqs: dict[str, int] = {}
        self._qq_turn_completed_seqs: dict[str, int] = {}
        self._command_index_cursor = 0
        self._sync_command_index()
        self.checkpoint_store = checkpoint_store or CheckpointStore(self.state_dir / "checkpoint.json")
        self.queue = queue or PriorityEventQueue()
        self.scheduler = scheduler or RealTimeScheduler(manifest.heartbeat_seconds)
        self.embodiment = embodiment_manager or EmbodimentManager(manifest.embodiment)
        self.action_coordinator = action_coordinator or ActionCoordinator(
            consistency_barrier=self._high_impact_barrier,
            capability_check=(
                self.embodiment.supports
                if embodiment_manager is not None or manifest.embodiment.mode != "off"
                else None
            ),
        )
        if self.action_coordinator.consistency_barrier is None:
            self.action_coordinator.consistency_barrier = self._high_impact_barrier
        if embodiment_manager is not None or manifest.embodiment.mode != "off":
            self.action_coordinator.capability_check = self.embodiment.supports
        self.delivery_backend = delivery_backend or InMemoryDeliveryBackend()
        self.watchdog = watchdog or RuntimeWatchdog()
        self.command_timeout_seconds = manifest.command_timeout_seconds if command_timeout_seconds is None else command_timeout_seconds
        self.action_timeout_seconds = action_timeout_seconds
        self.maintenance_drain_seconds = maintenance_drain_seconds
        condition = manifest.embodiment
        if condition.mode == "off" and body_client is not None:
            raise RuntimeStateError("off embodiment mode must not create a body client")
        if (
            condition.mode in {"observe", "grounded"}
            and condition.body_provider == "mistyai"
            and body_client is None
            and embodiment_manager is None
        ):
            raise RuntimeStateError(f"{condition.mode} embodiment mode requires a MistyAI client")

        self.lifecycle = RuntimeLifecycle.BOOTING
        self.lifecycle_since = utc_now()
        self.lifecycle_reason = "constructed"
        self._receive_seq = 0
        self._process_seq = 0
        self._goal_revision = 0
        self._cognitive_snapshot = DomainSnapshot(service="cognitive", revision=0)
        self._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0)
        self._last_heartbeat_at: datetime | None = None
        self._last_user_receive_seq = 0
        self._last_user_session_id: str | None = None
        self._last_user_channel = "chat"
        self._last_runtime_time: datetime | None = None
        self._journal_writable = not self.journal.degraded
        self._degraded_reasons: list[str] = []
        self._agency_state = AgencyState.AVAILABLE
        self._body_episode_id: str | None = None
        self._body_revision: int | None = None
        self._body_captured_at: datetime | None = None
        self._terminal_body_revision: int | None = None
        self._terminal_episode_id: str | None = None
        self._interoception: (
            EmbodimentInteroceptiveState | CognitiveInteroceptiveState | None
        ) = None
        self._listeners: list[RuntimeListener] = []
        self._lifecycle_lock = asyncio.Lock()
        self._foreground_lock = asyncio.Lock()
        self._approval_lock = asyncio.Lock()
        self._action_tasks: dict[str, asyncio.Task[None]] = {}
        self._perception_guard: PerceptionGuard | None = None
        self._body_state_guard = body_state_guard
        self._last_reconsideration_at: datetime | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._booted = False
        self._closed = False
        self._reconciliation_backlog = 0
        self._needs_refill = False
        self._action_context: dict[str, dict[str, Any]] = {}
        self._homeostasis_watchdog_signature: tuple[int, tuple[str, ...]] | None = None
        self._homeostatic_salience: str | None = None
        self._homeostatic_episode_signature: str | None = None
        self._homeostatic_marked_attempts: dict[str, datetime] = {}
        self._cancelled_autonomous_event_ids: set[str] = set()
        self._pending_user_ingress_event_ids: set[str] = set()
        # A watchdog may pause after Homeostasis committed an event but before
        # Cognitive saw it.  These revisions let an explicit resume continue
        # from Cognitive without applying the same homeostatic transition again.
        self._homeostasis_continuations: dict[str, int] = {}
        self.pause_observations = PauseObservationBuffer()
        self.ingress = RuntimeIngress(self)
        self.instance_lock = SingleInstanceLock(
            self.state_dir / "runtime.lock", runtime_instance_id=self.runtime_instance_id
        )
        self.body_monitor = (
            MistyBodyMonitor(
                client=body_client,
                body_instance_id=str(condition.body_instance_id),
                poll_interval_seconds=condition.poll_interval_seconds,
                max_state_age_seconds=condition.max_state_age_seconds,
                future_clock_tolerance_seconds=condition.future_clock_tolerance_seconds,
                on_snapshot=self.accept_body_snapshot,
                on_degraded=self.mark_body_degraded,
                on_recovered=self.mark_body_recovered,
            )
            if body_client is not None
            else None
        )
        self._basic_terminal = None
        if manifest.basic_terminal.enabled:
            from harness.runtime.basic_terminal import BasicTerminalRuntime

            self._basic_terminal = BasicTerminalRuntime(self)
        from harness.runtime.capabilities import CapabilityController

        self.capabilities = CapabilityController(self, self.state_dir)
        from harness.runtime.goals import GoalController
        self.goals = GoalController(self, self.state_dir)
        prior_execution_guard = self.action_coordinator.execution_guard

        async def capability_execution_guard(action):
            if prior_execution_guard is not None:
                allowed, reason = await prior_execution_guard(action)
                if not allowed:
                    return allowed, reason
            reason = await self.capabilities.validate_action(action)
            return reason is None, reason

        self.action_coordinator.execution_guard = capability_execution_guard

    @property
    def version_vector(self) -> VersionVector:
        cognitive = self._cognitive_snapshot.component_revisions
        return VersionVector(
            runtime_goal=self._goal_revision,
            runtime_action=self.action_coordinator.revision,
            cognitive_world=cognitive.get("world", 0),
            cognitive_relationship=cognitive.get("relationship", 0),
            cognitive_memory=cognitive.get("memory", self._cognitive_snapshot.revision),
            cognitive_session=cognitive.get("session", self._cognitive_snapshot.revision),
            cognitive_maintenance=cognitive.get("maintenance", 0),
            homeostasis_state=self._homeostasis_snapshot.revision,
        )

    def add_listener(self, listener: RuntimeListener) -> None:
        self._listeners.append(listener)

    def attach_perception_guard(self, guard: PerceptionGuard) -> None:
        self._perception_guard = guard

    def attach_body_state_guard(self, guard: BodyStateGuard) -> None:
        self._body_state_guard = guard

    def _start_embodiment_guards(self) -> None:
        legacy_mode = self.manifest.embodiment.mode == "off"
        if self._perception_guard is not None and (
            legacy_mode or self.embodiment.supports(OBSERVATION_SNAPSHOT)
        ):
            self._perception_guard.start()
        if (
            self._body_state_guard is not None
            and self.manifest.embodiment.mode != "off"
            and self.embodiment.supports(PHYSIOLOGY_BODY_STATE)
        ):
            self._body_state_guard.start()

    async def _refresh_embodiment(self) -> None:
        if self.manifest.embodiment.mode == "off":
            return
        before = self.embodiment.status()
        after = await self.embodiment.refresh()
        if after != before:
            self._append_journal(
                "embodiment_status_changed",
                data={"before": before.model_dump(mode="json"), "after": after.model_dump(mode="json")},
            )
            await self._publish("embodiment.status", after.model_dump(mode="json"))
        if (
            self.lifecycle == RuntimeLifecycle.RUNNING
            and self._required_embodiment_blocks_runtime(after)
        ):
            await self.pause(reason=f"required embodiment became {after.state.value}")
        if self.lifecycle == RuntimeLifecycle.RUNNING:
            self._start_embodiment_guards()
        if not self.embodiment.supports(OBSERVATION_SNAPSHOT) and self._perception_guard is not None:
            await self._perception_guard.stop()
        if not self.embodiment.supports(PHYSIOLOGY_BODY_STATE) and self._body_state_guard is not None:
            await self._body_state_guard.stop()

    def _required_embodiment_blocks_runtime(
        self,
        status: EmbodimentStatus | None = None,
    ) -> bool:
        effective = status or self.embodiment.status()
        return (
            self.manifest.embodiment.required_for_runtime
            and effective.state in {EmbodimentState.UNAVAILABLE, EmbodimentState.TERMINAL}
        )

    def accepts_event(self, event_type: RuntimeEventType) -> bool:
        if self.lifecycle in {RuntimeLifecycle.STOPPING, RuntimeLifecycle.STOPPED}:
            return False
        if self.lifecycle in {RuntimeLifecycle.BOOTING, RuntimeLifecycle.RECOVERING}:
            return event_type in CONTROL_EVENT_TYPES
        if self.lifecycle == RuntimeLifecycle.FAULTED:
            return event_type in {
                RuntimeEventType.DIAGNOSTIC,
                RuntimeEventType.STOP,
                RuntimeEventType.RECOVER,
            }
        return True

    async def submit_event(self, **kwargs: Any) -> RuntimeEvent | None:
        if kwargs.get("event_type") == RuntimeEventType.IDLE and not self.manifest.autonomous_chat_enabled:
            return None
        try:
            event = await self.ingress.accept(**kwargs)
            return event
        except QueueCapacityError:
            self._needs_refill = True
            self._append_journal("queue_overloaded", data={"statistics": self.queue.statistics().model_dump()})
            if self.lifecycle == RuntimeLifecycle.RUNNING:
                await self.pause(reason="event queue capacity exhausted")
            raise

    async def submit_user_message(
        self,
        text: str,
        *,
        session_id: str = "sess_default",
        speaker: str = "user",
        channel: str = "chat",
        event_id: str | None = None,
        correlation_id: str | None = None,
    ) -> RuntimeEvent | None:
        if not text.strip():
            raise ValueError("user message cannot be blank")
        return await self.submit_event(
            event_type=RuntimeEventType.USER_MESSAGE,
            priority=RuntimeEventPriority.USER,
            source="user",
            event_id=event_id,
            correlation_id=correlation_id,
            payload={
                "text": text,
                "session_id": session_id,
                "speaker": speaker,
                "channel": channel,
            },
        )

    async def accept_body_snapshot(self, snapshot: LegacyBodyStateSnapshot) -> None:
        """Accept only already schema/time/evidence-verified monitor output."""

        if self._body_revision is not None and snapshot.revision <= self._body_revision:
            raise RuntimeStateError(
                f"body revision {snapshot.revision} did not advance beyond restored "
                f"revision {self._body_revision}"
            )
        event_type = (
            RuntimeEventType.AGENCY_TERMINAL
            if snapshot.energy.terminal
            else RuntimeEventType.BODY_STATE
        )
        await self.submit_event(
            event_type=event_type,
            priority=(
                RuntimeEventPriority.CONTROL
                if snapshot.energy.terminal
                else RuntimeEventPriority.PERCEPTION
            ),
            source="mistyai",
            source_time=snapshot.captured_at,
            correlation_id=f"body:{snapshot.body_instance_id}:{snapshot.episode_id}",
            idempotency_key=f"body:{snapshot.body_instance_id}:r{snapshot.revision}",
            payload=snapshot.model_dump(mode="json"),
        )

    async def mark_body_degraded(self, reason: str) -> None:
        if self._agency_state != AgencyState.TERMINAL:
            self._agency_state = AgencyState.DEGRADED
        if "body" not in self._degraded_reasons:
            self._degraded_reasons.append("body")
        self._append_journal("body_monitor_degraded", data={"reason": reason})
        if self.accepts_event(RuntimeEventType.DIAGNOSTIC):
            await self.submit_event(
                event_type=RuntimeEventType.DIAGNOSTIC,
                priority=RuntimeEventPriority.CONTROL,
                source="mistyai_monitor",
                payload={"component": "body", "reason": reason},
            )

    async def mark_body_recovered(self) -> None:
        self._append_journal("body_monitor_transport_recovered")

    async def reset_agency(self) -> LegacyBodyStateSnapshot:
        """Research-control reset after MistyAI created a newer, non-terminal episode."""

        if self._agency_state != AgencyState.TERMINAL:
            raise RuntimeStateError("agency reset requires a sticky TERMINAL state")
        if self.body_monitor is None or self.body_monitor.last_snapshot is None:
            raise RuntimeStateError("no verified MistyAI snapshot is available")
        snapshot = self.body_monitor.last_snapshot
        if snapshot.energy.terminal:
            raise RuntimeStateError("new body episode remains terminal")
        if snapshot.episode_id == self._terminal_episode_id:
            raise RuntimeStateError("agency reset requires a new body episode")
        if self._terminal_body_revision is None or snapshot.revision <= self._terminal_body_revision:
            raise RuntimeStateError("agency reset requires a higher body revision")
        if (
            self._body_revision != snapshot.revision
            or self._body_episode_id != snapshot.episode_id
        ):
            raise RuntimeStateError("new body episode has not been applied by Runtime")
        self._agency_state = AgencyState.AVAILABLE
        self._degraded_reasons = [reason for reason in self._degraded_reasons if reason != "body"]
        self._append_journal(
            "agency_reset",
            data={
                "body_instance_id": snapshot.body_instance_id,
                "episode_id": snapshot.episode_id,
                "body_revision": snapshot.revision,
                "evidence_refs": snapshot.evidence_refs,
            },
        )
        return snapshot

    async def boot_and_recover(self) -> None:
        if self._booted:
            return
        try:
            self.instance_lock.acquire()
        except InstanceLockError as exc:
            self.lifecycle = RuntimeLifecycle.FAULTED
            self.lifecycle_since = utc_now()
            self.lifecycle_reason = str(exc)
            await self._publish(
                "lifecycle",
                LifecycleTransition(
                    previous=RuntimeLifecycle.BOOTING,
                    current=RuntimeLifecycle.FAULTED,
                    reason=str(exc),
                ).model_dump(mode="json"),
            )
            raise
        try:
            previous_lifecycle = self._restore_runtime_indexes()
            if self._basic_terminal is not None:
                self._basic_terminal.recover()
            self.goals.recover_contributions()
            await self._restore_research_memory()
            await self._transition(RuntimeLifecycle.RECOVERING, reason="boot recovery started")
            if self.journal.truncated_tail_bytes:
                self._append_journal(
                    "journal_tail_truncated",
                    data={"bytes": self.journal.truncated_tail_bytes},
                )
            homeostasis_watchdog = await self._check_services()
            if homeostasis_watchdog:
                await self._defer_reconciliation_for_homeostasis_watchdog(
                    homeostasis_watchdog,
                    phase="boot_initial_check",
                )
            embodiment_status = await self.embodiment.activate()
            self._append_journal(
                "embodiment_activated",
                data={"embodiment": embodiment_status.model_dump(mode="json")},
            )
            unresolved_action = False
            if not homeostasis_watchdog:
                await self._reconcile_incomplete_commands(services=frozenset({"homeostasis"}))
                homeostasis_watchdog = await self._check_services()
                if homeostasis_watchdog:
                    await self._defer_reconciliation_for_homeostasis_watchdog(
                        homeostasis_watchdog,
                        phase="boot_after_homeostasis_reconciliation",
                    )
            if not homeostasis_watchdog:
                await self._reconcile_incomplete_commands(services=frozenset({"cognitive"}))
                homeostasis_watchdog = await self._check_services()
                if homeostasis_watchdog:
                    await self._defer_reconciliation_for_homeostasis_watchdog(
                        homeostasis_watchdog,
                        phase="boot_after_cognitive_reconciliation",
                    )
                else:
                    unresolved_action = await self._restore_and_reconcile_actions()
                    homeostasis_watchdog = await self._check_services()
                    if homeostasis_watchdog:
                        await self._defer_reconciliation_for_homeostasis_watchdog(
                            homeostasis_watchdog,
                            phase="boot_after_action_reconciliation",
                        )
            await self._restore_pending_events()
            if self.journal.last_record_seq:
                now = utc_now()
                previous_time = self._last_runtime_time or next(self.journal.iter_records(reverse=True)).recorded_at
                self._append_journal(
                    "time_discontinuity",
                    data={
                        "last_runtime_time": previous_time.isoformat(),
                        "current_time": now.isoformat(),
                        "duration_seconds": max(0.0, (now - previous_time).total_seconds()),
                        "reason": "runtime_start",
                    },
                )
            requires_pause = (
                unresolved_action
                or self._required_embodiment_blocks_runtime(embodiment_status)
                or bool(homeostasis_watchdog)
                or previous_lifecycle in {
                RuntimeLifecycle.PAUSED,
                RuntimeLifecycle.PAUSING,
                RuntimeLifecycle.FAULTED,
                RuntimeLifecycle.STOPPING,
                }
            )
            target = RuntimeLifecycle.PAUSED if requires_pause else RuntimeLifecycle.RUNNING
            transition_reason = (
                "recovery complete; homeostasis watchdog requires pause: "
                + ", ".join(homeostasis_watchdog)
                if homeostasis_watchdog
                else "recovery complete"
            )
            await self._transition(target, reason=transition_reason)
            if self._research_memory is not None:
                if target == RuntimeLifecycle.RUNNING:
                    await self._research_memory.service.resume()
                else:
                    await self._research_memory.service.pause()
            if self._agency_service is not None:
                if target == RuntimeLifecycle.RUNNING:
                    await self._agency_service.resume()
                else:
                    await self._agency_service.pause(timeout=self.maintenance_drain_seconds)
            if homeostasis_watchdog:
                self._homeostasis_watchdog_signature = (
                    self._homeostasis_snapshot.revision,
                    homeostasis_watchdog,
                )
            if target == RuntimeLifecycle.RUNNING:
                await self._schedule_current_marked_trigger()
                self.scheduler.start()
                self._start_embodiment_guards()
            else:
                self.scheduler.suspend()
                self.pause_observations.begin(utc_now())
            if self.body_monitor is not None:
                await self.body_monitor.start()
            self._booted = True
        except Exception as exc:
            if self.lifecycle not in {RuntimeLifecycle.FAULTED, RuntimeLifecycle.PAUSED}:
                await self._transition(RuntimeLifecycle.FAULTED, reason=f"recovery failed: {exc}")
            if not self._research_workers_pending():
                self.instance_lock.release()
            raise

    async def run(self) -> None:
        await self.boot_and_recover()
        self._run_task = asyncio.current_task()
        try:
            while self.lifecycle != RuntimeLifecycle.STOPPED:
                predicate = self._dispatch_predicate()
                timeout = self.scheduler.seconds_until_deadline() if self.lifecycle == RuntimeLifecycle.RUNNING else None
                try:
                    event = await self.queue.get(timeout=timeout, predicate=predicate)
                except QueueClosedError:
                    break
                if event is None:
                    scheduled = self.scheduler.make_due_event()
                    if scheduled is None:
                        continue
                    event = await self.submit_event(**scheduled)
                    if event is None:
                        continue
                    # Scheduled events have already been inserted into the queue.
                    event = await self.queue.get(timeout=0, predicate=predicate)
                    if event is None:
                        continue
                await self.dispatch(event)
        except asyncio.CancelledError:
            if self.lifecycle not in {RuntimeLifecycle.STOPPED, RuntimeLifecycle.STOPPING}:
                await asyncio.shield(self.stop(reason="runtime task cancelled"))
            raise
        finally:
            self._run_task = None

    async def dispatch(self, event: RuntimeEvent) -> EventDispatchResult:
        self._process_seq += 1
        process_seq = self._process_seq
        self._append_journal(
            "processing_started",
            event=event,
            process_seq=process_seq,
            data={
                "event_type": event.event_type.value,
                "source": event.source,
                "first_receive_seq": event.sequence_range[0],
                "last_receive_seq": event.sequence_range[1],
                "coalesced_count": event.coalesced_count,
            },
        )
        await self._publish(
            "event.processing",
            {"event_id": event.event_id, "receive_seq": event.receive_seq, "process_seq": process_seq},
        )
        try:
            if event.event_type in CONTROL_EVENT_TYPES:
                result = await self._dispatch_control(event, process_seq)
            elif self.lifecycle != RuntimeLifecycle.RUNNING:
                result = EventDispatchResult(
                    event_id=event.event_id,
                    receive_seq=event.receive_seq,
                    process_seq=process_seq,
                    status="deferred",
                    detail={"lifecycle": self.lifecycle.value},
                )
            else:
                if event.event_type == RuntimeEventType.USER_MESSAGE and self.action_coordinator.current:
                    await self.action_coordinator.request_preemption(reason="user message preemption")
                result = await self._dispatch_domain_event(event, process_seq)
        except Exception as exc:
            error_detail = str(exc).strip() or type(exc).__name__
            self._append_journal(
                "processing_failed",
                event=event,
                process_seq=process_seq,
                data={"error": error_detail, "error_type": type(exc).__name__},
            )
            retryable = isinstance(exc, (DomainCommandError, TimeoutError, ConnectionError))
            if retryable:
                if self.lifecycle == RuntimeLifecycle.RUNNING:
                    try:
                        await self.pause(reason=f"critical domain failure: {error_detail}")
                    except Exception as pause_exc:
                        # Pausing is a recovery action, so its own failure must not
                        # kill the run loop before the original event is durably
                        # requeued.  pause() has already moved the lifecycle to a
                        # terminal recovery state when this path is reached.
                        self._append_journal(
                            "pause_failed",
                            event=event,
                            process_seq=process_seq,
                            data={
                                "error": str(pause_exc).strip() or type(pause_exc).__name__,
                                "error_type": type(pause_exc).__name__,
                            },
                        )
                try:
                    await self.queue.put(event)
                    self._append_journal(
                        "event_requeued",
                        event=event,
                        data={"reason": type(exc).__name__},
                    )
                except (QueueCapacityError, QueueClosedError):
                    self._reconciliation_backlog += 1
                    self._needs_refill = True
            result = EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="deferred" if retryable else "failed",
                detail={"error": error_detail, "error_type": type(exc).__name__},
            )
        self._append_journal(
            "processing_completed",
            event=event,
            process_seq=process_seq,
            data=result.model_dump(mode="json"),
        )
        self._last_runtime_time = result.completed_at
        await self._publish("event.completed", result.model_dump(mode="json"))
        if (
            self._needs_refill
            and self.lifecycle not in {RuntimeLifecycle.STOPPING, RuntimeLifecycle.STOPPED}
        ):
            await self._restore_pending_events()
        return result

    async def pause(self, *, reason: str) -> None:
        if self.lifecycle in {RuntimeLifecycle.PAUSED, RuntimeLifecycle.PAUSING}:
            return
        if self.lifecycle != RuntimeLifecycle.RUNNING:
            raise RuntimeStateError(f"cannot pause from {self.lifecycle}")
        await self._transition(RuntimeLifecycle.PAUSING, reason=reason)
        self.scheduler.suspend()
        self.pause_observations.begin(utc_now())
        try:
            try:
                await asyncio.wait_for(
                    self.action_coordinator.request_preemption(reason=reason),
                    timeout=self.maintenance_drain_seconds,
                )
            except TimeoutError as exc:
                raise RuntimeStateError("action could not reach safe point") from exc
            if not await self._drain_action_tasks(timeout=self.maintenance_drain_seconds):
                raise RuntimeStateError("foreground action did not stop")
            # The service-side timeout cannot help while Cognitive's event loop
            # is too busy to accept the request.  Bound the transport wait from
            # Runtime as well so PAUSING always converges to PAUSED or FAULTED.
            drained = await asyncio.wait_for(
                self.cognitive.pause_maintenance(timeout=self.maintenance_drain_seconds),
                timeout=self.maintenance_drain_seconds + 1.0,
            )
            self._append_journal("maintenance_drain", data={"completed": drained})
            if not drained:
                raise RuntimeStateError("cognitive maintenance did not drain within deadline")
            if self._research_memory is not None:
                await self._research_memory.service.pause()
                if not await self._research_memory.service.drain(self.maintenance_drain_seconds):
                    raise RuntimeStateError("body belief maintenance did not drain within deadline")
            await self._pause_research_agency()
            await self._transition(RuntimeLifecycle.PAUSED, reason=reason)
        except Exception as exc:
            error_detail = str(exc).strip() or type(exc).__name__
            if self.lifecycle == RuntimeLifecycle.PAUSING:
                await self._transition(
                    RuntimeLifecycle.FAULTED,
                    reason=f"pause failed: {error_detail}",
                )
            if isinstance(exc, RuntimeStateError):
                raise
            raise RuntimeStateError(f"pause failed: {error_detail}") from exc

    async def resume(self, *, reason: str = "explicit researcher resume") -> None:
        if self.lifecycle != RuntimeLifecycle.PAUSED:
            raise RuntimeStateError(f"cannot resume from {self.lifecycle}")
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            raise RuntimeStateError(
                "cannot resume while homeostasis watchdog remains active: "
                + ", ".join(homeostasis_watchdog)
            )
        await self._reconcile_incomplete_commands(services=frozenset({"homeostasis"}))
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="resume_after_homeostasis_reconciliation",
            )
            raise RuntimeStateError(
                "cannot resume after homeostasis reconciliation activated watchdog: "
                + ", ".join(homeostasis_watchdog)
            )
        await self._reconcile_incomplete_commands(services=frozenset({"cognitive"}))
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="resume_after_cognitive_reconciliation",
            )
            raise RuntimeStateError(
                "cannot resume while homeostasis watchdog is active: "
                + ", ".join(homeostasis_watchdog)
            )
        unresolved_action = await self._restore_and_reconcile_actions()
        if unresolved_action:
            raise RuntimeStateError("cannot resume while action reconciliation is unresolved")
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="resume_after_action_reconciliation",
            )
            raise RuntimeStateError(
                "cannot resume after action reconciliation activated homeostasis watchdog: "
                + ", ".join(homeostasis_watchdog)
            )
        await self._refresh_embodiment()
        if self.manifest.grounded_agency is not None and self.pause_observations.first_receive_seq is not None:
            # Pause keeps observation ingress durable but does not dispatch it.
            # Reconcile physical samples before assessing a goal continuation;
            # a summary or the current wall clock cannot replace those samples.
            first = self.pause_observations.first_receive_seq
            last = self.pause_observations.last_receive_seq
            pending_body = [RuntimeEvent.model_validate(record.data["event"]) for record in self.journal.iter_records()
                            if record.record_type == "event_accepted" and record.data.get("event", {}).get("event_type") == "body_state"
                            and first <= record.data["event"]["receive_seq"] <= last]
            for body_event in pending_body:
                await self._handle_grounded_body(body_event, self._process_seq)
        embodiment_status = self.embodiment.status()
        if self._required_embodiment_blocks_runtime(embodiment_status):
            raise RuntimeStateError(
                "cannot resume while required embodiment is "
                f"{embodiment_status.state.value}"
            )
        await self.cognitive.resume_maintenance()
        if self._research_memory is not None:
            await self._research_memory.service.resume()
        if self._agency_service is not None:
            await self._agency_service.resume()
        resumed_at = utc_now()
        summary = self.pause_observations.build(resumed_at=resumed_at)
        await self._transition(RuntimeLifecycle.RUNNING, reason=reason)
        await self._schedule_current_marked_trigger()
        # An explicit researcher resume permits one new heartbeat to reassess
        # the same persisted condition at a newer Homeostasis revision.
        self._homeostasis_watchdog_signature = None
        self.scheduler.reset_from_now()
        self._start_embodiment_guards()
        if summary is not None:
            await self.submit_event(
                event_type=RuntimeEventType.RESUME_OBSERVATION,
                priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT,
                source="perception_guard",
                source_time=resumed_at,
                payload=summary.model_dump(mode="json"),
            )
        self.pause_observations.clear()

    async def recover(self, *, reason: str = "explicit recovery") -> None:
        if self.lifecycle != RuntimeLifecycle.FAULTED:
            raise RuntimeStateError(f"cannot recover from {self.lifecycle}")
        await self._transition(RuntimeLifecycle.RECOVERING, reason=reason)
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="recover_initial_check",
            )
            self.pause_observations.begin(utc_now())
            return
        await self._reconcile_incomplete_commands(services=frozenset({"homeostasis"}))
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="recover_after_homeostasis_reconciliation",
            )
            self.pause_observations.begin(utc_now())
            return
        await self._reconcile_incomplete_commands(services=frozenset({"cognitive"}))
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="recover_after_cognitive_reconciliation",
            )
            self.pause_observations.begin(utc_now())
            return
        await self._restore_and_reconcile_actions()
        homeostasis_watchdog = await self._check_services()
        if homeostasis_watchdog:
            await self._defer_reconciliation_for_homeostasis_watchdog(
                homeostasis_watchdog,
                phase="recover_after_action_reconciliation",
            )
            self.pause_observations.begin(utc_now())
            return
        await self._transition(RuntimeLifecycle.PAUSED, reason="recovery reconciled; resume required")
        self.pause_observations.begin(utc_now())

    async def stop(self, *, reason: str = "graceful stop") -> None:
        if self.lifecycle == RuntimeLifecycle.STOPPED:
            return
        if self.lifecycle == RuntimeLifecycle.BOOTING:
            await self._transition(RuntimeLifecycle.FAULTED, reason="stopped before boot")
        if self.lifecycle not in {
            RuntimeLifecycle.RUNNING,
            RuntimeLifecycle.PAUSED,
            RuntimeLifecycle.PAUSING,
            RuntimeLifecycle.FAULTED,
        }:
            raise RuntimeStateError(f"cannot stop from {self.lifecycle}")
        await self._transition(RuntimeLifecycle.STOPPING, reason=reason)
        self.scheduler.suspend()
        if self._perception_guard is not None:
            await self._perception_guard.stop()
        if self._body_state_guard is not None:
            await self._body_state_guard.stop()
        if self.body_monitor is not None:
            await self.body_monitor.stop()
        try:
            await asyncio.wait_for(
                self.action_coordinator.request_preemption(reason=reason),
                timeout=self.maintenance_drain_seconds,
            )
            if not await self._drain_action_tasks(timeout=self.maintenance_drain_seconds):
                raise RuntimeStateError("foreground action did not stop during shutdown")
            drained = await asyncio.wait_for(
                self.cognitive.pause_maintenance(timeout=self.maintenance_drain_seconds),
                timeout=self.maintenance_drain_seconds + 1.0,
            )
            if not drained:
                raise RuntimeStateError("cognitive maintenance did not drain during shutdown")
            await self._pause_research_agency()
            if self._research_memory is not None:
                await self._research_memory.service.pause()
                if not await self._research_memory.service.drain(self.maintenance_drain_seconds):
                    raise RuntimeStateError("body belief maintenance did not drain during shutdown")
                await self._research_memory.service.close()
            if self._agency_service is not None:
                await self._agency_service.close()
            if self._social_world_service is not None:
                self._social_world_service.close()
            self.write_checkpoint()
            await self.cognitive.close()
            await self.homeostasis.close()
            if self._opaque_store is not None:
                self._opaque_store.close()
            await self._transition(RuntimeLifecycle.STOPPED, reason=reason, durable=True)
            await self.queue.close()
            self.capabilities.close()
            self.goals.close()
            self._closed = True
        except Exception as exc:
            try:
                await self._transition(RuntimeLifecycle.FAULTED, reason=f"graceful stop failed: {exc}")
            except Exception:
                self.lifecycle = RuntimeLifecycle.FAULTED
                self.lifecycle_reason = f"graceful stop failed: {exc}"
            raise
        finally:
            if not self._research_workers_pending():
                self.instance_lock.release()

    def _research_workers_pending(self) -> bool:
        """Keep exclusive state ownership while an uncancellable model worker runs."""
        tasks = list(self._social_appraisal_tasks)
        tasks.extend(getattr(self.cognitive, "research_tasks", ()))
        tasks.extend(getattr(self.cognitive, "command_tasks", ()))
        if self._research_memory is not None:
            tasks.extend(self._research_memory.service._tasks.values())
        if self._agency_service is not None and self._agency_service._inflight is not None:
            tasks.append(self._agency_service._inflight)
        return any(not task.done() for task in tasks)

    def write_checkpoint(self) -> RuntimeCheckpoint:
        checkpoint = RuntimeCheckpoint(
            character_id=self.character_id,
            runtime_instance_id=self.runtime_instance_id,
            manifest_revision=self.manifest.revision,
            journal_record_seq=self.journal.last_record_seq,
            journal_hash=self.journal.last_hash,
            receive_seq=self._receive_seq,
            process_seq=self._process_seq,
            lifecycle=self.lifecycle,
            lifecycle_reason=self.lifecycle_reason,
            version_vector=self.version_vector,
            pending_event_ids=[event.event_id for event in self.queue.snapshot()],
            prepared_response_ids=[
                str(context["prepared"].prepare_id) for context in self._action_context.values()
            ],
            pending_action_ids=[action.action_id for action in self.action_coordinator.pending()],
            cognitive_acknowledged_receive_seq=self._cognitive_snapshot.acknowledged_receive_seq,
            homeostasis_acknowledged_receive_seq=self._homeostasis_snapshot.acknowledged_receive_seq,
            agency_state=self._agency_state,
            body_episode_id=self._body_episode_id,
            body_revision=self._body_revision,
            body_captured_at=self._body_captured_at,
            interoceptive_state=(
                self._interoception.model_dump(mode="json")
                if self._interoception is not None
                else None
            ),
        )
        self.checkpoint_store.write(checkpoint)
        self._append_journal(
            "checkpoint_written",
            data={
                "journal_record_seq": checkpoint.journal_record_seq,
                "checkpoint_path": str(self.checkpoint_store.path),
            },
        )
        return checkpoint

    async def approve_action(self, action_id: str, *, approval: str) -> ActionExecution:
        async with self._approval_lock:
            existing = self.action_coordinator.get(action_id)
            if existing is not None and existing.state != ActionExecutionState.AWAITING_APPROVAL:
                return existing
            action = await self.action_coordinator.approve(action_id, approval=approval)
            self._append_journal("action_approved", data={"action": action.model_dump(mode="json")})
            if action.state == ActionExecutionState.READY and self.lifecycle == RuntimeLifecycle.RUNNING:
                context = self._action_context.get(action_id)
                action = await self._start_action(
                    action,
                    event=context.get("event") if context else None,
                )
            return action

    async def reject_action(self, action_id: str, *, reason: str) -> ActionExecution:
        action = await self.action_coordinator.reject(action_id, reason=reason)
        self._append_journal("action_rejected", data={"action": action.model_dump(mode="json")})
        # Apply the terminal outcome through the event queue.  Directly
        # mutating Homeostasis here can race the single Runtime dispatch loop
        # while it is preparing or delivering a user response.
        await self._submit_action_result(action)
        return action

    async def resolve_action(self, action_id: str, *, result: ActionBackendResult) -> ActionExecution:
        action = await self.action_coordinator.resolve_unknown(action_id, result=result)
        self._append_journal(
            "action_reconciled",
            idempotency_key=action.idempotency_key,
            data={"action": action.model_dump(mode="json"), "researcher_resolved": True},
        )
        await self._submit_action_result(action)
        return action

    def resolve_delivery(self, prepare_id: str, result: DeliveryResult) -> None:
        """Record researcher-verified evidence for an uncertain delivery window."""

        existing, uncertain = self._find_delivery_result(prepare_id)
        if existing is not None:
            if existing != result:
                raise RuntimeStateError("delivery already has a different proven result")
            return
        if not uncertain:
            raise RuntimeStateError("delivery has no unresolved intent")
        self._append_journal(
            "reply_delivery",
            idempotency_key=f"delivery:{prepare_id}",
            data={"prepare_id": prepare_id, "result": result.model_dump(mode="json"), "reconciled": True},
        )

    def status(self) -> RuntimeStatus:
        embodiment_status = self.embodiment.status()
        degraded_reasons = list(self._degraded_reasons)
        if (
            self.manifest.embodiment.mode != "off"
            and embodiment_status.state
            in {EmbodimentState.DEGRADED, EmbodimentState.UNAVAILABLE, EmbodimentState.TERMINAL}
            and "embodiment" not in degraded_reasons
        ):
            degraded_reasons.append("embodiment")
        health = RuntimeHealth(
            lifecycle=self.lifecycle,
            journal_writable=self._journal_writable,
            cognitive_healthy="cognitive" not in self._degraded_reasons,
            homeostasis_healthy="homeostasis" not in self._degraded_reasons,
            body_healthy=(
                self.manifest.embodiment.mode == "off"
                or (
                    embodiment_status.state == EmbodimentState.AVAILABLE
                    and self._agency_state == AgencyState.AVAILABLE
                )
            ),
            degraded_reasons=degraded_reasons,
            last_heartbeat_at=self._last_heartbeat_at,
            last_journal_append_at=self.journal.last_append_at,
            embodiment=embodiment_status,
            agency_state=self._agency_state,
            body_episode_id=self._body_episode_id,
            body_revision=self._body_revision,
            body_state_age_seconds=(
                max(0.0, (utc_now() - self._body_captured_at).total_seconds())
                if self._body_captured_at is not None
                else None
            ),
        )
        return RuntimeStatus(
            character_id=self.character_id,
            runtime_instance_id=self.runtime_instance_id,
            manifest_revision=self.manifest.revision,
            lifecycle=self.lifecycle,
            lifecycle_since=self.lifecycle_since,
            receive_seq=self._receive_seq,
            process_seq=self._process_seq,
            version_vector=self.version_vector,
            queue=self.queue.statistics(),
            health=health,
            current_action=self.action_coordinator.current,
            pending_actions=len(self.action_coordinator.pending()),
            pending_events=self.queue.qsize(),
            embodiment=embodiment_status,
            agency_state=self._agency_state,
        )

    async def check_watchdog(self) -> None:
        current = self.action_coordinator.current
        decision = self.watchdog.evaluate(
            queue=self.queue.statistics(),
            current_action=current.model_dump(mode="json") if current else None,
            reconciliation_backlog=self._reconciliation_backlog,
        )
        if decision is None:
            return
        self._append_journal("watchdog_decision", data=decision.model_dump(mode="json"))
        await self._publish("watchdog", decision.model_dump(mode="json"))
        if decision.action in {WatchdogAction.PAUSE, WatchdogAction.FAULT}:
            await self.submit_event(
                event_type=RuntimeEventType.WATCHDOG,
                priority=RuntimeEventPriority.CONTROL,
                source="watchdog",
                payload=decision.model_dump(mode="json"),
            )

    async def try_recover_journal(self) -> bool:
        recovered = self.journal.try_recover_writes()
        if recovered:
            self._journal_writable = True
            self._degraded_reasons = [reason for reason in self._degraded_reasons if reason != "journal"]
            self._append_journal("journal_recovered")
            await self._publish("journal.recovered", {"buffered_records": 0})
        return recovered

    async def _dispatch_control(self, event: RuntimeEvent, process_seq: int) -> EventDispatchResult:
        if (
            event.event_type == RuntimeEventType.AGENCY_TERMINAL
            and event.payload.get("schema_version") == "body-state-v1"
        ):
            if self.manifest.grounded_agency is not None:
                return await self._handle_grounded_body(event, process_seq)
            terminal_snapshot = LegacyBodyStateSnapshot.model_validate(event.payload)
            self._body_episode_id = terminal_snapshot.episode_id
            self._body_revision = terminal_snapshot.revision
            self._body_captured_at = terminal_snapshot.captured_at
            self._interoception = self._build_interoception(terminal_snapshot)
            self._agency_state = AgencyState.TERMINAL
            self._terminal_body_revision = terminal_snapshot.revision
            self._terminal_episode_id = terminal_snapshot.episode_id
            if "body" not in self._degraded_reasons:
                self._degraded_reasons.append("body")
            await self.action_coordinator.request_preemption(reason="body agency terminal")
            result = await self._apply_body_state(event, process_seq)
            self._append_journal(
                "agency_terminal",
                event=event,
                data={
                    "episode_id": self._body_episode_id,
                    "body_revision": self._body_revision,
                },
            )
            return result
        if event.event_type in {RuntimeEventType.PAUSE, RuntimeEventType.EMERGENCY_STOP, RuntimeEventType.WATCHDOG, RuntimeEventType.PERMISSION_REVOKED}:
            if self.lifecycle == RuntimeLifecycle.RUNNING:
                await self.pause(reason=str(event.payload.get("reason") or event.event_type.value))
        elif event.event_type == RuntimeEventType.RESUME:
            await self.resume(reason=str(event.payload.get("reason") or "explicit resume"))
        elif event.event_type == RuntimeEventType.RECOVER:
            await self.recover(reason=str(event.payload.get("reason") or "explicit recovery"))
        elif event.event_type == RuntimeEventType.STOP:
            await self.stop(reason=str(event.payload.get("reason") or "control stop"))
        return EventDispatchResult(
            event_id=event.event_id,
            receive_seq=event.receive_seq,
            process_seq=process_seq,
            status="completed",
            decision_kind="control",
            detail={"lifecycle": self.lifecycle.value},
        )

    async def _dispatch_domain_event(
        self, event: RuntimeEvent, process_seq: int
    ) -> EventDispatchResult:
        if event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY:
            return await self.capabilities.dispatch(event, process_seq)
        if event.event_type == RuntimeEventType.BOARD_REFLECTION:
            board = getattr(self, "board", None)
            if board is None:
                raise DomainCommandError("board body unavailable")
            return await board.dispatch(event, process_seq)
        if event.event_type == RuntimeEventType.DIARY_REFLECTION:
            diary = getattr(self, "diary", None)
            if diary is None:
                raise DomainCommandError("diary unavailable")
            return await diary.dispatch(event, process_seq)
        if event.event_type == RuntimeEventType.QQ_TURN:
            return await self._dispatch_qq_turn(event, process_seq)
        if event.event_type == RuntimeEventType.QQ_FACT:
            from harness.homeostasis.models import QQHomeostaticFact

            fact = QQHomeostaticFact.model_validate(event.payload.get("qq_fact"))
            if not -5 <= (utc_now() - fact.occurred_at).total_seconds() <= 300:
                return EventDispatchResult(event_id=event.event_id, receive_seq=event.receive_seq,
                    process_seq=process_seq, status="failed", decision_kind="qq_fact",
                    detail={"error_type": "qq_fact_expired"})
        if event.event_type == RuntimeEventType.BODY_STATE:
            if self.manifest.grounded_agency is not None:
                return await self._handle_grounded_body(event, process_seq)
            body_payload = event.payload.get("body_state") or event.payload
            if body_payload.get("schema_version") == "body-state-v1":
                return await self._apply_body_state(event, process_seq)
            return await self._handle_body_state(event, process_seq)
        if event.event_type == RuntimeEventType.ACTION_RESULT:
            action_id = str(event.payload.get("action_id") or "")
            action = self.action_coordinator.get(action_id)
            if action is None:
                raise DomainCommandError(f"unknown action result: {action_id}")
            if action.state == ActionExecutionState.UNKNOWN:
                raise DomainCommandError(
                    f"action {action_id} has an unknown external result; researcher resolution required"
                )
            if not action.terminal:
                raise DomainCommandError(f"action {action_id} result arrived before terminal state")
            await self._complete_goal_action(action, outcome_event=event)
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="action_result",
                detail={"action": action.model_dump(mode="json")},
            )
        if self.manifest.grounded_agency is not None and event.event_type != RuntimeEventType.QQ_FACT:
            return await self._dispatch_grounded_event(event, process_seq)
        if event.event_type == RuntimeEventType.TERMINAL_CONTINUATION:
            task = self._basic_terminal.tasks.get(str(event.payload.get("terminal_task_id") or "")) if self._basic_terminal else None
            if task is None or event.payload.get("step") != len(task["history"]):
                return EventDispatchResult(
                    event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
                    status="ignored", decision_kind="no_action", detail={"reason": "terminal task no longer active"},
                )
        if event.event_type == RuntimeEventType.IDLE:
            ineligible = self._autonomous_ineligible(event)
            if ineligible is not None:
                self._cancelled_autonomous_event_ids.discard(event.event_id)
                return EventDispatchResult(
                    event_id=event.event_id,
                    receive_seq=event.receive_seq,
                    process_seq=process_seq,
                    status="ignored",
                    decision_kind="no_action",
                    detail={"reason": ineligible},
                )
        homeostasis_revision_before = self._homeostasis_snapshot.revision
        continuation_revision = self._homeostasis_continuations.get(event.event_id)
        continued_after_homeostasis = continuation_revision is not None
        if continued_after_homeostasis:
            assert continuation_revision is not None
            if event.event_type != RuntimeEventType.USER_MESSAGE:
                raise DomainCommandError("only user messages may resume after a homeostasis pause")
            if continuation_revision > self._homeostasis_snapshot.revision:
                raise DomainCommandError(
                    "deferred homeostasis revision is newer than the current snapshot"
                )
            self._append_journal(
                "homeostasis_continuation_resumed",
                event=event,
                process_seq=process_seq,
                data={"homeostasis_revision": continuation_revision},
            )
            homeostasis_watchdog = False
        else:
            homeostasis_command = (
                "ApplyHeartbeat"
                if event.event_type == RuntimeEventType.HEARTBEAT
                else "ApplyEvent"
            )
            homeostasis_envelope = self._make_command(
                event,
                command_type=homeostasis_command,
                idempotency_key=f"homeostasis:{homeostasis_command}:{event.event_id}",
                expected_revision=self._homeostasis_snapshot.revision,
                payload={"event": event.model_dump(mode="json"),
                    **({"regulation_feedback": True} if self.goals.enabled else {})},
            )
            method = (
                self.homeostasis.apply_heartbeat
                if event.event_type == RuntimeEventType.HEARTBEAT
                else self.homeostasis.apply_event
            )
            homeostasis_result = await self._invoke_domain(
                service="homeostasis", envelope=homeostasis_envelope, method=method, event=event
            )
            homeostasis_watchdog = await self._update_homeostasis(
                homeostasis_result,
                event=event,
            )

        homeostatic_interoception = self._response_homeostatic_interoception()
        causal_context = self._homeostatic_causal_fields(homeostatic_interoception)
        if (
            continued_after_homeostasis
            and event.event_type == RuntimeEventType.USER_MESSAGE
            and homeostatic_interoception is not None
            and self.manifest.homeostatic_expression.mode == "full"
        ):
            # The event already changed Homeostasis before the watchdog pause,
            # so resume must not apply it twice.  It still represents the user
            # expression opportunity for the recovered, current summary.
            await self._maybe_schedule_marked_trigger(
                event,
                homeostatic_interoception,
                source_revision=self._homeostasis_snapshot.revision,
            )
        if homeostasis_watchdog:
            if event.event_type == RuntimeEventType.USER_MESSAGE:
                await self._defer_user_after_homeostasis(event, causal_context)
                status = "deferred"
            else:
                status = "completed"
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status=status,
                decision_kind="watchdog_pause",
                detail={
                    **causal_context,
                    "lifecycle": self.lifecycle.value,
                    "reason": "homeostasis reported an unsafe or unstable state",
                },
            )
        if event.event_type == RuntimeEventType.IDLE:
            stale_marked_reason = self._homeostatic_marked_trigger_ineligible(
                event,
                source_revision=homeostasis_revision_before,
                summary=homeostatic_interoception,
            )
            if stale_marked_reason is not None:
                self._cancelled_autonomous_event_ids.discard(event.event_id)
                self._append_journal(
                    "homeostatic_marked_trigger_stale",
                    event=event,
                    process_seq=process_seq,
                    data={"reason": stale_marked_reason, **causal_context},
                )
                return EventDispatchResult(
                    event_id=event.event_id,
                    receive_seq=event.receive_seq,
                    process_seq=process_seq,
                    status="ignored",
                    decision_kind="no_action",
                    detail={"reason": stale_marked_reason, **causal_context},
                )
            marked_cooldown_reason = self._ordinary_idle_marked_cooldown_reason(
                event,
                summary=homeostatic_interoception,
            )
            if marked_cooldown_reason is not None:
                self._cancelled_autonomous_event_ids.discard(event.event_id)
                self._append_journal(
                    "homeostatic_marked_idle_suppressed",
                    event=event,
                    process_seq=process_seq,
                    data={"reason": marked_cooldown_reason, **causal_context},
                )
                return EventDispatchResult(
                    event_id=event.event_id,
                    receive_seq=event.receive_seq,
                    process_seq=process_seq,
                    status="ignored",
                    decision_kind="no_action",
                    detail={"reason": marked_cooldown_reason, **causal_context},
                )
        if event.event_type == RuntimeEventType.HEARTBEAT:
            scheduled_after_user_receive_seq = self._causal_user_receive_seq(event)
            self._last_heartbeat_at = utc_now()
            await self._refresh_embodiment()
            await self.check_watchdog()
            if self._basic_terminal is not None and self.lifecycle == RuntimeLifecycle.RUNNING:
                await self._basic_terminal.expire_pending_approvals()
            board = getattr(self, "board", None)
            if board is not None:
                await board.heartbeat()
            diary = getattr(self, "diary", None)
            if diary is not None:
                await diary.heartbeat()
            daily_report = getattr(self, "daily_report", None)
            if daily_report is not None:
                try:
                    await daily_report.heartbeat()
                except Exception as exc:
                    self._append_journal(
                        "daily_report_failed",
                        data={"error_type": type(exc).__name__},
                    )
            await self.capabilities.heartbeat()
            policies: list[dict[str, Any]] = []
            if (
                self.manifest.autonomous_chat_enabled
                and self.lifecycle == RuntimeLifecycle.RUNNING
                and self.queue.empty()
                and not self._pending_user_ingress_event_ids
                and self.action_coordinator.current is None
            ):
                policies = await self.homeostasis.propose_policies(
                    version_vector=self.version_vector
                )
                if policies:
                    payload: dict[str, Any] = {
                        "mode": "autonomous",
                        "policies": policies,
                        "scheduled_after_user_receive_seq": scheduled_after_user_receive_seq,
                        "channel": self._last_user_channel,
                    }
                    if self._last_user_session_id is not None:
                        payload["session_id"] = self._last_user_session_id
                    await self.submit_event(
                        event_type=RuntimeEventType.IDLE,
                        priority=RuntimeEventPriority.IDLE,
                        source="scheduler",
                        payload=payload,
                    )
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="heartbeat",
                detail={
                    "homeostasis_revision": self._homeostasis_snapshot.revision,
                    "autonomous_candidates": len(policies),
                    **causal_context,
                },
            )
        if event.event_type not in {
            RuntimeEventType.USER_MESSAGE,
            RuntimeEventType.IDLE,
            RuntimeEventType.GOAL_RECONSIDERATION,
            RuntimeEventType.TERMINAL_CONTINUATION,
        }:
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                detail={
                    "homeostasis_revision": self._homeostasis_snapshot.revision,
                    **causal_context,
                },
            )

        assessment: AutonomousAssessment | None = None
        if event.event_type == RuntimeEventType.IDLE:
            assess_envelope = self._make_command(
                event,
                command_type="AssessAutonomous",
                idempotency_key=f"cognitive:assess:{event.event_id}",
                expected_revision=self._cognitive_snapshot.revision,
                payload={
                    "homeostasis": self._homeostasis_payload_for_cognitive(),
                    "policies": list(event.payload.get("policies") or []),
                    "session_id": event.payload.get("session_id"),
                    "channel": event.payload.get("channel") or "chat",
                    "trigger_reason": event.payload.get("trigger_reason"),
                    **(
                        {
                            "homeostatic_interoception": homeostatic_interoception,
                            "homeostatic_profile_ref": self.manifest.homeostatic_profile,
                        }
                        if homeostatic_interoception is not None
                        else {}
                    ),
                },
            )
            self._journal_homeostatic_route(
                event=event,
                command_id=assess_envelope.command_id,
                target="AssessAutonomous",
                summary=homeostatic_interoception,
            )
            assess_result = await self._invoke_domain(
                service="cognitive",
                envelope=assess_envelope,
                method=self.cognitive.assess_autonomous,
                event=event,
            )
            assessment = AutonomousAssessment.model_validate(assess_result.result)
            self._cognitive_snapshot = await self.cognitive.get_snapshot()
            ineligible = self._autonomous_ineligible(event)
            if assessment.decision == "silent" or ineligible is not None:
                self._cancelled_autonomous_event_ids.discard(event.event_id)
                return EventDispatchResult(
                    event_id=event.event_id,
                    receive_seq=event.receive_seq,
                    process_seq=process_seq,
                    status="completed" if ineligible is None else "ignored",
                    decision_kind="no_action",
                    detail={
                        "assessment_id": assessment.assessment_id,
                        "assessment_decision": assessment.decision,
                        "assessment_record_ref": assessment.record_ref,
                        "assessment_evaluator": assessment.evaluator,
                        "internal_memory_ref": assessment.internal_memory_ref,
                        "reason": ineligible or "autonomous assessment chose silence",
                        **causal_context,
                    },
                )

        cognitive_event_payload = {
            key: value
            for key, value in event.payload.items()
            if key not in _HOMEOSTATIC_COGNITIVE_RESERVED_PAYLOAD_KEYS
        }
        # This authority-bearing context is always built from Runtime's frozen
        # configuration and a fresh provider observation, never client input.
        if cognitive_event_payload.get("mode") == "basic_terminal_task":
            cognitive_event_payload.pop("mode")
        terminal_context = await self._basic_terminal.context(event) if self._basic_terminal is not None else None
        if terminal_context is not None:
            cognitive_event_payload.update({"mode": "basic_terminal_task", "basic_terminal": terminal_context})
        web_deadline = None
        if event.event_type == RuntimeEventType.USER_MESSAGE and terminal_context is None:
            from harness.runtime.web_dialogue import prepare_web_context
            web_context, web_deadline = await prepare_web_context(self, event)
            if web_context is not None:
                cognitive_event_payload.update(runtime_web_context=web_context,
                    runtime_web_deadline_at=web_deadline.isoformat())
        prepare_envelope = self._make_command(
            event,
            command_type="PrepareResponse",
            idempotency_key=self._prepare_idempotency_key(event),
            expected_revision=self._cognitive_snapshot.revision,
            payload={
                **cognitive_event_payload,
                "source_time": event.source_time,
                "call_id": event.correlation_id,
                **(
                    {
                        "homeostatic_interoception": homeostatic_interoception,
                        "homeostatic_profile_ref": self.manifest.homeostatic_profile,
                    }
                    if homeostatic_interoception is not None
                    else {}
                ),
                **(
                    {
                        "mode": "autonomous",
                        "session_id": assessment.session_id,
                        "channel": assessment.channel,
                        "assessment_id": assessment.assessment_id,
                        "record_ref": assessment.record_ref,
                        "internal_memory_ref": assessment.internal_memory_ref,
                        "homeostasis": self._homeostasis_payload_for_cognitive(),
                        "policies": list(event.payload.get("policies") or []),
                    }
                    if assessment is not None
                    else {}
                ),
                **(
                    {
                        "mode": "reconsideration",
                        "session_id": "sess_autonomous",
                        "channel": "runtime",
                    }
                    if event.event_type == RuntimeEventType.GOAL_RECONSIDERATION
                    else {}
                ),
                **(
                    {
                        "interoception": self._interoception_payload_for_cognitive()
                    }
                    if self._interoception is not None
                    and self.manifest.embodiment.mode in {"grounded", "active"}
                    else {}
                ),
            },
        )
        if web_deadline is not None:
            prepare_envelope = prepare_envelope.model_copy(update={"deadline": min(prepare_envelope.deadline, web_deadline)})
        self._journal_homeostatic_route(
            event=event,
            command_id=prepare_envelope.command_id,
            target="PrepareResponse",
            summary=homeostatic_interoception,
        )
        prepare_result = await self._invoke_domain(
            service="cognitive",
            envelope=prepare_envelope,
            method=self.cognitive.prepare_response,
            event=event,
        )
        prepared = PreparedResponse.model_validate(prepare_result.result)
        if event.event_type == RuntimeEventType.IDLE:
            ineligible = self._autonomous_ineligible(event)
            if ineligible is not None:
                self._cancelled_autonomous_event_ids.discard(event.event_id)
                self._append_journal(
                    "autonomous_cancelled_after_prepare",
                    event=event,
                    process_seq=process_seq,
                    data={"prepare_id": prepared.prepare_id, "reason": ineligible, **causal_context},
                )
                return EventDispatchResult(
                    event_id=event.event_id,
                    receive_seq=event.receive_seq,
                    process_seq=process_seq,
                    status="ignored",
                    decision_kind="no_action",
                    detail={"prepare_id": prepared.prepare_id, "reason": ineligible, **causal_context},
                )
        if utc_now() >= prepared.expires_at:
            raise LateDomainResultError(
                f"prepared response {prepared.prepare_id} expired before delivery"
            )
        decision: ReplyDecision | GoalProposal | NoActionDecision | DeferDecision = TypeAdapter(
            CharacterDecision
        ).validate_python(prepared.decision)
        if self._basic_terminal is not None:
            terminal_stop = self._basic_terminal.stop_reason(event) if terminal_context is not None else None
            if terminal_stop and isinstance(decision, GoalProposal):
                await self._commit_prepared(event, prepared, DecisionEvidence(
                    prepare_id=prepared.prepare_id, recorded_at=utc_now(),
                    decision_kind="no_action", reason=terminal_stop,
                ))
                detail = {"effective_decision_kind": "no_action", "reason": terminal_stop}
                self._basic_terminal.finish_decision(event, prepared, detail)
                return EventDispatchResult(
                    event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
                    status="ignored", decision_kind="no_action", detail=detail,
                )
            self._basic_terminal.bind(event, prepared)
        detail = await self._handle_decision(
            event,
            prepared,
            decision,
            homeostatic_context=causal_context,
        )
        if self._basic_terminal is not None:
            self._basic_terminal.finish_decision(event, prepared, detail)
        if continued_after_homeostasis:
            self._homeostasis_continuations.pop(event.event_id, None)
            self._append_journal(
                "homeostasis_continuation_consumed",
                event=event,
                process_seq=process_seq,
                data=causal_context,
            )
        self._cancelled_autonomous_event_ids.discard(event.event_id)
        return EventDispatchResult(
            event_id=event.event_id,
            receive_seq=event.receive_seq,
            process_seq=process_seq,
            status="completed",
            decision_kind=str(detail.get("effective_decision_kind") or decision.kind),
            detail={**detail, **causal_context},
        )

    async def _handle_grounded_body(self, event: RuntimeEvent, process_seq: int) -> EventDispatchResult:
        """Keep body facts local; a body tick never requests a policy or decision."""
        from harness.research.grounded_agency.hashes import content_hash

        self._grounded_binding.assert_matches(self.manifest)
        raw = event.payload.get("body_state") or event.payload
        model = LegacyBodyStateSnapshot if raw.get("schema_version") == "body-state-v1" else EmbodimentBodyStateSnapshot
        snapshot = model.model_validate(raw)
        expected = self.manifest.embodiment.body_instance_id
        if expected is not None and snapshot.body_instance_id != expected:
            raise DomainCommandError("body-state snapshot belongs to a different body instance")
        self._assert_grounded_snapshot_progress(snapshot, self._grounded_body)
        episode_id = getattr(snapshot, "episode_id", None) or self._body_episode_id
        trace = {"body_state": snapshot.model_dump(mode="json"), "manifest_hash": self.manifest.content_hash()}
        if self._opaque_store is not None:
            sensor_value = None
            if self._grounded_sensor_transform is not None:
                transformed = self._grounded_sensor_transform(snapshot)
                sensor_value = transformed.energy.available_ratio if hasattr(transformed, "energy") else transformed
                trace["sensor_intervention_value"] = sensor_value
            envelope = self._opaque_store.ingest(snapshot, episode_id=episode_id, sensor_value=sensor_value)
            trace.update(
                envelope=envelope.model_dump(mode="json"),
                observation_hash=content_hash(envelope.observation),
            )
        self._append_journal("grounded_body_observed", event=event, process_seq=process_seq, data=trace)
        # Publish the new runtime state only after all validation and sensor
        # persistence succeeds. Rejected revisions cannot poison a later call.
        self._install_grounded_body(snapshot)
        if snapshot.energy.terminal:
            self._agency_state = AgencyState.TERMINAL
            self._terminal_body_revision = snapshot.revision
            self._terminal_episode_id = self._body_episode_id
            self.embodiment.set_terminal("body provider reported terminal state")
            await self.action_coordinator.request_preemption(reason="body provider reported terminal state")
        if self._research_memory is not None:
            try:
                visible = self._opaque_store.observations(now=self._research_clock())
                self._append_journal("body_memory_observed", event=event,
                                     data={"observations": [item.model_dump(mode="json") for item in visible]})
                await self._research_memory.observe(visible)
            except Exception as exc:
                self._append_journal(
                    "body_memory_observation_failed", event=event,
                    data={"error_type": type(exc).__name__, "analysis_eligible": False},
                )
        if self._agency_service is not None and self._opaque_store is not None:
            from harness.runtime.research_agency import observe

            observe(self, self._opaque_store.observations(now=self._research_clock()))
        return EventDispatchResult(
            event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
            status="completed", decision_kind="body_observation",
            detail={"body_revision": snapshot.revision, "reconsideration_requested": False},
        )

    @staticmethod
    def _assert_grounded_snapshot_progress(snapshot, previous) -> None:
        from harness.research.grounded_agency.hashes import content_hash

        if previous is None:
            return
        if snapshot.body_instance_id != previous.body_instance_id:
            raise DomainCommandError("grounded body instance changed without a new run")
        if snapshot.revision < previous.revision:
            raise DomainCommandError("grounded body revision moved backwards")
        if snapshot.revision == previous.revision and content_hash(snapshot) != content_hash(previous):
            raise DomainCommandError("same body revision contains conflicting truth")
        if snapshot.captured_at < previous.captured_at:
            raise DomainCommandError("grounded body capture time moved backwards")

    def _install_grounded_body(self, snapshot) -> None:
        self._body_revision = snapshot.revision
        self._body_captured_at = snapshot.captured_at
        self._body_episode_id = getattr(snapshot, "episode_id", None) or self._body_episode_id
        self._grounded_body = snapshot
        self._interoception = None
        if self.manifest.grounded_agency.semantic_homeostasis:
            ratio = snapshot.energy.available_ratio
            self._interoception = CognitiveInteroceptiveState(
                level="critical" if ratio <= .1 else "low" if ratio < .3 else "nominal" if ratio < .7 else "high",
                trend=max(-1., min(1., snapshot.energy.delta_units_per_minute)), confidence=1.,
                captured_at=snapshot.captured_at, evidence_refs=[f"body-state:{snapshot.revision}"],
            )

    def _restore_grounded_body(self, records: Iterable[RuntimeJournalRecord]) -> None:
        """Rebuild body authority and a missing sensor cache from the durable journal."""
        if self.manifest.grounded_agency is None:
            return
        previous = None
        terminal_state = None
        episode_id = None
        for record in records:
            if record.record_type == "agency_reset":
                terminal_state = AgencyState.AVAILABLE
            if record.record_type != "grounded_body_observed":
                continue
            if record.data.get("manifest_hash") != self.manifest.content_hash():
                raise RuntimeStateError("grounded body journal manifest drift")
            raw = record.data["body_state"]
            model = LegacyBodyStateSnapshot if raw.get("schema_version") == "body-state-v1" else EmbodimentBodyStateSnapshot
            snapshot = model.model_validate(raw)
            self._assert_grounded_snapshot_progress(snapshot, previous)
            episode_id = getattr(snapshot, "episode_id", None) or episode_id
            if self._opaque_store is not None:
                envelope = self._opaque_store.ingest(
                    snapshot, episode_id=episode_id, sensor_value=record.data.get("sensor_intervention_value"),
                )
                if envelope.model_dump(mode="json") != record.data.get("envelope"):
                    raise RuntimeStateError("grounded sensor replay does not match its recorded envelope")
            previous = snapshot
            if snapshot.energy.terminal:
                terminal_state = AgencyState.TERMINAL
                self._terminal_body_revision = snapshot.revision
                self._terminal_episode_id = episode_id
        if previous is not None:
            self._install_grounded_body(previous)
        if terminal_state is not None:
            self._agency_state = terminal_state
        if self._agency_state == AgencyState.TERMINAL:
            self.embodiment.set_terminal("restored terminal body state")

    async def _dispatch_grounded_event(self, event: RuntimeEvent, process_seq: int) -> EventDispatchResult:
        """Independent research route: no legacy ranking, reflection or social increments."""
        self._grounded_binding.assert_matches(self.manifest)
        ga = self.manifest.grounded_agency
        if event.event_type == RuntimeEventType.HEARTBEAT:
            self._last_heartbeat_at = utc_now()
            await self._refresh_embodiment()
        agency_heartbeat = False
        if event.event_type == RuntimeEventType.HEARTBEAT and self._agency_service is not None:
            from harness.runtime.research_agency import heartbeat_due

            agency_heartbeat = heartbeat_due(self)
        if (event.event_type not in {
            RuntimeEventType.USER_MESSAGE, RuntimeEventType.ENVIRONMENT_CHANGE,
            RuntimeEventType.GOAL_RECONSIDERATION, RuntimeEventType.IDLE,
        } and not agency_heartbeat) or self._agency_state != AgencyState.AVAILABLE:
            return EventDispatchResult(
                event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
                status="completed", decision_kind="no_action",
                detail={"reason": "observation recorded without requesting a model decision"},
            )
        if event.event_type == RuntimeEventType.IDLE and self._autonomous_ineligible(event) is not None:
            return EventDispatchResult(
                event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
                status="ignored", decision_kind="no_action", detail={"reason": "idle event superseded"},
            )
        sampled_at = self._research_clock()
        observations = self._opaque_store.observations(now=sampled_at) if self._opaque_store else ()
        if self._research_memory is not None:
            try:
                self._append_journal("body_memory_observed", event=event,
                                     data={"observations": [item.model_dump(mode="json") for item in observations]})
                await self._research_memory.observe(observations)
            except Exception as exc:
                self._append_journal(
                    "body_memory_observation_failed", event=event,
                    data={"error_type": type(exc).__name__, "analysis_eligible": False},
                )
        self._append_journal(
            "grounded_observation_sampled", event=event,
            data={"sampled_at": sampled_at.isoformat()},
        )
        provider = self.embodiment.provider
        declared = getattr(provider, "supported_intents", None)
        if callable(declared):
            declared = declared()
            if inspect.isawaitable(declared):
                declared = await declared
        provider_intents = list(declared) if declared is not None else []
        if not self.embodiment.supports(ACTION_EXECUTE):
            provider_intents = []
        payload = {
            "mode": "grounded_agency_decision",
            "grounded_manifest_hash": self.manifest.content_hash(),
            "text": str(event.payload.get("text") or ""),
            "session_id": str(event.payload.get("session_id") or "grounded-agency"),
            "channel": str(event.payload.get("channel") or "runtime"),
            "source_time": event.source_time,
            "opaque_interoception": [item.model_dump(mode="json") for item in observations],
            "provider_intents": provider_intents,
        }
        if self._research_memory is not None:
            try:
                segment = self._research_memory.context(observations)
                if segment is not None:
                    payload["research_context"] = [segment.model_dump(mode="json")]
            except Exception as exc:
                self._append_journal(
                    "body_memory_context_failed", event=event,
                    data={"error_type": type(exc).__name__, "analysis_eligible": False},
                )
        if ga.action_selection == "engineered_argmin":
            payload["policies"] = await self.homeostasis.propose_policies(version_vector=self.version_vector)
        if ga.semantic_homeostasis and self._interoception is not None:
            payload["interoception"] = self._interoception.model_dump(mode="json", exclude={"evidence_refs"})
        research_segments = await self._social_research_context(event)
        if research_segments:
            payload.setdefault("research_context", []).extend(segment.model_dump(mode="json") for segment in research_segments)
        arbitration = None
        if self._agency_service is not None:
            from harness.core.inference import ContextSegment
            from harness.runtime.research_agency import reconsider

            arbitration, segment = await reconsider(
                self, event, observations,
                [ContextSegment.model_validate(item) for item in payload.get("research_context", [])], provider_intents,
            )
            if segment is not None:
                payload.setdefault("research_context", []).append(segment.model_dump(mode="json"))
            if agency_heartbeat and (not arbitration.model_called or arbitration.decision.operation in {"no_change", "pause", "abandon"}):
                return EventDispatchResult(
                    event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
                    status="completed", decision_kind="no_action", detail={"agency": arbitration.model_dump(mode="json")},
                )
        envelope = self._make_command(
            event, command_type="PrepareResponse", idempotency_key=self._prepare_idempotency_key(event),
            expected_revision=self._cognitive_snapshot.revision, payload=payload,
        )
        result = await self._invoke_domain(
            service="cognitive", envelope=envelope, method=self.cognitive.prepare_response, event=event,
        )
        prepared = PreparedResponse.model_validate(result.result)
        if prepared.expires_at <= utc_now():
            raise LateDomainResultError("research decision expired before execution")
        trace = getattr(self.cognitive, "research_traces", {}).get(event.event_id)
        if self._agency_service is not None and trace is not None and isinstance(prepared.decision, GoalProposal):
            from harness.runtime.research_agency import unresolved_plan_reason

            blocked = unresolved_plan_reason(self, trace.get("decision", {}), arbitration)
            if blocked is not None:
                deferred = DeferDecision(reason=blocked, required_evidence=["verified plan resumption or action reconciliation"])
                prepared = prepared.model_copy(update={"decision": deferred})
                trace.update(compiled_decision=deferred.model_dump(mode="json"), runtime_guard=blocked)
        if trace is not None:
            trace = {**trace, "visible_observations": [item.model_dump(mode="json") for item in observations],
                     "research_episode_id": self._body_episode_id or "runtime-episode"}
            if self._research_memory is not None:
                self._research_memory.remember(event.event_id, trace)
            self._append_journal("grounded_decision", event=event, data=trace)
            if self._agency_service is not None:
                from harness.runtime.research_agency import bind_decision

                bind_decision(self, event.event_id, trace, arbitration)
        if isinstance(prepared.decision, ReplyDecision) and ga.report_channel != "normal":
            raise DomainCommandError("action-only research cannot deliver a ReplyDecision")
        detail = await self._handle_decision(event, prepared, prepared.decision)
        if isinstance(prepared.decision, (NoActionDecision, DeferDecision)):
            self._record_grounded_experience(
                event, evidence=prepared.decision.model_dump(mode="json"), success=True, verified=True,
            )
        self._cognitive_snapshot = await self.cognitive.get_snapshot()
        return EventDispatchResult(
            event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
            status="completed", decision_kind=str(detail.get("effective_decision_kind") or prepared.decision.kind),
            detail=detail,
        )

    def _record_grounded_experience(self, event: RuntimeEvent, *, evidence, success: bool, verified: bool) -> None:
        if self._research_memory is None:
            return
        try:
            saved = next((record.data for record in self.journal.iter_records()
                          if record.record_type == "body_experience_outcome" and record.event_id == event.event_id), None)
            if saved is None:
                saved = {"decision_id": event.event_id, "evidence": evidence,
                         "completed_at": self._research_clock().isoformat(), "success": success, "verified": verified,
                         "outcome_ref": self._research_memory.outcome_reference(event.event_id, success=success, verified=verified)}
                self._append_journal("body_experience_outcome", event=event, data=saved)
            recorded = self._research_memory.complete(
                event.event_id, evidence=saved["evidence"], completed_at=datetime.fromisoformat(saved["completed_at"]),
                success=saved["success"], verified=saved["verified"],
            )
            if recorded:
                self._append_journal("body_experience_pending", event=event, data={"decision_id": event.event_id})
        except Exception as exc:
            self._append_journal("body_experience_recording_failed", event=event,
                                 data={"error": str(exc), "analysis_eligible": False})

    async def _pause_research_agency(self) -> None:
        if self._agency_service is not None:
            from harness.runtime.research_agency import checkpoint_goals

            if not await self._agency_service.pause(timeout=self.maintenance_drain_seconds):
                raise RuntimeStateError("agency model call did not drain")
            checkpoint_goals(self)
        if self._social_appraisal_tasks:
            _, pending = await asyncio.wait(self._social_appraisal_tasks, timeout=self.maintenance_drain_seconds)
            if pending:
                raise RuntimeStateError("entity appraisal did not drain")

    async def _restore_research_memory(self) -> None:
        bridge = self._research_memory
        if bridge is None:
            return
        from harness.research.interoception.contracts import OpaqueInteroceptiveObservation

        await bridge.service.pause()
        for record in self.journal.iter_records():
            if record.record_type == "body_experience_outcome":
                data = record.data
                bridge.complete(
                    data["decision_id"], evidence=data["evidence"],
                    completed_at=datetime.fromisoformat(data["completed_at"]),
                    success=data["success"], verified=data["verified"],
                )
            elif record.record_type == "body_memory_observed":
                await bridge.observe(tuple(OpaqueInteroceptiveObservation.model_validate(item)
                                           for item in record.data["observations"]))

    async def _social_research_context(self, event: RuntimeEvent):
        service = self._social_world_service
        if service is None:
            return []
        from harness.social_world.contracts import EntityObservation

        if self._social_appraisal_tasks:
            _, pending = await asyncio.wait(self._social_appraisal_tasks, timeout=self.command_timeout_seconds)
            if pending:
                raise DomainCommandError("previous entity appraisal is still running")
        raw_events = event.payload.get("entity_observations", [])
        if "entity_observation" in event.payload:
            raw_events = [*raw_events, event.payload["entity_observation"]]
        for raw in raw_events:
            observation = EntityObservation.model_validate(raw)
            service.observe(observation)
            self._append_journal("entity_observation_recorded", event=event, data=observation.model_dump(mode="json"))
            if service.condition.entity_memory == "general_beliefs":
                llm = getattr(getattr(self.cognitive, "pipeline", None), "llm", None)
                if llm is None:
                    raise DomainCommandError("entity appraisal requires an explicit local model port")
                task = asyncio.create_task(asyncio.to_thread(service.appraise, llm))
                self._social_appraisal_tasks.add(task)
                task.add_done_callback(self._social_appraisal_tasks.discard)
                completion_state = {"late": False}

                def record_appraisal(completed, *, state=completion_state, causal_event=event):
                    if completed.cancelled():
                        return
                    try:
                        result = completed.result()
                        self._append_journal("entity_appraisal", event=causal_event, data={
                            **result.model_dump(mode="json"), "late_completion": state["late"],
                            "appraised_at": self._research_clock().isoformat(),
                            "analysis_eligible": not state["late"],
                        })
                    except Exception as exc:
                        self._append_journal("entity_appraisal_failed", event=causal_event,
                                             data={"error_type": type(exc).__name__, "analysis_eligible": False})

                task.add_done_callback(record_appraisal)
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=self.command_timeout_seconds)
                except Exception as exc:
                    self._append_journal("entity_appraisal_failed", event=event, data={"error": str(exc), "analysis_eligible": False})
                    if isinstance(exc, TimeoutError):
                        completion_state["late"] = True
                        raise DomainCommandError("entity appraisal exceeded its deadline") from exc
        return [segment for segment in (service.current_observation(), service.model_context()) if segment.text]

    async def _handle_body_state(
        self,
        event: RuntimeEvent,
        process_seq: int,
    ) -> EventDispatchResult:
        snapshot = EmbodimentBodyStateSnapshot.model_validate(
            event.payload.get("body_state") or event.payload
        )
        expected_instance = self.manifest.embodiment.body_instance_id
        if expected_instance and snapshot.body_instance_id != expected_instance:
            raise DomainCommandError("body-state snapshot belongs to a different body instance")

        interoception = EmbodimentInteroceptiveState(
            source_revision=snapshot.revision,
            resource_level=snapshot.energy.available_ratio,
            resource_trend=snapshot.energy.delta_units_per_minute,
            estimated_time_to_limit_seconds=snapshot.energy.estimated_time_to_empty_seconds,
            evidence_refs=snapshot.evidence_refs,
        )
        previous_interoception = self._interoception
        self._interoception = interoception
        self._append_journal(
            "body_state_observed",
            event=event,
            process_seq=process_seq,
            data={"body_state": snapshot.model_dump(mode="json")},
        )

        terminal = snapshot.energy.terminal
        if terminal:
            self.embodiment.set_terminal("body-state provider reported terminal resource state")
            if self.action_coordinator.current is not None:
                await self.action_coordinator.request_preemption(reason="embodiment terminal state")
            await self.submit_event(
                event_type=RuntimeEventType.AGENCY_TERMINAL,
                priority=RuntimeEventPriority.CONTROL,
                source="embodiment",
                source_time=snapshot.captured_at,
                idempotency_key=f"agency-terminal:{snapshot.body_instance_id}:{snapshot.revision}",
                payload={"body_state_revision": snapshot.revision},
            )

        mode = self.manifest.embodiment.mode
        homeostasis_watchdog = False
        if mode in {"grounded", "active"}:
            observation = EmbodimentHomeostaticObservation(
                source="system_sensor",
                precision=0.95,
                observed_values={"energy_reserve": snapshot.energy.available_ratio},
                evidence_refs=snapshot.evidence_refs,
                observed_at=snapshot.captured_at,
            )
            envelope = self._make_command(
                event,
                command_type="AssimilateObservation",
                idempotency_key=f"homeostasis:body-state:{snapshot.body_instance_id}:{snapshot.revision}",
                expected_revision=self._homeostasis_snapshot.revision,
                payload={"observation": observation.model_dump(mode="json")},
            )
            result = await self._invoke_domain(
                service="homeostasis",
                envelope=envelope,
                method=self.homeostasis.assimilate_observation,
                event=event,
            )
            homeostasis_watchdog = await self._update_homeostasis(result, event=event)
        if homeostasis_watchdog:
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="watchdog_pause",
                detail={
                    "mode": mode,
                    "body_state_revision": snapshot.revision,
                    "homeostasis_revision": self._homeostasis_snapshot.revision,
                    "lifecycle": self.lifecycle.value,
                },
            )

        reconsider = terminal
        if (
            mode == "active"
            and not terminal
            and isinstance(previous_interoception, EmbodimentInteroceptiveState)
        ):
            target_band = self._energy_target_band()
            if target_band is not None:
                previous_band = self._target_band_position(
                    previous_interoception.resource_level,
                    target_band,
                )
                current_band = self._target_band_position(interoception.resource_level, target_band)
                crossed = previous_band != current_band
                significant = (
                    abs(interoception.resource_level - previous_interoception.resource_level)
                    >= self.manifest.embodiment.replan_hysteresis
                )
                interval_satisfied = (
                    self._last_reconsideration_at is None
                    or (snapshot.captured_at - self._last_reconsideration_at).total_seconds()
                    >= self.manifest.embodiment.minimum_replan_interval_seconds
                )
                reconsider = crossed and significant and interval_satisfied
        if mode == "active" and reconsider:
            self._last_reconsideration_at = snapshot.captured_at
            await self.submit_event(
                event_type=RuntimeEventType.GOAL_RECONSIDERATION,
                priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT,
                source="homeostasis",
                source_time=snapshot.captured_at,
                idempotency_key=f"goal-reconsideration:{snapshot.body_instance_id}:{snapshot.revision}",
                payload={
                    "interoception": interoception.model_dump(mode="json"),
                    "terminal": terminal,
                },
            )

        return EventDispatchResult(
            event_id=event.event_id,
            receive_seq=event.receive_seq,
            process_seq=process_seq,
            status="completed",
            decision_kind="body_state",
            detail={
                "mode": mode,
                "body_state_revision": snapshot.revision,
                "homeostasis_revision": self._homeostasis_snapshot.revision,
                "terminal": terminal,
                "reconsideration_requested": mode == "active" and reconsider,
            },
        )

    async def _apply_body_state(
        self, event: RuntimeEvent, process_seq: int
    ) -> EventDispatchResult:
        snapshot = LegacyBodyStateSnapshot.model_validate(event.payload)
        self._body_episode_id = snapshot.episode_id
        self._body_revision = snapshot.revision
        self._body_captured_at = snapshot.captured_at
        self._interoception = self._build_interoception(snapshot)
        trace = {
            "mode": self.manifest.embodiment.mode,
            "body_instance_id": snapshot.body_instance_id,
            "episode_id": snapshot.episode_id,
            "body_revision": snapshot.revision,
            "captured_at": snapshot.captured_at.isoformat(),
            "available_ratio": snapshot.energy.available_ratio,
            "evidence_refs": snapshot.evidence_refs,
            "terminal": snapshot.energy.terminal,
            "interoception": self._interoception.model_dump(mode="json"),
        }
        self._append_body_analysis_trace(trace)
        self._append_journal("body_state_observed", event=event, data=trace)
        if self.manifest.embodiment.mode == "observe":
            if self._agency_state == AgencyState.DEGRADED:
                self._agency_state = AgencyState.AVAILABLE
                self._degraded_reasons = [
                    reason for reason in self._degraded_reasons if reason != "body"
                ]
            self._append_journal(
                "body_state_applied",
                event=event,
                data={"terminal": snapshot.energy.terminal, "homeostasis_updated": False},
            )
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="body_observation",
                detail={**trace, "homeostasis_updated": False},
            )
        if self.manifest.embodiment.mode != "grounded":
            raise RuntimeStateError("body event received while embodiment is off")
        command = self._make_command(
            event,
            command_type="ApplyObservation",
            idempotency_key=(
                f"homeostasis:body:{snapshot.body_instance_id}:"
                f"{snapshot.episode_id}:r{snapshot.revision}"
            ),
            expected_revision=self._homeostasis_snapshot.revision,
            payload={
                "observation": {
                    "source": "system_sensor",
                    "observed_values": {
                        "energy_reserve": snapshot.energy.available_ratio,
                    },
                    "body_instance_id": snapshot.body_instance_id,
                    "episode_id": snapshot.episode_id,
                    "body_revision": snapshot.revision,
                    "captured_at": snapshot.captured_at,
                    "evidence_refs": snapshot.evidence_refs,
                    "experiment_mode": "grounded",
                }
            },
        )
        result = await self._invoke_domain(
            service="homeostasis",
            envelope=command,
            method=self.homeostasis.apply_observation,
            event=event,
        )
        homeostasis_watchdog = await self._update_homeostasis(result, event=event)
        if homeostasis_watchdog:
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="watchdog_pause",
                detail={
                    **trace,
                    "homeostasis_updated": True,
                    "homeostasis_revision": self._homeostasis_snapshot.revision,
                    "lifecycle": self.lifecycle.value,
                },
            )
        if self._agency_state == AgencyState.DEGRADED:
            self._agency_state = AgencyState.AVAILABLE
            self._degraded_reasons = [
                reason for reason in self._degraded_reasons if reason != "body"
            ]
        self._append_journal(
            "body_state_applied",
            event=event,
            data={"terminal": snapshot.energy.terminal, "homeostasis_updated": True},
        )
        return EventDispatchResult(
            event_id=event.event_id,
            receive_seq=event.receive_seq,
            process_seq=process_seq,
            status="completed",
            decision_kind="body_observation",
            detail={
                **trace,
                "homeostasis_updated": True,
                "homeostasis_revision": self._homeostasis_snapshot.revision,
            },
        )

    def _energy_target_band(self) -> tuple[float, float] | None:
        payload = self._homeostasis_snapshot.payload
        state = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
        dimensions = state.get("dimensions") if isinstance(state, dict) else None
        energy = dimensions.get("energy_reserve") if isinstance(dimensions, dict) else None
        target = energy.get("target_band") if isinstance(energy, dict) else None
        if not isinstance(target, (list, tuple)) or len(target) != 2:
            return None
        return float(target[0]), float(target[1])

    @staticmethod
    def _target_band_position(value: float, target: tuple[float, float]) -> str:
        if value < target[0]:
            return "low"
        if value > target[1]:
            return "high"
        return "in_band"

    def _build_interoception(
        self, snapshot: LegacyBodyStateSnapshot
    ) -> CognitiveInteroceptiveState:
        ratio = snapshot.energy.available_ratio
        level: Literal["critical", "low", "nominal", "high", "full"]
        if snapshot.energy.terminal or ratio <= 0.1:
            level = "critical"
        elif ratio < 0.3:
            level = "low"
        elif ratio < 0.7:
            level = "nominal"
        elif ratio < 0.98:
            level = "high"
        else:
            level = "full"
        trend = max(
            -1.0,
            min(1.0, snapshot.energy.delta_units_per_minute / snapshot.energy.capacity_units),
        )
        return CognitiveInteroceptiveState(
            resource_label=(
                "resource_A" if self.manifest.embodiment.blinded_labels else "internal_resource"
            ),
            level=level,
            trend=trend,
            time_to_action_limit_seconds=snapshot.energy.estimated_time_to_terminal_seconds,
            confidence=1.0,
            captured_at=snapshot.captured_at,
            evidence_refs=snapshot.evidence_refs,
        )

    def _interoception_payload_for_cognitive(self) -> dict[str, Any]:
        """Project body state without forwarding provider evidence identifiers."""

        interoception = self._interoception
        if interoception is None:
            raise RuntimeStateError("interoception is unavailable")
        payload = interoception.model_dump(mode="json", exclude={"evidence_refs"})
        if isinstance(interoception, CognitiveInteroceptiveState):
            if self._body_revision is None:
                raise RuntimeStateError(
                    "legacy interoception is missing its body-state revision"
                )
            payload["source_revision"] = self._body_revision
        return payload

    def _append_body_analysis_trace(self, trace: dict[str, Any]) -> None:
        path = self.state_dir / "body-analysis.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(
                json.dumps(trace, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                + "\n"
            )
            handle.flush()
            os.fsync(handle.fileno())

    async def _handle_decision(
        self,
        event: RuntimeEvent,
        prepared: PreparedResponse,
        decision: ReplyDecision | GoalProposal | NoActionDecision | DeferDecision,
        *,
        homeostatic_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        causal = homeostatic_context or {}
        if event.event_type == RuntimeEventType.IDLE:
            ineligible = self._autonomous_ineligible(event)
            if ineligible is not None:
                self._cancelled_autonomous_event_ids.discard(event.event_id)
                self._append_journal(
                    "autonomous_cancelled_before_effect",
                    event=event,
                    data={"prepare_id": prepared.prepare_id, "reason": ineligible, **causal},
                )
                return {
                    "prepare_id": prepared.prepare_id,
                    "reason": ineligible,
                    "effective_decision_kind": "no_action",
                }
        if isinstance(decision, ReplyDecision):
            deadline = min(prepared.expires_at, utc_now() + timedelta(seconds=self.action_timeout_seconds))
            delivery_key = f"delivery:{prepared.prepare_id}"
            delivery, uncertain = self._find_delivery_result(prepared.prepare_id)
            if uncertain:
                raise UnknownDeliveryStateError(
                    f"delivery result is unknown for prepare {prepared.prepare_id}; researcher confirmation required"
                )
            if delivery is None:
                async with self._foreground_lock:
                    # User ingress does not take this lock, but it can only run at
                    # an await boundary.  Keeping this final check, durable intent,
                    # and invocation start in one synchronous critical section
                    # closes the running-IDLE cancellation race.
                    if event.event_type == RuntimeEventType.IDLE:
                        ineligible = self._autonomous_ineligible(event)
                        if ineligible is not None:
                            self._cancelled_autonomous_event_ids.discard(event.event_id)
                            self._append_journal(
                                "autonomous_cancelled_before_delivery",
                                event=event,
                                data={
                                    "prepare_id": prepared.prepare_id,
                                    "reason": ineligible,
                                    **causal,
                                },
                            )
                            return {
                                "prepare_id": prepared.prepare_id,
                                "reason": ineligible,
                                "effective_decision_kind": "no_action",
                            }
                    delivery, uncertain = self._find_delivery_result(prepared.prepare_id)
                    if uncertain:
                        raise UnknownDeliveryStateError(
                            "delivery result is unknown for prepare "
                            f"{prepared.prepare_id}; researcher confirmation required"
                        )
                    if delivery is None:
                        self._append_journal(
                            "reply_delivery_intent",
                            event=event,
                            idempotency_key=delivery_key,
                            data={
                                "prepare_id": prepared.prepare_id,
                                "channel": decision.channel,
                                **causal,
                            },
                        )
                        delivery = await self.delivery_backend.deliver(
                            text=decision.text,
                            segments=list(decision.segments),
                            channel=decision.channel,
                            idempotency_key=delivery_key,
                            deadline=deadline,
                        )
            self._append_journal(
                "reply_delivery",
                event=event,
                idempotency_key=delivery_key,
                data={
                    "prepare_id": prepared.prepare_id,
                    "result": delivery.model_dump(mode="json"),
                    **causal,
                },
            )
            delivery_evidence = DeliveryEvidence(
                prepare_id=prepared.prepare_id,
                delivered_at=utc_now(),
                channel=decision.channel,
                success=delivery.success,
                delivered_segments=delivery.delivered_segments,
                external_ref=delivery.external_ref,
                error=delivery.error,
            )
            committed = await self._commit_prepared(event, prepared, delivery_evidence)
            return {
                "prepare_id": prepared.prepare_id,
                "delivered": delivery.success,
                "delivery": delivery.model_dump(mode="json"),
                "cognitive_revision": committed.committed_revision,
            }
        if isinstance(decision, GoalProposal):
            if self.manifest.embodiment.mode != "off" and decision.embodied_action is None:
                reason = "goal has no structured embodied action and cannot be sent to a provider"
                committed = await self._commit_prepared(
                    event,
                    prepared,
                    DecisionEvidence(
                        prepare_id=prepared.prepare_id,
                        recorded_at=utc_now(),
                        decision_kind="defer",
                        reason=reason,
                    ),
                )
                return {
                    "prepare_id": prepared.prepare_id,
                    "reason": reason,
                    "cognitive_revision": committed.committed_revision,
                    "effective_decision_kind": "defer",
                }
            if self._agency_state != AgencyState.AVAILABLE:
                reason = f"goal proposal blocked while agency is {self._agency_state.value}"
                self._append_journal(
                    "goal_proposal_blocked",
                    event=event,
                    data={"prepare_id": prepared.prepare_id, "reason": reason},
                )
                evidence = DecisionEvidence(
                    prepare_id=prepared.prepare_id,
                    recorded_at=utc_now(),
                    decision_kind="no_action",
                    reason=reason,
                )
                committed = await self._commit_prepared(event, prepared, evidence)
                return {
                    "prepare_id": prepared.prepare_id,
                    "reason": reason,
                    "cognitive_revision": committed.committed_revision,
                    "effective_decision_kind": "no_action",
                }
            if self.manifest.embodiment.mode != "off" and (
                self.embodiment.status().state == EmbodimentState.TERMINAL
                or not self.embodiment.supports(ACTION_EXECUTE)
            ):
                reason = "embodied action is unavailable under the current negotiated health"
                committed = await self._commit_prepared(
                    event,
                    prepared,
                    DecisionEvidence(
                        prepare_id=prepared.prepare_id,
                        recorded_at=utc_now(),
                        decision_kind="defer",
                        reason=reason,
                    ),
                )
                return {
                    "prepare_id": prepared.prepare_id,
                    "reason": reason,
                    "cognitive_revision": committed.committed_revision,
                    "effective_decision_kind": "defer",
                }
            if (
                event.event_type == RuntimeEventType.GOAL_RECONSIDERATION
                and self.action_coordinator.current is not None
            ):
                current_action: ActionExecution | None = self.action_coordinator.current
                if self.manifest.embodiment.enable_drive_preemption:
                    current_action = await self.action_coordinator.request_preemption(
                        reason="goal replacement after interoceptive reconsideration"
                    )
                if current_action is not None and not current_action.terminal:
                    reason = (
                        "replacement is waiting for the current action to reach a safe point"
                        if self.manifest.embodiment.enable_drive_preemption
                        else "replacement is waiting because drive preemption is disabled"
                    )
                    committed = await self._commit_prepared(
                        event,
                        prepared,
                        DecisionEvidence(
                            prepare_id=prepared.prepare_id,
                            recorded_at=utc_now(),
                            decision_kind="defer",
                            reason=reason,
                        ),
                    )
                    return {
                        "prepare_id": prepared.prepare_id,
                        "reason": reason,
                        "cognitive_revision": committed.committed_revision,
                        "effective_decision_kind": "defer",
                    }
            self._goal_revision += 1
            action_deadline = min(
                prepared.expires_at,
                utc_now() + timedelta(seconds=self.action_timeout_seconds),
            )
            terminal_item = None
            if self._basic_terminal is not None and decision.policy_id.startswith("basic_terminal_"):
                terminal_item = self._basic_terminal.contexts.get(event.event_id)
                if terminal_item is not None and decision.required_approvals:
                    # Preparing a plan has a short RPC deadline. Human approval
                    # has its own bounded window, within the original task.
                    action_deadline = min(
                        utc_now() + timedelta(seconds=self.manifest.basic_terminal.approval_timeout_seconds),
                        datetime.fromisoformat(terminal_item["task"]["deadline"]),
                    )
            action = await self.action_coordinator.propose(
                decision,
                idempotency_key=f"action:{prepared.prepare_id}:{decision.goal_id}",
                deadline=action_deadline,
                version_vector=prepared.read_versions,
                risk_class=decision.permission_class,
            )
            self._action_context[action.action_id] = {
                "event": event,
                "prepared": prepared,
            }
            if terminal_item is not None:
                observed = terminal_item["context"].get("observation") or {}
                # Retain only target identity while approval is pending; command
                # text remains the exact text shown in the proposed action.
                self._action_context[action.action_id]["terminal_target"] = {
                    "active_window": observed.get("active_window"),
                    "windows": observed.get("windows", []),
                    "a11y_nodes": [
                        {key: node[key] for key in ("app_id", "window_id", "role", "name") if key in node}
                        for node in observed.get("a11y_nodes", [])
                    ],
                }
            self._append_journal(
                "action_proposed",
                event=event,
                data={
                    "action": action.model_dump(mode="json"),
                    "prepared": prepared.model_dump(mode="json"),
                },
            )
            if action.state == ActionExecutionState.READY:
                action = await self._start_action(action, event=event)
            return {"prepare_id": prepared.prepare_id, "action": action.model_dump(mode="json")}
        decision_evidence = DecisionEvidence(
            prepare_id=prepared.prepare_id,
            recorded_at=utc_now(),
            decision_kind=decision.kind,
            reason=decision.reason,
        )
        committed = await self._commit_prepared(event, prepared, decision_evidence)
        return {
            "prepare_id": prepared.prepare_id,
            "reason": decision.reason,
            "cognitive_revision": committed.committed_revision,
        }

    async def _start_action(
        self,
        action: ActionExecution,
        *,
        event: RuntimeEvent | None,
    ) -> ActionExecution:
        action_context = self._action_context.get(action.action_id)
        source_decision = action_context["prepared"].decision if action_context else None
        capability_stop = await self.capabilities.validate_action(action)
        if capability_stop:
            rejected = await self.action_coordinator.reject(action.action_id, reason=capability_stop)
            self._append_journal("action_blocked", event=event, data={"action": rejected.model_dump(mode="json")})
            await self._submit_action_result(rejected)
            return rejected
        action = self.action_coordinator.get(action.action_id) or action
        if (
            self._basic_terminal is not None and event is not None
            and isinstance(source_decision, GoalProposal)
            and (source_decision.policy_id or "").startswith("basic_terminal_")
        ):
            terminal_stop = self._basic_terminal.stop_reason(event, require_active=True)
            observation_evidence: dict[str, Any] = {}
            if terminal_stop is None:
                try:
                    action, observation_evidence = await self._refresh_basic_terminal_target(action, event, action_context)
                except (ValueError, TimeoutError) as exc:
                    terminal_stop = f"终端操作执行前复核失败，已取消操作：{exc}"
                latest = self.action_coordinator.get(action.action_id)
                if latest is not None and latest.state != ActionExecutionState.READY:
                    # A concurrent approval may already have started this action,
                    # or the user may have rejected it while observe was pending.
                    return latest
                # Observing may have yielded while a newer user message arrived.
                terminal_stop = terminal_stop or self._basic_terminal.stop_reason(event, require_active=True)
            if terminal_stop:
                rejected = await self.action_coordinator.reject(action.action_id, reason=terminal_stop)
                self._append_journal("action_blocked", event=event, data={"action": rejected.model_dump(mode="json")})
                await self._submit_action_result(rejected)
                return rejected
            if observation_evidence:
                self.action_coordinator.restore(action)
                self._append_journal(
                    "action_target_revalidated", event=event, idempotency_key=action.idempotency_key,
                    data={"action": action.model_dump(mode="json"), **observation_evidence},
                )
        if self.manifest.embodiment.mode != "off" and (
            self.embodiment.status().state == EmbodimentState.TERMINAL
            or not self.embodiment.supports(ACTION_EXECUTE)
        ):
            rejected = await self.action_coordinator.reject(
                action.action_id,
                reason="embodied action capability is unavailable",
            )
            self._append_journal(
                "action_blocked",
                event=event,
                idempotency_key=action.idempotency_key,
                data={"action": rejected.model_dump(mode="json")},
            )
            await self._submit_action_result(rejected)
            return rejected
        self._append_journal(
            "action_execution_intent",
            event=event,
            idempotency_key=action.idempotency_key,
            data={"action": action.model_dump(mode="json")},
        )
        reserved = await self.action_coordinator.reserve(action.action_id)
        self._append_journal(
            "action_started",
            event=event,
            idempotency_key=reserved.idempotency_key,
            data={"action": reserved.model_dump(mode="json")},
        )
        if reserved.terminal:
            await self._submit_action_result(reserved)
            return reserved
        task = asyncio.create_task(
            self._run_action(reserved.action_id),
            name=f"runtime-action-{reserved.action_id}",
        )
        self._action_tasks[reserved.action_id] = task
        return reserved

    async def _refresh_basic_terminal_target(
        self, action: ActionExecution, event: RuntimeEvent, context: dict[str, Any],
    ) -> tuple[ActionExecution, dict[str, Any]]:
        from harness.cognitive.basic_terminal import BasicTerminalActionPlanner, normalize_terminal_observation

        spec = action.embodied_action
        if spec is None:
            raise ValueError("missing typed GUI action")
        intent_id = spec.intent.intent_id
        if intent_id in {"app.open.v1", "control.observe.v1", "control.wait.v1"}:
            return action, {}
        if self._perception_guard is None or not context.get("terminal_target"):
            raise ValueError("missing original target or desktop observation backend")
        remaining = (action.deadline - utc_now()).total_seconds()
        if remaining <= 0:
            raise ValueError("approval deadline expired")
        try:
            snapshot = await asyncio.wait_for(
                self._perception_guard.backend.observe(), timeout=min(self.command_timeout_seconds, remaining),
            )
        except Exception as exc:
            raise ValueError(f"desktop observation unavailable ({type(exc).__name__})") from exc
        observation_age = (utc_now() - snapshot.captured_at).total_seconds()
        if observation_age < -self.manifest.embodiment.future_clock_tolerance_seconds:
            raise ValueError("desktop observation is in the future")
        if observation_age > self.manifest.embodiment.max_state_age_seconds:
            raise ValueError("desktop observation is stale")
        observed = normalize_terminal_observation(snapshot, terminal_app_id=self.manifest.basic_terminal.terminal_app_id)
        original = context["terminal_target"]
        active = observed.get("active_window") or {}
        original_active = original.get("active_window") or {}
        if intent_id != "window.focus.v1" and (
            not original_active.get("window_id")
            or active.get("window_id") != original_active["window_id"]
        ):
            raise ValueError("the approved terminal window is no longer focused")

        arguments = dict(spec.intent.arguments)
        selector = arguments.get("selector")
        if selector is not None:
            def target(observation):
                matches = [
                    node for node in observation.get("a11y_nodes", [])
                    if all(selector.get(key) is None or node.get(key) == selector[key]
                           for key in ("app_id", "window_id", "role", "name"))
                    and node.get("window_id") in {None, active.get("window_id")}
                ]
                # Identical anonymous nodes cannot be distinguished after a
                # refresh. Do not silently retarget an approved text input.
                if len(matches) != 1 or selector.get("occurrence", 0) != 0:
                    raise ValueError("the approved terminal node is missing or ambiguous")
                return {key: matches[0].get(key) for key in ("app_id", "window_id", "role", "name")}

            if target(original) != target(observed):
                raise ValueError("the approved terminal node changed")
            arguments["selector"] = {**selector, "observation_id": observed.get("observation_id")}
        refreshed_spec = spec.model_copy(update={"intent": spec.intent.model_copy(update={"arguments": arguments})})
        task_id = str(event.payload.get("terminal_task_id") or event.event_id)
        task = self._basic_terminal.tasks.get(task_id)
        if task is None:
            raise ValueError("terminal task is no longer active")
        BasicTerminalActionPlanner._validate_target(
            refreshed_spec, observed, task["history"], self.manifest.basic_terminal.terminal_app_id, task_id,
        )
        return action.model_copy(update={"embodied_action": refreshed_spec}), {
            "observation_id": observed.get("observation_id"),
            "evidence_refs": observed.get("evidence_refs", []),
        }

    async def _run_action(self, action_id: str) -> None:
        try:
            async with self._foreground_lock:
                action = await self.action_coordinator.execute(action_id)
            self._append_journal(
                "action_completed",
                idempotency_key=action.idempotency_key,
                data={"action": action.model_dump(mode="json")},
            )
            await self._submit_action_result(action)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.watchdog.record_failure("action_backend")
            self._append_journal(
                "action_execution_failed",
                data={"action_id": action_id, "error": str(exc)},
            )
            if self.lifecycle == RuntimeLifecycle.RUNNING:
                await self.submit_event(
                    event_type=RuntimeEventType.WATCHDOG,
                    priority=RuntimeEventPriority.CONTROL,
                    source="action_coordinator",
                    payload={"reason": f"action execution failed: {exc}"},
                )
        finally:
            self._action_tasks.pop(action_id, None)

    async def _submit_action_result(self, action: ActionExecution) -> None:
        try:
            await self.submit_event(
                event_type=RuntimeEventType.ACTION_RESULT,
                priority=RuntimeEventPriority.ACTION_RESULT,
                source="action_coordinator",
                idempotency_key=f"action-result:{action.idempotency_key}",
                payload={"action_id": action.action_id, "state": action.state.value},
            )
        except RuntimeStateError:
            self._append_journal(
                "action_result_pending_recovery",
                idempotency_key=action.idempotency_key,
                data={"action": action.model_dump(mode="json")},
            )

    async def _drain_action_tasks(self, *, timeout: float) -> bool:
        tasks = [task for task in self._action_tasks.values() if not task.done()]
        if not tasks:
            return True
        _, pending = await asyncio.wait(tasks, timeout=timeout)
        return not pending

    async def _complete_goal_action(
        self,
        action: ActionExecution,
        *,
        outcome_event: RuntimeEvent | None = None,
    ) -> None:
        context = self._action_context.get(action.action_id)
        if context is None or not action.terminal:
            return
        original_event: RuntimeEvent = context["event"]
        effective_outcome_event = outcome_event or self._action_outcome_causal_event(
            action,
            original_event=original_event,
        )
        prepared: PreparedResponse = context["prepared"]
        outcome = action.outcome or {}
        evidence_refs = [
            item.ref if hasattr(item, "ref") else str(item.get("ref") if isinstance(item, dict) else item)
            for item in (outcome.get("evidence_refs") or [])
        ]
        evidence = ActionOutcomeEvidence(
            prepare_id=prepared.prepare_id,
            goal_id=action.goal_id,
            observed_at=utc_now(),
            success=action.state == ActionExecutionState.SUCCEEDED,
            summary=str(outcome.get("summary") or action.error or action.state.value),
            evidence_refs=evidence_refs,
            external_effect_ref=outcome.get("external_effect_ref"),
        )
        # The prepared interaction belongs to the original goal-producing
        # event, while outcome application and its causal trace belong to the
        # ACTION_RESULT (when one exists).
        await self._commit_prepared(original_event, prepared, evidence)
        ga = self.manifest.grounded_agency
        if original_event.event_type != RuntimeEventType.CAPABILITY_OPPORTUNITY and (
            ga is None or ga.action_selection == "engineered_argmin"
        ):
            outcome_command = self._make_command(
                effective_outcome_event,
                command_type="ApplyOutcome",
                idempotency_key=f"homeostasis:outcome:{action.idempotency_key}",
                expected_revision=self._homeostasis_snapshot.revision,
                payload={"action": action.model_dump(mode="json"), "evidence": evidence.model_dump(mode="json")},
            )
            result = await self._invoke_domain(
                service="homeostasis",
                envelope=outcome_command,
                method=self.homeostasis.apply_outcome,
                event=effective_outcome_event,
            )
            await self._update_homeostasis(result, event=effective_outcome_event)
        self._record_grounded_experience(
            original_event, evidence=evidence.model_dump(mode="json"),
            success=action.state == ActionExecutionState.SUCCEEDED,
            verified=action.state in {ActionExecutionState.SUCCEEDED, ActionExecutionState.FAILED},
        )
        self._append_journal(
            "action_completed",
            event=effective_outcome_event,
            data={"action": action.model_dump(mode="json")},
        )
        if self._agency_service is not None:
            from harness.runtime.research_agency import record_action

            record_action(self, original_event.event_id, action)
        self._append_journal(
            "action_interaction_committed",
            event=effective_outcome_event,
            idempotency_key=action.idempotency_key,
            data={"action_id": action.action_id, "prepare_id": prepared.prepare_id},
        )
        self._action_context.pop(action.action_id, None)
        if original_event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY:
            await self.capabilities.reconcile()
        if self._basic_terminal is not None:
            await self._basic_terminal.action_completed(original_event, action, evidence)

    def _action_outcome_causal_event(
        self,
        action: ActionExecution,
        *,
        original_event: RuntimeEvent,
    ) -> RuntimeEvent:
        """Build stable non-user provenance for direct/recovered action outcomes."""

        digest = hashlib.sha256(action.idempotency_key.encode("utf-8")).hexdigest()[:32]
        event_id = f"event_action_outcome_{digest}"
        return RuntimeEvent(
            event_id=event_id,
            receive_seq=original_event.receive_seq,
            correlation_id=original_event.correlation_id,
            event_type=RuntimeEventType.ACTION_RESULT,
            priority=RuntimeEventPriority.ACTION_RESULT,
            source="runtime_action_outcome",
            source_time=action.updated_at,
            received_at=utc_now(),
            idempotency_key=f"action-outcome-causal:{digest}",
            manifest_revision=original_event.manifest_revision,
            payload={
                "action_id": action.action_id,
                "state": action.state.value,
                "synthetic_causal_event": True,
                "original_event_id": original_event.event_id,
            },
            first_receive_seq=original_event.sequence_range[0],
            last_receive_seq=original_event.sequence_range[1],
            coalesced_count=original_event.coalesced_count,
        )

    async def _commit_prepared(
        self,
        event: RuntimeEvent,
        prepared: PreparedResponse,
        evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence,
    ) -> DomainCommandResult[Any]:
        envelope = self._make_command(
            event,
            command_type="CommitInteraction",
            idempotency_key=f"cognitive:commit:{prepared.prepare_id}",
            expected_revision=self._cognitive_snapshot.revision,
            payload={"prepare_id": prepared.prepare_id, "evidence": evidence.model_dump(mode="json")},
        )
        result = await self._invoke_domain(
            service="cognitive",
            envelope=envelope,
            method=self.cognitive.commit_interaction,
            event=event,
        )
        self._cognitive_snapshot = await self.cognitive.get_snapshot()
        return result

    def _prepare_idempotency_key(self, event: RuntimeEvent) -> str:
        self._sync_command_index()
        base = f"cognitive:prepare:{event.event_id}"
        indexed_intent = self._prepare_intent_seqs.get(event.event_id)
        if indexed_intent is None:
            return base
        intent_seq, intent_count = indexed_intent
        latest = self._indexed_command_record(intent_seq)
        result_seq = self._command_result_seqs.get(latest.command_id)
        if result_seq is None:
            return latest.idempotency_key or base
        result_record = self._indexed_command_record(result_seq)
        if result_record.record_type == "domain_command_late_result":
            return f"{base}:reconcile:{intent_count}"
        try:
            result = result_record.data["result"]
            prepared = PreparedResponse.model_validate(result["result"])
        except (KeyError, TypeError, ValueError):
            prepared = None
        if prepared is not None and utc_now() < prepared.expires_at:
            return latest.idempotency_key or base
        return f"{base}:reconcile:{intent_count}"

    def _make_command(
        self,
        event: RuntimeEvent,
        *,
        command_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        expected_revision: int | None,
    ) -> DomainCommandEnvelope[dict[str, Any]]:
        self._sync_command_index()
        intent_seq = self._command_intent_seqs.get(idempotency_key)
        if intent_seq is not None:
            record = self._indexed_command_record(intent_seq)
            previous = record.data.get("envelope")
            if previous:
                envelope = DomainCommandEnvelope[dict[str, Any]].model_validate(previous)
                return self._renew_command_window(envelope)
        issued_at = utc_now()
        return DomainCommandEnvelope[dict[str, Any]](
            command_type=command_type,
            event_id=event.event_id,
            receive_seq=event.receive_seq,
            correlation_id=event.correlation_id,
            character_id=self.character_id,
            runtime_instance_id=self.runtime_instance_id,
            manifest_revision=self.manifest.revision,
            idempotency_key=idempotency_key,
            issued_at=issued_at,
            deadline=issued_at + timedelta(seconds=self.command_timeout_seconds),
            expected_revision=expected_revision,
            version_vector=self.version_vector,
            payload=payload,
        )

    def _sync_command_index(self) -> None:
        """Rebuild at startup or catch up only newly appended journal pages.

        The catch-up also covers emergency-buffer appends whose write raised,
        and direct journal appends used by recovery tools and test fixtures.
        """
        boundary = self.journal.last_record_seq
        while self._command_index_cursor < boundary:
            records = self.journal.records_after(
                self._command_index_cursor,
                limit=min(500, boundary - self._command_index_cursor),
            )
            if not records:
                raise RuntimeStateError("command journal index is missing its next record")
            for record in records:
                if record.record_seq != self._command_index_cursor + 1:
                    raise RuntimeStateError("command journal index sequence is not contiguous")
                if record.record_type == "domain_command_intent":
                    envelope = record.data.get("envelope")
                    if record.idempotency_key is not None and envelope:
                        self._command_intent_seqs[record.idempotency_key] = record.record_seq
                    if (
                        record.event_id is not None
                        and record.data.get("service") == "cognitive"
                        and isinstance(envelope, dict)
                        and envelope.get("command_type") == "PrepareResponse"
                    ):
                        _, count = self._prepare_intent_seqs.get(record.event_id, (0, 0))
                        self._prepare_intent_seqs[record.event_id] = (record.record_seq, count + 1)
                elif record.record_type in {"domain_command_result", "domain_command_late_result"}:
                    self._command_result_seqs[record.command_id] = record.record_seq
                elif record.record_type == "event_accepted" and record.event_id is not None:
                    event = record.data.get("event", {})
                    if event.get("event_type") == RuntimeEventType.QQ_TURN.value:
                        self._qq_turn_event_seqs[record.event_id] = record.record_seq
                        self._qq_turn_completed_seqs.pop(record.event_id, None)
                elif record.record_type == "processing_completed" and record.event_id in self._qq_turn_event_seqs:
                    self._qq_turn_completed_seqs[record.event_id] = record.record_seq
                self._command_index_cursor = record.record_seq

    def qq_turn_history(self, event_id: str):
        """Seek an exact QQ turn; new identities never scan historical payloads."""
        self._sync_command_index()
        sequence = self._qq_turn_event_seqs.get(event_id)
        if sequence is None:
            return None, None
        accepted = self._indexed_command_record(sequence)
        if accepted.record_type != "event_accepted" or accepted.event_id != event_id:
            raise RuntimeStateError("QQ event journal index binding is invalid")
        event = RuntimeEvent.model_validate(accepted.data["event"])
        if event.event_id != event_id or event.event_type != RuntimeEventType.QQ_TURN:
            raise RuntimeStateError("QQ event journal index source is invalid")
        completed_sequence = self._qq_turn_completed_seqs.get(event_id)
        if completed_sequence is None:
            return event, None
        completed = self._indexed_command_record(completed_sequence)
        if completed.record_type != "processing_completed" or completed.event_id != event_id:
            raise RuntimeStateError("QQ completion journal index binding is invalid")
        return event, completed.data

    def _indexed_command_record(self, record_seq: int) -> RuntimeJournalRecord:
        records = self.journal.records_after(record_seq - 1, limit=1)
        if not records or records[0].record_seq != record_seq:
            raise RuntimeStateError(f"command journal index points to missing record {record_seq}")
        return records[0]

    def _renew_command_window(
        self,
        envelope: DomainCommandEnvelope[dict[str, Any]],
    ) -> DomainCommandEnvelope[dict[str, Any]]:
        """Give an idempotent replay a usable transport window.

        Command identity and every request-fingerprint field stay unchanged;
        only the transport timestamps are renewed.  This lets a service return
        its durable idempotent result after Runtime restarts without reissuing
        the already-completed external effect.
        """

        issued_at = utc_now()
        return envelope.model_copy(
            update={
                "issued_at": issued_at,
                "deadline": issued_at + timedelta(seconds=self.command_timeout_seconds),
            }
        )

    async def _dispatch_qq_turn(self, event: RuntimeEvent, process_seq: int) -> EventDispatchResult:
        from harness.qq.shared import QQSharedPrepareRequest

        request = QQSharedPrepareRequest.model_validate(event.payload)
        remaining = (request.deadline_at - utc_now()).total_seconds()
        if remaining > 0:
            grace = ERROR_FALLBACK_SEND_SECONDS if remaining >= 1 else 0
            budget = asyncio.timeout(remaining + grace)
            try:
                # Web preparation, emotion observation, cognition and the final
                # snapshot share the body's immutable reply window. Cancelling
                # this waiter leaves Cognitive's shielded command/lock intact.
                async with budget:
                    result = await self._prepare_qq_turn(event, process_seq, request)
                if utc_now() < request.deadline_at or (
                    result.status == "completed" and _qq_error_fallback_payload(result.detail)
                ):
                    return result
            except (TimeoutError, LateDomainResultError):
                # A shorter RPC timeout or an early domain failure still uses
                # ordinary Runtime recovery. Only this turn's expiry is local.
                if not budget.expired() and utc_now() < request.deadline_at:
                    raise
        # Ignored is terminal in journal recovery: an expired preparation must
        # never be queued again or become a fresh send after resume/restart.
        return EventDispatchResult(event_id=event.event_id, receive_seq=event.receive_seq,
            process_seq=process_seq, status="ignored", decision_kind="qq_prepare",
            detail={"error_type": "qq_turn_expired"})

    @staticmethod
    def _qq_turn_expired(event: RuntimeEvent) -> bool:
        if event.event_type != RuntimeEventType.QQ_TURN:
            return False
        from harness.qq.shared import QQSharedPrepareRequest

        try:
            request = QQSharedPrepareRequest.model_validate(event.payload)
        except ValueError:
            return False
        return utc_now() >= request.deadline_at

    async def _prepare_qq_turn(self, event: RuntimeEvent, process_seq: int, request) -> EventDispatchResult:
        method = getattr(self.cognitive, "prepare_qq_response", None)
        if method is None:
            raise DomainCommandError("shared cognitive QQ preparation is unavailable")
        key = f"cognitive:qq:{event.event_id}"
        self._sync_command_index()
        payload = request.model_dump(mode="json")
        if key not in self._command_intent_seqs:
            # Only the shared Runtime owns the current expression projection.
            # Freeze it with the first command's VersionVector; retries below
            # reuse the durable envelope, even after Homeostasis advances.
            summary = self._response_homeostatic_interoception()
            if summary is not None and summary["salience"] == "critical":
                raise DomainCommandError("critical homeostatic state must pause before QQ cognition")
            if request.idle_intent is not None:
                if summary is None:
                    raise DomainCommandError("QQ idle expression requires responses or full mode")
                if request.idle_intent.homeostatic_interoception.model_dump(mode="json") != summary:
                    raise DomainCommandError("QQ idle interoception differs from the current trusted summary")
            # Ordinary body-supplied state is replaced, not trusted. Clearing
            # it in off/shadow also prevents indirect user-JSON influence.
            payload["homeostatic_interoception"] = summary
            from harness.runtime.web_dialogue import prepare_web_context
            web_context, _ = await prepare_web_context(self, event, channel="qq", deadline=request.deadline_at)
            if web_context is not None:
                payload["runtime_web_context"] = web_context
            goal_context = self.goals.qq_context(request) if self.goals.enabled else None
            if goal_context is not None:
                payload["runtime_goal_context"] = goal_context
        # QQ preparation reads the current owner under Cognitive's command lock.
        # Background memory commits can advance it after Runtime's last snapshot;
        # this request carries no write computed from that stale snapshot.
        envelope = self._make_command(event, command_type="PrepareQQResponse",
            idempotency_key=key, expected_revision=None, payload=payload)
        envelope = envelope.model_copy(update={"deadline": min(envelope.deadline, request.deadline_at)})
        self._journal_homeostatic_route(event=event, command_id=envelope.command_id,
            target="qq_response", summary=envelope.payload.get("homeostatic_interoception"))
        result = await self._invoke_domain(service="cognitive", envelope=envelope, method=method, event=event)
        current = await asyncio.wait_for(self.cognitive.get_snapshot(), timeout=max(0.001,
            (request.deadline_at - utc_now()).total_seconds()))
        self._validate_snapshot(current, "cognitive")
        if current.revision < max(self._cognitive_snapshot.revision, result.committed_revision or 0):
            raise DomainCommandError("shared QQ cognitive snapshot regressed")
        self._cognitive_snapshot = current
        # This is preparation only. Neither the local chat delivery backend nor
        # Homeostasis/CommitInteraction is invoked for a QQ preparation event.
        if not isinstance(result.result, dict) or set(result.result) != {"decision", "trace"}:
            raise DomainCommandError("shared QQ cognitive result is invalid")
        from harness.qq.decision import QQReplyDecision

        decision = QQReplyDecision.model_validate(result.result["decision"])
        trace = result.result["trace"]
        if ((decision.group_id, decision.self_member_id, decision.trace_id) !=
                (request.group_id, request.self_member_id, request.trace.trace_id)
                or not isinstance(trace, dict) or trace.get("trace_id") != request.trace.trace_id
                or trace.get("event_id") != request.trace.event_id):
            raise DomainCommandError("shared QQ cognitive result binding is invalid")
        return EventDispatchResult(event_id=event.event_id, receive_seq=event.receive_seq,
            process_seq=process_seq, status="completed", decision_kind="qq_prepare", detail=result.result)

    async def _invoke_domain(
        self,
        *,
        service: str,
        envelope: DomainCommandEnvelope[dict[str, Any]],
        method: Callable[[DomainCommandEnvelope[dict[str, Any]]], Awaitable[DomainCommandResult[Any]]],
        event: RuntimeEvent,
    ) -> DomainCommandResult[Any]:
        if service == "cognitive":
            from harness.runtime.emotion import with_emotion_context

            envelope = await with_emotion_context(self, event, envelope)
        self._append_journal(
            "domain_command_intent",
            event=event,
            command_id=envelope.command_id,
            idempotency_key=envelope.idempotency_key,
            data={"service": service, "envelope": envelope.model_dump(mode="json")},
        )
        timeout = max(0.001, (envelope.deadline - utc_now()).total_seconds())
        if event.event_type == RuntimeEventType.QQ_TURN and timeout >= 1:
            timeout += ERROR_FALLBACK_SEND_SECONDS
        try:
            result = await asyncio.wait_for(method(envelope), timeout=timeout)
            if not self._qq_turn_expired(event):
                self.watchdog.record_success(service)
                self._degraded_reasons = [reason for reason in self._degraded_reasons if reason != service]
        except Exception as exc:
            # Losing a QQ reply window is not evidence that a dependency is
            # unhealthy. Preserve earlier failures rather than clearing them.
            if not (isinstance(exc, TimeoutError) and self._qq_turn_expired(event)):
                self.watchdog.record_failure(service)
                if service not in self._degraded_reasons:
                    self._degraded_reasons.append(service)
            raise
        late = result.completed_at > envelope.deadline
        record_type = "domain_command_late_result" if late else "domain_command_result"
        self._append_journal(
            record_type,
            event=event,
            command_id=envelope.command_id,
            idempotency_key=envelope.idempotency_key,
            data={"service": service, "result": result.model_dump(mode="json")},
        )
        if event.event_type == RuntimeEventType.QQ_TURN and result.status not in {"applied", "idempotent_replay"}:
            # A late failed/conflicting result still reports a real domain
            # failure. Only the owner's explicit deadline results can become
            # turn expiry, including a result completed on time but received
            # after the body's deadline.
            deadline_failure = service == "cognitive" and (
                (result.status == "rejected" and result.error == "command deadline expired")
                or (envelope.command_type == "PrepareQQResponse" and result.status == "failed"
                    and result.error == "qq_shared_prepare_failed:QQDeadlineExpired")
            )
            if deadline_failure and self._qq_turn_expired(event):
                raise TimeoutError("qq_turn_expired")
            raise DomainCommandError(
                f"{service} {envelope.command_type} returned {result.status}: {result.error}"
            )
        if late and not (
            event.event_type == RuntimeEventType.QQ_TURN and _qq_error_fallback_payload(result.result)
        ):
            raise LateDomainResultError(f"late {service} result for {envelope.command_type}")
        if result.status not in {"applied", "idempotent_replay"}:
            raise DomainCommandError(
                f"{service} {envelope.command_type} returned {result.status}: {result.error}"
            )
        if service == "homeostasis":
            self.goals.record_homeostasis(envelope, result)
        return result

    async def _check_services(self) -> tuple[str, ...]:
        cognitive_health, homeostasis_health = await asyncio.gather(
            asyncio.wait_for(self.cognitive.health(), timeout=self.command_timeout_seconds),
            asyncio.wait_for(self.homeostasis.health(), timeout=self.command_timeout_seconds),
        )
        self._validate_health(cognitive_health, "cognitive")
        if getattr(self.manifest, "emotion_mode", "off") != cognitive_health.detail.get("emotion_mode", "off"):
            raise DomainCommandError("Runtime and Cognitive emotion rollout modes differ")
        health_watchdog = self._homeostasis_watchdog_reasons(homeostasis_health.detail)
        projection_errors = homeostasis_health.detail.get("projection_errors")
        watchdog_only = bool(health_watchdog) and not projection_errors
        self._validate_health(
            homeostasis_health,
            "homeostasis",
            allow_unhealthy=watchdog_only,
        )
        self._cognitive_snapshot, self._homeostasis_snapshot = await asyncio.gather(
            asyncio.wait_for(self.cognitive.get_snapshot(), timeout=self.command_timeout_seconds),
            asyncio.wait_for(self.homeostasis.get_snapshot(), timeout=self.command_timeout_seconds),
        )
        self._validate_snapshot(self._cognitive_snapshot, "cognitive")
        self._validate_snapshot(self._homeostasis_snapshot, "homeostasis")
        self._validate_homeostasis_contract(self._homeostasis_snapshot)
        snapshot_watchdog = self._homeostasis_watchdog_reasons(
            self._homeostasis_snapshot.payload
        )
        if snapshot_watchdog != health_watchdog:
            raise DomainCommandError(
                "homeostasis health/snapshot watchdog state is inconsistent"
            )
        if self.manifest.homeostatic_expression.mode != "off":
            summary = self._validated_homeostatic_interoception(required=True)
            if (summary or {}).get("salience") == "critical" and not snapshot_watchdog:
                raise DomainCommandError(
                    "critical homeostatic interoception has no watchdog evidence"
                )
            if snapshot_watchdog and (summary or {}).get("salience") != "critical":
                raise DomainCommandError(
                    "homeostasis watchdog is missing critical interoceptive salience"
                )
        self._degraded_reasons = [
            reason
            for reason in self._degraded_reasons
            if reason not in {"cognitive", "homeostasis"}
        ]
        if snapshot_watchdog:
            self._degraded_reasons.append("homeostasis")
        cursor_floor = max(self._cognitive_snapshot.acknowledged_receive_seq,
                           self._homeostasis_snapshot.acknowledged_receive_seq)
        # A retained domain can predate this Runtime journal. New ingress must
        # start above both durable cursors; old event identities stay unchanged.
        async with self.ingress._sequence_lock:
            if cursor_floor > self._receive_seq:
                previous = self._receive_seq
                self._receive_seq = cursor_floor
                self._append_journal("ingress_cursor_floor", data={"previous":previous, "receive_seq":cursor_floor})
        self._append_journal(
            "service_reconciled",
            data={
                "cognitive": self._cognitive_snapshot.model_dump(mode="json"),
                "homeostasis": self._homeostasis_snapshot.model_dump(mode="json"),
            },
        )
        return snapshot_watchdog

    async def _defer_reconciliation_for_homeostasis_watchdog(
        self,
        reasons: tuple[str, ...],
        *,
        phase: str,
    ) -> None:
        self._homeostasis_watchdog_signature = (
            self._homeostasis_snapshot.revision,
            reasons,
        )
        reason = (
            "recovery complete; homeostasis watchdog requires pause; "
            "reconciliation deferred: " + ", ".join(reasons)
        )
        self._append_journal(
            "reconciliation_deferred_homeostasis_watchdog",
            data={
                "phase": phase,
                "homeostasis_revision": self._homeostasis_snapshot.revision,
                "reasons": list(reasons),
            },
        )
        if self.lifecycle == RuntimeLifecycle.RECOVERING:
            await self._transition(RuntimeLifecycle.PAUSED, reason=reason, durable=True)

    @staticmethod
    def _validate_health(
        health: ServiceHealth,
        expected_service: str,
        *,
        allow_unhealthy: bool = False,
    ) -> None:
        if health.service != expected_service:
            raise DomainCommandError(
                f"expected {expected_service} service, received {health.service}"
            )
        if not health.healthy and not allow_unhealthy:
            raise DomainCommandError(f"{expected_service} service is unhealthy")
        if health.schema_version != CONTRACT_SCHEMA_VERSION:
            raise DomainCommandError(
                f"{expected_service} schema {health.schema_version} is incompatible with "
                f"{CONTRACT_SCHEMA_VERSION}"
            )

    @staticmethod
    def _validate_snapshot(snapshot: DomainSnapshot, expected_service: str) -> None:
        if snapshot.service != expected_service:
            raise DomainCommandError(
                f"expected {expected_service} snapshot, received {snapshot.service}"
            )
        if snapshot.schema_version != CONTRACT_SCHEMA_VERSION:
            raise DomainCommandError(
                f"{expected_service} snapshot schema {snapshot.schema_version} is incompatible"
            )

    def _validate_homeostasis_contract(self, snapshot: DomainSnapshot) -> None:
        payload = snapshot.payload
        nested_state = payload.get("snapshot")
        state = nested_state if isinstance(nested_state, dict) else payload
        if self.manifest.homeostatic_expression.mode != "off":
            self._validated_interoceptive_summary_from_payload(
                payload,
                expected_revision=snapshot.revision,
                required=True,
            )
        profile = payload.get("profile")
        if not isinstance(profile, dict):
            if self.manifest.homeostatic_profile == "passive-v1":
                return
            if isinstance(nested_state, dict):
                profile_id = state.get("profile_id")
                profile_revision = state.get("profile_revision")
                if not isinstance(profile_id, str) or not isinstance(profile_revision, int):
                    raise DomainCommandError(
                        "homeostasis result snapshot profile identity is incomplete"
                    )
                result_profile_ref = f"{profile_id}@{profile_revision}"
                if result_profile_ref != self.manifest.homeostatic_profile:
                    raise DomainCommandError(
                        "homeostasis result snapshot profile does not match the manifest"
                    )
                if state.get("schema_version") != self.manifest.schema_version:
                    raise DomainCommandError(
                        "homeostasis result snapshot schema does not match the manifest"
                    )
                return
            raise DomainCommandError("homeostasis snapshot is missing its profile")
        profile_id = profile.get("profile_id")
        profile_revision = profile.get("revision")
        if not isinstance(profile_id, str) or not isinstance(profile_revision, int):
            raise DomainCommandError("homeostasis snapshot profile identity is incomplete")
        if (
            state.get("profile_id") != profile_id
            or state.get("profile_revision") != profile_revision
        ):
            raise DomainCommandError(
                "homeostasis state identity does not match its embedded profile"
            )
        expected_profile = f"{profile_id}@{profile_revision}"
        contracts = {
            "homeostatic_profile": expected_profile,
            "schema_version": state.get("schema_version"),
            "persona_version": profile.get("persona_version"),
            "a_version": profile.get("observation_model_version"),
            "b_version": profile.get("transition_model_version"),
            "c_version": profile.get("preference_model_version"),
            "precision_version": profile.get("precision_profile_version"),
            "coupling_version": profile.get("coupling_matrix_version"),
        }
        mismatches = [
            f"{field}={getattr(self.manifest, field)!r}, profile={value!r}"
            for field, value in contracts.items()
            if getattr(self.manifest, field) != value
        ]
        if mismatches:
            raise DomainCommandError(
                "homeostasis profile/manifest contract mismatch: " + "; ".join(mismatches)
            )

    async def _update_homeostasis(
        self,
        result: DomainCommandResult[Any],
        *,
        event: RuntimeEvent,
    ) -> bool:
        previous = self._homeostasis_snapshot
        effective_result = result
        if result.status == "idempotent_replay":
            authoritative = await asyncio.wait_for(
                self.homeostasis.get_snapshot(),
                timeout=self.command_timeout_seconds,
            )
            self._validate_snapshot(authoritative, "homeostasis")
            replay_revision = int(result.committed_revision or 0)
            if authoritative.revision < max(previous.revision, replay_revision):
                raise DomainCommandError(
                    "authoritative homeostasis snapshot regressed during idempotent replay"
                )
            if self.manifest.homeostatic_expression.mode != "off":
                self._validate_homeostasis_contract(authoritative)
            self._homeostasis_snapshot = authoritative
            effective_result = result.model_copy(
                update={
                    "committed_revision": authoritative.revision,
                    "acknowledged_receive_seq": authoritative.acknowledged_receive_seq,
                    "acknowledged_event_id": authoritative.acknowledged_event_id,
                    "result": authoritative.payload,
                }
            )
            self._append_journal(
                "homeostasis_idempotent_replay_refreshed",
                event=event,
                data={
                    "replayed_revision": replay_revision,
                    "previous_runtime_revision": previous.revision,
                    "authoritative_revision": authoritative.revision,
                },
            )
        else:
            revision = int(result.committed_revision or previous.revision)
            if revision < previous.revision:
                raise DomainCommandError(
                    "homeostasis result revision regressed below the Runtime snapshot"
                )
            candidate = previous.model_copy(
                update={
                    "revision": revision,
                    "acknowledged_receive_seq": max(
                        previous.acknowledged_receive_seq,
                        int(result.acknowledged_receive_seq or 0),
                    ),
                    "acknowledged_event_id": (
                        result.acknowledged_event_id or previous.acknowledged_event_id
                    ),
                    "component_revisions": {"state": revision},
                    "payload": result.result or {},
                }
            )
            if self.manifest.homeostatic_expression.mode != "off":
                self._validate_homeostasis_contract(candidate)
            self._homeostasis_snapshot = candidate
        reasons = await self._route_homeostasis_watchdog(effective_result, event=event)
        mode = self.manifest.homeostatic_expression.mode
        if mode != "off":
            summary = self._validated_homeostatic_interoception(required=True)
            assert summary is not None
            if (summary or {}).get("salience") == "critical" and not reasons:
                raise DomainCommandError(
                    "critical homeostatic interoception has no watchdog evidence"
                )
            if reasons and (summary or {}).get("salience") != "critical":
                raise DomainCommandError(
                    "homeostasis watchdog is missing critical interoceptive salience"
                )
            causal = self._homeostatic_causal_fields(summary)
            self._append_journal(
                "homeostatic_interoception_observed",
                event=event,
                data={"rollout_mode": mode, **causal},
            )
            if not reasons and mode == "full":
                await self._maybe_schedule_marked_trigger(
                    event,
                    summary,
                    source_revision=previous.revision,
                )
        return bool(reasons)

    async def _route_homeostasis_watchdog(
        self,
        result: DomainCommandResult[Any],
        *,
        event: RuntimeEvent,
    ) -> tuple[str, ...]:
        payload = result.result if isinstance(result.result, dict) else {}
        reasons = self._homeostasis_watchdog_reasons(payload)
        if not reasons:
            self._homeostasis_watchdog_signature = None
            return ()
        if "homeostasis" not in self._degraded_reasons:
            self._degraded_reasons.append("homeostasis")
        revision = int(
            result.committed_revision or self._homeostasis_snapshot.revision
        )
        signature = (revision, reasons)
        if signature == self._homeostasis_watchdog_signature:
            return reasons
        decision = WatchdogDecision(
            action=WatchdogAction.PAUSE,
            reason="homeostasis reported an unsafe or unstable state",
            evidence={
                "homeostasis_revision": revision,
                "reasons": list(reasons),
            },
            observed_at=result.completed_at,
        )
        serialized = decision.model_dump(mode="json")
        self._append_journal("watchdog_decision", event=event, data=serialized)
        await self._publish("watchdog", serialized)
        self._homeostasis_watchdog_signature = signature
        self._cancel_pending_autonomous(
            reason="homeostasis watchdog requires an immediate pause",
            event=event,
        )
        if self.lifecycle == RuntimeLifecycle.RUNNING:
            await self.pause(reason=decision.reason)
            self._append_journal(
                "homeostasis_watchdog_paused_synchronously",
                event=event,
                data={
                    "homeostasis_revision": revision,
                    "reasons": list(reasons),
                    "lifecycle": self.lifecycle.value,
                },
            )
        return reasons

    def _validated_interoceptive_summary_from_payload(
        self,
        payload: dict[str, Any],
        *,
        expected_revision: int,
        required: bool,
    ) -> dict[str, Any] | None:
        top_level = payload.get("interoceptive_summary")
        snapshot = payload.get("snapshot")
        embedded: Any = None
        if isinstance(snapshot, dict):
            embedded = snapshot.get("interoceptive_summary")
            if required and (not isinstance(top_level, dict) or not isinstance(embedded, dict)):
                raise DomainCommandError(
                    "homeostasis result must carry matching top-level and snapshot summaries"
                )
            if isinstance(top_level, dict) and isinstance(embedded, dict) and top_level != embedded:
                raise DomainCommandError(
                    "homeostasis top-level and snapshot interoceptive summaries differ"
                )
        summary = top_level if isinstance(top_level, dict) else embedded
        validated = self._validate_interoceptive_summary(
            summary if isinstance(summary, dict) else None,
            expected_revision=expected_revision,
            required=required,
        )
        if validated is None:
            return None

        state = snapshot if isinstance(snapshot, dict) else payload
        state_profile_id = state.get("profile_id")
        state_profile_revision = state.get("profile_revision")
        if state_profile_id is not None or state_profile_revision is not None:
            if not isinstance(state_profile_id, str) or not isinstance(
                state_profile_revision, int
            ):
                raise DomainCommandError(
                    "homeostasis state profile identity is incomplete"
                )
            embedded_profile_ref = f"{state_profile_id}@{state_profile_revision}"
            if validated.get("profile_ref") != embedded_profile_ref:
                raise DomainCommandError(
                    "homeostatic interoception profile does not match its snapshot"
                )
        return validated

    def _validate_interoceptive_summary(
        self,
        summary: dict[str, Any] | None,
        *,
        expected_revision: int,
        required: bool,
    ) -> dict[str, Any] | None:
        if summary is None:
            if required:
                raise DomainCommandError(
                    "homeostasis response is missing homeostatic interoception"
                )
            return None
        if summary.get("schema_version") != "homeostatic-interoception-v1":
            raise DomainCommandError("homeostatic interoception schema is incompatible")
        if summary.get("profile_ref") != self.manifest.homeostatic_profile:
            raise DomainCommandError(
                "homeostatic interoception profile does not match the manifest"
            )
        revision = summary.get("snapshot_revision")
        if not isinstance(revision, int) or revision != expected_revision:
            raise DomainCommandError(
                "homeostatic interoception revision does not match VersionVector.homeostasis_state"
            )
        if summary.get("salience") not in {"nominal", "noticeable", "marked", "critical"}:
            raise DomainCommandError("homeostatic interoception salience is invalid")
        signature = summary.get("episode_signature")
        if not isinstance(signature, str) or not signature.strip():
            raise DomainCommandError("homeostatic interoception episode signature is missing")
        return summary

    def _validated_homeostatic_interoception(
        self,
        *,
        required: bool,
    ) -> dict[str, Any] | None:
        return self._validated_interoceptive_summary_from_payload(
            self._homeostasis_snapshot.payload,
            expected_revision=self.version_vector.homeostasis_state,
            required=required,
        )

    def _response_homeostatic_interoception(self) -> dict[str, Any] | None:
        if self.manifest.homeostatic_expression.mode not in {"responses", "full"}:
            return None
        return self._validated_homeostatic_interoception(required=True)

    def _homeostasis_payload_for_cognitive(self) -> dict[str, Any]:
        payload = self._homeostasis_snapshot.payload
        if self.manifest.homeostatic_expression.mode in {"responses", "full"}:
            return payload
        # In off/shadow modes the semantic projection must not influence either
        # autonomous assessment or the main model through the legacy snapshot.
        sanitized = dict(payload)
        sanitized.pop("interoceptive_summary", None)
        snapshot = sanitized.get("snapshot")
        if isinstance(snapshot, dict):
            sanitized_snapshot = dict(snapshot)
            sanitized_snapshot.pop("interoceptive_summary", None)
            sanitized["snapshot"] = sanitized_snapshot
        return sanitized

    @staticmethod
    def _homeostatic_causal_fields(
        summary: dict[str, Any] | None,
    ) -> dict[str, Any]:
        if summary is None:
            return {}
        canonical = json.dumps(
            summary,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return {
            "homeostasis_revision": int(summary["snapshot_revision"]),
            "homeostatic_summary_hash": hashlib.sha256(canonical).hexdigest(),
            "homeostatic_salience": str(summary["salience"]),
            "homeostatic_episode_signature": str(summary["episode_signature"]),
        }

    def _journal_homeostatic_route(
        self,
        *,
        event: RuntimeEvent,
        command_id: str,
        target: str,
        summary: dict[str, Any] | None,
    ) -> None:
        if summary is None:
            return
        self._append_journal(
            "homeostatic_interoception_routed",
            event=event,
            command_id=command_id,
            data={"target": target, **self._homeostatic_causal_fields(summary)},
        )

    async def _maybe_schedule_marked_trigger(
        self,
        event: RuntimeEvent | None,
        summary: dict[str, Any],
        *,
        source_revision: int,
    ) -> None:
        # Freeze the user-activity baseline before policy proposal yields.  For
        # a causal event, a user receive sequence newer than that event must not
        # be treated as preceding the autonomous trigger merely because it
        # arrived while Homeostasis or policy proposal was awaiting I/O.
        scheduled_after_user_receive_seq = self._causal_user_receive_seq(event)
        if not self.manifest.autonomous_chat_enabled or self.manifest.homeostatic_expression.mode != "full":
            return
        salience = str(summary["salience"])
        signature = str(summary["episode_signature"])
        self._homeostatic_salience = salience
        self._homeostatic_episode_signature = signature
        if salience != "marked":
            return

        now = utc_now()
        is_user_opportunity = (
            event is not None and event.event_type == RuntimeEventType.USER_MESSAGE
        )
        is_idle_opportunity = (
            event is not None
            and event.event_type == RuntimeEventType.IDLE
            and event.payload.get("trigger_reason") == "homeostasis_marked"
        )
        if is_idle_opportunity and event is not None:
            scheduled_revision = event.payload.get("source_homeostasis_revision")
            source_signature = event.payload.get("source_episode_signature")
            is_idle_opportunity = (
                isinstance(scheduled_revision, int)
                and scheduled_revision == source_revision
                and source_signature == signature
            )
        if is_user_opportunity or is_idle_opportunity:
            self._homeostatic_marked_attempts[signature] = now
            self._append_journal(
                "homeostatic_marked_expression_opportunity",
                event=event,
                data={
                    "opportunity": event.event_type.value if event is not None else "recovery",
                    **self._homeostatic_causal_fields(summary),
                },
            )
            return

        if self.lifecycle in {RuntimeLifecycle.BOOTING, RuntimeLifecycle.RECOVERING}:
            self._append_journal(
                "homeostatic_marked_trigger_deferred_lifecycle",
                event=event,
                data={
                    "lifecycle": self.lifecycle.value,
                    **self._homeostatic_causal_fields(summary),
                },
            )
            return

        last_attempt = self._homeostatic_marked_attempts.get(signature)
        cooldown = timedelta(
            seconds=self.manifest.homeostatic_expression.marked_cooldown_seconds
        )
        cooldown_elapsed = last_attempt is None or now - last_attempt >= cooldown
        # A prior attempt for the same episode always owns the cooldown window.
        # Crossing out of and back into ``marked`` must not reset it; only a new
        # signature (which has no attempt entry) is immediately eligible.
        if not cooldown_elapsed:
            return

        pending_marked = next(
            (
                queued
                for queued in self.queue.snapshot()
                if queued.event_type == RuntimeEventType.IDLE
                and queued.payload.get("trigger_reason") == "homeostasis_marked"
                and queued.payload.get("source_episode_signature") == signature
            ),
            None,
        )
        if pending_marked is not None and last_attempt is None:
            self._homeostatic_marked_attempts[signature] = now
            self._append_journal(
                "homeostatic_marked_trigger_reused",
                event=event,
                data={
                    "trigger_event_id": pending_marked.event_id,
                    **self._homeostatic_causal_fields(summary),
                },
            )
            return

        try:
            policies = await self.homeostasis.propose_policies(
                version_vector=self.version_vector
            )
        except Exception as exc:
            self._append_journal(
                "homeostatic_marked_trigger_failed",
                event=event,
                data={
                    "error": str(exc).strip() or type(exc).__name__,
                    **self._homeostatic_causal_fields(summary),
                },
            )
            return
        payload: dict[str, Any] = {
            "mode": "autonomous",
            "trigger_reason": "homeostasis_marked",
            "policies": policies,
            "scheduled_after_user_receive_seq": scheduled_after_user_receive_seq,
            "channel": self._last_user_channel,
            "source_homeostasis_revision": summary["snapshot_revision"],
            "source_episode_signature": signature,
        }
        if self._last_user_session_id is not None:
            payload["session_id"] = self._last_user_session_id
        scheduled = await self.submit_event(
            event_type=RuntimeEventType.IDLE,
            # This priority prevents a continuous perception stream from
            # starving a marked expression, while CONTROL and USER stay ahead.
            priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT,
            source="homeostasis",
            payload=payload,
        )
        if scheduled is None:
            return
        self._homeostatic_marked_attempts[signature] = now
        self._append_journal(
            "homeostatic_marked_trigger_scheduled",
            event=event,
            data={
                "trigger_event_id": scheduled.event_id,
                "priority": int(scheduled.priority),
                "cooldown_seconds": (
                    self.manifest.homeostatic_expression.marked_cooldown_seconds
                ),
                **self._homeostatic_causal_fields(summary),
            },
        )

    async def _schedule_current_marked_trigger(self) -> None:
        if self.manifest.homeostatic_expression.mode != "full":
            return
        if self._pending_user_ingress_event_ids:
            return
        current = self._response_homeostatic_interoception()
        if current is None or current.get("salience") != "marked":
            return
        pending_user = next(
            (
                queued
                for queued in self.queue.snapshot()
                if queued.event_type == RuntimeEventType.USER_MESSAGE
            ),
            None,
        )
        if pending_user is not None:
            # A restored or resumed user message owns the next expression
            # opportunity and will receive this exact authoritative summary.
            await self._maybe_schedule_marked_trigger(
                pending_user,
                current,
                source_revision=self._homeostasis_snapshot.revision,
            )
            return
        await self._maybe_schedule_marked_trigger(
            None,
            current,
            source_revision=self._homeostasis_snapshot.revision,
        )

    def _homeostatic_marked_trigger_ineligible(
        self,
        event: RuntimeEvent,
        *,
        source_revision: int,
        summary: dict[str, Any] | None,
    ) -> str | None:
        if event.payload.get("trigger_reason") != "homeostasis_marked":
            return None
        scheduled_revision = event.payload.get("source_homeostasis_revision")
        scheduled_signature = event.payload.get("source_episode_signature")
        if not isinstance(scheduled_revision, int) or scheduled_revision != source_revision:
            return "homeostasis-marked trigger source revision is stale"
        if not isinstance(scheduled_signature, str) or not scheduled_signature:
            return "homeostasis-marked trigger source signature is missing"
        if summary is None or summary.get("salience") != "marked":
            return "homeostasis is no longer marked"
        if summary.get("episode_signature") != scheduled_signature:
            return "homeostasis-marked episode changed while the trigger was queued"
        return None

    def _ordinary_idle_marked_cooldown_reason(
        self,
        event: RuntimeEvent,
        *,
        summary: dict[str, Any] | None,
    ) -> str | None:
        if event.payload.get("trigger_reason") == "homeostasis_marked":
            return None
        if summary is None or summary.get("salience") != "marked":
            return None
        signature = summary.get("episode_signature")
        if not isinstance(signature, str) or not signature:
            return None
        last_attempt = self._homeostatic_marked_attempts.get(signature)
        if last_attempt is None:
            return None
        cooldown = timedelta(
            seconds=self.manifest.homeostatic_expression.marked_cooldown_seconds
        )
        if utc_now() - last_attempt >= cooldown:
            return None
        return "ordinary idle is suppressed by the homeostasis-marked episode cooldown"

    def _cancel_pending_autonomous(
        self,
        *,
        reason: str,
        event: RuntimeEvent,
    ) -> None:
        for queued in self.queue.snapshot():
            if queued.event_type != RuntimeEventType.IDLE:
                continue
            if queued.event_id in self._cancelled_autonomous_event_ids:
                continue
            self._cancelled_autonomous_event_ids.add(queued.event_id)
            self._append_journal(
                "autonomous_expression_cancelled",
                event=event,
                data={
                    "cancelled_event_id": queued.event_id,
                    "reason": reason,
                    "trigger_reason": queued.payload.get("trigger_reason"),
                },
            )

    async def _defer_user_after_homeostasis(
        self,
        event: RuntimeEvent,
        causal_context: dict[str, Any],
    ) -> None:
        revision = self._homeostasis_snapshot.revision
        self._homeostasis_continuations[event.event_id] = revision
        self._append_journal(
            "homeostasis_continuation_deferred",
            event=event,
            data={"homeostasis_revision": revision, **causal_context},
        )
        self.journal.flush()
        await self.queue.put(event)
        self._append_journal(
            "event_requeued",
            event=event,
            data={"reason": "homeostasis_watchdog_pause", "resume_stage": "cognitive"},
        )

    @staticmethod
    def _homeostasis_watchdog_reasons(payload: dict[str, Any]) -> tuple[str, ...]:
        state = payload.get("snapshot") if isinstance(payload.get("snapshot"), dict) else payload
        raw_reasons = state.get("watchdog") if isinstance(state, dict) else None
        if not isinstance(raw_reasons, list):
            return ()
        return tuple(sorted({str(reason) for reason in raw_reasons if str(reason)}))

    async def _high_impact_barrier(
        self, action: ActionExecution
    ) -> tuple[bool, VersionVector, str | None]:
        if not self._journal_writable:
            return False, self.version_vector, "runtime journal is degraded"
        try:
            cognitive, homeostasis = await asyncio.gather(
                self.cognitive.get_snapshot(), self.homeostasis.get_snapshot()
            )
        except Exception as exc:
            return False, self.version_vector, f"snapshot barrier failed: {exc}"
        context = self._action_context.get(action.action_id)
        if context is not None:
            event: RuntimeEvent = context["event"]
            if cognitive.acknowledged_receive_seq < event.receive_seq:
                return False, self.version_vector, "cognitive service has not acknowledged event"
            if homeostasis.acknowledged_receive_seq < event.receive_seq:
                return False, self.version_vector, "homeostasis service has not acknowledged event"
        self._cognitive_snapshot = cognitive
        self._homeostasis_snapshot = homeostasis
        return True, self.version_vector, None

    async def _transition(
        self,
        target: RuntimeLifecycle,
        *,
        reason: str,
        durable: bool = False,
    ) -> LifecycleTransition:
        async with self._lifecycle_lock:
            previous = self.lifecycle
            if target == previous:
                return LifecycleTransition(previous=previous, current=target, reason=reason)
            if target not in _TRANSITIONS[previous]:
                raise RuntimeStateError(f"invalid lifecycle transition {previous} -> {target}")
            transition = LifecycleTransition(previous=previous, current=target, reason=reason)
            self._append_journal("lifecycle_transition", data=transition.model_dump(mode="json"))
            if durable:
                self.journal.flush()
            self.lifecycle = target
            self.lifecycle_since = transition.at
            self.lifecycle_reason = reason
        await self.queue.wake()
        await self._publish("lifecycle", transition.model_dump(mode="json"))
        return transition

    def _append_journal(
        self,
        record_type: str,
        *,
        event: RuntimeEvent | None = None,
        process_seq: int | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> RuntimeJournalRecord | None:
        try:
            record = self.journal.append(
                record_type,
                event_id=event.event_id if event else None,
                receive_seq=event.receive_seq if event else None,
                process_seq=process_seq,
                correlation_id=event.correlation_id if event else None,
                command_id=command_id,
                idempotency_key=idempotency_key,
                data=data,
            )
            self._sync_command_index()
            self._journal_writable = not self.journal.degraded
            return record
        except JournalUnavailableError:
            self._sync_command_index()
            self._journal_writable = False
            if "journal" not in self._degraded_reasons:
                self._degraded_reasons.append("journal")
            if self.journal.buffered_records >= self.journal.emergency_buffer_size:
                raise
            return None

    async def _publish(self, topic: str, payload: dict[str, Any]) -> None:
        for listener in list(self._listeners):
            try:
                result = listener(topic, payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                # Observers are non-authoritative and cannot stop Runtime.
                continue

    def _remember_user_event(self, event: RuntimeEvent) -> None:
        if event.receive_seq < self._last_user_receive_seq:
            return
        self._last_user_receive_seq = event.receive_seq
        session_id = str(event.payload.get("session_id") or "").strip()
        if session_id:
            self._last_user_session_id = session_id
        channel = str(event.payload.get("channel") or "").strip()
        if channel:
            self._last_user_channel = channel

    def _causal_user_receive_seq(self, event: RuntimeEvent | None) -> int:
        """Freeze the newest user sequence that causally precedes an event."""

        if event is None:
            return self._last_user_receive_seq
        return min(self._last_user_receive_seq, event.receive_seq)

    def _autonomous_ineligible(self, event: RuntimeEvent) -> str | None:
        if not self.manifest.autonomous_chat_enabled:
            return "autonomous chat is disabled"
        if event.event_id in self._cancelled_autonomous_event_ids:
            return "autonomous output was cancelled"
        if self.lifecycle != RuntimeLifecycle.RUNNING:
            return f"runtime lifecycle is {self.lifecycle.value}"
        if self._pending_user_ingress_event_ids:
            return "new user activity is entering the queue"
        if self._agency_state != AgencyState.AVAILABLE:
            return f"agency state is {self._agency_state.value}"
        if self.action_coordinator.current is not None:
            return "foreground action is active"
        baseline = int(event.payload.get("scheduled_after_user_receive_seq") or 0)
        if self._last_user_receive_seq > baseline:
            return "new user activity superseded autonomous output"
        if any(
            queued.event_type == RuntimeEventType.USER_MESSAGE
            for queued in self.queue.snapshot()
        ):
            return "a user message is waiting"
        if any(
            queued.priority <= RuntimeEventPriority.USER
            for queued in self.queue.snapshot()
        ):
            return "a foreground event is waiting"
        return None

    def _dispatch_predicate(self) -> Callable[[RuntimeEvent], bool]:
        if self.lifecycle == RuntimeLifecycle.RUNNING:
            return lambda event: True
        if self.lifecycle == RuntimeLifecycle.FAULTED:
            allowed = {
                RuntimeEventType.DIAGNOSTIC,
                RuntimeEventType.STOP,
                RuntimeEventType.RECOVER,
            }
            return lambda event: event.event_type in allowed
        return lambda event: event.event_type in CONTROL_EVENT_TYPES

    def _restore_runtime_indexes(self) -> RuntimeLifecycle | None:
        previous_lifecycle: RuntimeLifecycle | None = None
        journaled_goals: set[str] = set()
        for record in self.journal.iter_records():
            if record.receive_seq:
                self._receive_seq = max(self._receive_seq, record.receive_seq)
            if record.process_seq:
                self._process_seq = max(self._process_seq, record.process_seq)
            if record.record_type == "lifecycle_transition":
                value = record.data.get("current")
                if value:
                    previous_lifecycle = RuntimeLifecycle(value)
            if record.record_type == "event_accepted":
                payload = record.data.get("event")
                if payload:
                    accepted = RuntimeEvent.model_validate(payload)
                    if accepted.event_type == RuntimeEventType.USER_MESSAGE:
                        self._remember_user_event(accepted)
                        self._pending_user_ingress_event_ids.add(accepted.event_id)
            if record.record_type == "homeostatic_interoception_observed":
                salience = record.data.get("homeostatic_salience")
                signature = record.data.get("homeostatic_episode_signature")
                if isinstance(salience, str):
                    self._homeostatic_salience = salience
                if isinstance(signature, str) and signature:
                    self._homeostatic_episode_signature = signature
            if record.record_type in {
                "homeostatic_marked_trigger_scheduled",
                "homeostatic_marked_trigger_reused",
                "homeostatic_marked_expression_opportunity",
            }:
                signature = record.data.get("homeostatic_episode_signature")
                if isinstance(signature, str) and signature:
                    self._homeostatic_marked_attempts[signature] = record.recorded_at
            if record.record_type == "autonomous_expression_cancelled":
                cancelled_event_id = record.data.get("cancelled_event_id")
                if isinstance(cancelled_event_id, str) and cancelled_event_id:
                    self._cancelled_autonomous_event_ids.add(cancelled_event_id)
            elif (
                record.record_type == "processing_completed"
                and record.event_id
                and record.data.get("status") in {"completed", "ignored"}
            ):
                self._cancelled_autonomous_event_ids.discard(record.event_id)
                self._pending_user_ingress_event_ids.discard(record.event_id)
            if record.record_type == "homeostasis_continuation_deferred" and record.event_id:
                revision = record.data.get("homeostasis_revision")
                if isinstance(revision, int):
                    self._homeostasis_continuations[record.event_id] = revision
            elif record.record_type == "homeostasis_continuation_consumed" and record.event_id:
                self._homeostasis_continuations.pop(record.event_id, None)
            if record.record_type == "action_proposed":
                goal_id = (record.data.get("action") or {}).get("goal_id")
                if goal_id:
                    journaled_goals.add(str(goal_id))
            if record.record_type == "body_state_observed":
                self._body_episode_id = record.data.get("episode_id")
                revision = record.data.get("body_revision")
                self._body_revision = int(revision) if revision is not None else None
                captured = record.data.get("captured_at")
                if captured:
                    self._body_captured_at = datetime.fromisoformat(
                        str(captured).replace("Z", "+00:00")
                    )
                interoception = record.data.get("interoception")
                if interoception:
                    model = (
                        CognitiveInteroceptiveState
                        if "resource_label" in interoception
                        else EmbodimentInteroceptiveState
                    )
                    self._interoception = model.model_validate(interoception)
            elif record.record_type == "agency_terminal":
                self._agency_state = AgencyState.TERMINAL
                self._terminal_body_revision = self._body_revision
                self._terminal_episode_id = self._body_episode_id
            elif record.record_type == "agency_reset":
                self._agency_state = AgencyState.AVAILABLE
            elif record.record_type == "body_monitor_degraded" and self._agency_state != AgencyState.TERMINAL:
                self._agency_state = AgencyState.DEGRADED
            elif (
                record.record_type == "body_state_applied"
                and not bool(record.data.get("terminal"))
                and self._agency_state != AgencyState.TERMINAL
            ):
                self._agency_state = AgencyState.AVAILABLE
            self._last_runtime_time = record.recorded_at
        self._goal_revision = max(self._goal_revision, len(journaled_goals))
        checkpoint = self.checkpoint_store.load()
        if (
            checkpoint
            and checkpoint.character_id == self.character_id
            and checkpoint.manifest_revision == self.manifest.revision
            and checkpoint.journal_record_seq <= self.journal.last_record_seq
        ):
            self._receive_seq = max(self._receive_seq, checkpoint.receive_seq)
            self._process_seq = max(self._process_seq, checkpoint.process_seq)
            self._goal_revision = max(self._goal_revision, checkpoint.version_vector.runtime_goal)
            self.action_coordinator.set_revision_floor(checkpoint.version_vector.runtime_action)
            self._agency_state = checkpoint.agency_state
            self._body_episode_id = checkpoint.body_episode_id
            self._body_revision = checkpoint.body_revision
            self._body_captured_at = checkpoint.body_captured_at
            if checkpoint.interoceptive_state is not None:
                state = checkpoint.interoceptive_state
                model = (
                    CognitiveInteroceptiveState
                    if "resource_label" in state
                    else EmbodimentInteroceptiveState
                )
                self._interoception = model.model_validate(state)
            if checkpoint.agency_state == AgencyState.TERMINAL:
                self._terminal_episode_id = checkpoint.body_episode_id
                self._terminal_body_revision = checkpoint.body_revision
        self._restore_grounded_body(self.journal.iter_records())
        if self._agency_service is not None:
            for record in self.journal.iter_records():
                if record.record_type == "persistent_goal_decision_bound":
                    self._agency_decision_bindings[record.data["decision_id"]] = {
                        "goal_id": record.data["goal_id"], "step_id": record.data["step_id"],
                    }
        if self._research_memory is not None:
            for record in self.journal.iter_records():
                if record.record_type == "grounded_decision" and record.event_id:
                    self._research_memory.remember(record.event_id, record.data)
        return previous_lifecycle

    async def _restore_pending_events(self) -> None:
        self._needs_refill = False
        accepted: dict[str, RuntimeEvent] = {}
        completed: set[str] = set()
        completed_ranges: list[tuple[str, str, int, int]] = []
        started: dict[str, RuntimeJournalRecord] = {}
        for record in self.journal.iter_records():
            if record.record_type == "event_accepted":
                payload = record.data.get("event")
                if payload:
                    event = RuntimeEvent.model_validate(payload)
                    accepted[event.event_id] = event
            elif record.record_type == "processing_started" and record.event_id:
                started[record.event_id] = record
            elif record.record_type == "event_dropped" and record.event_id:
                completed.add(record.event_id)
                sequence_range = record.data.get("sequence_range") or [record.receive_seq, record.receive_seq]
                completed_ranges.append(
                    (
                        str(record.data.get("event_type") or ""),
                        str(record.data.get("source") or ""),
                        int(sequence_range[0] or 0),
                        int(sequence_range[1] or 0),
                    )
                )
            elif record.record_type == "processing_completed" and record.event_id:
                if record.data.get("status") in {"completed", "ignored"}:
                    completed.add(record.event_id)
                    start = started.get(record.event_id)
                    if start is not None:
                        completed_ranges.append(
                            (
                                str(start.data.get("event_type") or ""),
                                str(start.data.get("source") or ""),
                                int(start.data.get("first_receive_seq") or start.receive_seq or 0),
                                int(start.data.get("last_receive_seq") or start.receive_seq or 0),
                            )
                        )
        queued_ids = {queued.event_id for queued in self.queue.snapshot()}
        for event_id, event in sorted(accepted.items(), key=lambda item: item[1].receive_seq):
            covered = any(
                event.event_type.value == event_type
                and event.source == source
                and first <= event.receive_seq <= last
                for event_type, source, first, last in completed_ranges
            )
            if event_id in completed or event_id in queued_ids or covered:
                self._pending_user_ingress_event_ids.discard(event_id)
                continue
            if event.event_type in CONTROL_EVENT_TYPES or event.event_type == RuntimeEventType.HEARTBEAT:
                continue
            try:
                await self.queue.put(event)
                queued_ids.add(event.event_id)
                if event.event_type == RuntimeEventType.USER_MESSAGE:
                    self._pending_user_ingress_event_ids.discard(event.event_id)
            except QueueCapacityError:
                self._reconciliation_backlog += 1
                self._needs_refill = True
                break

    async def _reconcile_incomplete_commands(
        self,
        *,
        services: frozenset[str] | None = None,
    ) -> None:
        intents: dict[str, RuntimeJournalRecord] = {}
        results: set[str] = set()
        for record in self.journal.iter_records():
            if record.record_type == "domain_command_intent" and record.command_id:
                intents[record.command_id] = record
            elif record.record_type in {"domain_command_result", "domain_command_late_result", "domain_command_expired"} and record.command_id:
                results.add(record.command_id)
        for command_id, record in intents.items():
            if command_id in results:
                continue
            envelope_data = record.data.get("envelope")
            service = record.data.get("service")
            if not envelope_data or service not in {"cognitive", "homeostasis"}:
                self._reconciliation_backlog += 1
                continue
            if services is not None and service not in services:
                continue
            envelope = DomainCommandEnvelope[dict[str, Any]].model_validate(envelope_data)
            if service == "cognitive" and envelope.command_type in {
                "PrepareQQResponse", "ObserveEmotion", "PrepareWebStep",
            }:
                source, _ = self.qq_turn_history(envelope.event_id)
                if source is not None and self._qq_turn_expired(source):
                    # The original domain operation may still complete under
                    # its own lock. This records only Runtime's expired intent;
                    # it does not claim that operation was cancelled or failed.
                    self._append_journal("domain_command_expired", event=source,
                        command_id=envelope.command_id, idempotency_key=envelope.idempotency_key,
                        data={"service": service, "error_type": "qq_turn_expired"})
                    continue
            envelope = self._renew_command_window(envelope)
            method = self._command_method(service, envelope.command_type)
            if method is None:
                self._reconciliation_backlog += 1
                continue
            try:
                result = await method(envelope)
                self._append_journal(
                    "domain_command_result",
                    command_id=envelope.command_id,
                    idempotency_key=envelope.idempotency_key,
                    data={"service": service, "result": result.model_dump(mode="json"), "reconciled": True},
                )
                if service == "homeostasis":
                    self.goals.record_homeostasis(envelope, result)
            except Exception:
                self._reconciliation_backlog += 1

    def _find_delivery_result(self, prepare_id: str) -> tuple[DeliveryResult | None, bool]:
        intent = False
        for record in self.journal.iter_records():
            if record.data.get("prepare_id") != prepare_id:
                continue
            if record.record_type == "reply_delivery_intent":
                intent = True
            elif record.record_type == "reply_delivery":
                payload = record.data.get("result")
                if payload:
                    return DeliveryResult.model_validate(payload), False
        return None, intent

    async def _restore_and_reconcile_actions(self) -> bool:
        accepted: dict[str, RuntimeEvent] = {}
        latest: dict[str, ActionExecution] = {}
        prepared: dict[str, PreparedResponse] = {}
        execution_intents: set[str] = set()
        interaction_committed: set[str] = set()
        for record in self.journal.iter_records():
            if record.record_type == "event_accepted":
                event_payload = record.data.get("event")
                if event_payload:
                    event = RuntimeEvent.model_validate(event_payload)
                    accepted[event.event_id] = event
            action_payload = record.data.get("action")
            if isinstance(action_payload, dict):
                action = ActionExecution.model_validate(action_payload)
                latest[action.action_id] = action
                prepared_payload = record.data.get("prepared")
                if prepared_payload:
                    prepared[action.action_id] = PreparedResponse.model_validate(prepared_payload)
            if record.record_type == "action_execution_intent" and isinstance(
                action_payload, dict
            ):
                execution_intents.add(action.action_id)
            if record.record_type == "action_interaction_committed":
                action_id = record.data.get("action_id")
                if action_id:
                    interaction_committed.add(str(action_id))

        unresolved = False
        for action_id, action in latest.items():
            if action_id in execution_intents and not action.terminal:
                action = action.model_copy(
                    update={"state": ActionExecutionState.RUNNING, "updated_at": utc_now()}
                )
            self.action_coordinator.restore(action)
            proposed_event_id: str | None = next(
                (
                    record.event_id
                    for record in self.journal.iter_records(reverse=True)
                    if record.record_type == "action_proposed"
                    and record.event_id is not None
                    and (record.data.get("action") or {}).get("action_id") == action_id
                ),
                None,
            )
            proposed_event: RuntimeEvent | None = (
                accepted.get(proposed_event_id) if proposed_event_id is not None else None
            )
            if (
                proposed_event is not None
                and action_id in prepared
                and action_id not in interaction_committed
            ):
                self._action_context[action_id] = {
                    "event": proposed_event,
                    "prepared": prepared[action_id],
                }

            if action.state in {ActionExecutionState.RUNNING, ActionExecutionState.VERIFYING}:
                reconciled = await self.action_coordinator.reconcile(action_id)
                self._append_journal(
                    "action_reconciled",
                    event=proposed_event,
                    idempotency_key=action.idempotency_key,
                    data={"action": reconciled.model_dump(mode="json")},
                )
                action = reconciled
            if action.state == ActionExecutionState.UNKNOWN:
                unresolved = True
                continue
            if action.terminal and action_id in self._action_context:
                try:
                    original_event: RuntimeEvent = self._action_context[action_id]["event"]
                    await self._complete_goal_action(
                        action,
                        outcome_event=self._action_outcome_causal_event(
                            action,
                            original_event=original_event,
                        ),
                    )
                except Exception:
                    unresolved = True
        return unresolved

    def _command_method(
        self, service: str, command_type: str
    ) -> Callable[[DomainCommandEnvelope[dict[str, Any]]], Awaitable[DomainCommandResult[Any]]] | None:
        if (service, command_type) == ("cognitive", "PrepareWebStep"):
            return getattr(self.cognitive, "prepare_web_step", None)
        if (service, command_type) == ("cognitive", "ObserveEmotion"):
            return getattr(self.cognitive, "observe_emotion", None)
        if (service, command_type) == ("cognitive", "PrepareGoalDecision"):
            return getattr(self.cognitive, "prepare_goal_decision", None)
        if (service, command_type) == ("homeostasis", "ApplyAppraisal"):
            return getattr(self.homeostasis, "apply_appraisal", None)
        if (service, command_type) == ("cognitive", "PrepareCapabilityCall"):
            return getattr(self.cognitive, "prepare_capability_call", None)
        if (service, command_type) == ("cognitive", "PrepareQQResponse"):
            return getattr(self.cognitive, "prepare_qq_response", None)
        if service == "cognitive" and command_type in {"PrepareBoardEntry", "CommitBoardEntry"}:
            return getattr(self.cognitive, "prepare_board_entry" if command_type == "PrepareBoardEntry" else "commit_board_entry", None)
        if service == "cognitive" and command_type in {"PrepareDiaryEntry", "CommitDiaryEntry"}:
            return getattr(self.cognitive, "prepare_diary_entry" if command_type == "PrepareDiaryEntry" else "commit_diary_entry", None)
        mapping: dict[tuple[str, str], Callable[..., Awaitable[Any]]] = {
            ("cognitive", "AssessAutonomous"): self.cognitive.assess_autonomous,
            ("cognitive", "PrepareResponse"): self.cognitive.prepare_response,
            ("cognitive", "CommitInteraction"): self.cognitive.commit_interaction,
            ("homeostasis", "ApplyEvent"): self.homeostasis.apply_event,
            ("homeostasis", "ApplyHeartbeat"): self.homeostasis.apply_heartbeat,
            ("homeostasis", "ApplyOutcome"): self.homeostasis.apply_outcome,
            ("homeostasis", "AssimilateObservation"): self.homeostasis.assimilate_observation,
            ("homeostasis", "ApplyObservation"): self.homeostasis.apply_observation,
        }
        return mapping.get((service, command_type))
