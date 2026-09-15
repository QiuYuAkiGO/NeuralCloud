"""Cross-domain emotion integration guards; only local fake model ports are used."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.contracts.envelope import DomainCommandResult
from harness.contracts.decisions import DeliveryEvidence
from harness.emotion import EmotionInput
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.models import RuntimeEventType
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_emotion_runtime import event, manifest, owner


def supervisor(tmp_path, adapter):
    runtime = RuntimeSupervisor(manifest=manifest(), cognitive=adapter, homeostasis=object(),
                                state_dir=tmp_path / "runtime", command_timeout_seconds=60)
    runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=0, payload={})
    runtime._cognitive_snapshot = adapter._committed_snapshot
    return runtime


def test_assess_then_prepare_same_idle_event_reuses_frozen_observation_and_guidance(tmp_path):
    async def run():
        adapter, main, aux = owner(tmp_path)
        runtime = supervisor(tmp_path, adapter)
        incoming = event().model_copy(update={"event_type": RuntimeEventType.IDLE})
        captures = []

        async def consume(command):
            captures.append((command, await adapter.emotion_guidance(command)))
            return DomainCommandResult(command_id=command.command_id, command_type=command.command_type,
                status="applied", request_hash=command.effective_request_hash(), committed_revision=0,
                acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
                result={"done": True}, completed_at=datetime.now(UTC))

        for kind, payload in (("AssessAutonomous", {"session_id": "main", "trigger_reason": "idle"}),
                              ("PrepareResponse", {"session_id": "main", "mode": "autonomous",
                                                   "assessment_id": "new-assessment", "record_ref": "new-ref"})):
            command = runtime._make_command(incoming, command_type=kind,
                idempotency_key=f"{kind}:event-1", expected_revision=0, payload=payload)
            await runtime._invoke_domain(service="cognitive", envelope=command, method=consume, event=incoming)
        first, second = captures
        assert first[0].payload["emotion_context_ref"] == second[0].payload["emotion_context_ref"]
        assert first[1] == second[1]
        intents = [record.data["envelope"] for record in runtime.journal.iter_records()
                   if record.record_type == "domain_command_intent" and record.data["envelope"]["command_type"] == "ObserveEmotion"]
        assert len(intents) == 2
        assert intents[0]["payload"] == intents[1]["payload"]
        assert len(aux.calls) == 1  # No new user stimulus: only the cached guidance stage.
        assert not main.chat_calls and not adapter.emotion_service.snapshot()["global_affect"]
    asyncio.run(run())


def test_runtime_critical_snapshot_blocks_before_any_observation_or_model(tmp_path):
    async def run():
        adapter, main, aux = owner(tmp_path)
        runtime = supervisor(tmp_path, adapter)
        runtime._homeostasis_snapshot = DomainSnapshot(service="homeostasis", revision=3,
            payload={"watchdog": ["boundary"], "interoceptive_summary": {"salience": "critical"}})
        incoming = event()
        command = runtime._make_command(incoming, command_type="PrepareResponse", idempotency_key="blocked",
                                         expected_revision=0, payload=incoming.payload)
        with pytest.raises(ValueError, match="critical"):
            await runtime._invoke_domain(service="cognitive", envelope=command, method=adapter.prepare_response,
                                         event=incoming)
        assert not aux.calls and not main.chat_calls
        assert not adapter.emotion_service.snapshot()["reservations"]
    asyncio.run(run())


def test_adapter_commit_failure_leaves_resumable_reservation_without_session_mutation(tmp_path):
    async def run():
        adapter, _, aux = owner(tmp_path)
        runtime = supervisor(tmp_path, adapter)
        incoming = event()
        command = runtime._make_command(incoming, command_type="ObserveEmotion", idempotency_key="reserved",
            expected_revision=None, payload={"context_id": "reserved", "scope": "local:main", "external_input": True,
                "source_payload": {"session_id": "main", "text": "本轮真实刺激", "source_time": incoming.source_time},
                "homeostasis": {"snapshot_revision": 0}})
        persist = adapter._persist_state

        def fail_commit():
            entry = adapter.emotion_service.snapshot()["reservations"].get("reserved")
            if entry and entry["status"] == "committed":
                raise OSError("simulated state replacement failure")
            persist()

        adapter.emotion_service.persist = fail_commit
        with pytest.raises(OSError, match="state replacement"):
            await adapter.observe_emotion(command)
        restored, _, restored_aux = owner(tmp_path)
        assert restored.pipeline.session_store.recent("main") == []
        assert restored.emotion_service.snapshot()["reservations"]["reserved"]["status"] == "reserved"
        applied = await restored.observe_emotion(command)
        assert applied.status == "applied"
        assert restored.emotion_service.snapshot()["revision"] == 1
        assert len(aux.calls) == len(restored_aux.calls) == 1
        replay = await restored.observe_emotion(command)
        assert replay.idempotency_hit and len(restored_aux.calls) == 1
    asyncio.run(run())


def test_restart_silent_scope_reads_only_its_window_and_decayed_global_labels(tmp_path):
    async def run():
        adapter, _, _ = owner(tmp_path)
        runtime = supervisor(tmp_path, adapter)
        incoming = event()
        command = runtime._make_command(incoming, command_type="ObserveEmotion", idempotency_key="private-input",
            expected_revision=None, payload={"context_id": "private-input", "scope": "local:main", "external_input": True,
                "source_payload": {"session_id": "main", "text": "PRIVATE_CAUSE", "source_time": incoming.source_time},
                "homeostasis": {"snapshot_revision": 0}})
        await adapter.observe_emotion(command)
        restored, _, restored_aux = owner(tmp_path)
        frame = restored.emotion_service.observe(EmotionInput(context_id="elsewhere-idle", scope="local:elsewhere",
            as_of=command.issued_at + timedelta(minutes=10), messages=[], new_source_ids=[]))
        assert frame.labels["anger"] == pytest.approx(.475, abs=.001)
        assert frame.revision == 1
        assert not restored_aux.calls
        restored.emotion_service.guidance(frame)
        assert "PRIVATE_CAUSE" not in restored_aux.calls[0][1]
        assert restored.emotion_service.snapshot()["windows"]["local:elsewhere"] == []
    asyncio.run(run())


def test_observation_and_delivered_session_share_original_source_time(tmp_path):
    async def run():
        adapter, _, _ = owner(tmp_path)
        runtime = supervisor(tmp_path, adapter)
        incoming = event().model_copy(update={"source_time": datetime.now(UTC) - timedelta(seconds=1)})
        prepare = runtime._make_command(incoming, command_type="PrepareResponse",
            idempotency_key="prepare:event-1", expected_revision=0, payload=incoming.payload)
        result = await runtime._invoke_domain(service="cognitive", envelope=prepare,
            method=adapter.prepare_response, event=incoming)
        assert result.status == "applied", result.error
        assert result.result is not None
        evidence = DeliveryEvidence(prepare_id=result.result.prepare_id, delivered_at=datetime.now(UTC),
            channel="chat", success=True, delivered_segments=list(result.result.decision.segments))
        commit = runtime._make_command(incoming, command_type="CommitInteraction",
            idempotency_key="commit:event-1", expected_revision=0,
            payload={"prepare_id": result.result.prepare_id, "evidence": evidence.model_dump(mode="json")})
        committed = await adapter.commit_interaction(commit)
        assert committed.status == "applied", committed.error
        recorded = adapter.pipeline.session_store.recent("main")[0]
        assert recorded.timestamp == incoming.source_time
        # The next observation reimports SessionStore rows into the emotion
        # window. A mismatched observation/prepare timestamp would conflict here.
        now = datetime.now(UTC)
        idle = incoming.model_copy(update={"event_id": "idle-2", "receive_seq": 2,
            "event_type": RuntimeEventType.IDLE, "source_time": now, "received_at": now})
        observation = runtime._make_command(idle, command_type="ObserveEmotion",
            idempotency_key="idle-2", expected_revision=None,
            payload={"context_id": "idle-2", "scope": "local:main", "source_payload": {"session_id": "main"},
                     "homeostasis": {"snapshot_revision": 0}})
        observed = await adapter.observe_emotion(observation)
        assert observed.status == "applied", observed.error
        window = adapter.emotion_service.snapshot()["windows"]["local:main"]
        assert [item["source_id"] for item in window].count(incoming.event_id) == 1
        await adapter.close()
    asyncio.run(run())


@pytest.mark.parametrize("route", ["board", "capability", "goal", "qq"])
def test_shared_expression_routes_keep_original_frozen_deadline_after_restart(route, tmp_path, monkeypatch):
    from harness.cognitive import board, capabilities, goals
    from harness.cognitive.service import CognitiveServiceHost
    from harness.qq.telemetry import QQTurnTrace
    from tests.test_capability_cognition import command
    from tests.test_emotion_expression_routing import _owner
    from tests.test_goal_cognition import goal_command

    async def run():
        adapter, _, _ = owner(tmp_path)
        scope = "board:board:1" if route == "board" else "qq:group:10001" if route == "qq" else "local:main"
        now = datetime.now(UTC)
        frame = adapter.emotion_service.observe(EmotionInput(context_id="expired", scope=scope,
            as_of=now - timedelta(seconds=2), deadline_at=now - timedelta(seconds=1)))
        adapter.emotion_service.guidance(frame)
        await adapter.close()
        restored, _, aux = owner(tmp_path)
        host = _owner(output={"kind": "wait"})
        host.adapter = restored
        host.chat.active_session_id = "main"
        request = goal_command() if route == "goal" else command()
        try:
            if route == "board":
                host.shared.board_context = lambda **kwargs: {"entries": []}
                monkeypatch.setattr(board, "_settings", lambda *_: SimpleNamespace(
                    autonomous_enabled=True, user_model_seconds=20, autonomous_model_seconds=20, model_max_tokens=512))
                request = request.model_copy(update={"command_type": "PrepareBoardEntry", "payload": {
                    "entry_id": "entry:1", "source_event_id": request.event_id,
                    "body_instance_id": "body:1", "board_id": "board:1", "trigger": "user_request",
                    "request_text": "写下来", "deadline_at": request.deadline.isoformat()}})
            elif route == "qq":
                trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32)
                request = request.model_copy(update={"command_type": "PrepareQQResponse", "payload": {
                    "group_id": "10001", "self_member_id": "29999", "trigger_member_id": "20001",
                    "deadline_at": (now + timedelta(seconds=50)).isoformat(),
                    "trace": {"trace_id": trace.trace_id, "event_id": trace.event_id,
                              "first_seen_at": trace.first_seen_at.isoformat(), "first_seen_mono": trace.first_seen_mono}}})
                host.shared.qq_settings = SimpleNamespace(memory_db=tmp_path / "unused.sqlite3")
                host.shared.qq_binding = lambda **kwargs: SimpleNamespace(
                    preset=SimpleNamespace(persona=host.chat.memory.get_persona()))
                monkeypatch.setattr("harness.runtime.qq_turns._source", lambda *args: None)

                def unexpected_main(**kwargs):
                    host.chat.llm.calls.append("QQ main was invoked after original deadline")
                    raise RuntimeError("unexpected model call")

                monkeypatch.setattr("harness.qq.decision.decide_qq_reply", unexpected_main)
            request.payload.update(emotion_context_ref=frame.context_id, emotion_revision=frame.revision)
            assert request.deadline > now and restored.emotion_service.existing_input("expired").deadline_at < now
            if route == "qq":
                result = await CognitiveServiceHost._prepare_qq_response(host, request)
                assert result.status == "failed" and result.error.endswith("QQDeadlineExpired")
            else:
                with pytest.raises(TimeoutError, match="expired"):
                    await {"board": board.prepare, "capability": capabilities.prepare, "goal": goals.prepare}[route](host, request)
            assert not host.chat.llm.calls and not aux.calls
            assert restored.emotion_service.existing_input("expired").deadline_at == frame.deadline_at
        finally:
            await restored.close()
    asyncio.run(run())
