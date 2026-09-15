"""Diary Runtime queue policy without a live model."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
from types import SimpleNamespace

import pytest

from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
from harness.diary.config import DiarySettings
from harness.diary.models import DiaryOriginal
from harness.runtime.diary import DiaryController, _in_window
from harness.runtime.models import RuntimeEvent, RuntimeLifecycle, utc_now


class FakeCognitive:
    def __init__(self):
        self.revision = 0
        self.action = "write"
        self.prepare_calls = []
        self.commit_calls = []

    async def get_snapshot(self):
        return SimpleNamespace(revision=self.revision)

    def result(self, command, payload):
        return DomainCommandResult(
            command_id=command.command_id, command_type=command.command_type,
            status="applied", request_hash=command.effective_request_hash(),
            committed_revision=self.revision, acknowledged_event_id=command.event_id,
            acknowledged_receive_seq=command.receive_seq, result=payload, completed_at=utc_now(),
        )

    async def prepare_diary_entry(self, command):
        self.prepare_calls.append(command)
        payload = command.payload
        return self.result(command, {"decision": {
            "entry_id": payload["entry_id"],
            "decision_id": "diary-decision:" + hashlib.sha256(command.command_id.encode()).hexdigest(),
            "action": self.action, "text": "今天把窗口边的雨声记下来了。" if self.action == "write" else "",
            "source_event_id": command.event_id, "prepared_at": utc_now().isoformat(),
            "deadline_at": payload["deadline_at"], "day": payload["day"],
        }, "timing": {"model_calls": 1}})

    async def commit_diary_entry(self, command):
        self.commit_calls.append(command)
        self.revision += 1
        return self.result(command, {"memory_created": True})


class FakeSupervisor:
    def __init__(self):
        self.lifecycle = RuntimeLifecycle.RUNNING
        self.cognitive = FakeCognitive()
        self._cognitive_snapshot = SimpleNamespace(revision=0)
        self.listeners = []
        self.events = []
        self.queue = SimpleNamespace(empty=lambda: True)
        self.manifest = SimpleNamespace(control_mode="study_runtime")
        self.capabilities = None
        self.goals = SimpleNamespace(enabled=False)

    def add_listener(self, listener):
        self.listeners.append(listener)

    async def submit_event(self, **kwargs):
        now = utc_now()
        kwargs.setdefault("event_id", f"runtime-event:{len(self.events)+1}")
        kwargs.setdefault("idempotency_key", kwargs["event_id"])
        event = RuntimeEvent(
            **kwargs, receive_seq=len(self.events)+1, correlation_id=kwargs["event_id"],
            source_time=now, received_at=now, manifest_revision=1,
        )
        self.events.append(event)
        return event

    def _response_homeostatic_interoception(self):
        return {"salience": "noticeable"}

    def _make_command(self, event, **kwargs):
        now = utc_now()
        return DomainCommandEnvelope(
            **kwargs, event_id=event.event_id, receive_seq=event.receive_seq,
            correlation_id=event.correlation_id, runtime_instance_id="test-runtime",
            manifest_revision=1, issued_at=now, deadline=now + timedelta(seconds=60),
        )

    async def _invoke_domain(self, *, envelope, method, **kwargs):
        return await method(envelope)

    def _validate_snapshot(self, snapshot, service):
        assert service == "cognitive"


@pytest.fixture
def rig(tmp_path):
    supervisor = FakeSupervisor()
    settings = DiarySettings(autonomous_enabled=True, window_local="00:00-23:59")
    controller = DiaryController(supervisor, settings, tmp_path / "runtime")
    supervisor.diary = controller
    yield controller, supervisor
    controller.close()


def test_window_wraps_midnight():
    from datetime import datetime
    from zoneinfo import ZoneInfo
    zone = ZoneInfo("Asia/Shanghai")
    assert _in_window(datetime(2026, 9, 15, 22, 0, tzinfo=zone), "21:00-02:00")
    assert _in_window(datetime(2026, 9, 16, 1, 0, tzinfo=zone), "21:00-02:00")
    assert not _in_window(datetime(2026, 9, 15, 12, 0, tzinfo=zone), "21:00-02:00")


@pytest.mark.parametrize("action", ["write", "wait"])
def test_diary_write_and_wait(rig, action):
    async def run():
        controller, supervisor = rig
        supervisor.cognitive.action = action
        accepted = await controller.request({"request_id": "one"})
        result = await controller.dispatch(supervisor.events[-1], 1)
        row = controller.turns.get(accepted["entry_id"])
        assert result.status == "completed"
        assert row["status"] == ("committed" if action == "write" else "wait")
        assert len(supervisor.cognitive.prepare_calls) == 1
        assert len(supervisor.cognitive.commit_calls) == (1 if action == "write" else 0)
        day = controller._day()
        assert controller.store.count(day) == (1 if action == "write" else 0)
        snap = controller.snapshot()
        assert snap["wrote_today"] is (action == "write")
        assert "bodies" not in snap
        bodies = controller.snapshot(include_bodies=True)
        if action == "write":
            assert bodies["bodies"][0]["text"]
        return accepted

    accepted = asyncio.run(run())
    asyncio.run(rig[0].request({"request_id": "one"}))
    assert accepted["status"] in {"committed", "wait", "queued"}


def test_diary_memory_is_self_visible_only(tmp_path):
    from datetime import UTC, datetime

    from harness.memory.shared import SharedMemoryStore, SharedReadScope

    store = SharedMemoryStore(tmp_path / "shared.db")
    original = DiaryOriginal(
        entry_id="diary-entry:self", decision_id="diary-decision:self",
        text="只留给自己看的一句。", written_at=datetime.now(UTC),
        body_hash=hashlib.sha256("只留给自己看的一句。".encode()).hexdigest(),
        source_event_id="diary-event:self",
    )
    written = store.import_diary_entry(original)
    assert written.created and written.record.channel == "private_diary"
    assert written.record.kind == "diary_entry"
    assert store.query(SharedReadScope(audience="local:main", kinds=("diary_entry",))).records == (written.record,)
    for audience in ("qq:group:1044946257", "qq:group:100", "local:other"):
        assert store.query(SharedReadScope(audience=audience)).records == ()


def test_diary_day_limit_and_pause(rig):
    async def run():
        controller, supervisor = rig
        await controller.request({"request_id": "first"})
        await controller.dispatch(supervisor.events[-1], 1)
        with pytest.raises(ValueError, match="diary_day_limit"):
            await controller.request({"request_id": "second"})
        controller.store.pause(True)
        assert controller.snapshot()["paused"] is True

    asyncio.run(run())
