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
def test_host_prepares_and_resumes_without_repeating_memory_or_guidance(setup, tmp_path, monkeypatch, action, via_runtime):
    async def run():
        store, client, _, args, records = setup
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
        settings.qq_web_policy = "after_gate"
        host.chat.settings = settings
        host.chat.llm = client
        host.shared.qq_settings = SimpleNamespace(memory_db=store.path)
        owner = SharedQQBinding(settings, preset, client,
            ScopedQQMemoryView(host.chat.memory, group_id="10001", member_id="20002"), {}, ())
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
        client.choice = json.dumps({"action": action, "reason": "fixture"})
        try:
            if via_runtime:
                from harness.qq.shared import QQSharedPrepareRequest
                from harness.runtime.adapters import DomainSnapshot
                from harness.runtime.models import RuntimeEvent, RuntimeEventType, RuntimeEventPriority, RuntimeLifecycle
                from harness.runtime.supervisor import RuntimeSupervisor
                from tests.test_runtime_supervisor import _manifest
                runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=host, homeostasis=object(),
                    state_dir=tmp_path / "runtime")
                runtime.lifecycle = RuntimeLifecycle.RUNNING
                runtime._cognitive_snapshot = await host.get_snapshot()
                runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
                request = QQSharedPrepareRequest.model_validate({key: value for key, value in payload.items()
                    if not key.startswith("qq_")})
                event = RuntimeEvent(event_id="evt_qq_1", receive_seq=1, correlation_id=trace.trace_id,
                    source="qq_body", event_type=RuntimeEventType.QQ_TURN, priority=RuntimeEventPriority.USER,
                    source_time=now, received_at=now, idempotency_key="qq-fixture-turn",
                    manifest_revision=runtime.manifest.revision,
                    payload=request.model_dump(mode="json"))
                result = await runtime._prepare_qq_turn(event, 1, request)
                assert result.status == "completed"
                assert result.detail["decision"]["action"] == action
                assert calls == {"memory": 1, "guidance": 1}
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
            assert calls == {"memory": 1, "guidance": 1}
            assert len(client.gate_messages) == 1 and not client.chat_calls
            again = await host.prepare_qq_turn(_command("PrepareQQTurn", 1, payload))
            assert again.status == "idempotent_replay" and len(client.gate_messages) == 1
            if action == "wait":
                assert data["kind"] == "completed" and data["decision"]["action"] == "wait"
                return
            assert data["kind"] == "prepared"
            resume_payload = {**payload, "prepare_id": data["prepare_id"], "snapshot_hash": data["snapshot_hash"],
                              "turn_binding": data["turn_binding"], "runtime_web_context": None}
            resume_command = _command("ResumeQQResponse", 2, resume_payload).model_copy(update={"event_id": "evt_chat_1"})
            resumed = await host.resume_qq_response(resume_command)
            assert resumed.status == "applied", resumed.error
            assert resumed.result["decision"]["outcome"] == "ready"
            assert calls == {"memory": 1, "guidance": 1}
            assert len(client.gate_messages) == 1 and len(client.chat_calls) == 1
            assert (await host.resume_qq_response(resume_command)).status == "idempotent_replay"
            assert len(client.chat_calls) == 1
        finally:
            await host.close()
    asyncio.run(run())
