"""Real Runtime/Cognitive/emotion commands with only the model endpoints scripted."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import httpx
import pytest

from harness.cognitive import qq_turn
from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.emotion import EmotionService
from harness.llm.client import RoutingLLMClient
from harness.qq import decision
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQTurnTrace
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_basic_persona_continuity import _host
from tests.test_qq_decision import setup as setup
from tests.test_runtime_supervisor import _manifest


class EmotionEndpoint:
    def __init__(self, order):
        self.calls = []
        self.order = order

    def complete_json(self, *, system, user, **kwargs):
        payload = json.loads(user)
        stage = kwargs["response_format"]["json_schema"]["name"]
        self.calls.append((stage, payload, kwargs))
        self.order.append(stage)
        if stage == "semantic_review":
            return {"coverage_complete": True, "claims": [], "violations": [], "instruction": "",
                    "alignment": {"status": "aligned", "issues": []}}
        if stage == "emotion_appraisal":
            return {"events": [{"source_id": source_id, "signals": [
                {"label": "anger", "intensity": .9, "confidence": 1.0}], "recovery": []}
                for source_id in payload["new_source_ids"]]}
        assert stage == "emotion_guidance"
        return {**payload["required_expression"], "delivery": "措辞贴合当前心情"}

    def close(self):
        pass


@pytest.mark.parametrize("action", ["wait", "speak"])
@pytest.mark.parametrize("semantic_mode", ["off", "observe", "enforce"])
def test_runtime_qq_keeps_two_real_emotion_steps_once_across_prepare_resume_and_replay(
        setup, tmp_path, monkeypatch, action, semantic_mode):
    async def run():
        store, main, _, args, records = setup
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)

        def forbidden_network(*args, **kwargs):
            pytest.fail("QQ emotion integration attempted a real network request")

        monkeypatch.setattr(httpx.AsyncClient, "send", forbidden_network)
        # The imported setup fixture also forbids httpx.Client.send.
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
        settings = settings.model_copy(update={"emotion_mode": "on", "qq_web_policy": "after_gate",
            "qq_semantic_alignment_mode": semantic_mode, "qq_semantic_alignment_conversations": ["29999/10001"],
            "validator_enabled": False, "aux_llm_base_url": "https://aux.invalid/v1", "aux_llm_model": "qwen-test"})
        order = []
        aux = EmotionEndpoint(order)
        routed = RoutingLLMClient(main, aux)
        if semantic_mode == "enforce":
            settings = settings.model_copy(update={"validator_enabled": True})
            main._structured = SimpleNamespace(settings=settings)
        host.chat.settings = host.chat.pipeline.settings = settings
        host.chat.llm = host.chat.pipeline.llm = routed
        host._emotion_mode = "on"
        emotion = host.adapter.emotion_service
        assert isinstance(emotion, EmotionService)
        emotion.settings, emotion.llm = settings, routed
        host.shared.qq_settings = SimpleNamespace(memory_db=store.path)
        owner = SharedQQBinding(settings, preset, routed,
            ScopedQQMemoryView(host.chat.memory, group_id="10001", member_id="20002"), {}, ())
        recalls = []
        monkeypatch.setattr(host.shared, "qq_binding", lambda **kwargs: (recalls.append(kwargs), owner)[1])
        monkeypatch.setattr(qq_turn, "_resume_owner", lambda *args: replace(owner))

        # Source authenticity is a transport fixture boundary. Both the real
        # emotion window and Prepare reread this exact persisted observation.
        now = datetime.now(timezone.utc)
        first_seen = now - timedelta(seconds=12)
        original_deadline = first_seen + timedelta(seconds=60)
        event_id = "qq-watch-event:" + "d" * 32
        source = records[1].model_copy(update={"capture_item_id": event_id, "observed_at": first_seen})
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE qq_visual_observations SET capture_item_id=?,observed_at=?,record_json=? WHERE record_id=?",
                       (event_id, first_seen.isoformat(), source.model_dump_json(), source.record_id))

        def verified_source(request, *unused, **kwargs):
            assert request.trace.event_id == event_id
            assert request.trigger_member_id == source.member_id
            assert request.deadline_at == original_deadline
            return {"digest": "d" * 64, "text": source.text, "source": source.model_dump(mode="json")}

        monkeypatch.setattr(qq_turn, "verified_qq_source", verified_source)
        monkeypatch.setattr("harness.runtime.qq_turns._source",
                            lambda config, watcher, request, **kwargs: verified_source(request))

        actual_observe, actual_guidance = emotion.observe, emotion.guidance
        service_calls = {"observe": 0, "guidance": 0}

        def observe(value):
            service_calls["observe"] += 1
            assert value.deadline_at == original_deadline
            assert value.new_source_ids == [source.record_id]
            return actual_observe(value)

        def guidance(value, **options):
            service_calls["guidance"] += 1
            assert options.get("expression_only", False) == (semantic_mode == "enforce")
            return actual_guidance(value, **options)

        monkeypatch.setattr(emotion, "observe", observe)
        monkeypatch.setattr(emotion, "guidance", guidance)
        actual_main = main.complete_chat_result

        def complete_main(messages, **kwargs):
            order.append("gate" if decision.is_qq_gate_messages(messages) else "body")
            return actual_main(messages, **kwargs)

        monkeypatch.setattr(main, "complete_chat_result", complete_main)
        main.choice = json.dumps({"action": action, "reason": "fixture"})
        manifest = _manifest().model_copy(update={"emotion_mode": "on", "prompt_version": "qwen-emotion-v1"})
        runtime = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=object(),
                                    state_dir=tmp_path / "runtime", command_timeout_seconds=60)
        runtime.lifecycle = RuntimeLifecycle.RUNNING
        runtime._cognitive_snapshot = await host.get_snapshot()
        runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
        trace = QQTurnTrace(event_id=event_id, first_seen_at=first_seen)
        request = QQSharedPrepareRequest(group_id="10001", self_member_id="29999", trigger_member_id="20002",
            deadline_at=original_deadline, trace={"trace_id": trace.trace_id, "event_id": event_id,
                "first_seen_at": first_seen.isoformat(), "first_seen_mono": trace.first_seen_mono})
        event = RuntimeEvent(event_id="evt_emotion_qq_1", receive_seq=1, correlation_id=trace.trace_id,
            source="qq_body", event_type=RuntimeEventType.QQ_TURN, priority=RuntimeEventPriority.USER,
            source_time=first_seen, received_at=now, idempotency_key="qq-emotion-pipeline",
            manifest_revision=manifest.revision, payload=request.model_dump(mode="json"))
        resume_calls = []
        actual_resume = host.resume_qq_response

        async def checked_resume(command):
            before, calls = emotion.snapshot(), [call for call in aux.calls if call[0].startswith("emotion_")]
            result = await actual_resume(command)
            assert emotion.snapshot() == before
            assert [call for call in aux.calls if call[0].startswith("emotion_")] == calls
            assert command.deadline == original_deadline
            assert command.payload["turn_binding"]["deadline_at"] == original_deadline.isoformat()
            resume_calls.append(command)
            return result

        monkeypatch.setattr(host, "resume_qq_response", checked_resume)
        try:
            result = await runtime._prepare_qq_turn(event, 1, request)
            assert result.status == "completed"
            def unexpected_background(*args, **kwargs):
                pytest.fail("staged QQ turn must not appraise the same sources again")
            monkeypatch.setattr(runtime.affect, "submit", unexpected_background)
            runtime._submit_affect_after_qq(event, request, result)
            assert result.detail["decision"]["action"] == action
            assert order == ["emotion_appraisal", "emotion_guidance", "gate", *(["body"] if action == "speak" else []),
                             *(["semantic_review"] if semantic_mode == "enforce" and action == "speak" else [])]
            assert service_calls == {"observe": 1, "guidance": 1}
            assert len(recalls) == len(main.gate_messages) == 1
            assert len(main.chat_calls) == len(resume_calls) == int(action == "speak")
            state = emotion.snapshot()
            assert state["revision"] == 1 and len(state["sources"]) == 1
            assert all(item["processed"] for item in state["sources"].values())
            reservation = next(iter(state["reservations"].values()))
            assert reservation["frame"]["status"] == reservation["guidance"]["status"] == "model"
            assert datetime.fromisoformat(reservation["request"]["deadline_at"]) == original_deadline
            assert aux.calls[0][1]["new_source_ids"] == [source.record_id]

            measured = result.detail["trace"]
            for stage in ("appraisal", "guidance"):
                assert measured["counters"][f"emotion_{stage}_model_calls"] == 1
                assert measured["counters"][f"emotion_{stage}_results"] == 1
                assert measured["counters"][f"emotion_{stage}_fallbacks"] == 0
                assert measured["counters"][f"emotion_{stage}_reused_results"] == 0
                assert sum(item["name"] == f"emotion.{stage}.model" for item in measured["stages"]) == 1
            assert measured["counters"]["gate_model_calls"] == 1
            assert measured["counters"].get("persona_model_calls", 0) == int(action == "speak") * (2 if semantic_mode == "enforce" else 1)
            assert measured["counters"]["web_requested"] == 0
            intents = [item.data["envelope"] for item in runtime.journal.iter_records()
                       if item.record_type == "domain_command_intent"]
            assert [item["command_type"] for item in intents] == [
                "ObserveEmotion", "PrepareQQTurn", *(["ResumeQQResponse"] if action == "speak" else [])]
            assert all(datetime.fromisoformat(item["deadline"]) == original_deadline for item in intents)
            assert (original_deadline - datetime.fromisoformat(intents[0]["issued_at"])).total_seconds() < 49

            # Runtime and host command ledgers replay the frozen result. A
            # replay of Resume neither stimulates emotion nor recounts work.
            replay = await runtime._prepare_qq_turn(event, 2, request)
            assert replay.detail["decision"]["decision_id"] == result.detail["decision"]["decision_id"]
            assert emotion.snapshot() == state
            assert service_calls == {"observe": 1, "guidance": 1}
            assert len(aux.calls) == 2 + int(semantic_mode == "enforce" and action == "speak") and len(main.gate_messages) == 1
            assert len(main.chat_calls) == int(action == "speak")
            for stage in ("appraisal", "guidance"):
                assert replay.detail["trace"]["counters"][f"emotion_{stage}_model_calls"] == 1
        finally:
            await host.close()

    asyncio.run(run())
