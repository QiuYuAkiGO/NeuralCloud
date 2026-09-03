"""Persistent event-driven Runtime Supervisor."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from pydantic import TypeAdapter

from harness.contracts.decisions import (
    ActionOutcomeEvidence,
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
from harness.core.ids import new_id
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
    EmbodimentState,
)
from harness.embodiment.contracts import (
    BodyStateSnapshot,
    HomeostaticObservation,
    InteroceptiveState,
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
        command_timeout_seconds: float = 30.0,
        action_timeout_seconds: float = 120.0,
        maintenance_drain_seconds: float = 5.0,
    ) -> None:
        self.manifest = manifest
        self.cognitive = cognitive
        self.homeostasis = homeostasis
        self.character_id = character_id
        self.runtime_instance_id = runtime_instance_id or new_id("runtime")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.journal = journal or RuntimeJournal(
            self.state_dir / "journal",
            runtime_instance_id=self.runtime_instance_id,
            character_id=character_id,
            manifest_revision=manifest.revision,
        )
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
        self.command_timeout_seconds = command_timeout_seconds
        self.action_timeout_seconds = action_timeout_seconds
        self.maintenance_drain_seconds = maintenance_drain_seconds

        self.lifecycle = RuntimeLifecycle.BOOTING
        self.lifecycle_since = utc_now()
        self.lifecycle_reason = "constructed"
        self._receive_seq = 0
        self._process_seq = 0
        self._goal_revision = 0
        self._cognitive_snapshot = DomainSnapshot(service="cognitive", revision=0)
        self._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0)
        self._last_heartbeat_at: datetime | None = None
        self._last_runtime_time: datetime | None = None
        self._journal_writable = not self.journal.degraded
        self._degraded_reasons: list[str] = []
        self._listeners: list[RuntimeListener] = []
        self._lifecycle_lock = asyncio.Lock()
        self._foreground_lock = asyncio.Lock()
        self._action_tasks: dict[str, asyncio.Task[None]] = {}
        self._perception_guard: PerceptionGuard | None = None
        self._body_state_guard = body_state_guard
        self._interoception: InteroceptiveState | None = None
        self._last_reconsideration_at: datetime | None = None
        self._run_task: asyncio.Task[None] | None = None
        self._booted = False
        self._closed = False
        self._reconciliation_backlog = 0
        self._needs_refill = False
        self._action_context: dict[str, dict[str, Any]] = {}
        self.pause_observations = PauseObservationBuffer()
        self.ingress = RuntimeIngress(self)
        self.instance_lock = SingleInstanceLock(
            self.state_dir / "runtime.lock", runtime_instance_id=self.runtime_instance_id
        )

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
        if self.lifecycle == RuntimeLifecycle.RUNNING:
            self._start_embodiment_guards()
        if not self.embodiment.supports(OBSERVATION_SNAPSHOT) and self._perception_guard is not None:
            await self._perception_guard.stop()
        if not self.embodiment.supports(PHYSIOLOGY_BODY_STATE) and self._body_state_guard is not None:
            await self._body_state_guard.stop()

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
        try:
            return await self.ingress.accept(**kwargs)
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
            await self._transition(RuntimeLifecycle.RECOVERING, reason="boot recovery started")
            if self.journal.truncated_tail_bytes:
                self._append_journal(
                    "journal_tail_truncated",
                    data={"bytes": self.journal.truncated_tail_bytes},
                )
            await self._check_services()
            embodiment_status = await self.embodiment.activate()
            self._append_journal(
                "embodiment_activated",
                data={"embodiment": embodiment_status.model_dump(mode="json")},
            )
            await self._reconcile_incomplete_commands()
            await self._check_services()
            unresolved_action = await self._restore_and_reconcile_actions()
            await self._restore_pending_events()
            if self.journal.records():
                now = utc_now()
                previous_time = self._last_runtime_time or self.journal.records()[-1].recorded_at
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
                or (
                    self.manifest.embodiment.required_for_runtime
                    and embodiment_status.state
                    in {EmbodimentState.UNAVAILABLE, EmbodimentState.TERMINAL}
                )
                or previous_lifecycle in {
                RuntimeLifecycle.PAUSED,
                RuntimeLifecycle.PAUSING,
                RuntimeLifecycle.FAULTED,
                RuntimeLifecycle.STOPPING,
                }
            )
            target = RuntimeLifecycle.PAUSED if requires_pause else RuntimeLifecycle.RUNNING
            await self._transition(target, reason="recovery complete")
            if target == RuntimeLifecycle.RUNNING:
                self.scheduler.start()
                self._start_embodiment_guards()
            else:
                self.scheduler.suspend()
                self.pause_observations.begin(utc_now())
            self._booted = True
        except Exception as exc:
            if self.lifecycle not in {RuntimeLifecycle.FAULTED, RuntimeLifecycle.PAUSED}:
                await self._transition(RuntimeLifecycle.FAULTED, reason=f"recovery failed: {exc}")
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
            self._append_journal(
                "processing_failed",
                event=event,
                process_seq=process_seq,
                data={"error": str(exc), "error_type": type(exc).__name__},
            )
            retryable = isinstance(exc, (DomainCommandError, TimeoutError, ConnectionError))
            if retryable:
                if self.lifecycle == RuntimeLifecycle.RUNNING:
                    await self.pause(reason=f"critical domain failure: {exc}")
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
                detail={"error": str(exc), "error_type": type(exc).__name__},
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
            await asyncio.wait_for(
                self.action_coordinator.request_preemption(reason=reason),
                timeout=self.maintenance_drain_seconds,
            )
        except TimeoutError as exc:
            await self._transition(RuntimeLifecycle.FAULTED, reason="action could not reach safe point")
            raise RuntimeStateError("action could not reach safe point") from exc
        if not await self._drain_action_tasks(timeout=self.maintenance_drain_seconds):
            await self._transition(RuntimeLifecycle.FAULTED, reason="foreground action did not stop")
            raise RuntimeStateError("foreground action did not stop")
        drained = await self.cognitive.pause_maintenance(timeout=self.maintenance_drain_seconds)
        self._append_journal("maintenance_drain", data={"completed": drained})
        if not drained:
            await self._transition(
                RuntimeLifecycle.FAULTED,
                reason="cognitive maintenance did not drain within deadline",
            )
            raise RuntimeStateError("cognitive maintenance did not drain within deadline")
        await self._transition(RuntimeLifecycle.PAUSED, reason=reason)

    async def resume(self, *, reason: str = "explicit researcher resume") -> None:
        if self.lifecycle != RuntimeLifecycle.PAUSED:
            raise RuntimeStateError(f"cannot resume from {self.lifecycle}")
        await self._check_services()
        await self.cognitive.resume_maintenance()
        resumed_at = utc_now()
        summary = self.pause_observations.build(resumed_at=resumed_at)
        await self._transition(RuntimeLifecycle.RUNNING, reason=reason)
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
        await self._check_services()
        await self._reconcile_incomplete_commands()
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
        try:
            await asyncio.wait_for(
                self.action_coordinator.request_preemption(reason=reason),
                timeout=self.maintenance_drain_seconds,
            )
            if not await self._drain_action_tasks(timeout=self.maintenance_drain_seconds):
                raise RuntimeStateError("foreground action did not stop during shutdown")
            drained = await self.cognitive.pause_maintenance(timeout=self.maintenance_drain_seconds)
            if not drained:
                raise RuntimeStateError("cognitive maintenance did not drain during shutdown")
            self.write_checkpoint()
            await self.cognitive.close()
            await self.homeostasis.close()
            await self._transition(RuntimeLifecycle.STOPPED, reason=reason, durable=True)
            await self.queue.close()
            self._closed = True
        except Exception as exc:
            await self._transition(RuntimeLifecycle.FAULTED, reason=f"graceful stop failed: {exc}")
            raise
        finally:
            self.instance_lock.release()

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
        await self._complete_goal_action(action)
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
                or embodiment_status.state == EmbodimentState.AVAILABLE
            ),
            degraded_reasons=degraded_reasons,
            last_heartbeat_at=self._last_heartbeat_at,
            last_journal_append_at=self.journal.last_append_at,
            embodiment=embodiment_status,
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
            await self._complete_goal_action(action)
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="action_result",
                detail={"action": action.model_dump(mode="json")},
            )
        if event.event_type == RuntimeEventType.BODY_STATE:
            return await self._handle_body_state(event, process_seq)
        if event.event_type == RuntimeEventType.AGENCY_TERMINAL:
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                decision_kind="agency_terminal",
                detail={"embodiment": self.embodiment.status().model_dump(mode="json")},
            )
        homeostasis_command = "ApplyHeartbeat" if event.event_type == RuntimeEventType.HEARTBEAT else "ApplyEvent"
        homeostasis_envelope = self._make_command(
            event,
            command_type=homeostasis_command,
            idempotency_key=f"homeostasis:{homeostasis_command}:{event.event_id}",
            expected_revision=self._homeostasis_snapshot.revision,
            payload={"event": event.model_dump(mode="json")},
        )
        method = (
            self.homeostasis.apply_heartbeat
            if event.event_type == RuntimeEventType.HEARTBEAT
            else self.homeostasis.apply_event
        )
        homeostasis_result = await self._invoke_domain(
            service="homeostasis", envelope=homeostasis_envelope, method=method, event=event
        )
        self._update_homeostasis(homeostasis_result)
        if event.event_type == RuntimeEventType.HEARTBEAT:
            self._last_heartbeat_at = utc_now()
            await self._refresh_embodiment()
            await self.check_watchdog()
            policies: list[dict[str, Any]] = []
            if (
                self.lifecycle == RuntimeLifecycle.RUNNING
                and self.queue.empty()
                and self.action_coordinator.current is None
            ):
                policies = await self.homeostasis.propose_policies(
                    version_vector=self.version_vector
                )
                if policies:
                    await self.submit_event(
                        event_type=RuntimeEventType.IDLE,
                        priority=RuntimeEventPriority.IDLE,
                        source="scheduler",
                        payload={"mode": "autonomous", "policies": policies},
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
                },
            )
        if event.event_type not in {
            RuntimeEventType.USER_MESSAGE,
            RuntimeEventType.IDLE,
            RuntimeEventType.GOAL_RECONSIDERATION,
        }:
            return EventDispatchResult(
                event_id=event.event_id,
                receive_seq=event.receive_seq,
                process_seq=process_seq,
                status="completed",
                detail={"homeostasis_revision": self._homeostasis_snapshot.revision},
            )

        prepare_envelope = self._make_command(
            event,
            command_type="PrepareResponse",
            idempotency_key=self._prepare_idempotency_key(event),
            expected_revision=self._cognitive_snapshot.revision,
            payload={
                **event.payload,
                "source_time": event.source_time,
                "call_id": event.correlation_id,
                **(
                    {"mode": "autonomous", "session_id": "sess_autonomous", "channel": "runtime"}
                    if event.event_type == RuntimeEventType.IDLE
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
                        "interoception": self._interoception.model_dump(
                            mode="json",
                            exclude={"evidence_refs"},
                        )
                    }
                    if self._interoception is not None
                    and self.manifest.embodiment.mode in {"grounded", "active"}
                    else {}
                ),
            },
        )
        prepare_result = await self._invoke_domain(
            service="cognitive",
            envelope=prepare_envelope,
            method=self.cognitive.prepare_response,
            event=event,
        )
        prepared = PreparedResponse.model_validate(prepare_result.result)
        if utc_now() >= prepared.expires_at:
            raise LateDomainResultError(
                f"prepared response {prepared.prepare_id} expired before delivery"
            )
        decision: ReplyDecision | GoalProposal | NoActionDecision | DeferDecision = TypeAdapter(
            CharacterDecision
        ).validate_python(prepared.decision)
        detail = await self._handle_decision(event, prepared, decision)
        return EventDispatchResult(
            event_id=event.event_id,
            receive_seq=event.receive_seq,
            process_seq=process_seq,
            status="completed",
            decision_kind=decision.kind,
            detail=detail,
        )

    async def _handle_body_state(
        self,
        event: RuntimeEvent,
        process_seq: int,
    ) -> EventDispatchResult:
        snapshot = BodyStateSnapshot.model_validate(
            event.payload.get("body_state") or event.payload
        )
        expected_instance = self.manifest.embodiment.body_instance_id
        if expected_instance and snapshot.body_instance_id != expected_instance:
            raise DomainCommandError("body-state snapshot belongs to a different body instance")

        interoception = InteroceptiveState(
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
        if mode in {"grounded", "active"}:
            observation = HomeostaticObservation(
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
            self._update_homeostasis(result)

        reconsider = terminal
        if mode == "active" and not terminal and previous_interoception is not None:
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

    async def _handle_decision(
        self,
        event: RuntimeEvent,
        prepared: PreparedResponse,
        decision: ReplyDecision | GoalProposal | NoActionDecision | DeferDecision,
    ) -> dict[str, Any]:
        if isinstance(decision, ReplyDecision):
            deadline = min(prepared.expires_at, utc_now() + timedelta(seconds=self.action_timeout_seconds))
            delivery_key = f"delivery:{prepared.prepare_id}"
            delivery, uncertain = self._find_delivery_result(prepared.prepare_id)
            if uncertain:
                raise UnknownDeliveryStateError(
                    f"delivery result is unknown for prepare {prepared.prepare_id}; researcher confirmation required"
                )
            if delivery is None:
                self._append_journal(
                    "reply_delivery_intent",
                    event=event,
                    idempotency_key=delivery_key,
                    data={"prepare_id": prepared.prepare_id, "channel": decision.channel},
                )
                async with self._foreground_lock:
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
                data={"prepare_id": prepared.prepare_id, "result": delivery.model_dump(mode="json")},
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
                    }
            self._goal_revision += 1
            action = await self.action_coordinator.propose(
                decision,
                idempotency_key=f"action:{prepared.prepare_id}:{decision.goal_id}",
                deadline=min(
                    prepared.expires_at,
                    utc_now() + timedelta(seconds=self.action_timeout_seconds),
                ),
                version_vector=prepared.read_versions,
                risk_class=decision.permission_class,
            )
            self._action_context[action.action_id] = {
                "event": event,
                "prepared": prepared,
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

    async def _complete_goal_action(self, action: ActionExecution) -> None:
        context = self._action_context.get(action.action_id)
        if context is None or not action.terminal:
            return
        event: RuntimeEvent = context["event"]
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
        await self._commit_prepared(event, prepared, evidence)
        outcome_command = self._make_command(
            event,
            command_type="ApplyOutcome",
            idempotency_key=f"homeostasis:outcome:{action.idempotency_key}",
            expected_revision=self._homeostasis_snapshot.revision,
            payload={"action": action.model_dump(mode="json"), "evidence": evidence.model_dump(mode="json")},
        )
        result = await self._invoke_domain(
            service="homeostasis",
            envelope=outcome_command,
            method=self.homeostasis.apply_outcome,
            event=event,
        )
        self._update_homeostasis(result)
        self._append_journal("action_completed", event=event, data={"action": action.model_dump(mode="json")})
        self._append_journal(
            "action_interaction_committed",
            event=event,
            idempotency_key=action.idempotency_key,
            data={"action_id": action.action_id, "prepare_id": prepared.prepare_id},
        )
        self._action_context.pop(action.action_id, None)

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
        base = f"cognitive:prepare:{event.event_id}"
        intents = [
            record
            for record in self.journal.records()
            if record.record_type == "domain_command_intent"
            and record.event_id == event.event_id
            and record.data.get("service") == "cognitive"
            and (record.data.get("envelope") or {}).get("command_type") == "PrepareResponse"
        ]
        if not intents:
            return base
        latest = intents[-1]
        result_record = next(
            (
                record
                for record in reversed(self.journal.records())
                if record.command_id == latest.command_id
                and record.record_type in {"domain_command_result", "domain_command_late_result"}
            ),
            None,
        )
        if result_record is None:
            return latest.idempotency_key or base
        if result_record.record_type == "domain_command_late_result":
            return f"{base}:reconcile:{len(intents)}"
        try:
            result = result_record.data["result"]
            prepared = PreparedResponse.model_validate(result["result"])
        except (KeyError, TypeError, ValueError):
            prepared = None
        if prepared is not None and utc_now() < prepared.expires_at:
            return latest.idempotency_key or base
        return f"{base}:reconcile:{len(intents)}"

    def _make_command(
        self,
        event: RuntimeEvent,
        *,
        command_type: str,
        idempotency_key: str,
        payload: dict[str, Any],
        expected_revision: int | None,
    ) -> DomainCommandEnvelope[dict[str, Any]]:
        for record in reversed(self.journal.records()):
            if record.record_type != "domain_command_intent":
                continue
            if record.idempotency_key != idempotency_key:
                continue
            previous = record.data.get("envelope")
            if previous:
                return DomainCommandEnvelope[dict[str, Any]].model_validate(previous)
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

    async def _invoke_domain(
        self,
        *,
        service: str,
        envelope: DomainCommandEnvelope[dict[str, Any]],
        method: Callable[[DomainCommandEnvelope[dict[str, Any]]], Awaitable[DomainCommandResult[Any]]],
        event: RuntimeEvent,
    ) -> DomainCommandResult[Any]:
        self._append_journal(
            "domain_command_intent",
            event=event,
            command_id=envelope.command_id,
            idempotency_key=envelope.idempotency_key,
            data={"service": service, "envelope": envelope.model_dump(mode="json")},
        )
        timeout = max(0.001, (envelope.deadline - utc_now()).total_seconds())
        try:
            result = await asyncio.wait_for(method(envelope), timeout=timeout)
            self.watchdog.record_success(service)
            self._degraded_reasons = [reason for reason in self._degraded_reasons if reason != service]
        except Exception:
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
        if late:
            raise LateDomainResultError(f"late {service} result for {envelope.command_type}")
        if result.status not in {"applied", "idempotent_replay"}:
            raise DomainCommandError(
                f"{service} {envelope.command_type} returned {result.status}: {result.error}"
            )
        return result

    async def _check_services(self) -> None:
        cognitive_health, homeostasis_health = await asyncio.gather(
            asyncio.wait_for(self.cognitive.health(), timeout=self.command_timeout_seconds),
            asyncio.wait_for(self.homeostasis.health(), timeout=self.command_timeout_seconds),
        )
        self._validate_health(cognitive_health, "cognitive")
        self._validate_health(homeostasis_health, "homeostasis")
        self._degraded_reasons = [
            reason
            for reason in self._degraded_reasons
            if reason not in {"cognitive", "homeostasis"}
        ]
        self._cognitive_snapshot, self._homeostasis_snapshot = await asyncio.gather(
            asyncio.wait_for(self.cognitive.get_snapshot(), timeout=self.command_timeout_seconds),
            asyncio.wait_for(self.homeostasis.get_snapshot(), timeout=self.command_timeout_seconds),
        )
        self._validate_snapshot(self._cognitive_snapshot, "cognitive")
        self._validate_snapshot(self._homeostasis_snapshot, "homeostasis")
        self._append_journal(
            "service_reconciled",
            data={
                "cognitive": self._cognitive_snapshot.model_dump(mode="json"),
                "homeostasis": self._homeostasis_snapshot.model_dump(mode="json"),
            },
        )

    @staticmethod
    def _validate_health(health: ServiceHealth, expected_service: str) -> None:
        if health.service != expected_service:
            raise DomainCommandError(
                f"expected {expected_service} service, received {health.service}"
            )
        if not health.healthy:
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

    def _update_homeostasis(self, result: DomainCommandResult[Any]) -> None:
        revision = result.committed_revision or self._homeostasis_snapshot.revision
        self._homeostasis_snapshot = self._homeostasis_snapshot.model_copy(
            update={
                "revision": revision,
                "acknowledged_receive_seq": result.acknowledged_receive_seq or 0,
                "acknowledged_event_id": result.acknowledged_event_id,
                "component_revisions": {"state": revision},
                "payload": result.result or {},
            }
        )

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
            self._journal_writable = not self.journal.degraded
            return record
        except JournalUnavailableError:
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
        records = self.journal.records()
        previous_lifecycle: RuntimeLifecycle | None = None
        journaled_goals: set[str] = set()
        for record in records:
            if record.receive_seq:
                self._receive_seq = max(self._receive_seq, record.receive_seq)
            if record.process_seq:
                self._process_seq = max(self._process_seq, record.process_seq)
            if record.record_type == "lifecycle_transition":
                value = record.data.get("current")
                if value:
                    previous_lifecycle = RuntimeLifecycle(value)
            if record.record_type == "action_proposed":
                goal_id = (record.data.get("action") or {}).get("goal_id")
                if goal_id:
                    journaled_goals.add(str(goal_id))
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
        return previous_lifecycle

    async def _restore_pending_events(self) -> None:
        self._needs_refill = False
        accepted: dict[str, RuntimeEvent] = {}
        completed: set[str] = set()
        completed_ranges: list[tuple[str, str, int, int]] = []
        started: dict[str, RuntimeJournalRecord] = {}
        for record in self.journal.records():
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
                continue
            if event.event_type in CONTROL_EVENT_TYPES or event.event_type == RuntimeEventType.HEARTBEAT:
                continue
            try:
                await self.queue.put(event)
                queued_ids.add(event.event_id)
            except QueueCapacityError:
                self._reconciliation_backlog += 1
                self._needs_refill = True
                break

    async def _reconcile_incomplete_commands(self) -> None:
        intents: dict[str, RuntimeJournalRecord] = {}
        results: set[str] = set()
        for record in self.journal.records():
            if record.record_type == "domain_command_intent" and record.command_id:
                intents[record.command_id] = record
            elif record.record_type in {"domain_command_result", "domain_command_late_result"} and record.command_id:
                results.add(record.command_id)
        for command_id, record in intents.items():
            if command_id in results:
                continue
            envelope_data = record.data.get("envelope")
            service = record.data.get("service")
            if not envelope_data or service not in {"cognitive", "homeostasis"}:
                self._reconciliation_backlog += 1
                continue
            envelope = DomainCommandEnvelope[dict[str, Any]].model_validate(envelope_data)
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
            except Exception:
                self._reconciliation_backlog += 1

    def _find_delivery_result(self, prepare_id: str) -> tuple[DeliveryResult | None, bool]:
        intent = False
        for record in self.journal.records():
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
        for record in self.journal.records():
            if record.record_type == "event_accepted":
                event_payload = record.data.get("event")
                if event_payload:
                    event = RuntimeEvent.model_validate(event_payload)
                    accepted[event.event_id] = event
            action_payload = record.data.get("action")
            if action_payload:
                action = ActionExecution.model_validate(action_payload)
                latest[action.action_id] = action
                prepared_payload = record.data.get("prepared")
                if prepared_payload:
                    prepared[action.action_id] = PreparedResponse.model_validate(prepared_payload)
            if record.record_type == "action_execution_intent" and action_payload:
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
                    for record in reversed(self.journal.records())
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
                    await self._complete_goal_action(action)
                except Exception:
                    unresolved = True
        return unresolved

    def _command_method(
        self, service: str, command_type: str
    ) -> Callable[[DomainCommandEnvelope[dict[str, Any]]], Awaitable[DomainCommandResult[Any]]] | None:
        mapping: dict[tuple[str, str], Callable[..., Awaitable[Any]]] = {
            ("cognitive", "PrepareResponse"): self.cognitive.prepare_response,
            ("cognitive", "CommitInteraction"): self.cognitive.commit_interaction,
            ("homeostasis", "ApplyEvent"): self.homeostasis.apply_event,
            ("homeostasis", "ApplyHeartbeat"): self.homeostasis.apply_heartbeat,
            ("homeostasis", "ApplyOutcome"): self.homeostasis.apply_outcome,
            ("homeostasis", "AssimilateObservation"): self.homeostasis.assimilate_observation,
        }
        return mapping.get((service, command_type))
