"""Production host boundary and durable commands with scripted models only."""
import asyncio
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.cognitive import qq_turn
from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.qq import decision
from harness.qq.policy import QQLatencyOptions
from harness.qq.telemetry import QQTurnTrace
from tests.test_basic_persona_continuity import _host, _command
from tests.test_qq_decision import setup as setup


@pytest.mark.parametrize("action", ["wait", "speak"])
@pytest.mark.parametrize("via_runtime", [False, True])
@pytest.mark.parametrize("production", [False, True])
def test_host_prepares_and_resumes_without_repeating_memory_or_guidance(setup, tmp_path, monkeypatch, action, via_runtime, production):
    async def run():
        store, client, _, args, records = setup
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
        settings.qq_web_policy = "after_gate"
        host.chat.settings = settings
        host.chat.llm = client
        host.shared.qq_settings = SimpleNamespace(memory_db=store.path, allowed_group_ids=["10001"],
            allowed_private_peer_ids=[], self_member_id="29999")
        if production:
            from harness.production.store import ProductionStore
            host.adapter.production_meter = ProductionStore(tmp_path / "production.sqlite3")
            from tests.test_production_cognition import context
            host.adapter.production_meter.context = lambda **kwargs: context()
            preset = preset.model_copy(update={"persona": host.chat.memory.get_persona()})
            original_complete = client.complete_chat_result
            def complete(messages, **kwargs):
                if "ProductionAppraisal" in messages[0]["content"]:
                    return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                                           has_refusal=False, text='{"affect":null,"memory_updates":[]}')
                return original_complete(messages, **kwargs)
            monkeypatch.setattr(client, "complete_chat_result", complete)
        owner = SharedQQBinding(settings, preset, client,
            ScopedQQMemoryView(host.chat.memory, group_id="10001", member_id="20002"), {}, (),
            production_store=host.shared.production_store if production else None)
        calls = {"memory": 0, "guidance": 0}
        def recall(**kwargs):
            calls["memory"] += 1
            return owner
        async def guidance(command):
            calls["guidance"] += 1
            return None
        monkeypatch.setattr(host.shared, "qq_binding", recall)
        monkeypatch.setattr(host.adapter, "emotion_guidance", guidance)
        monkeypatch.setattr(qq_turn, "_resume_owner", lambda *args: replace(owner))
        source = records[1]
        event_id = "qq-watch-event:" + "a" * 32
        source = source.model_copy(update={"capture_item_id": event_id})
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE qq_visual_observations SET capture_item_id=?,record_json=? WHERE record_id=?",
                       (event_id, source.model_dump_json(), source.record_id))
        monkeypatch.setattr(qq_turn, "verified_qq_source", lambda *args: {"digest": "a" * 64,
                           "text": source.text, "source": source.model_dump(mode="json")})
        now = datetime.now(timezone.utc)
        trace = QQTurnTrace(event_id=event_id, first_seen_at=now)
        policy = QQLatencyOptions.from_settings(settings)
        payload = {"group_id": "10001", "self_member_id": "29999", "trigger_member_id": "20002",
            "deadline_at": (now + timedelta(seconds=59)).isoformat(),
            "trace": {"trace_id": trace.trace_id, "event_id": event_id,
                      "first_seen_at": now.isoformat(), "first_seen_mono": trace.first_seen_mono},
            "qq_frozen_policy": policy.model_dump(), "qq_turn_runtime": {
                "runtime_instance_id": "runtime_basic_chat", "manifest_revision": 1},
            "qq_web_capabilities": {"enabled": False}}
        if production:
            from tests.test_production_cognition import context
            payload["production_context"] = context()
        expected_calls = {"memory": 2 if production else 1, "guidance": 0 if production else 1}
        client.choice = json.dumps({"action": action, "reason": "fixture"})
        try:
            if via_runtime:
                from harness.qq.shared import QQSharedPrepareRequest
                from harness.runtime.adapters import DomainSnapshot
                from harness.runtime.models import RuntimeEvent, RuntimeEventType, RuntimeEventPriority, RuntimeLifecycle
                from harness.runtime.supervisor import RuntimeSupervisor
                from tests.test_runtime_supervisor import _manifest
                from harness.production.config import ProductionConfig
                manifest = _manifest()
                if production:
                    manifest = manifest.model_copy(update={"production": ProductionConfig()})
                runtime = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=object(),
                    state_dir=tmp_path / "runtime")
                runtime.lifecycle = RuntimeLifecycle.RUNNING
                if production:
                    host.adapter.production_meter.context = lambda **kwargs: {
                        **runtime.production_runtime.context_snapshot("owner"), "runtime_lifecycle": str(runtime.lifecycle)}
                runtime._cognitive_snapshot = await host.get_snapshot()
                runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
                request = QQSharedPrepareRequest.model_validate({key: value for key, value in payload.items()
                    if not key.startswith("qq_") and key != "production_context"})
                event = RuntimeEvent(event_id="evt_qq_1", receive_seq=1, correlation_id=trace.trace_id,
                    source="qq_body", event_type=RuntimeEventType.QQ_TURN, priority=RuntimeEventPriority.USER,
                    source_time=now, received_at=now, idempotency_key="qq-fixture-turn",
                    manifest_revision=runtime.manifest.revision,
                    payload=request.model_dump(mode="json"))
                result = await runtime._prepare_qq_turn(event, 1, request)
                assert result.status == "completed"
                assert result.detail["decision"]["action"] == action
                assert calls == expected_calls
                assert len(client.gate_messages) == 1
                assert len(client.chat_calls) == int(action == "speak")
                assert result.detail["trace"]["counters"]["web_requested"] == 0
                return
            prepared = await host.prepare_qq_turn(_command("PrepareQQTurn", 1, payload))
            assert prepared.status == "applied", prepared.error
            data = prepared.result
            assert any(stage["name"] == "persistence.command" for stage in data["trace"]["stages"])
            assert data["turn_binding"]["request_revision"] == 1
            assert data["turn_binding"]["source_event_ids"] == [event_id]
            assert data["turn_binding"]["authority_anchor"] == event_id
            assert calls == expected_calls
            assert len(client.gate_messages) == 1 and not client.chat_calls
            again = await host.prepare_qq_turn(_command("PrepareQQTurn", 1, payload))
            assert again.status == "idempotent_replay" and len(client.gate_messages) == 1
            if action == "wait":
                assert data["kind"] == "completed" and data["decision"]["action"] == "wait"
                if production:
                    host.shared.production_store.retract_source(source.record_id, event_id="withdraw-wait", reason="withdrawn")
                    replay = await host.prepare_qq_turn(_command("PrepareQQTurn", 1, payload))
                    assert replay.status == "rejected" and not client.chat_calls
                return
            assert data["kind"] == "prepared"
            resume_payload = {**payload, "prepare_id": data["prepare_id"], "snapshot_hash": data["snapshot_hash"],
                              "turn_binding": data["turn_binding"], "runtime_web_context": None}
            resume_command = _command("ResumeQQResponse", 2, resume_payload).model_copy(update={"event_id": "evt_chat_1"})
            resumed = await host.resume_qq_response(resume_command)
            assert resumed.status == "applied", resumed.error
            assert resumed.result["decision"]["outcome"] == "ready"
            assert calls == expected_calls
            assert len(client.gate_messages) == 1 and len(client.chat_calls) == 1
            assert (await host.resume_qq_response(resume_command)).status == "idempotent_replay"
            assert len(client.chat_calls) == 1
            if production:
                host.shared.production_store.retract_source(source.record_id, event_id="withdraw-reply", reason="withdrawn")
                assert (await host.resume_qq_response(resume_command)).status == "rejected"
                assert len(client.chat_calls) == 1
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("action", ["wait", "speak"])
@pytest.mark.parametrize("invalidate", ["source", "runtime"])
def test_production_idle_host_fences_real_intent_history_and_cached_results(setup, tmp_path, monkeypatch, action, invalidate):
    from harness.production.store import ProductionStore
    from harness.qq.idle import QQIdleIntent
    from harness.qq.turn_checkpoints import QQTurnCheckpointStore
    from tests.test_production_cognition import context

    async def run():
        raw, client, _, args, records = setup
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, [])
        settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
        settings.production_memory_enabled = True
        settings.validator_enabled = False
        host.chat.settings, host.chat.llm = settings, client
        host.shared.qq_settings = SimpleNamespace(memory_db=raw.path, allowed_group_ids=["10001"],
            allowed_private_peer_ids=[], self_member_id="29999")
        host.adapter.production_meter = ProductionStore(tmp_path / "idle-budget.sqlite3")
        runtime_context = context(autonomous=True)
        host.adapter.production_meter.context = lambda **kwargs: runtime_context
        owner = SharedQQBinding(settings, preset.model_copy(update={"persona": host.chat.memory.get_persona()}), client,
            ScopedQQMemoryView(host.chat.memory, group_id="10001", member_id=None), {}, (),
            production_store=host.shared.production_store)
        monkeypatch.setattr(host.shared, "qq_binding", lambda **kwargs: owner)
        monkeypatch.setattr(qq_turn, "_resume_owner", lambda *args: replace(owner))
        # The transport's live authority is external. Keep the actual
        # verified_qq_source idle DTO/digest path and all SQL/checkpoint guards.
        monkeypatch.setattr("harness.runtime.qq_turns._source", lambda *args: None)
        client.choice = json.dumps({"action": action, "reason": "a voluntary production choice"})
        now = datetime.now(timezone.utc)
        deadline = now + timedelta(seconds=59)
        event_id = "qq-watch-event:" + "b" * 32
        trace = QQTurnTrace(event_id=event_id, first_seen_at=now)
        idle = QQIdleIntent(intent_id="production-idle:test", group_id="10001", origin="production",
            self_member_id="29999", run_id="run-idle", created_at=now, deadline_at=deadline,
            episode_signature="production-body", homeostasis_revision=0, context_signature="verified-opportunity",
            body_instance_id="misty", environment_revision=1, window_id="qq-window",
            opportunity_ref="opportunity:1", selection_ref="selection:1", scheduler_generation=0)
        payload = {"group_id": "10001", "self_member_id": "29999", "trigger_member_id": None,
            "deadline_at": deadline.isoformat(), "idle_intent": idle.model_dump(mode="json"),
            "trace": {"trace_id": trace.trace_id, "event_id": event_id,
                "first_seen_at": now.isoformat(), "first_seen_mono": trace.first_seen_mono},
            "qq_frozen_policy": QQLatencyOptions.from_settings(settings).model_dump(),
            "qq_turn_runtime": {"runtime_instance_id": "runtime_basic_chat", "manifest_revision": 1},
            "qq_web_capabilities": {"enabled": False}, "production_context": runtime_context.copy()}
        prepare = _command("PrepareQQTurn", 1, payload)
        try:
            result = await host.prepare_qq_turn(prepare)
            assert result.status == "applied", result.error
            assert result.result["kind"] == ("prepared" if action == "speak" else "completed")
            assert (await host.prepare_qq_turn(prepare)).status == "idempotent_replay"
            checkpoint = QQTurnCheckpointStore(raw.path).load(result.result["prepare_id"])
            manifest = (checkpoint.get("snapshot") or {}).get("used_sources") or checkpoint.get("used_sources")
            assert manifest["production"] and "orientation" in manifest
            resume = None
            if action == "speak":
                resume = _command("ResumeQQResponse", 2, {**payload,
                    "prepare_id": result.result["prepare_id"], "snapshot_hash": result.result["snapshot_hash"],
                    "turn_binding": result.result["turn_binding"], "runtime_web_context": None}).model_copy(update={"event_id": "evt_chat_1"})
                response = await host.resume_qq_response(resume)
                assert response.status == "applied", response.error
                assert response.result["decision"]["outcome"] == "ready"
                assert (await host.resume_qq_response(resume)).status == "idempotent_replay"
            if invalidate == "source":
                host.shared.production_store.retract_source(records[0].record_id, event_id="withdraw-idle-history", reason="withdrawn")
            else:
                runtime_context["runtime_lifecycle"] = "PAUSED"
            assert (await host.prepare_qq_turn(prepare)).status == "rejected"
            if resume is not None:
                assert (await host.resume_qq_response(resume)).status == "rejected"
            assert len(client.gate_messages) == 1 and len(client.chat_calls) == int(action == "speak")
        finally:
            await host.close()
    asyncio.run(run())
