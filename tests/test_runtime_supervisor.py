from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness.config import Settings
from harness.contracts.decisions import DeliveryEvidence, GoalProposal
from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    EVIDENCE_REFERENCE,
    CapabilityStatus,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.simulator import DeterministicEmbodimentSimulator
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.manifest import ExperimentManifest
from harness.runtime import (
    ActionBackendResult,
    ActionCoordinator,
    ActionExecutionState,
    ControlAuthorizationError,
    ControlPermission,
    ControlPlane,
    JournalCorruptionError,
    PassiveHomeostasisAdapter,
    PipelineCognitiveAdapter,
    PriorityEventQueue,
    RealTimeScheduler,
    RuntimeEvent,
    RuntimeEventPriority,
    RuntimeEventType,
    RuntimeJournal,
    RuntimeLifecycle,
    RuntimeStateError,
    RuntimeSupervisor,
)
from tests.factories import vaelith

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _manifest(*, mode: str = "development_debug") -> ExperimentManifest:
    payload = {
        "experiment_id": "runtime-test",
        "revision": 1,
        "control_mode": mode,
        "condition": "llm_top3",
        "persona_version": "v3",
        "homeostatic_profile": "passive-v1",
        "heartbeat_seconds": 60,
        "policy_horizon": 3,
        "random_seed": 7,
        "initial_snapshot_ref": "snapshot://test/base",
        "model_version": "mock",
        "prompt_version": "memory-v1",
        "schema_version": "state-v1",
        "a_version": "A-v1",
        "b_version": "B-v1",
        "c_version": "C-v1",
        "precision_version": "precision-v1",
        "coupling_version": "coupling-v1",
        "priority_policy": "runtime-v1",
        "environment": "test",
        "retention": "test",
        "watchdog_action": "pause_preserve_state",
    }
    if mode == "study_runtime":
        payload.update(
            {
                "participant_ref": "pseudonym:test",
                "authorized_data_scope_ref": "consent://test/v1",
                "high_impact_gate": "independent_confirmation",
            }
        )
    return ExperimentManifest.model_validate(payload)


def _event(sequence: int, event_type: RuntimeEventType, priority: RuntimeEventPriority) -> RuntimeEvent:
    return RuntimeEvent(
        event_id=f"evt_{sequence}",
        receive_seq=sequence,
        correlation_id=f"corr_{sequence}",
        event_type=event_type,
        priority=priority,
        source="test",
        source_time=NOW,
        received_at=NOW,
        idempotency_key=f"event:{sequence}",
        manifest_revision=1,
        payload={"sequence": sequence},
    )


def _pipeline_adapter(tmp_path):
    settings = Settings(
        parser_llm_enabled=False,
        planner_llm_enabled=False,
        tier_llm_enabled=False,
        validator_sample_rate=0,
        continuation_enabled=False,
        call_log_enabled=False,
    )
    memory = build_inmemory_service(settings=settings)
    memory.bootstrap_persona(vaelith())
    pipeline = InferencePipeline(memory, settings=settings)
    return memory, pipeline, PipelineCognitiveAdapter(
        pipeline,
        state_path=tmp_path / "cognitive-state.json",
    )


def test_priority_queue_orders_and_coalesces_without_losing_sequence_range() -> None:
    async def scenario() -> None:
        queue = PriorityEventQueue(capacity=4)
        await queue.put(_event(1, RuntimeEventType.PERCEPTION, RuntimeEventPriority.PERCEPTION))
        await queue.put(_event(2, RuntimeEventType.PERCEPTION, RuntimeEventPriority.PERCEPTION))
        await queue.put(_event(3, RuntimeEventType.USER_MESSAGE, RuntimeEventPriority.USER))

        first = await queue.get(timeout=0)
        second = await queue.get(timeout=0)
        assert first is not None and first.receive_seq == 3
        assert second is not None and second.receive_seq == 2
        assert second.sequence_range == (1, 2)
        assert second.coalesced_count == 2
        assert queue.statistics().coalesced == 1

    asyncio.run(scenario())


def test_action_coordinator_uses_one_slot_and_preempts_at_safe_point() -> None:
    class CooperativeBackend:
        def __init__(self) -> None:
            self.started = asyncio.Event()
            self.release = asyncio.Event()

        async def execute(self, execution):
            self.started.set()
            await self.release.wait()
            return ActionBackendResult(
                success=True,
                summary="step completed",
                evidence_refs=["backend:step"],
                safe_point="step-1",
            )

        async def request_safe_point(self, execution):
            self.release.set()
            return ActionBackendResult(
                success=False,
                summary="stopped at safe point",
                safe_point="step-1",
                continuation={"next_step": 2},
                retry_safe=True,
            )

        async def verify(self, execution):
            return ActionBackendResult(
                success=True,
                summary="verified",
                evidence_refs=["backend:verified"],
            )

        async def reconcile(self, execution):
            return ActionBackendResult(success=False, summary="not executed", retry_safe=True)

    async def scenario() -> None:
        backend = CooperativeBackend()
        coordinator = ActionCoordinator(backend)
        action = await coordinator.propose(
            GoalProposal(
                policy_id="policy_1",
                goal="update a reversible draft",
                success_criteria=["draft exists"],
                permission_class="low",
                reversible=True,
                verification_method="read draft",
            ),
            idempotency_key="action:test",
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            version_vector=VersionVector(),
        )
        reserved = await coordinator.reserve(action.action_id)
        assert reserved.state == ActionExecutionState.RUNNING
        execution = asyncio.create_task(coordinator.execute(action.action_id))
        await backend.started.wait()
        preempted = await coordinator.request_preemption(reason="user message")
        completed = await execution
        assert preempted is not None
        assert preempted.state == ActionExecutionState.CANCELLED
        assert completed.state == ActionExecutionState.CANCELLED
        assert completed.continuation == {"next_step": 2}
        assert coordinator.current is None

    asyncio.run(scenario())


def test_runtime_journal_hash_chain_truncates_partial_tail_and_rejects_middle_corruption(tmp_path) -> None:
    path = tmp_path / "runtime.jsonl"
    journal = RuntimeJournal(
        path,
        runtime_instance_id="runtime_1",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    journal.append("first", data={"value": 1})
    journal.append("second", data={"value": 2})
    with path.open("ab") as handle:
        handle.write(b'{"partial":')

    recovered = RuntimeJournal(
        path,
        runtime_instance_id="runtime_2",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    assert recovered.last_record_seq == 2
    assert recovered.truncated_tail_bytes > 0

    lines = path.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    first["data"] = {"value": 99}
    lines[0] = json.dumps(first, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    with pytest.raises(JournalCorruptionError, match="checksum"):
        RuntimeJournal(
            path,
            runtime_instance_id="runtime_3",
            character_id="character",
            manifest_revision=1,
            fsync=False,
        )


def test_segmented_journal_recovery_and_scheduler_do_not_catch_up_missed_heartbeats(tmp_path) -> None:
    journal = RuntimeJournal(
        tmp_path / "segments",
        runtime_instance_id="runtime_1",
        character_id="character",
        manifest_revision=1,
        fsync=False,
        max_segment_bytes=1024,
    )
    for index in range(8):
        journal.append("payload", data={"index": index, "text": "x" * 400})
    assert len(list((tmp_path / "segments").glob("runtime-*.jsonl"))) > 1
    recovered = RuntimeJournal(
        tmp_path / "segments",
        runtime_instance_id="runtime_2",
        character_id="character",
        manifest_revision=1,
        fsync=False,
        max_segment_bytes=1024,
    )
    assert recovered.last_record_seq == 8

    clock = [0.0]
    scheduler = RealTimeScheduler(60, monotonic=lambda: clock[0], wall_clock=lambda: NOW)
    scheduler.start()
    clock[0] = 180.0
    assert scheduler.make_due_event() is not None
    assert scheduler.next_deadline() == 240.0
    assert scheduler.make_due_event() is None


def test_pipeline_prepare_is_side_effect_free_and_commit_is_idempotent(tmp_path) -> None:
    memory, pipeline, adapter = _pipeline_adapter(tmp_path)

    async def scenario() -> None:
        before_sessions = pipeline.session_store.snapshot()
        before_events = len(memory.stores.episodic.list_all())
        envelope = DomainCommandEnvelope[dict[str, object]](
            command_type="PrepareResponse",
            event_id="evt_prepare",
            receive_seq=1,
            correlation_id="corr_prepare",
            runtime_instance_id="runtime_1",
            manifest_revision=1,
            idempotency_key="prepare:evt_prepare",
            issued_at=datetime.now(UTC),
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            expected_revision=0,
            version_vector=VersionVector(),
            payload={"text": "你还好吗？", "session_id": "sess_1", "speaker": "user"},
        )
        prepared_result = await adapter.prepare_response(envelope)
        assert prepared_result.status == "applied"
        assert pipeline.session_store.snapshot() == before_sessions
        assert len(memory.stores.episodic.list_all()) == before_events

        replay = await adapter.prepare_response(envelope)
        assert replay.status == "idempotent_replay"
        assert replay.result == prepared_result.result

        assert prepared_result.result is not None
        evidence = DeliveryEvidence(
            prepare_id=prepared_result.result.prepare_id,
            delivered_at=datetime.now(UTC),
            channel="chat",
            success=True,
            delivered_segments=list(prepared_result.result.decision.segments),
        )
        commit = DomainCommandEnvelope[dict[str, object]](
            command_type="CommitInteraction",
            event_id="evt_prepare",
            receive_seq=1,
            correlation_id="corr_prepare",
            runtime_instance_id="runtime_1",
            manifest_revision=1,
            idempotency_key="commit:evt_prepare",
            issued_at=datetime.now(UTC),
            deadline=datetime.now(UTC) + timedelta(seconds=30),
            expected_revision=0,
            version_vector=VersionVector(),
            payload={
                "prepare_id": prepared_result.result.prepare_id,
                "evidence": evidence.model_dump(mode="json"),
            },
        )
        committed = await adapter.commit_interaction(commit)
        replayed_commit = await adapter.commit_interaction(commit)
        assert committed.committed_revision == 1
        assert replayed_commit.status == "idempotent_replay"
        assert len(pipeline.session_store.recent("sess_1")) == 2
        memory.wait_background()
        assert len(memory.stores.episodic.list_all()) == 2
        await adapter.close()

    asyncio.run(scenario())


def test_replayed_domain_command_renews_only_transport_window(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime-command-replay",
    )
    event = _event(1, RuntimeEventType.USER_MESSAGE, RuntimeEventPriority.USER)
    original = DomainCommandEnvelope[dict[str, object]](
        command_id="cmd_replay",
        command_type="PrepareResponse",
        event_id=event.event_id,
        receive_seq=event.receive_seq,
        correlation_id=event.correlation_id,
        runtime_instance_id=supervisor.runtime_instance_id,
        manifest_revision=1,
        idempotency_key="cognitive:prepare:evt_1",
        issued_at=NOW - timedelta(minutes=2),
        deadline=NOW - timedelta(minutes=1),
        expected_revision=0,
        version_vector=VersionVector(),
        payload={"text": "replay safely"},
    )
    original_hash = original.effective_request_hash()
    supervisor.journal.append(
        "domain_command_intent",
        event_id=event.event_id,
        receive_seq=event.receive_seq,
        correlation_id=event.correlation_id,
        command_id=original.command_id,
        idempotency_key=original.idempotency_key,
        data={"service": "cognitive", "envelope": original.model_dump(mode="json")},
    )

    replay = supervisor._make_command(
        event,
        command_type="PrepareResponse",
        idempotency_key=original.idempotency_key,
        expected_revision=0,
        payload={"text": "replay safely"},
    )

    assert replay.command_id == original.command_id
    assert replay.idempotency_key == original.idempotency_key
    assert replay.payload == original.payload
    assert replay.effective_request_hash() == original_hash
    assert replay.issued_at > original.deadline
    assert replay.deadline > replay.issued_at
    cognitive.pipeline.memory.close()


def test_supervisor_user_event_pause_resume_and_checkpoint(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING

        accepted = await supervisor.submit_user_message("你好。", session_id="sess_1")
        assert accepted is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None
        result = await supervisor.dispatch(queued)
        assert result.status == "completed"
        assert result.decision_kind == "reply"
        assert supervisor.version_vector.cognitive_session == 1
        assert supervisor.version_vector.homeostasis_state == 1

        await supervisor.pause(reason="test pause")
        paused_observation = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="desktop",
            payload={"active_window": "editor"},
        )
        assert paused_observation is not None
        assert supervisor.queue.empty()
        await supervisor.resume(reason="test resume")
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        summary = await supervisor.queue.get(timeout=0)
        assert summary is not None
        assert summary.event_type == RuntimeEventType.RESUME_OBSERVATION
        assert summary.payload["observation_count"] == 1

        checkpoint = supervisor.write_checkpoint()
        assert checkpoint.receive_seq == supervisor.status().receive_seq
        assert supervisor.checkpoint_store.load() is not None
        await supervisor.stop(reason="test complete")
        assert supervisor.lifecycle == RuntimeLifecycle.STOPPED

    asyncio.run(scenario())


def test_homeostasis_watchdog_routes_through_control_queue_and_pauses(tmp_path) -> None:
    class BoundaryHomeostasis(PassiveHomeostasisAdapter):
        async def apply_heartbeat(self, command):
            result = await super().apply_heartbeat(command)
            payload = dict(result.result or {})
            payload["snapshot"] = {
                "watchdog": ["cognitive_load:hard_bound_sustained"]
            }
            return result.model_copy(update={"result": payload})

    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=BoundaryHomeostasis(),
        state_dir=tmp_path / "runtime-homeostasis-watchdog",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        heartbeat = await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        assert heartbeat is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None
        await supervisor.dispatch(queued)

        watchdog = await supervisor.queue.get(timeout=0)
        assert watchdog is not None
        assert watchdog.event_type == RuntimeEventType.WATCHDOG
        assert watchdog.source == "homeostasis"
        assert watchdog.payload["evidence"]["reasons"] == [
            "cognitive_load:hard_bound_sustained"
        ]

        await supervisor.dispatch(watchdog)
        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert (
            supervisor.lifecycle_reason
            == "homeostasis reported an unsafe or unstable state"
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_persisted_homeostasis_watchdog_recovers_paused_and_allows_resume(
    tmp_path,
) -> None:
    class PersistedBoundaryHomeostasis(PassiveHomeostasisAdapter):
        reasons = ["cognitive_load:hard_bound_sustained"]

        async def health(self):
            health = await super().health()
            return health.model_copy(
                update={
                    "healthy": False,
                    "detail": {
                        "watchdog": self.reasons,
                        "projection_errors": {},
                    },
                }
            )

        async def get_snapshot(self):
            snapshot = await super().get_snapshot()
            return snapshot.model_copy(
                update={"payload": {"watchdog": self.reasons, "policies": []}}
            )

    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PersistedBoundaryHomeostasis(),
        state_dir=tmp_path / "runtime-persisted-homeostasis-watchdog",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert "homeostasis watchdog requires pause" in supervisor.lifecycle_reason
        assert supervisor.status().health.homeostasis_healthy is False

        await supervisor.resume(reason="researcher reviewed persisted boundary")
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        assert supervisor._homeostasis_watchdog_signature is None
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_pause_failure_faults_instead_of_sticking_in_pausing(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)

    async def failing_pause_maintenance(*, timeout: float) -> bool:
        raise ConnectionError("maintenance endpoint unavailable")

    cognitive.pause_maintenance = failing_pause_maintenance  # type: ignore[method-assign]
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime-pause-failure",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        with pytest.raises(RuntimeStateError, match="maintenance endpoint unavailable"):
            await supervisor.pause(reason="test failing pause")
        assert supervisor.lifecycle == RuntimeLifecycle.FAULTED
        assert "maintenance endpoint unavailable" in supervisor.lifecycle_reason
        cognitive.pipeline.memory.close()

    asyncio.run(scenario())


def test_pause_transport_wait_is_bounded(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)

    async def blocked_pause_maintenance(*, timeout: float) -> bool:
        await asyncio.Event().wait()
        return True

    cognitive.pause_maintenance = blocked_pause_maintenance  # type: ignore[method-assign]
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime-blocked-pause",
        maintenance_drain_seconds=0.01,
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        with pytest.raises(RuntimeStateError, match="TimeoutError"):
            await supervisor.pause(reason="test bounded pause")
        assert supervisor.lifecycle == RuntimeLifecycle.FAULTED
        cognitive.pipeline.memory.close()

    asyncio.run(scenario())


def test_domain_timeout_requeues_event_even_when_recovery_pause_fails(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)

    async def failing_prepare(command):
        raise TimeoutError()

    async def failing_pause_maintenance(*, timeout: float) -> bool:
        raise ConnectionError("maintenance endpoint unavailable")

    cognitive.prepare_response = failing_prepare  # type: ignore[method-assign]
    cognitive.pause_maintenance = failing_pause_maintenance  # type: ignore[method-assign]
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime-domain-timeout",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        accepted = await supervisor.submit_user_message("retry me", session_id="sess_1")
        assert accepted is not None
        event = await supervisor.queue.get(timeout=0)
        assert event is not None

        result = await supervisor.dispatch(event)

        assert result.status == "deferred"
        assert result.detail["error"] == "TimeoutError"
        assert supervisor.lifecycle == RuntimeLifecycle.FAULTED
        replay = await supervisor.queue.get(timeout=0)
        assert replay is not None
        assert replay.event_id == event.event_id
        record_types = [record.record_type for record in supervisor.journal.records()]
        assert "pause_failed" in record_types
        assert "event_requeued" in record_types
        cognitive.pipeline.memory.close()

    asyncio.run(scenario())


def test_required_embodiment_runtime_loss_pauses_and_blocks_resume(tmp_path) -> None:
    manifest_payload = _manifest().model_dump(mode="json")
    manifest_payload["embodiment"] = {
        "mode": "observe",
        "required_for_runtime": True,
        "body_provider": "none",
        "capability_requirements": [
            {"capability": ACTION_EXECUTE, "required": True},
            {"capability": EVIDENCE_REFERENCE, "required": True},
        ],
    }
    manifest = ExperimentManifest.model_validate(manifest_payload)
    simulator = DeterministicEmbodimentSimulator(clock=lambda: NOW)
    manager = EmbodimentManager(manifest.embodiment, simulator)
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=manifest,
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        embodiment_manager=manager,
        state_dir=tmp_path / "runtime-required-embodiment",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING

        simulator.set_capability_status(EVIDENCE_REFERENCE, CapabilityStatus.UNAVAILABLE)
        await supervisor._refresh_embodiment()

        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert manager.supports(ACTION_EXECUTE) is True
        assert manager.supports(EVIDENCE_REFERENCE) is False
        with pytest.raises(
            RuntimeStateError,
            match="cannot resume while required embodiment is unavailable",
        ):
            await supervisor.resume()

        simulator.set_capability_status(EVIDENCE_REFERENCE, CapabilityStatus.AVAILABLE)
        await supervisor.resume(reason="required embodiment recovered")
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_study_control_plane_rejects_debug_mutation_and_audits_it(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(mode="study_runtime"),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime",
    )
    control = ControlPlane(supervisor, token="local-secret")
    control.authenticate("local-secret")
    with pytest.raises(ControlAuthorizationError):
        control.authenticate("wrong")
    with pytest.raises(ControlAuthorizationError, match="not allowed"):
        control.authorize(ControlPermission.DEBUG_MUTATION, operation="persona.rename")
    assert any(record.record_type == "control_command_rejected" for record in supervisor.journal.records())
    cognitive.pipeline.memory.close()
