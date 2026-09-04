from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.cognitive.autonomous import parse_assessment_output
from harness.config import Settings
from harness.contracts.decisions import DeliveryEvidence
from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.llm.client import MockLLM, RoutingLLMClient
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.manifest import ExperimentManifest
from harness.runtime import (
    PassiveHomeostasisAdapter,
    PipelineCognitiveAdapter,
    RuntimeEventPriority,
    RuntimeEventType,
    RuntimeSupervisor,
)
from tests.factories import vaelith


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("\n silent \n无需打扰。", ("silent", "无需打扰。")),
        ("SpEaK\n值得主动问候。", ("speak", "值得主动问候。")),
        ("SILENT", ("silent", "")),
        ("SPEAK", None),
        ("MAYBE\n理由不确定。", None),
        ("**SILENT**\nMarkdown 标记无效。", None),
        ('{"decision":"silent"}', None),
    ],
)
def test_plain_text_assessment_protocol(
    raw: str,
    expected: tuple[str, str] | None,
) -> None:
    assert parse_assessment_output(raw) == expected


def _settings() -> Settings:
    return Settings(
        parser_llm_enabled=False,
        planner_llm_enabled=False,
        tier_llm_enabled=False,
        validator_sample_rate=0,
        continuation_enabled=False,
        call_log_enabled=False,
    )


def _adapter(
    tmp_path: Path,
    *,
    main: MockLLM,
    aux: MockLLM | None,
    state_name: str = "cognitive-state.json",
) -> tuple[object, InferencePipeline, PipelineCognitiveAdapter]:
    settings = _settings()
    llm = RoutingLLMClient(main, aux) if aux is not None else main
    memory = build_inmemory_service(settings=settings, llm=llm)
    memory.bootstrap_persona(vaelith())
    pipeline = InferencePipeline(memory, settings=settings, llm=llm)
    adapter = PipelineCognitiveAdapter(
        pipeline,
        state_path=tmp_path / state_name,
        autonomous_log_dir=tmp_path / "autonomous",
        default_session_id="sess_active",
    )
    return memory, pipeline, adapter


def _command(
    command_type: str,
    *,
    revision: int = 0,
    payload: dict | None = None,
    event_id: str = "evt_idle",
    receive_seq: int = 1,
) -> DomainCommandEnvelope[dict]:
    now = datetime.now(UTC)
    return DomainCommandEnvelope[dict](
        command_type=command_type,
        event_id=event_id,
        receive_seq=receive_seq,
        correlation_id=f"corr_{event_id}",
        runtime_instance_id="runtime_test",
        manifest_revision=1,
        idempotency_key=f"{command_type}:{event_id}",
        issued_at=now,
        deadline=now + timedelta(seconds=30),
        expected_revision=revision,
        version_vector=VersionVector(cognitive_memory=revision),
        payload=payload or {},
    )


def _assessment_payload() -> dict:
    return {
        "session_id": "sess_active",
        "channel": "chat",
        "homeostasis": {
            "snapshot": {
                "emotion": {"label": "settled", "intensity": 0.2},
                "dimensions": {"social_connection": {"value": 0.4}},
            }
        },
        "policies": [
            {
                "policy_id": "connect",
                "label": "进行低压力的主动联系",
                "rank": 1,
            }
        ],
    }


def test_silent_assessment_uses_aux_text_and_has_no_cognitive_side_effects(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["main must not run"])
    aux = MockLLM(chat_script=["\nSILENT\n没有足够理由打扰用户。"])
    memory, pipeline, adapter = _adapter(tmp_path, main=main, aux=aux)

    async def scenario() -> None:
        command = _command("AssessAutonomous", payload=_assessment_payload())
        first = await adapter.assess_autonomous(command)
        replay = await adapter.assess_autonomous(command)

        assert first.result is not None
        assert first.result.decision == "silent"
        assert first.result.evaluator == "aux"
        assert replay.status == "idempotent_replay"
        assert len(aux.chat_calls) == 1
        assert main.chat_calls == []
        assert pipeline.session_store.recent("sess_active") == []
        assert memory.stores.episodic.list_all() == []
        record = Path(first.result.record_ref)
        assert record.suffix == ".txt"
        assert "SILENT" in record.read_text(encoding="utf-8")
        assert list((tmp_path / "autonomous").rglob("*.json")) == []
        await adapter.close()

    asyncio.run(scenario())


def test_assessment_record_is_reused_after_cognitive_state_loss(tmp_path: Path) -> None:
    first_main = MockLLM(chat_script=["main must not run"])
    first_aux = MockLLM(chat_script=["SILENT\n首次判定保持静默。"])
    _, _, first_adapter = _adapter(tmp_path, main=first_main, aux=first_aux)

    async def scenario() -> None:
        command = _command("AssessAutonomous", payload=_assessment_payload())
        first = await first_adapter.assess_autonomous(command)
        assert first.result is not None
        await first_adapter.close()

        restarted_main = MockLLM(chat_script=["main must not run"])
        restarted_aux = MockLLM(chat_script=["SPEAK\nmust not run"])
        _, _, restarted_adapter = _adapter(
            tmp_path,
            main=restarted_main,
            aux=restarted_aux,
            state_name="cognitive-restarted.json",
        )
        restored = await restarted_adapter.assess_autonomous(command)

        assert restored.result is not None
        assert restored.result.assessment_id == first.result.assessment_id
        assert restored.result.decision == "silent"
        assert restarted_aux.chat_calls == []
        assert restarted_main.chat_calls == []
        assert len(list((tmp_path / "autonomous").rglob("*.txt"))) == 1
        await restarted_adapter.close()

    asyncio.run(scenario())


def test_speak_assessment_records_internal_memory_then_main_reply_only(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["……今天还顺利吗？"])
    aux = MockLLM(chat_script=["SPEAK\n想确认用户今天过得是否还好。"])
    memory, pipeline, adapter = _adapter(tmp_path, main=main, aux=aux)

    async def scenario() -> None:
        assessed = await adapter.assess_autonomous(
            _command("AssessAutonomous", payload=_assessment_payload())
        )
        assert assessed.result is not None
        assert assessed.result.decision == "speak"
        assert assessed.result.internal_memory_ref is not None
        reflection = memory.stores.episodic.get(assessed.result.internal_memory_ref)
        assert reflection is not None
        assert reflection.channel == "internal"
        assert reflection.event_type == "internal_reflection"
        assert "确认用户" in reflection.summary

        prepare_payload = {
            **_assessment_payload(),
            "mode": "autonomous",
            "assessment_id": assessed.result.assessment_id,
            "record_ref": assessed.result.record_ref,
            "internal_memory_ref": assessed.result.internal_memory_ref,
        }
        prepared = await adapter.prepare_response(
            _command("PrepareResponse", revision=1, payload=prepare_payload)
        )
        assert prepared.result is not None
        assert prepared.result.decision.kind == "reply"
        assert len(main.chat_calls) == 1

        evidence = DeliveryEvidence(
            prepare_id=prepared.result.prepare_id,
            delivered_at=datetime.now(UTC),
            channel="chat",
            success=True,
            delivered_segments=list(prepared.result.decision.segments),
        )
        committed = await adapter.commit_interaction(
            _command(
                "CommitInteraction",
                revision=1,
                payload={
                    "prepare_id": prepared.result.prepare_id,
                    "evidence": evidence.model_dump(mode="json"),
                },
            )
        )
        assert committed.status == "applied"
        turns = pipeline.session_store.recent("sess_active")
        assert turns and all(turn.role == "character" for turn in turns)
        assert all("主动表达准备" not in turn.text for turn in turns)
        assert memory.stores.episodic.get("evt_idle") is None
        await adapter.close()

    asyncio.run(scenario())


def test_invalid_aux_output_falls_back_to_main_text_assessment(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["SILENT\n主模型兜底后保持静默。"])
    aux = MockLLM(chat_script=['{"decision":"silent"}'])
    _, _, adapter = _adapter(tmp_path, main=main, aux=aux)

    async def scenario() -> None:
        result = await adapter.assess_autonomous(
            _command("AssessAutonomous", payload=_assessment_payload())
        )
        assert result.result is not None
        assert result.result.decision == "silent"
        assert result.result.evaluator == "main_fallback"
        assert len(aux.chat_calls) == 1
        assert len(main.chat_calls) == 1
        text = Path(result.result.record_ref).read_text(encoding="utf-8")
        assert "aux | invalid" in text
        assert "main_fallback | valid" in text
        await adapter.close()

    asyncio.run(scenario())


def test_invalid_main_fallback_forces_silence(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["```json\n{\"decision\":\"speak\"}\n```"])
    memory, _, adapter = _adapter(tmp_path, main=main, aux=None)

    async def scenario() -> None:
        result = await adapter.assess_autonomous(
            _command("AssessAutonomous", payload=_assessment_payload())
        )
        assert result.result is not None
        assert result.result.decision == "silent"
        assert result.result.evaluator == "forced_silent"
        assert memory.stores.episodic.list_all() == []
        await adapter.close()

    asyncio.run(scenario())


def _manifest() -> ExperimentManifest:
    return ExperimentManifest.model_validate(
        {
            "experiment_id": "autonomous-gate-test",
            "revision": 1,
            "control_mode": "development_debug",
            "condition": "llm_top3",
            "persona_version": "v3",
            "homeostatic_profile": "passive-v1",
            "heartbeat_seconds": 60,
            "policy_horizon": 3,
            "random_seed": 1,
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
    )


def _policy() -> dict:
    return {
        "policy_id": "connect",
        "label": "进行低压力的主动联系",
        "source_drive": "social_connection",
        "expected_impacts": {"social_connection": 0.1},
        "reversible": True,
        "success_criteria": ["connection improves"],
        "efe": {
            "pragmatic_value": -0.1,
            "ambiguity": 0.1,
            "risk": 0.02,
            "information_gain": 0.01,
            "total": 0.01,
        },
        "rank": 1,
    }


def test_runtime_silent_idle_never_invokes_main_or_delivery(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["main must not run"])
    aux = MockLLM(chat_script=["SILENT\n保持安静。"])
    _, _, cognitive = _adapter(tmp_path, main=main, aux=aux)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(policies=[_policy()]),
        state_dir=tmp_path / "runtime",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={"scheduled_at": datetime.now(UTC).isoformat()},
        )
        heartbeat = await supervisor.queue.get(timeout=0)
        assert heartbeat is not None
        await supervisor.dispatch(heartbeat)
        idle = await supervisor.queue.get(timeout=0)
        assert idle is not None and idle.event_type == RuntimeEventType.IDLE
        result = await supervisor.dispatch(idle)
        assert result.decision_kind == "no_action"
        assert result.detail["assessment_decision"] == "silent"
        assert main.chat_calls == []
        assert supervisor.delivery_backend.deliveries == {}
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_runtime_speak_idle_invokes_main_and_persists_proactive_reply(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["……今天还顺利吗？"])
    aux = MockLLM(chat_script=["SPEAK\n想用低压力的方式问候用户。"])
    memory, pipeline, cognitive = _adapter(tmp_path, main=main, aux=aux)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(policies=[_policy()]),
        state_dir=tmp_path / "runtime",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        heartbeat = await supervisor.queue.get(timeout=0)
        assert heartbeat is not None
        await supervisor.dispatch(heartbeat)
        idle = await supervisor.queue.get(timeout=0)
        assert idle is not None
        result = await supervisor.dispatch(idle)

        assert result.decision_kind == "reply"
        assert result.detail["delivered"] is True
        assert len(main.chat_calls) == 1
        assert len(supervisor.delivery_backend.deliveries) == 1
        turns = pipeline.session_store.recent("sess_active")
        assert turns and all(turn.role == "character" for turn in turns)
        reflections = [
            item for item in memory.stores.episodic.list_all()
            if item.event_type == "internal_reflection"
        ]
        assert len(reflections) == 1
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_user_message_waiting_before_idle_skips_assessment(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["main must not run"])
    aux = MockLLM(chat_script=["SPEAK\n想主动问候用户。"])
    _, _, cognitive = _adapter(tmp_path, main=main, aux=aux)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(policies=[_policy()]),
        state_dir=tmp_path / "runtime",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        heartbeat = await supervisor.queue.get(timeout=0)
        assert heartbeat is not None
        await supervisor.dispatch(heartbeat)
        idle = await supervisor.queue.get(timeout=0)
        assert idle is not None
        await supervisor.submit_user_message("继续说吧。", session_id="sess_active")

        result = await supervisor.dispatch(idle)

        assert result.status == "ignored"
        assert "user" in result.detail["reason"]
        assert aux.chat_calls == []
        assert main.chat_calls == []
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_foreground_event_waiting_before_idle_skips_assessment(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["main must not run"])
    aux = MockLLM(chat_script=["SPEAK\n想主动问候用户。"])
    _, _, cognitive = _adapter(tmp_path, main=main, aux=aux)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(policies=[_policy()]),
        state_dir=tmp_path / "runtime",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        heartbeat = await supervisor.queue.get(timeout=0)
        assert heartbeat is not None
        await supervisor.dispatch(heartbeat)
        idle = await supervisor.queue.get(timeout=0)
        assert idle is not None
        await supervisor.submit_event(
            event_type=RuntimeEventType.ACTION_RESULT,
            priority=RuntimeEventPriority.ACTION_RESULT,
            source="action_coordinator",
            payload={"action_id": "action_foreground"},
        )

        result = await supervisor.dispatch(idle)

        assert result.status == "ignored"
        assert "foreground" in result.detail["reason"]
        assert aux.chat_calls == []
        assert main.chat_calls == []
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


class _BlockingAux(MockLLM):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()

    def complete_chat_result(self, messages, *, temperature=0.7, stream=False, max_tokens=None):
        self.started.set()
        assert self.release.wait(timeout=5)
        return super().complete_chat_result(
            messages,
            temperature=temperature,
            stream=stream,
            max_tokens=max_tokens,
        ).model_copy(update={"text": "SPEAK\n想主动问候用户。"})


def test_user_message_arriving_during_assessment_cancels_main_expression(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["main expression must not run"])
    aux = _BlockingAux()
    _, _, cognitive = _adapter(tmp_path, main=main, aux=aux)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(policies=[_policy()]),
        state_dir=tmp_path / "runtime",
    )

    async def scenario() -> None:
        await supervisor.boot_and_recover()
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler",
            payload={},
        )
        heartbeat = await supervisor.queue.get(timeout=0)
        assert heartbeat is not None
        await supervisor.dispatch(heartbeat)
        idle = await supervisor.queue.get(timeout=0)
        assert idle is not None
        task = asyncio.create_task(supervisor.dispatch(idle))
        assert await asyncio.to_thread(aux.started.wait, 3)
        await supervisor.submit_user_message("我来了。", session_id="sess_active")
        aux.release.set()
        result = await task
        assert result.status == "ignored"
        assert "user activity" in result.detail["reason"]
        assert main.chat_calls == []
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())
