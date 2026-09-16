"""AffectWorker coalescing, snapshot injection and drain, with isolated state_dir."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.embodiment.capabilities import AFFECT_APPRAISE, AFFECT_WINDOW_READ
from harness.runtime import PassiveHomeostasisAdapter, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.models import RuntimeEvent
from harness.runtime.supervisor import RuntimeStateError, RuntimeSupervisor
from harness.runtime import supervisor as module
from tests.test_emotion_runtime import manifest, owner
from tests.test_emotion_service import FakeAux, message, request
from tests.test_runtime_qq_deadline import _accept, _domain_result, _prepared, _request
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


def _on_manifest():
    return manifest("on")


def _event(i: int) -> RuntimeEvent:
    now = datetime.now(UTC)
    return RuntimeEvent(
        event_id=f"evt-{i}", receive_seq=i, correlation_id=f"corr-{i}",
        event_type=RuntimeEventType.QQ_TURN, priority=RuntimeEventPriority.USER,
        source="qq_body", source_time=now, received_at=now, idempotency_key=f"evt-{i}",
        manifest_revision=1, payload={},
    )


def _trigger(i: int) -> dict:
    first = datetime(2026, 9, 15, 8, 0, tzinfo=UTC) + timedelta(seconds=i)
    return {
        "event_id": f"qq-watch-event:{i:032x}",
        "member_id": "20001",
        "trace_id": f"qq-trace:{i:032x}",
        "first_seen_at": first.isoformat(),
        "first_seen_mono": float(i),
        "deadline_at": (first + timedelta(seconds=60)).isoformat(),
        "group_id": "10001",
        "self_member_id": "29999",
        "transport": "onebot",
    }


@pytest.fixture
def affect_runtime(tmp_path):
    memory, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = module.RuntimeSupervisor(
        manifest=_on_manifest(), cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    supervisor.lifecycle = RuntimeLifecycle.RUNNING

    async def prepare(command):
        return _prepared(command)

    cognitive.prepare_qq_response = prepare
    yield supervisor
    memory.close()


def test_burst_same_scope_coalesces_and_respects_interval(affect_runtime, monkeypatch):
    runtime = affect_runtime
    calls = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated(command):
        calls.append(command)
        if len(calls) == 1:
            started.set()
            await release.wait()
        return _domain_result(command, {"accepted_ids": [t["event_id"] for t in command.payload["triggers"]]})

    monkeypatch.setattr(runtime.cognitive, "appraise_affect", gated)
    sleeps = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    monkeypatch.setattr("harness.runtime.affect.asyncio.sleep", fake_sleep)

    async def scenario():
        first = _event(1)
        runtime.affect.submit("qq:group:10001", _trigger(1), event=first)
        await started.wait()
        for index in range(2, 11):
            item = _event(index)
            runtime.affect.submit("qq:group:10001", _trigger(index), event=item)
        release.set()
        if runtime.affect.tasks:
            await asyncio.wait(set(runtime.affect.tasks))
        assert 1 <= len(calls) <= 2
        covered = {item["event_id"] for command in calls for item in command.payload["triggers"]}
        assert covered == {f"qq-watch-event:{i:032x}" for i in range(1, 11)}
        if len(calls) == 2:
            assert sleeps and sleeps[0] >= 2
            second_ids = {item["event_id"] for item in calls[1].payload["triggers"]}
            assert len(second_ids) >= 9

    asyncio.run(scenario())


def test_snapshot_injects_committed_guidance_into_the_next_prepare(tmp_path, monkeypatch):
    adapter, _, _aux = owner(tmp_path)
    adapter.emotion_service.llm = FakeAux()
    runtime = RuntimeSupervisor(
        manifest=_on_manifest(), cognitive=adapter,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    runtime.lifecycle = RuntimeLifecycle.RUNNING
    now = datetime.now(UTC)
    frame = adapter.emotion_service.observe(
        request(scope="qq:group:10001", as_of=now,
                messages=[message(scope="qq:group:10001", timestamp=now)]))
    guidance = adapter.emotion_service.guidance(frame)
    captured = []

    async def prepare(command):
        captured.append(dict(command.payload))
        return _prepared(command)

    async def skip_appraise(command):
        return _domain_result(command, {"accepted_ids": []})

    monkeypatch.setattr(adapter, "prepare_qq_response", prepare, raising=False)
    monkeypatch.setattr(adapter, "appraise_affect", skip_appraise)

    async def scenario():
        request_dto = _request(seconds=10)
        event = await _accept(runtime, request_dto)
        result = await runtime.dispatch(event)
        assert result.status == "completed"
        if runtime.affect.tasks:
            await asyncio.wait(set(runtime.affect.tasks))
        snapshot = captured[0]["emotion_snapshot"]
        assert snapshot["guidance"]["text"] == guidance.text
        assert snapshot["guidance"]["status"] == "model"

    asyncio.run(scenario())


def test_read_timeout_leaves_turn_without_snapshot(affect_runtime, monkeypatch):
    runtime = affect_runtime
    captured = []

    async def prepare(command):
        captured.append(dict(command.payload))
        return _prepared(command)

    async def hang(_payload):
        await asyncio.Future()

    async def skip_appraise(command):
        return _domain_result(command, {"accepted_ids": []})

    monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", prepare)
    monkeypatch.setattr(runtime.cognitive, "read_affect_snapshot", hang)
    monkeypatch.setattr(runtime.cognitive, "appraise_affect", skip_appraise)
    runtime.cognitive.pipeline.settings.emotion_affect_read_timeout_seconds = 0.05

    async def scenario():
        request_dto = _request(seconds=10)
        event = await _accept(runtime, request_dto)
        result = await runtime.dispatch(event)
        assert result.status == "completed"
        if runtime.affect.tasks:
            await asyncio.wait(set(runtime.affect.tasks))
        assert "emotion_snapshot" not in captured[0]
        assert any(record.record_type == "affect_snapshot_unavailable" for record in runtime.journal.records())

    asyncio.run(scenario())


def test_critical_homeostasis_skips_appraise(affect_runtime, monkeypatch):
    runtime = affect_runtime
    runtime._homeostasis_snapshot = DomainSnapshot(
        service="homeostasis", revision=1,
        payload={"interoceptive_summary": {"salience": "critical"}})
    calls = []

    async def record(command):
        calls.append(command)
        return _domain_result(command, {"accepted_ids": []})

    monkeypatch.setattr(runtime.cognitive, "appraise_affect", record)

    async def scenario():
        event = _event(1)
        runtime.affect.submit("qq:group:10001", _trigger(1), event=event)
        if runtime.affect.tasks:
            await asyncio.wait(set(runtime.affect.tasks))
        assert calls == []
        assert any(record.record_type == "affect_submit_skipped_critical" for record in runtime.journal.records())

    asyncio.run(scenario())


def test_emotion_off_and_grounded_agency_do_not_register_write_path(tmp_path):
    memory, _, cognitive = _pipeline_adapter(tmp_path)
    runtime = module.RuntimeSupervisor(
        manifest=_manifest(), cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime-off")
    assert runtime.affect.status() == []
    event = _event(1)
    runtime.affect.submit("qq:group:10001", _trigger(1), event=event)
    assert not runtime.affect.tasks

    async def read_off():
        return await runtime.affect.read("qq:group:10001", datetime.now(UTC))

    assert asyncio.run(read_off()) is None
    runtime.manifest = SimpleNamespace(emotion_mode="on", grounded_agency=object())
    assert runtime.affect.status() == []
    runtime.manifest = _on_manifest()
    capabilities = {item.capability for item in runtime.affect.status()}
    assert capabilities == {AFFECT_WINDOW_READ, AFFECT_APPRAISE}
    memory.close()


def test_pending_over_window_drops_oldest(affect_runtime, monkeypatch):
    runtime = affect_runtime
    started = asyncio.Event()
    release = asyncio.Event()

    async def gated(command):
        started.set()
        await release.wait()
        return _domain_result(command, {"accepted_ids": []})

    monkeypatch.setattr(runtime.cognitive, "appraise_affect", gated)

    async def scenario():
        first = _event(1)
        runtime.affect.submit("qq:group:10001", _trigger(1), event=first)
        await started.wait()
        for index in range(2, 23):
            item = _event(index)
            runtime.affect.submit("qq:group:10001", _trigger(index), event=item)
        release.set()
        if runtime.affect.tasks:
            await asyncio.wait(set(runtime.affect.tasks))
        dropped = [record.data["event_id"] for record in runtime.journal.records()
                   if record.record_type == "affect_source_dropped_expired"]
        assert dropped
        assert len(dropped) >= 1

    asyncio.run(scenario())


def test_pause_does_not_drain_hung_affect(affect_runtime, monkeypatch):
    runtime = affect_runtime
    runtime.maintenance_drain_seconds = 0.05

    async def hang(command):
        await asyncio.Future()

    monkeypatch.setattr(runtime.cognitive, "appraise_affect", hang)

    async def scenario():
        event = _event(1)
        runtime.affect.submit("qq:group:10001", _trigger(1), event=event)
        await asyncio.sleep(0.01)
        with pytest.raises(RuntimeStateError, match="affect appraisal did not drain"):
            await runtime._pause_research_agency()
        for task in list(runtime.affect.tasks):
            task.cancel()
        if runtime.affect.tasks:
            await asyncio.wait(set(runtime.affect.tasks))

    asyncio.run(scenario())


def test_sidecar_hooks_are_gone(affect_runtime):
    runtime = affect_runtime
    assert not hasattr(runtime, "_qq_emotion_tasks")
    assert not hasattr(RuntimeSupervisor, "_schedule_qq_emotion_sidecar")
    assert not hasattr(RuntimeSupervisor, "_qq_emotion_sidecar")
