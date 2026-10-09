"""Runtime command/persistence checks; no network or real model calls."""
from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import httpx
import pytest

from gui.presets import load_builtin
from harness.core.error_notice import ERROR_FALLBACK_REPLY
from harness.config import Settings
from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.llm.client import MockLLM, RoutingLLMClient
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.manifest import ExperimentManifest
from harness.runtime.adapters import DomainSnapshot, PipelineCognitiveAdapter
from harness.runtime.emotion import observation_input, project_homeostasis
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType
from harness.runtime.rpc import HttpCognitiveClient, create_domain_service_app
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_homeostatic_expression_manifest import _legacy_payload


class EmotionAux(MockLLM):
    def complete_json(self, *, system, user, **kwargs):
        self.calls.append((system, user))
        if "EMOTION_APPRAISAL" in system:
            return {"events": [{"source_id": "event-1", "signals": [
                {"label": "anger", "intensity": .95, "confidence": 1.0}], "recovery": []}]}
        if "EMOTION_GUIDANCE" in system:
            return {**json.loads(user)["required_expression"], "delivery": "可以短促直接地表达不满"}
        return {}


def owner(tmp_path, *, mode="on", aux=None):
    settings = Settings(emotion_mode=mode, parser_llm_enabled=False, planner_llm_enabled=False,
        tier_llm_enabled=False, validator_sample_rate=0, continuation_enabled=False, call_log_enabled=False,
        aux_llm_model="qwen-test", aux_llm_base_url="http://qwen.invalid/v1")
    main = MockLLM(chat_script=["这句话让我很不舒服。请别再这样说了。[END]"])
    aux = aux or EmotionAux()
    llm = RoutingLLMClient(main, aux)
    memory = build_inmemory_service(settings=settings, llm=llm)
    memory.bootstrap_persona(load_builtin("misty").persona)
    pipeline = InferencePipeline(memory, settings=settings, llm=llm)
    adapter = PipelineCognitiveAdapter(pipeline, state_path=tmp_path / "cognition.json", default_session_id="main")
    return adapter, main, aux


def manifest(mode="on"):
    return ExperimentManifest.model_validate({**_legacy_payload(), "emotion_mode": mode,
        "prompt_version": "qwen-emotion-v1" if mode == "on" else "memory-v1"})


def event():
    now = datetime.now(UTC)
    return RuntimeEvent(event_id="event-1", receive_seq=1, correlation_id="correlation-1",
        event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
        source="local", source_time=now, received_at=now, idempotency_key="source-1",
        manifest_revision=1, payload={"text": "这是本轮真实输入", "session_id": "main"})


def test_full_runtime_observes_before_main_and_replays_without_reappraisal(tmp_path):
    async def run():
        adapter, main, aux = owner(tmp_path)
        runtime = RuntimeSupervisor(manifest=manifest(), cognitive=adapter, homeostasis=object(),
            state_dir=tmp_path / "runtime", command_timeout_seconds=60)
        runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
        runtime._cognitive_snapshot = await adapter.get_snapshot()
        incoming = event()
        envelope = runtime._make_command(incoming, command_type="PrepareResponse",
            idempotency_key="prepare:event-1", expected_revision=0, payload=incoming.payload)
        result = await runtime._invoke_domain(service="cognitive", envelope=envelope,
            method=adapter.prepare_response, event=incoming)
        assert result.status == "applied", result.error
        intents = [record.data["envelope"] for record in runtime.journal.iter_records()
            if record.record_type == "domain_command_intent"]
        assert [item["command_type"] for item in intents] == ["ObserveEmotion", "PrepareResponse"]
        reference = intents[1]["payload"]["emotion_context_ref"]
        frame = adapter.emotion_service.snapshot()
        assert "anger" in json.dumps(frame)
        assert adapter.pipeline.session_store.recent("main") == []
        rendered = "\n".join(message["content"] for message in main.chat_calls[-1])
        assert "短促直接" in rendered
        assert "当前心理（short）" not in rendered and "normalized_deviation" not in rendered
        assert reference not in rendered
        calls = len(aux.calls), len(main.chat_calls)
        replay = runtime._make_command(incoming, command_type="PrepareResponse",
            idempotency_key="prepare:event-1", expected_revision=0, payload=incoming.payload)
        repeated = await runtime._invoke_domain(service="cognitive", envelope=replay,
            method=adapter.prepare_response, event=incoming)
        assert repeated.idempotency_hit
        assert (len(aux.calls), len(main.chat_calls)) == calls
        restored, _, restored_aux = owner(tmp_path)
        assert restored.emotion_service.guidance(reference).text == adapter.emotion_service.guidance(reference).text
        assert not restored_aux.calls
    asyncio.run(run())


def test_observed_stimulus_survives_failed_generation(tmp_path):
    async def run():
        adapter, main, _ = owner(tmp_path)
        runtime = RuntimeSupervisor(manifest=manifest(), cognitive=adapter, homeostasis=object(),
            state_dir=tmp_path / "runtime", command_timeout_seconds=60)
        runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
        runtime._cognitive_snapshot = await adapter.get_snapshot()
        incoming = event()
        envelope = runtime._make_command(incoming, command_type="PrepareResponse",
            idempotency_key="prepare:event-1", expected_revision=0, payload=incoming.payload)
        def fail(*args, **kwargs):
            raise RuntimeError("synthetic generation failure")
        main.complete_chat_result = fail
        result = await runtime._invoke_domain(service="cognitive", envelope=envelope,
            method=adapter.prepare_response, event=incoming)
        assert result.status == "applied"
        assert result.result.decision.text == ERROR_FALLBACK_REPLY
        restored, _, _ = owner(tmp_path)
        assert "anger" in json.dumps(restored.emotion_service.snapshot())
        assert restored.pipeline.session_store.recent("main") == []
    asyncio.run(run())


def test_observe_emotion_rpc_and_revision_binding(tmp_path):
    async def run():
        adapter, _, aux = owner(tmp_path)
        now = datetime.now(UTC)
        command = DomainCommandEnvelope(command_type="ObserveEmotion", event_id="event-1",
            receive_seq=1, correlation_id="correlation-1", runtime_instance_id="runtime-1",
            manifest_revision=1, idempotency_key="emotion-1", issued_at=now,
            deadline=now + timedelta(seconds=60), version_vector=VersionVector(homeostasis_state=3),
            payload={"context_id": "emotion-1", "scope": "local:main", "external_input": True,
                "source_payload": {"text": "本轮刺激", "session_id": "main"},
                "homeostasis": {"snapshot_revision": 2}})
        app = create_domain_service_app(adapter, service_token="test-token", service_name="cognitive")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test",
                headers={"Authorization": "Bearer test-token"}) as client:
            rpc = HttpCognitiveClient("http://test", service_token="test-token", client=client)
            refused = await rpc.observe_emotion(command)
            assert refused.status == "rejected" and not aux.calls
            command = command.model_copy(update={"version_vector": VersionVector(homeostasis_state=2)})
            result = await rpc.observe_emotion(command)
            assert result.status == "applied" and result.result["emotion_context_ref"] == "emotion-1"
    asyncio.run(run())


def test_full_projection_preserves_seventh_dimension_and_critical_blocks():
    dimensions = {name: {"value": .5, "target_band": [.3, .7], "precision": .9, "velocity": 0}
        for name in ("energy_reserve", "cognitive_load", "safety_predictability", "competence",
                     "autonomy", "social_connection", "novelty")}
    dimensions["novelty"]["value"] = .99
    payload = {"snapshot": {"dimensions": dimensions},
        "profile": {"dimensions": {name: {"hard_bounds": [0, 1]} for name in dimensions}}}
    projected = project_homeostasis(payload, 7)
    assert len(projected["dimensions"]) == 7
    assert projected["dimensions"]["novelty"]["normalized_deviation"] == pytest.approx(.29 / .3)
    payload["snapshot"]["watchdog"] = ["hard_bound"]
    with pytest.raises(ValueError, match="critical"):
        project_homeostasis(payload, 7)


def test_legacy_manifest_hash_and_new_prompt_binding():
    old = ExperimentManifest.model_validate(_legacy_payload())
    assert "emotion_mode" not in json.loads(old.canonical_json())
    assert manifest().emotion_mode == "on"
    with pytest.raises(ValueError, match="qwen-emotion-v1"):
        ExperimentManifest.model_validate({**_legacy_payload(), "emotion_mode": "on"})


def test_frozen_local_scope_survives_active_session_change(tmp_path):
    adapter, _, _ = owner(tmp_path)
    now = datetime.now(UTC)
    adapter.pipeline.session_store.append("main", "user", "原会话可见", event_id="old", timestamp=now)
    adapter.pipeline.session_store.append("other", "user", "另一会话私密", event_id="private", timestamp=now)
    adapter.default_session_id = "other"
    command = DomainCommandEnvelope(command_type="ObserveEmotion", event_id="event-1", receive_seq=1,
        correlation_id="correlation-1", runtime_instance_id="runtime-1", manifest_revision=1,
        idempotency_key="freeze", issued_at=now, deadline=now + timedelta(seconds=60),
        payload={"context_id": "frozen", "scope": "local:main", "source_payload": {}})
    value = observation_input(adapter, command)
    assert [item.text for item in value.messages] == ["原会话可见"]


def test_guidance_scope_and_revision_are_checked(tmp_path):
    async def run():
        adapter, _, _ = owner(tmp_path)
        now = datetime.now(UTC)
        command = DomainCommandEnvelope(command_type="ObserveEmotion", event_id="event-1", receive_seq=1,
            correlation_id="correlation-1", runtime_instance_id="runtime-1", manifest_revision=1,
            idempotency_key="bound", issued_at=now, deadline=now + timedelta(seconds=60),
            payload={"context_id": "bound", "scope": "local:main", "source_payload": {}})
        result = await adapter.observe_emotion(command)
        payload = {**result.result, "session_id": "other"}
        prepare = command.model_copy(update={"command_type": "PrepareResponse", "payload": payload})
        with pytest.raises(ValueError, match="scope"):
            await adapter.emotion_guidance(prepare)
        prepare = prepare.model_copy(update={"payload": {**payload, "session_id": "main", "emotion_revision": 999}})
        with pytest.raises(ValueError, match="revision"):
            await adapter.emotion_guidance(prepare)
    asyncio.run(run())


def test_renewed_transport_cannot_extend_frozen_emotion_model_deadline(tmp_path):
    from harness.emotion import EmotionInput

    async def run():
        adapter, main, aux = owner(tmp_path)
        now = datetime.now(UTC)
        frame = adapter.emotion_service.observe(EmotionInput(context_id="expired", scope="local:main",
            as_of=now - timedelta(seconds=2), deadline_at=now - timedelta(seconds=1),
            persona=adapter.pipeline.memory.get_persona()))
        adapter.emotion_service.guidance(frame)
        command = DomainCommandEnvelope(command_type="PrepareResponse", event_id="event-1", receive_seq=1,
            correlation_id="correlation-1", runtime_instance_id="runtime-1", manifest_revision=1,
            idempotency_key="renewed", issued_at=now, deadline=now + timedelta(seconds=60),
            payload={"text": "当前输入", "session_id": "main", "emotion_context_ref": frame.context_id,
                     "emotion_revision": frame.revision})
        result = await adapter.prepare_response(command)
        assert result.status == "applied"
        assert result.result.decision.text == ERROR_FALLBACK_REPLY
        assert not main.chat_calls and not aux.calls
    asyncio.run(run())
