from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness.config import Settings
from harness.contracts.body import BodyStateSnapshot as LegacyBodyStateSnapshot
from harness.contracts.decisions import DeliveryEvidence, GoalProposal, PreparedResponse
from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    EVIDENCE_REFERENCE,
    CapabilityStatus,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.simulator import DeterministicEmbodimentSimulator
from harness.llm.client import MockLLM, RoutingLLMClient
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.manifest import EmbodimentCondition, ExperimentManifest
from harness.runtime import (
    ActionBackendResult,
    ActionCoordinator,
    ActionExecution,
    ActionExecutionState,
    ControlAuthorizationError,
    ControlPermission,
    ControlPlane,
    DomainCommandError,
    DomainSnapshot,
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


def _manifest(
    *,
    mode: str = "development_debug",
    expression_mode: str = "off",
) -> ExperimentManifest:
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
        "homeostatic_expression": {
            "mode": expression_mode,
            "marked_cooldown_seconds": 1800,
        },
    }
    if mode == "study_runtime":
        payload.update(
            {
                "participant_ref": "pseudonym:test",
                "authorized_data_scope_ref": "consent://test/v1",
                "high_impact_gate": "independent_confirmation",
            }
        )
    if expression_mode != "off":
        payload.update(
            {
                "homeostatic_profile": "summary-test@3",
                "prompt_version": "homeostatic-interoception-v1",
                "schema_version": "homeostasis-state-v2",
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


def _expression_pipeline_adapter(tmp_path, *, main: MockLLM, aux: MockLLM | None = None):
    settings = Settings(
        parser_llm_enabled=False,
        planner_llm_enabled=False,
        tier_llm_enabled=False,
        validator_sample_rate=0,
        continuation_enabled=False,
        call_log_enabled=False,
    )
    llm = RoutingLLMClient(main, aux) if aux is not None else main
    memory = build_inmemory_service(settings=settings, llm=llm)
    memory.bootstrap_persona(vaelith())
    pipeline = InferencePipeline(memory, settings=settings, llm=llm)
    return memory, pipeline, PipelineCognitiveAdapter(
        pipeline,
        state_path=tmp_path / "cognitive-expression-state.json",
        autonomous_log_dir=tmp_path / "autonomous",
        default_session_id="sess_active",
    )


class _SummaryHomeostasis(PassiveHomeostasisAdapter):
    def __init__(
        self,
        *,
        salience: str = "marked",
        watchdog: bool = False,
        policies: list[dict] | None = None,
        state_path=None,
    ) -> None:
        super().__init__(policies=policies, state_path=state_path)
        self.salience = salience
        self.watchdog = watchdog
        self.episode_signature = "homeostasis:marked-load"
        self.apply_event_calls = 0

    def _summary(self, revision: int) -> dict:
        return {
            "schema_version": "homeostatic-interoception-v1",
            "profile_ref": "summary-test@3",
            "snapshot_revision": revision,
            "salience": "critical" if self.watchdog else self.salience,
            "dominant_needs": [
                {
                    "dimension": "cognitive_load",
                    "direction": "high",
                    "normalized_deviation": 0.75,
                    "trend": "worsening",
                    "confidence": 0.9,
                    "semantic_need": "需要降低负荷并简化当前任务",
                }
            ],
            "emotion_hint": {"label": "strained", "intensity": 0.8},
            "policy_refs": ["reduce-load"],
            "episode_signature": self.episode_signature,
        }

    def _decorate_result(self, result):
        payload = dict(result.result or {})
        revision = int(result.committed_revision or 0)
        summary = self._summary(revision)
        snapshot = dict(payload.get("snapshot") or {})
        snapshot["watchdog"] = (
            ["cognitive_load:hard_bound_sustained"] if self.watchdog else []
        )
        snapshot.update(
            {
                "schema_version": "homeostasis-state-v2",
                "profile_id": "summary-test",
                "profile_revision": 3,
            }
        )
        snapshot["interoceptive_summary"] = summary
        payload.update({"snapshot": snapshot, "interoceptive_summary": summary})
        return result.model_copy(update={"result": payload})

    async def health(self):
        health = await super().health()
        reasons = ["cognitive_load:hard_bound_sustained"] if self.watchdog else []
        return health.model_copy(
            update={
                "healthy": not reasons,
                "detail": {"watchdog": reasons, "projection_errors": {}},
            }
        )

    async def get_snapshot(self):
        snapshot = await super().get_snapshot()
        reasons = ["cognitive_load:hard_bound_sustained"] if self.watchdog else []
        summary = self._summary(snapshot.revision)
        if snapshot.revision == 0 and not self.watchdog:
            # Model the normal transition exercised by these tests: the fresh
            # persisted state is nominal and the first applied event produces
            # the configured salience. Tests for crash recovery persist a
            # revision first, so they still boot from authoritative marked.
            summary = {
                **summary,
                "salience": "nominal",
                "dominant_needs": [],
                "emotion_hint": None,
                "policy_refs": [],
                "episode_signature": "homeostasis:nominal",
            }
        return snapshot.model_copy(
            update={
                "payload": {
                    "schema_version": "homeostasis-state-v2",
                    "profile_id": "summary-test",
                    "profile_revision": 3,
                    "policies": list(self._policies),
                    "watchdog": reasons,
                    "interoceptive_summary": summary,
                    "profile": {
                        "profile_id": "summary-test",
                        "revision": 3,
                        "persona_version": "v3",
                        "observation_model_version": "A-v1",
                        "transition_model_version": "B-v1",
                        "preference_model_version": "C-v1",
                        "precision_profile_version": "precision-v1",
                        "coupling_matrix_version": "coupling-v1",
                    },
                }
            }
        )

    async def apply_event(self, command):
        self.apply_event_calls += 1
        return self._decorate_result(await super().apply_event(command))

    async def apply_heartbeat(self, command):
        return self._decorate_result(await super().apply_heartbeat(command))

    async def apply_outcome(self, command):
        return self._decorate_result(await super().apply_outcome(command))


def _expression_policy() -> dict:
    return {
        "policy_id": "reduce-load",
        "label": "降低当前认知负荷",
        "source_drive": "cognitive_load",
        "expected_impacts": {"cognitive_load": -0.1},
        "reversible": True,
        "success_criteria": ["负荷开始下降"],
        "efe": {
            "pragmatic_value": -0.1,
            "ambiguity": 0.1,
            "risk": 0.02,
            "information_gain": 0.01,
            "total": 0.01,
        },
        "rank": 1,
    }


def test_grounded_legacy_interoception_is_redacted_and_compiled_semantically(
    tmp_path,
) -> None:
    async def scenario() -> None:
        evidence_ref = "evidence://sha256/" + "b" * 64
        main = MockLLM(chat_routes={"": "我想把事情说简单一点。"})
        _memory, _pipeline, cognitive = _expression_pipeline_adapter(
            tmp_path,
            main=main,
        )
        homeostasis = _SummaryHomeostasis(
            salience="marked",
            state_path=tmp_path / "grounded-homeostasis-state.json",
        )
        manifest = _manifest(expression_mode="responses").model_copy(
            update={"embodiment": EmbodimentCondition(mode="grounded")}
        )
        supervisor = RuntimeSupervisor(
            manifest=manifest,
            cognitive=cognitive,
            homeostasis=homeostasis,
            state_dir=tmp_path / "grounded-runtime",
        )
        await supervisor.boot_and_recover()
        body_state = LegacyBodyStateSnapshot.model_validate(
            {
                "body_instance_id": "body-test",
                "episode_id": "episode-test",
                "revision": 11,
                "captured_at": NOW,
                "energy": {
                    "capacity_units": 100,
                    "available_units": 20,
                    "available_ratio": 0.2,
                    "delta_units_per_minute": -1,
                    "charging": False,
                    "estimated_time_to_terminal_seconds": 1200,
                    "terminal": False,
                },
                "evidence_refs": [evidence_ref],
            }
        )
        supervisor._body_revision = body_state.revision
        supervisor._interoception = supervisor._build_interoception(body_state)

        accepted = await supervisor.submit_user_message(
            "请简短回复。",
            session_id="grounded-redaction",
        )
        assert accepted is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None
        result = await supervisor.dispatch(queued)
        assert result.status == "completed"

        prepare_intents = [
            record
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_intent"
            and (record.data.get("envelope") or {}).get("command_type")
            == "PrepareResponse"
        ]
        interoception = prepare_intents[-1].data["envelope"]["payload"][
            "interoception"
        ]
        assert interoception["source_revision"] == body_state.revision
        assert "evidence_refs" not in interoception
        assert evidence_ref not in str(interoception)

        prompt = "\n".join(
            message["content"] for call in main.chat_calls for message in call
        )
        assert "精力偏低" in prompt
        assert "精力还在下降" in prompt
        assert evidence_ref not in prompt
        assert "internal_resource" not in prompt
        assert "source_revision" not in prompt
        assert "1200" not in prompt
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def _goal_action_fixture(
    *,
    suffix: str,
    state: ActionExecutionState,
) -> tuple[RuntimeEvent, PreparedResponse, ActionExecution]:
    now = datetime.now(UTC)
    original_event = RuntimeEvent(
        event_id=f"evt_goal_{suffix}",
        receive_seq=1,
        correlation_id=f"corr_goal_{suffix}",
        event_type=RuntimeEventType.USER_MESSAGE,
        priority=RuntimeEventPriority.USER,
        source="user",
        source_time=now,
        received_at=now,
        idempotency_key=f"event:goal:{suffix}",
        manifest_revision=1,
        payload={"text": "请处理这个任务", "session_id": "sess_goal"},
    )
    proposal = GoalProposal(
        goal_id=f"goal_{suffix}",
        policy_id=f"policy_{suffix}",
        goal="完成可逆任务",
        success_criteria=["任务完成"],
        permission_class="low",
        reversible=True,
        verification_method="检查结果",
    )
    prepared = PreparedResponse(
        prepare_id=f"prepare_{suffix}",
        event_id=original_event.event_id,
        correlation_id=original_event.correlation_id,
        session_id="sess_goal",
        decision=proposal,
        read_versions=VersionVector(),
        prepared_at=now,
        expires_at=now + timedelta(minutes=5),
        model_version="mock",
        prompt_version="test",
    )
    action = ActionExecution(
        action_id=f"action_{suffix}",
        goal_id=proposal.goal_id,
        goal=proposal.goal,
        idempotency_key=f"action:{suffix}",
        state=state,
        reversible=True,
        verification_method=proposal.verification_method,
        deadline=now + timedelta(minutes=5),
        outcome=(
            {"summary": "任务已完成"}
            if state == ActionExecutionState.SUCCEEDED
            else None
        ),
    )
    return original_event, prepared, action


def _journal_completed_goal_action(
    supervisor: RuntimeSupervisor,
    *,
    original_event: RuntimeEvent,
    prepared: PreparedResponse,
    action: ActionExecution,
) -> None:
    proposed = action.model_copy(
        update={"state": ActionExecutionState.READY, "outcome": None}
    )
    supervisor.journal.append(
        "event_accepted",
        event_id=original_event.event_id,
        receive_seq=original_event.receive_seq,
        correlation_id=original_event.correlation_id,
        idempotency_key=original_event.idempotency_key,
        data={"event": original_event.model_dump(mode="json")},
    )
    supervisor.journal.append(
        "action_proposed",
        event_id=original_event.event_id,
        receive_seq=original_event.receive_seq,
        correlation_id=original_event.correlation_id,
        idempotency_key=action.idempotency_key,
        data={
            "action": proposed.model_dump(mode="json"),
            "prepared": prepared.model_dump(mode="json"),
        },
    )
    supervisor.journal.append(
        "action_reconciled",
        idempotency_key=action.idempotency_key,
        data={"action": action.model_dump(mode="json")},
    )
    supervisor.journal.append(
        "processing_completed",
        event_id=original_event.event_id,
        receive_seq=original_event.receive_seq,
        correlation_id=original_event.correlation_id,
        data={"status": "completed"},
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


def test_homeostasis_watchdog_pauses_synchronously_without_control_queue_hop(tmp_path) -> None:
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
        result = await supervisor.dispatch(queued)

        assert result.decision_kind == "watchdog_pause"
        assert await supervisor.queue.get(timeout=0) is None
        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert (
            supervisor.lifecycle_reason
            == "homeostasis reported an unsafe or unstable state"
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_persisted_homeostasis_watchdog_blocks_resume_until_cleared(
    tmp_path,
) -> None:
    class PersistedBoundaryHomeostasis(PassiveHomeostasisAdapter):
        reasons = ["cognitive_load:hard_bound_sustained"]

        async def health(self):
            health = await super().health()
            return health.model_copy(
                update={
                    "healthy": not self.reasons,
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

        with pytest.raises(RuntimeStateError, match="watchdog remains active"):
            await supervisor.resume(reason="researcher reviewed persisted boundary")
        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED

        PersistedBoundaryHomeostasis.reasons = []
        await supervisor.resume(reason="researcher verified a recovered boundary")
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        assert supervisor._homeostasis_watchdog_signature is None
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_critical_boot_defers_cognitive_and_action_reconciliation_until_resume(
    tmp_path,
) -> None:
    class CountingActionBackend:
        def __init__(self) -> None:
            self.reconcile_calls = 0

        async def reconcile(self, execution):
            self.reconcile_calls += 1
            return ActionBackendResult(
                success=False,
                summary="external action did not complete",
                retry_safe=True,
            )

    main = MockLLM(chat_script=["我需要先缓一下，再继续回应。"])
    aux = MockLLM(chat_script=["SPEAK\n恢复后可以重新评估。"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main, aux=aux)
    homeostasis = _SummaryHomeostasis(
        salience="marked",
        watchdog=True,
        policies=[_expression_policy()],
    )
    action_backend = CountingActionBackend()
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        action_coordinator=ActionCoordinator(action_backend),
        state_dir=tmp_path / "runtime-critical-reconciliation-gate",
    )
    issued_at = datetime.now(UTC)
    marked_summary = _SummaryHomeostasis(salience="marked")._summary(0)
    common = {
        "runtime_instance_id": supervisor.runtime_instance_id,
        "manifest_revision": supervisor.manifest.revision,
        "issued_at": issued_at,
        "deadline": issued_at + timedelta(minutes=5),
        "expected_revision": 0,
        "version_vector": VersionVector(homeostasis_state=0),
    }
    prepare = DomainCommandEnvelope[dict[str, object]](
        command_id="cmd_deferred_prepare",
        command_type="PrepareResponse",
        event_id="evt_deferred_prepare",
        receive_seq=1,
        correlation_id="corr_deferred_prepare",
        idempotency_key="cognitive:prepare:deferred",
        payload={
            "text": "恢复后请继续。",
            "session_id": "sess_deferred",
            "speaker": "user",
            "channel": "chat",
            "source_time": issued_at,
            "homeostatic_interoception": marked_summary,
            "homeostatic_profile_ref": "summary-test@3",
        },
        **common,
    )
    assess = DomainCommandEnvelope[dict[str, object]](
        command_id="cmd_deferred_assess",
        command_type="AssessAutonomous",
        event_id="evt_deferred_assess",
        receive_seq=2,
        correlation_id="corr_deferred_assess",
        idempotency_key="cognitive:assess:deferred",
        payload={
            "homeostasis": {},
            "policies": [_expression_policy()],
            "session_id": "sess_deferred",
            "channel": "chat",
            "homeostatic_interoception": marked_summary,
            "homeostatic_profile_ref": "summary-test@3",
        },
        **common,
    )
    for envelope in (prepare, assess):
        supervisor.journal.append(
            "domain_command_intent",
            event_id=envelope.event_id,
            receive_seq=envelope.receive_seq,
            correlation_id=envelope.correlation_id,
            command_id=envelope.command_id,
            idempotency_key=envelope.idempotency_key,
            data={"service": "cognitive", "envelope": envelope.model_dump(mode="json")},
        )
    running_action = ActionExecution(
        action_id="action_deferred_reconcile",
        goal_id="goal_deferred_reconcile",
        goal="确认外部动作结果",
        idempotency_key="action:deferred-reconcile",
        state=ActionExecutionState.RUNNING,
        reversible=True,
        verification_method="读取外部状态",
        deadline=issued_at + timedelta(minutes=5),
    )
    supervisor.journal.append(
        "action_execution_intent",
        idempotency_key=running_action.idempotency_key,
        data={"action": running_action.model_dump(mode="json")},
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()

        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert main.chat_calls == []
        assert aux.chat_calls == []
        assert supervisor.delivery_backend.deliveries == {}
        assert action_backend.reconcile_calls == 0
        assert supervisor.action_coordinator.get(running_action.action_id) is None
        completed_commands = {
            record.command_id
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_result"
        }
        assert prepare.command_id not in completed_commands
        assert assess.command_id not in completed_commands
        assert any(
            record.record_type == "reconciliation_deferred_homeostasis_watchdog"
            for record in supervisor.journal.records()
        )

        homeostasis.watchdog = False
        await supervisor.resume(reason="homeostasis recovered")

        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        assert len(main.chat_calls) == 1
        assert len(aux.chat_calls) == 1
        assert supervisor.delivery_backend.deliveries == {}
        assert action_backend.reconcile_calls == 1
        completed_commands = {
            record.command_id
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_result"
        }
        assert prepare.command_id in completed_commands
        assert assess.command_id in completed_commands
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_response_paths_route_same_revision_homeostatic_interoception(tmp_path) -> None:
    main = MockLLM(chat_script=["我想先慢一点。", "我想先安静一会儿。"])
    aux = MockLLM(chat_script=["SPEAK\n当前负荷值得自然表达边界。"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main, aux=aux)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-expression-routing",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        user = await supervisor.submit_user_message("你还好吗？", session_id="sess_user")
        assert user is not None
        queued_user = await supervisor.queue.get(timeout=0)
        assert queued_user is not None
        user_result = await supervisor.dispatch(queued_user)
        assert user_result.decision_kind == "reply"

        homeostasis.episode_signature = "homeostasis:marked-load-changed"
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        heartbeat = await supervisor.queue.get(timeout=0)
        assert heartbeat is not None
        await supervisor.dispatch(heartbeat)
        marked_idle = await supervisor.queue.get(timeout=0)
        assert marked_idle is not None
        assert marked_idle.event_type == RuntimeEventType.IDLE
        assert marked_idle.payload["trigger_reason"] == "homeostasis_marked"
        idle_result = await supervisor.dispatch(marked_idle)
        assert idle_result.decision_kind == "reply"

        intents = [
            record.data["envelope"]
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_intent"
            and record.data.get("service") == "cognitive"
            and (record.data.get("envelope") or {}).get("command_type")
            in {"AssessAutonomous", "PrepareResponse"}
        ]
        user_prepare = next(
            envelope
            for envelope in intents
            if envelope["command_type"] == "PrepareResponse"
            and envelope["event_id"] == user.event_id
        )
        autonomous = [
            envelope for envelope in intents if envelope["event_id"] == marked_idle.event_id
        ]
        assess = next(
            envelope for envelope in autonomous if envelope["command_type"] == "AssessAutonomous"
        )
        autonomous_prepare = next(
            envelope for envelope in autonomous if envelope["command_type"] == "PrepareResponse"
        )
        for envelope in (user_prepare, assess, autonomous_prepare):
            summary = envelope["payload"]["homeostatic_interoception"]
            assert summary["snapshot_revision"] == envelope["version_vector"][
                "homeostasis_state"
            ]
            assert envelope["payload"]["homeostatic_profile_ref"] == "summary-test@3"
            assert envelope["payload"]["homeostatic_profile_ref"] == summary["profile_ref"]
        assert (
            assess["payload"]["homeostatic_interoception"]
            == autonomous_prepare["payload"]["homeostatic_interoception"]
        )
        delivery_records = [
            record
            for record in supervisor.journal.records()
            if record.record_type == "reply_delivery"
        ]
        assert delivery_records
        assert all(record.data.get("homeostatic_summary_hash") for record in delivery_records)
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_homeostatic_summary_contract_rejects_cross_projection_and_profile_mismatch(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis()
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="responses"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-summary-contract",
    )
    summary = homeostasis._summary(4)

    conflicting = dict(summary)
    conflicting["episode_signature"] = "homeostasis:other-episode"
    with pytest.raises(DomainCommandError, match="top-level and snapshot"):
        supervisor._validate_homeostasis_contract(
            DomainSnapshot(
                service="homeostasis",
                revision=4,
                payload={
                    "interoceptive_summary": summary,
                    "snapshot": {"interoceptive_summary": conflicting},
                },
            )
        )

    wrong_manifest_profile = dict(summary)
    wrong_manifest_profile["profile_ref"] = "other-profile@3"
    with pytest.raises(DomainCommandError, match="profile does not match the manifest"):
        supervisor._validate_homeostasis_contract(
            DomainSnapshot(
                service="homeostasis",
                revision=4,
                payload={"interoceptive_summary": wrong_manifest_profile},
            )
        )

    with pytest.raises(DomainCommandError, match="profile does not match its snapshot"):
        supervisor._validate_homeostasis_contract(
            DomainSnapshot(
                service="homeostasis",
                revision=4,
                payload={
                    "interoceptive_summary": summary,
                    "snapshot": {
                        "schema_version": "homeostasis-state-v2",
                        "profile_id": "other-profile",
                        "profile_revision": 3,
                        "interoceptive_summary": summary,
                    },
                },
            )
        )
    cognitive.pipeline.memory.close()


def test_idempotent_homeostasis_replay_refreshes_authoritative_snapshot(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis()
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="responses"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-homeostasis-replay",
    )

    async def apply_perception(index: int) -> RuntimeEvent:
        event = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source=f"sensor-{index}",
            payload={"sample": index},
        )
        assert event is not None
        queued = await supervisor.queue.get(
            timeout=0,
            predicate=lambda candidate: candidate.event_id == event.event_id,
        )
        assert queued is not None
        completed = await supervisor.dispatch(queued)
        assert completed.status == "completed"
        return event

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        first = await apply_perception(1)
        homeostasis.episode_signature = "homeostasis:authoritative-latest"
        await apply_perception(2)
        assert supervisor.version_vector.homeostasis_state == 2

        replayed = await supervisor.dispatch(first)

        assert replayed.status == "completed"
        assert supervisor.version_vector.homeostasis_state == 2
        summary = supervisor._validated_homeostatic_interoception(required=True)
        assert summary is not None
        assert summary["snapshot_revision"] == 2
        assert summary["episode_signature"] == "homeostasis:authoritative-latest"
        refresh = next(
            record
            for record in reversed(supervisor.journal.records())
            if record.record_type == "homeostasis_idempotent_replay_refreshed"
        )
        assert refresh.data == {
            "replayed_revision": 1,
            "previous_runtime_revision": 2,
            "authoritative_revision": 2,
        }
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("expression_mode", "injected"),
    [("shadow", False), ("responses", True)],
)
def test_homeostatic_expression_rollout_gates_injection_and_proactive_trigger(
    tmp_path,
    expression_mode: str,
    injected: bool,
) -> None:
    main = MockLLM(chat_script=["我听见了。"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode=expression_mode),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / f"runtime-expression-{expression_mode}",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="camera",
            payload={"frame": 1},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None
        await supervisor.dispatch(queued_perception)
        assert not any(
            event.event_type == RuntimeEventType.IDLE
            for event in supervisor.queue.snapshot()
        )

        user = await supervisor.submit_user_message("你听见了吗？", session_id="sess_user")
        assert user is not None
        queued_user = await supervisor.queue.get(timeout=0)
        assert queued_user is not None
        await supervisor.dispatch(queued_user)
        prepare_intent = next(
            record
            for record in reversed(supervisor.journal.records())
            if record.record_type == "domain_command_intent"
            and (record.data.get("envelope") or {}).get("command_type") == "PrepareResponse"
        )
        payload = prepare_intent.data["envelope"]["payload"]
        assert ("homeostatic_interoception" in payload) is injected
        assert ("homeostatic_profile_ref" in payload) is injected
        observed = [
            record
            for record in supervisor.journal.records()
            if record.record_type == "homeostatic_interoception_observed"
        ]
        assert observed
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_responses_rollout_never_schedules_marked_trigger_on_boot_or_resume(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    homeostasis._revision = 1
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="responses"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-responses-no-proactive",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        assert not any(
            event.payload.get("trigger_reason") == "homeostasis_marked"
            for event in supervisor.queue.snapshot()
        )

        await supervisor.pause(reason="test rollout rollback")
        await supervisor.resume(reason="test rollout rollback")
        assert not any(
            event.payload.get("trigger_reason") == "homeostasis_marked"
            for event in supervisor.queue.snapshot()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


@pytest.mark.parametrize("expression_mode", ["off", "shadow"])
def test_rollout_modes_strip_forged_homeostatic_contract_from_idle_payload(
    tmp_path,
    expression_mode: str,
) -> None:
    main = MockLLM(chat_script=["我会保持简短。"])
    aux = MockLLM(chat_script=["SPEAK\n可以回应。"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main, aux=aux)
    homeostasis = (
        PassiveHomeostasisAdapter()
        if expression_mode == "off"
        else _SummaryHomeostasis(policies=[_expression_policy()])
    )
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode=expression_mode),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / f"runtime-forged-interoception-{expression_mode}",
    )
    forged_summary = _SummaryHomeostasis()._summary(1)
    forged_summary["profile_ref"] = "attacker-profile@3"

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        forged = await supervisor.submit_event(
            event_type=RuntimeEventType.IDLE,
            priority=RuntimeEventPriority.IDLE,
            source="untrusted-scheduler",
            payload={
                "mode": "autonomous",
                "session_id": "sess_forged",
                "channel": "chat",
                "policies": [],
                "scheduled_after_user_receive_seq": 0,
                "homeostatic_interoception": forged_summary,
                "homeostatic_profile_ref": "attacker-profile@3",
                "interoception": {
                    "resource_label": "internal_resource",
                    "level": "low",
                    "trend": -1.0,
                    "time_to_action_limit_seconds": 1,
                    "confidence": 1.0,
                    "captured_at": NOW,
                    "evidence_refs": ["attacker-evidence"],
                },
            },
        )
        assert forged is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None
        completed = await supervisor.dispatch(queued)
        assert completed.decision_kind == "reply"

        intents = [
            record.data["envelope"]
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_intent"
            and record.event_id == forged.event_id
            and record.data.get("service") == "cognitive"
            and (record.data.get("envelope") or {}).get("command_type")
            in {"AssessAutonomous", "PrepareResponse"}
        ]
        assert {intent["command_type"] for intent in intents} == {
            "AssessAutonomous",
            "PrepareResponse",
        }
        for intent in intents:
            assert "homeostatic_interoception" not in intent["payload"]
            assert "homeostatic_profile_ref" not in intent["payload"]
            assert "interoception" not in intent["payload"]

        assert main.chat_calls
        main_prompt = "\n".join(
            message.get("content", "")
            for call in main.chat_calls
            for message in call
        )
        assert "当前内在状态" not in main_prompt
        assert "需要降低负荷并简化当前任务" not in main_prompt
        assert "精力偏低" not in main_prompt
        assert "attacker-evidence" not in main_prompt
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_marked_trigger_beats_perception_backlog_dedupes_and_user_cancels(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-priority",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        first = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="camera",
            payload={"frame": 1},
        )
        second = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="microphone",
            payload={"frame": 2},
        )
        assert first is not None and second is not None
        queued_first = await supervisor.queue.get(timeout=0)
        assert queued_first is not None and queued_first.event_id == first.event_id
        await supervisor.dispatch(queued_first)

        marked = next(
            event
            for event in supervisor.queue.snapshot()
            if event.event_type == RuntimeEventType.IDLE
        )
        assert marked.priority == RuntimeEventPriority.IMPORTANT_ENVIRONMENT
        assert marked.payload["trigger_reason"] == "homeostasis_marked"

        queued_second = await supervisor.queue.get(
            timeout=0,
            predicate=lambda event: event.event_id == second.event_id,
        )
        assert queued_second is not None
        await supervisor.dispatch(queued_second)
        assert len(
            [
                event
                for event in supervisor.queue.snapshot()
                if event.event_type == RuntimeEventType.IDLE
            ]
        ) == 1

        user = await supervisor.submit_user_message("我回来了。", session_id="sess_user")
        assert user is not None
        cancelled = await supervisor.queue.get(
            timeout=0,
            predicate=lambda event: event.event_id == marked.event_id,
        )
        assert cancelled is not None
        cancelled_result = await supervisor.dispatch(cancelled)
        assert cancelled_result.status == "ignored"
        assert "cancelled" in cancelled_result.detail["reason"]
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_idle_queued_before_marked_is_suppressed_by_dedicated_episode_attempt(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(
        salience="nominal",
        policies=[_expression_policy()],
    )
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-prequeued-idle-marked-cooldown",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        ordinary_idle = await supervisor.submit_event(
            event_type=RuntimeEventType.IDLE,
            priority=RuntimeEventPriority.IDLE,
            source="scheduler",
            payload={
                "mode": "autonomous",
                "policies": [_expression_policy()],
                "scheduled_after_user_receive_seq": 0,
                "channel": "chat",
            },
        )
        assert ordinary_idle is not None
        homeostasis.salience = "marked"
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="sensor",
            payload={"sample": 1},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None and queued_perception.event_id == perception.event_id
        await supervisor.dispatch(queued_perception)
        assert any(
            event.payload.get("trigger_reason") == "homeostasis_marked"
            for event in supervisor.queue.snapshot()
        )

        queued_idle = await supervisor.queue.get(
            timeout=0,
            predicate=lambda event: event.event_id == ordinary_idle.event_id,
        )
        assert queued_idle is not None
        ignored = await supervisor.dispatch(queued_idle)

        assert ignored.status == "ignored"
        assert "episode cooldown" in ignored.detail["reason"]
        assert not any(
            record.record_type == "domain_command_intent"
            and record.event_id == ordinary_idle.event_id
            and record.data.get("service") == "cognitive"
            for record in supervisor.journal.records()
        )
        assert supervisor.delivery_backend.deliveries == {}
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_heartbeat_idle_during_marked_cooldown_skips_cognitive_and_delivery(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-heartbeat-idle-marked-cooldown",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="sensor",
            payload={"sample": 1},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None
        await supervisor.dispatch(queued_perception)
        dedicated = await supervisor.queue.get(timeout=0)
        assert dedicated is not None
        assert dedicated.payload["trigger_reason"] == "homeostasis_marked"

        heartbeat = await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        assert heartbeat is not None
        queued_heartbeat = await supervisor.queue.get(timeout=0)
        assert queued_heartbeat is not None and queued_heartbeat.event_id == heartbeat.event_id
        await supervisor.dispatch(queued_heartbeat)
        ordinary_idle = await supervisor.queue.get(timeout=0)
        assert ordinary_idle is not None
        assert ordinary_idle.event_type == RuntimeEventType.IDLE
        assert ordinary_idle.payload.get("trigger_reason") is None

        ignored = await supervisor.dispatch(ordinary_idle)

        assert ignored.status == "ignored"
        assert "episode cooldown" in ignored.detail["reason"]
        assert not any(
            record.record_type == "domain_command_intent"
            and record.event_id == ordinary_idle.event_id
            and record.data.get("service") == "cognitive"
            for record in supervisor.journal.records()
        )
        assert supervisor.delivery_backend.deliveries == {}
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_running_autonomous_reply_is_rechecked_inside_delivery_lock(
    tmp_path,
) -> None:
    main = MockLLM(chat_script=["我想先停一下，稍后再继续。"])
    aux = MockLLM(chat_script=["SPEAK\n当前负荷值得自然表达边界。"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main, aux=aux)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-autonomous-delivery-race",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="sensor",
            payload={"sample": 1},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None
        await supervisor.dispatch(queued_perception)
        marked = await supervisor.queue.get(timeout=0)
        assert marked is not None and marked.payload["trigger_reason"] == "homeostasis_marked"

        outer_delivery_check_passed = asyncio.Event()
        eligibility_checks = [0]
        original_ineligible = supervisor._autonomous_ineligible

        def tracked_ineligible(event: RuntimeEvent) -> str | None:
            reason = original_ineligible(event)
            if event.event_id == marked.event_id:
                eligibility_checks[0] += 1
                # Start-of-dispatch, post-assessment, post-prepare, then the
                # outer _handle_decision check immediately before the lock.
                if eligibility_checks[0] == 4:
                    assert reason is None
                    outer_delivery_check_passed.set()
            return reason

        supervisor._autonomous_ineligible = tracked_ineligible  # type: ignore[method-assign]
        await supervisor._foreground_lock.acquire()
        dispatch_task = asyncio.create_task(supervisor.dispatch(marked))
        try:
            await asyncio.wait_for(outer_delivery_check_passed.wait(), timeout=2)
            user = await supervisor.submit_user_message(
                "先回应我。",
                session_id="sess_user",
            )
            assert user is not None
        finally:
            supervisor._foreground_lock.release()

        cancelled = await dispatch_task
        assert cancelled.status == "completed"
        assert cancelled.decision_kind == "no_action"
        assert "user activity" in cancelled.detail["reason"]
        assert eligibility_checks[0] == 5
        assert supervisor.delivery_backend.deliveries == {}
        assert not any(
            record.record_type == "reply_delivery_intent"
            and record.event_id == marked.event_id
            for record in supervisor.journal.records()
        )
        assert any(
            record.record_type == "autonomous_cancelled_before_delivery"
            and record.event_id == marked.event_id
            for record in supervisor.journal.records()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_user_ingress_cancels_autonomous_before_async_event_listener(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime-user-ingress-preemption",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        idle = await supervisor.submit_event(
            event_type=RuntimeEventType.IDLE,
            priority=RuntimeEventPriority.IDLE,
            source="scheduler",
            payload={
                "mode": "autonomous",
                "policies": [],
                "scheduled_after_user_receive_seq": 0,
                "channel": "chat",
            },
        )
        assert idle is not None

        listener_entered = asyncio.Event()
        release_listener = asyncio.Event()

        async def blocking_listener(topic: str, payload: dict[str, object]) -> None:
            if topic == "event.accepted" and payload.get("event_type") == "user_message":
                listener_entered.set()
                await release_listener.wait()

        supervisor.add_listener(blocking_listener)
        user_task = asyncio.create_task(supervisor.submit_user_message("先回应我。"))
        try:
            await asyncio.wait_for(listener_entered.wait(), timeout=2)
            assert not user_task.done()
            accepted_user = next(
                RuntimeEvent.model_validate(record.data["event"])
                for record in supervisor.journal.records()
                if record.record_type == "event_accepted"
                and (record.data.get("event") or {}).get("event_type") == "user_message"
            )
            assert accepted_user.event_id in supervisor._pending_user_ingress_event_ids
            assert supervisor._last_user_receive_seq > idle.receive_seq
            assert idle.event_id in supervisor._cancelled_autonomous_event_ids
            assert supervisor._autonomous_ineligible(idle) == "autonomous output was cancelled"
        finally:
            release_listener.set()

        user = await user_task
        assert user is not None
        assert user.event_id not in supervisor._pending_user_ingress_event_ids
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_resume_does_not_schedule_marked_while_user_ingress_is_pending(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(salience="marked", policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-resume-pending-user",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await supervisor.pause(reason="test pending user during resume")
        # Revision zero is intentionally projected as nominal by the test
        # adapter.  Make the next service reconciliation authoritative marked.
        homeostasis._revision = 1

        listener_entered = asyncio.Event()
        release_listener = asyncio.Event()

        async def blocking_listener(topic: str, payload: dict[str, object]) -> None:
            if topic == "event.accepted" and payload.get("event_type") == "user_message":
                listener_entered.set()
                await release_listener.wait()

        supervisor.add_listener(blocking_listener)
        user_task = asyncio.create_task(supervisor.submit_user_message("恢复后先回应我。"))
        await asyncio.wait_for(listener_entered.wait(), timeout=2)
        assert supervisor._pending_user_ingress_event_ids

        await supervisor.resume(reason="test resume")
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        assert not any(
            candidate.payload.get("trigger_reason") == "homeostasis_marked"
            for candidate in supervisor.queue.snapshot()
        )
        assert supervisor.delivery_backend.deliveries == {}

        release_listener.set()
        user = await user_task
        assert user is not None
        assert user.event_id not in supervisor._pending_user_ingress_event_ids
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_user_arriving_during_heartbeat_policy_proposal_supersedes_idle(
    tmp_path,
) -> None:
    class BlockingPolicyHomeostasis(PassiveHomeostasisAdapter):
        def __init__(self) -> None:
            super().__init__(policies=[_expression_policy()])
            self.policy_started = asyncio.Event()
            self.release_policy = asyncio.Event()

        async def propose_policies(self, **kwargs):
            self.policy_started.set()
            await self.release_policy.wait()
            return await super().propose_policies(**kwargs)

    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = BlockingPolicyHomeostasis()
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-heartbeat-policy-user-race",
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
        queued_heartbeat = await supervisor.queue.get(timeout=0)
        assert queued_heartbeat is not None

        heartbeat_task = asyncio.create_task(supervisor.dispatch(queued_heartbeat))
        await asyncio.wait_for(homeostasis.policy_started.wait(), timeout=2)

        listener_entered = asyncio.Event()
        release_listener = asyncio.Event()

        async def blocking_listener(topic: str, payload: dict[str, object]) -> None:
            if topic == "event.accepted" and payload.get("event_type") == "user_message":
                listener_entered.set()
                await release_listener.wait()

        supervisor.add_listener(blocking_listener)
        user_task = asyncio.create_task(supervisor.submit_user_message("先回应我。"))
        await asyncio.wait_for(listener_entered.wait(), timeout=2)
        assert not user_task.done()

        homeostasis.release_policy.set()
        await heartbeat_task
        idle = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
        )
        assert idle.payload["scheduled_after_user_receive_seq"] < (
            supervisor._last_user_receive_seq
        )

        queued_idle = await supervisor.queue.get(
            timeout=0,
            predicate=lambda candidate: candidate.event_id == idle.event_id,
        )
        assert queued_idle is not None
        ignored = await supervisor.dispatch(queued_idle)
        assert ignored.status == "ignored"
        assert "user activity" in ignored.detail["reason"]
        assert not any(
            record.record_type == "domain_command_intent"
            and record.event_id == idle.event_id
            and record.data.get("service") == "cognitive"
            for record in supervisor.journal.records()
        )
        assert supervisor.delivery_backend.deliveries == {}

        release_listener.set()
        user = await user_task
        assert user is not None
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_user_arriving_during_marked_policy_proposal_supersedes_trigger(
    tmp_path,
) -> None:
    class BlockingPolicyHomeostasis(_SummaryHomeostasis):
        def __init__(self) -> None:
            super().__init__(policies=[_expression_policy()])
            self.policy_started = asyncio.Event()
            self.release_policy = asyncio.Event()

        async def propose_policies(self, **kwargs):
            self.policy_started.set()
            await self.release_policy.wait()
            return await super().propose_policies(**kwargs)

    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = BlockingPolicyHomeostasis()
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-policy-user-race",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="sensor",
            payload={"sample": 1},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None

        perception_task = asyncio.create_task(supervisor.dispatch(queued_perception))
        await asyncio.wait_for(homeostasis.policy_started.wait(), timeout=2)

        listener_entered = asyncio.Event()
        release_listener = asyncio.Event()

        async def blocking_listener(topic: str, payload: dict[str, object]) -> None:
            if topic == "event.accepted" and payload.get("event_type") == "user_message":
                listener_entered.set()
                await release_listener.wait()

        supervisor.add_listener(blocking_listener)
        user_task = asyncio.create_task(supervisor.submit_user_message("先回应我。"))
        await asyncio.wait_for(listener_entered.wait(), timeout=2)
        assert not user_task.done()

        homeostasis.release_policy.set()
        await perception_task
        marked = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.payload.get("trigger_reason") == "homeostasis_marked"
        )
        assert marked.payload["scheduled_after_user_receive_seq"] < (
            supervisor._last_user_receive_seq
        )

        queued_marked = await supervisor.queue.get(
            timeout=0,
            predicate=lambda candidate: candidate.event_id == marked.event_id,
        )
        assert queued_marked is not None
        ignored = await supervisor.dispatch(queued_marked)
        assert ignored.status == "ignored"
        assert "user activity" in ignored.detail["reason"]
        assert not any(
            record.record_type == "domain_command_intent"
            and record.event_id == marked.event_id
            and record.data.get("service") == "cognitive"
            for record in supervisor.journal.records()
        )
        assert supervisor.delivery_backend.deliveries == {}

        release_listener.set()
        user = await user_task
        assert user is not None
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_goal_reconsideration_schedules_a_distinct_homeostasis_marked_idle(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-after-reconsideration",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        reconsideration = await supervisor.submit_event(
            event_type=RuntimeEventType.GOAL_RECONSIDERATION,
            priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT,
            source="embodiment",
            payload={"reason": "resource boundary crossed"},
        )
        assert reconsideration is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None and queued.event_id == reconsideration.event_id
        await supervisor.dispatch(queued)

        marked = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
        )
        assert marked.payload["trigger_reason"] == "homeostasis_marked"
        assert marked.payload["source_homeostasis_revision"] == 1
        assert not any(
            record.record_type == "homeostatic_marked_expression_opportunity"
            and record.event_id == reconsideration.event_id
            for record in supervisor.journal.records()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_action_result_uses_current_event_and_schedules_first_marked_trigger(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-action-outcome",
    )
    now = datetime.now(UTC)
    original_event = _event(
        40,
        RuntimeEventType.USER_MESSAGE,
        RuntimeEventPriority.USER,
    )
    proposal = GoalProposal(
        goal_id="goal_outcome",
        policy_id="policy_outcome",
        goal="完成可逆任务",
        success_criteria=["任务完成"],
        permission_class="low",
        reversible=True,
        verification_method="检查结果",
    )
    prepared = PreparedResponse(
        prepare_id="prepare_outcome",
        event_id=original_event.event_id,
        correlation_id=original_event.correlation_id,
        session_id="sess_goal",
        decision=proposal,
        read_versions=VersionVector(),
        prepared_at=now,
        expires_at=now + timedelta(minutes=5),
        model_version="mock",
        prompt_version="test",
    )
    action = ActionExecution(
        action_id="action_outcome",
        goal_id=proposal.goal_id,
        goal=proposal.goal,
        idempotency_key="action:outcome",
        state=ActionExecutionState.SUCCEEDED,
        reversible=True,
        verification_method=proposal.verification_method,
        deadline=now + timedelta(minutes=5),
        outcome={"summary": "任务已完成"},
    )
    committed_events: list[RuntimeEvent] = []

    async def record_commit(event, prepared_response, evidence):
        committed_events.append(event)
        return None

    supervisor._commit_prepared = record_commit  # type: ignore[method-assign]

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        supervisor.action_coordinator.restore(action)
        supervisor._action_context[action.action_id] = {
            "event": original_event,
            "prepared": prepared,
        }
        outcome_event = await supervisor.submit_event(
            event_type=RuntimeEventType.ACTION_RESULT,
            priority=RuntimeEventPriority.ACTION_RESULT,
            source="action_coordinator",
            payload={"action_id": action.action_id, "state": action.state.value},
        )
        assert outcome_event is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None and queued.event_id == outcome_event.event_id

        completed = await supervisor.dispatch(queued)

        assert completed.decision_kind == "action_result"
        assert committed_events == [original_event]
        marked = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
        )
        assert marked.payload["trigger_reason"] == "homeostasis_marked"
        assert marked.payload["source_homeostasis_revision"] == 1
        apply_outcome = next(
            record
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_intent"
            and (record.data.get("envelope") or {}).get("command_type") == "ApplyOutcome"
        )
        assert apply_outcome.event_id == outcome_event.event_id
        for record_type in (
            "homeostatic_interoception_observed",
            "homeostatic_marked_trigger_scheduled",
            "action_completed",
            "action_interaction_committed",
        ):
            record = next(
                item
                for item in supervisor.journal.records()
                if item.record_type == record_type
            )
            assert record.event_id == outcome_event.event_id
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_rejected_action_uses_non_user_outcome_provenance_and_schedules_marked(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-rejected-action",
    )
    original_event, prepared, action = _goal_action_fixture(
        suffix="rejected",
        state=ActionExecutionState.READY,
    )

    async def skip_commit(event, prepared_response, evidence):
        assert event == original_event
        return None

    supervisor._commit_prepared = skip_commit  # type: ignore[method-assign]

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        supervisor.action_coordinator.restore(action)
        supervisor._action_context[action.action_id] = {
            "event": original_event,
            "prepared": prepared,
        }

        rejected = await supervisor.reject_action(
            action.action_id,
            reason="researcher rejected",
        )

        assert rejected.state == ActionExecutionState.CANCELLED
        assert not any(
            record.record_type == "domain_command_intent"
            and (record.data.get("envelope") or {}).get("command_type")
            == "ApplyOutcome"
            for record in supervisor.journal.records()
        )
        outcome_event = await supervisor.queue.get(timeout=0)
        assert outcome_event is not None
        assert outcome_event.event_type == RuntimeEventType.ACTION_RESULT
        outcome_result = await supervisor.dispatch(outcome_event)
        assert outcome_result.status == "completed"
        apply_outcome = next(
            record
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_intent"
            and (record.data.get("envelope") or {}).get("command_type")
            == "ApplyOutcome"
        )
        assert apply_outcome.event_id != original_event.event_id
        causal_event_id = apply_outcome.event_id
        marked = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
        )
        assert marked.payload["trigger_reason"] == "homeostasis_marked"
        assert any(
            record.record_type == "homeostatic_marked_trigger_scheduled"
            and record.event_id == causal_event_id
            for record in supervisor.journal.records()
        )
        assert not any(
            record.record_type == "homeostatic_marked_expression_opportunity"
            and record.event_id == original_event.event_id
            for record in supervisor.journal.records()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_boot_flushes_marked_trigger_from_recovered_terminal_action(tmp_path) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-recovered-action",
    )
    original_event, prepared, action = _goal_action_fixture(
        suffix="recovered_marked",
        state=ActionExecutionState.SUCCEEDED,
    )
    _journal_completed_goal_action(
        supervisor,
        original_event=original_event,
        prepared=prepared,
        action=action,
    )

    async def skip_commit(event, prepared_response, evidence):
        assert event == original_event
        return None

    supervisor._commit_prepared = skip_commit  # type: ignore[method-assign]

    async def scenario() -> None:
        await supervisor.boot_and_recover()

        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        marked = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
        )
        assert marked.payload["trigger_reason"] == "homeostasis_marked"
        assert any(
            record.record_type == "homeostatic_marked_trigger_deferred_lifecycle"
            for record in supervisor.journal.records()
        )
        apply_outcome = next(
            record
            for record in supervisor.journal.records()
            if record.record_type == "domain_command_intent"
            and (record.data.get("envelope") or {}).get("command_type")
            == "ApplyOutcome"
        )
        assert apply_outcome.event_id != original_event.event_id
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_boot_restores_unfinished_deferred_marked_trigger_from_journal(tmp_path) -> None:
    state_dir = tmp_path / "runtime-marked-deferred-crash"
    prior_journal = RuntimeJournal(
        state_dir / "journal",
        runtime_instance_id="runtime_before_deferred_flush",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    summary = _SummaryHomeostasis()._summary(1)
    prior_journal.append(
        "homeostatic_marked_trigger_deferred_lifecycle",
        event_id="event_action_outcome_before_crash",
        receive_seq=1,
        correlation_id="corr_action_outcome_before_crash",
        data={
            "lifecycle": RuntimeLifecycle.RECOVERING.value,
            "homeostasis_revision": 1,
            "homeostatic_summary_hash": "0" * 64,
            "homeostatic_salience": "marked",
            "homeostatic_episode_signature": summary["episode_signature"],
        },
    )
    prior_journal.flush()

    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    homeostasis._revision = 1
    recovered_journal = RuntimeJournal(
        state_dir / "journal",
        runtime_instance_id="runtime_after_deferred_crash",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=state_dir,
        journal=recovered_journal,
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()

        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        marked = next(
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
        )
        assert marked.payload["trigger_reason"] == "homeostasis_marked"
        assert marked.payload["source_episode_signature"] == summary["episode_signature"]
        assert any(
            record.record_type == "homeostatic_marked_trigger_scheduled"
            for record in supervisor.journal.records()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_boot_schedules_marked_trigger_when_homeostasis_committed_before_runtime_result(
    tmp_path,
) -> None:
    state_dir = tmp_path / "runtime-marked-domain-commit-crash"
    homeostasis_path = tmp_path / "homeostasis-marked-domain-commit-crash.json"
    heartbeat = RuntimeEvent(
        event_id="event_heartbeat_domain_committed",
        receive_seq=1,
        correlation_id="corr_heartbeat_domain_committed",
        event_type=RuntimeEventType.HEARTBEAT,
        priority=RuntimeEventPriority.HEARTBEAT,
        source="scheduler",
        source_time=datetime.now(UTC),
        received_at=datetime.now(UTC),
        idempotency_key="event:heartbeat:domain-committed",
        manifest_revision=1,
        payload={},
    )
    now = datetime.now(UTC)
    envelope = DomainCommandEnvelope[dict[str, object]](
        command_id="command_heartbeat_domain_committed",
        command_type="ApplyHeartbeat",
        event_id=heartbeat.event_id,
        receive_seq=heartbeat.receive_seq,
        correlation_id=heartbeat.correlation_id,
        runtime_instance_id="runtime_before_domain_commit_crash",
        manifest_revision=1,
        idempotency_key=f"homeostasis:ApplyHeartbeat:{heartbeat.event_id}",
        issued_at=now,
        deadline=now + timedelta(seconds=30),
        expected_revision=0,
        version_vector=VersionVector(homeostasis_state=0),
        payload={"event": heartbeat.model_dump(mode="json")},
    )
    prior_journal = RuntimeJournal(
        state_dir / "journal",
        runtime_instance_id="runtime_before_domain_commit_crash",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    prior_journal.append(
        "event_accepted",
        event_id=heartbeat.event_id,
        receive_seq=heartbeat.receive_seq,
        correlation_id=heartbeat.correlation_id,
        idempotency_key=heartbeat.idempotency_key,
        data={"event": heartbeat.model_dump(mode="json")},
    )
    prior_journal.append(
        "domain_command_intent",
        event_id=heartbeat.event_id,
        receive_seq=heartbeat.receive_seq,
        correlation_id=heartbeat.correlation_id,
        command_id=envelope.command_id,
        idempotency_key=envelope.idempotency_key,
        data={
            "service": "homeostasis",
            "envelope": envelope.model_dump(mode="json"),
        },
    )
    prior_journal.flush()

    async def scenario() -> None:
        before_crash = _SummaryHomeostasis(
            policies=[_expression_policy()],
            state_path=homeostasis_path,
        )
        committed = await before_crash.apply_heartbeat(envelope)
        assert committed.committed_revision == 1
        await before_crash.close()

        _, _, cognitive = _pipeline_adapter(tmp_path)
        recovered_homeostasis = _SummaryHomeostasis(
            policies=[_expression_policy()],
            state_path=homeostasis_path,
        )
        recovered_journal = RuntimeJournal(
            state_dir / "journal",
            runtime_instance_id="runtime_after_domain_commit_crash",
            character_id="character",
            manifest_revision=1,
            fsync=False,
        )
        supervisor = RuntimeSupervisor(
            manifest=_manifest(expression_mode="full"),
            cognitive=cognitive,
            homeostasis=recovered_homeostasis,
            state_dir=state_dir,
            journal=recovered_journal,
        )

        await supervisor.boot_and_recover()

        marked = [
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
            and candidate.payload.get("trigger_reason") == "homeostasis_marked"
        ]
        assert len(marked) == 1
        assert marked[0].payload["source_homeostasis_revision"] == 1
        assert any(
            record.record_type == "domain_command_result"
            and record.command_id == envelope.command_id
            and record.data.get("reconciled") is True
            for record in supervisor.journal.records()
        )
        assert not any(
            record.record_type == "homeostatic_marked_trigger_deferred_lifecycle"
            for record in supervisor.journal.records()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_boot_pauses_when_recovered_action_outcome_becomes_critical(tmp_path) -> None:
    class OutcomeCriticalHomeostasis(_SummaryHomeostasis):
        async def apply_outcome(self, command):
            self.watchdog = True
            return await super().apply_outcome(command)

    main = MockLLM(chat_script=["不应调用主模型"])
    aux = MockLLM(chat_script=["SPEAK\n不应调用辅助模型"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main, aux=aux)
    homeostasis = OutcomeCriticalHomeostasis(
        salience="marked",
        policies=[_expression_policy()],
    )
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-critical-recovered-action",
    )
    original_event, prepared, action = _goal_action_fixture(
        suffix="recovered_critical",
        state=ActionExecutionState.SUCCEEDED,
    )
    _journal_completed_goal_action(
        supervisor,
        original_event=original_event,
        prepared=prepared,
        action=action,
    )

    async def skip_commit(event, prepared_response, evidence):
        assert event == original_event
        return None

    supervisor._commit_prepared = skip_commit  # type: ignore[method-assign]

    async def scenario() -> None:
        await supervisor.boot_and_recover()

        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert "homeostasis watchdog requires pause" in supervisor.lifecycle_reason
        assert main.chat_calls == []
        assert aux.chat_calls == []
        assert supervisor.delivery_backend.deliveries == {}
        assert not any(
            event.event_type == RuntimeEventType.IDLE
            for event in supervisor.queue.snapshot()
        )
        deferred = next(
            record
            for record in supervisor.journal.records()
            if record.record_type == "reconciliation_deferred_homeostasis_watchdog"
            and record.data.get("phase") == "boot_after_action_reconciliation"
        )
        assert deferred.data["homeostasis_revision"] == 1
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


@pytest.mark.parametrize(
    ("next_salience", "next_signature", "reason_fragment", "replacement_expected"),
    [
        ("nominal", "homeostasis:marked-load", "no longer marked", False),
        ("marked", "homeostasis:new-episode", "episode changed", True),
    ],
)
def test_queued_marked_trigger_is_ignored_after_recovery_or_episode_change(
    tmp_path,
    next_salience: str,
    next_signature: str,
    reason_fragment: str,
    replacement_expected: bool,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / f"runtime-stale-marked-{next_salience}",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="sensor",
            payload={"sample": 1},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None
        await supervisor.dispatch(queued_perception)
        marked = await supervisor.queue.get(timeout=0)
        assert marked is not None and marked.payload["trigger_reason"] == "homeostasis_marked"

        homeostasis.salience = next_salience
        homeostasis.episode_signature = next_signature
        ignored = await supervisor.dispatch(marked)

        assert ignored.status == "ignored"
        assert reason_fragment in ignored.detail["reason"]
        assert not any(
            record.record_type == "domain_command_intent"
            and record.event_id == marked.event_id
            and record.data.get("service") == "cognitive"
            for record in supervisor.journal.records()
        )
        assert supervisor.delivery_backend.deliveries == {}
        replacements = [
            candidate
            for candidate in supervisor.queue.snapshot()
            if candidate.event_type == RuntimeEventType.IDLE
            and candidate.payload.get("trigger_reason") == "homeostasis_marked"
        ]
        assert bool(replacements) is replacement_expected
        if replacement_expected:
            assert replacements[0].payload["source_episode_signature"] == next_signature
            assert replacements[0].payload["source_homeostasis_revision"] == 2
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_queued_marked_trigger_with_stale_source_revision_skips_cognitive(
    tmp_path,
) -> None:
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-stale-marked-revision",
    )

    async def dispatch_perception(index: int) -> RuntimeEvent:
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source=f"sensor-{index}",
            payload={"sample": index},
        )
        assert perception is not None
        queued = await supervisor.queue.get(
            timeout=0,
            predicate=lambda candidate: candidate.event_id == perception.event_id,
        )
        assert queued is not None
        await supervisor.dispatch(queued)
        return perception

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await dispatch_perception(1)
        marked = await supervisor.queue.get(timeout=0)
        assert marked is not None and marked.payload["source_homeostasis_revision"] == 1
        await dispatch_perception(2)

        ignored = await supervisor.dispatch(marked)

        assert ignored.status == "ignored"
        assert "source revision is stale" in ignored.detail["reason"]
        assert not any(
            record.record_type == "domain_command_intent"
            and record.event_id == marked.event_id
            and record.data.get("service") == "cognitive"
            for record in supervisor.journal.records()
        )
        assert supervisor.delivery_backend.deliveries == {}
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_same_signature_marked_reentry_remains_suppressed_during_cooldown(
    tmp_path,
    monkeypatch,
) -> None:
    clock = [datetime.now(UTC)]
    monkeypatch.setattr("harness.runtime.supervisor.utc_now", lambda: clock[0])
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-reentry-cooldown",
    )

    async def dispatch_perception(index: int) -> None:
        event = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source=f"sensor-{index}",
            payload={"sample": index},
        )
        assert event is not None
        queued = await supervisor.queue.get(
            timeout=0,
            predicate=lambda candidate: candidate.event_id == event.event_id,
        )
        assert queued is not None
        await supervisor.dispatch(queued)

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await dispatch_perception(1)
        first_marked = await supervisor.queue.get(timeout=0)
        assert first_marked is not None

        clock[0] += timedelta(seconds=30)
        homeostasis.salience = "noticeable"
        await dispatch_perception(2)
        clock[0] += timedelta(seconds=30)
        homeostasis.salience = "marked"
        await dispatch_perception(3)

        assert not any(
            event.event_type == RuntimeEventType.IDLE for event in supervisor.queue.snapshot()
        )
        scheduled = [
            record
            for record in supervisor.journal.records()
            if record.record_type == "homeostatic_marked_trigger_scheduled"
        ]
        assert len(scheduled) == 1
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_marked_trigger_cooldown_reopens_at_configured_boundary(
    tmp_path,
    monkeypatch,
) -> None:
    clock = [datetime.now(UTC)]
    monkeypatch.setattr("harness.runtime.supervisor.utc_now", lambda: clock[0])
    _, _, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = _SummaryHomeostasis(policies=[_expression_policy()])
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-marked-cooldown",
    )

    async def apply_perception(index: int) -> None:
        event = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source=f"sensor-{index}",
            payload={"sample": index},
        )
        assert event is not None
        queued = await supervisor.queue.get(
            timeout=0,
            predicate=lambda candidate: candidate.event_id == event.event_id,
        )
        assert queued is not None
        await supervisor.dispatch(queued)

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await apply_perception(1)
        assert len(
            [event for event in supervisor.queue.snapshot() if event.event_type == RuntimeEventType.IDLE]
        ) == 1

        clock[0] += timedelta(seconds=1799)
        await apply_perception(2)
        assert len(
            [event for event in supervisor.queue.snapshot() if event.event_type == RuntimeEventType.IDLE]
        ) == 1

        clock[0] += timedelta(seconds=1)
        await apply_perception(3)
        assert len(
            [event for event in supervisor.queue.snapshot() if event.event_type == RuntimeEventType.IDLE]
        ) == 2
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_critical_user_event_pauses_before_cognitive_and_resumes_without_reapply(
    tmp_path,
) -> None:
    main = MockLLM(chat_script=["我需要先缓一下，但仍然听见你了。"])
    _, _, cognitive = _expression_pipeline_adapter(tmp_path, main=main)
    prepare_calls: list[DomainCommandEnvelope] = []
    original_prepare = cognitive.prepare_response

    async def recording_prepare(command):
        prepare_calls.append(command)
        return await original_prepare(command)

    cognitive.prepare_response = recording_prepare  # type: ignore[method-assign]
    homeostasis = _SummaryHomeostasis(salience="marked")
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime-critical-continuation",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        homeostasis.watchdog = True
        user = await supervisor.submit_user_message("你能回应我吗？", session_id="sess_user")
        assert user is not None
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None
        paused = await supervisor.dispatch(queued)
        assert paused.status == "deferred"
        assert paused.decision_kind == "watchdog_pause"
        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert homeostasis.apply_event_calls == 1
        assert prepare_calls == []
        assert supervisor.delivery_backend.deliveries == {}

        with pytest.raises(RuntimeStateError, match="watchdog remains active"):
            await supervisor.resume(reason="boundary not yet recovered")
        homeostasis.watchdog = False
        await supervisor.resume(reason="researcher verified recovery")
        continuation = await supervisor.queue.get(timeout=0)
        assert continuation is not None and continuation.event_id == user.event_id
        completed = await supervisor.dispatch(continuation)
        assert completed.decision_kind == "reply"
        assert homeostasis.apply_event_calls == 1
        assert len(prepare_calls) == 1
        assert len(supervisor.delivery_backend.deliveries) == 1
        perception = await supervisor.submit_event(
            event_type=RuntimeEventType.PERCEPTION,
            priority=RuntimeEventPriority.PERCEPTION,
            source="sensor",
            payload={"sample": "same marked episode"},
        )
        assert perception is not None
        queued_perception = await supervisor.queue.get(timeout=0)
        assert queued_perception is not None
        assert queued_perception.event_id == perception.event_id
        await supervisor.dispatch(queued_perception)
        assert not any(
            candidate.event_type == RuntimeEventType.IDLE
            for candidate in supervisor.queue.snapshot()
        )
        assert any(
            record.record_type == "homeostatic_marked_expression_opportunity"
            and record.event_id == user.event_id
            for record in supervisor.journal.records()
        )
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_watchdog_cancelled_autonomous_event_stays_cancelled_after_restart(
    tmp_path,
) -> None:
    journal_path = tmp_path / "runtime-cancel-restart.jsonl"
    first_journal = RuntimeJournal(
        journal_path,
        runtime_instance_id="runtime_before_restart",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    idle = _event(1, RuntimeEventType.IDLE, RuntimeEventPriority.IMPORTANT_ENVIRONMENT)
    watchdog = _event(2, RuntimeEventType.WATCHDOG, RuntimeEventPriority.CONTROL)
    first_journal.append(
        "event_accepted",
        event_id=idle.event_id,
        receive_seq=idle.receive_seq,
        correlation_id=idle.correlation_id,
        data={"event": idle.model_dump(mode="json")},
    )
    first_journal.append(
        "autonomous_expression_cancelled",
        event_id=watchdog.event_id,
        receive_seq=watchdog.receive_seq,
        correlation_id=watchdog.correlation_id,
        data={
            "cancelled_event_id": idle.event_id,
            "reason": "homeostasis watchdog requires an immediate pause",
        },
    )

    _, _, cognitive = _pipeline_adapter(tmp_path)
    recovered_journal = RuntimeJournal(
        journal_path,
        runtime_instance_id="runtime_after_restart",
        character_id="character",
        manifest_revision=1,
        fsync=False,
    )
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "runtime-cancel-restart",
        journal=recovered_journal,
    )
    supervisor._restore_runtime_indexes()
    assert idle.event_id in supervisor._cancelled_autonomous_event_ids
    assert supervisor._autonomous_ineligible(idle) == "autonomous output was cancelled"
    cognitive.pipeline.memory.close()


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
