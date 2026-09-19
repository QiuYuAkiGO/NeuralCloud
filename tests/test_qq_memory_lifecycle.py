"""The worker reconciles durable receipts against the current owner generation."""

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from harness.cognitive.qq_memory import (
    QQMemoryControl, QQMemoryWorker, build_commit, install_committed, snapshot_owner,
)
from harness.config import Settings
from tests.test_qq_memory_commit import NOW, SourceFixture, _commit, _memory, _preset


def test_same_preset_id_reload_recovers_jobs_missing_from_new_memory(tmp_path, monkeypatch):
    sources = SourceFixture(tmp_path)
    sources.add()
    job = sources.jobs()[0]
    committed = _commit(_preset(), job)
    sources.queue.acknowledge(job.job_id, set(committed.qq_memory_state["receipts"]))

    async def exercise():
        old_memory = _memory(committed)
        new_preset = _preset()
        restored_memory = _memory(new_preset)
        snapshots = {id(old_memory): committed, id(restored_memory): new_preset}
        host = SimpleNamespace(
            _control_lock=asyncio.Lock(),
            chat=SimpleNamespace(memory=old_memory, active_preset_id="misty", settings=Settings(qq_memory_enabled=True)),
            adapter=SimpleNamespace(_lock=asyncio.Lock(), _maintenance_paused=False),
            shared=SimpleNamespace(store=sources.shared, sync_qq=lambda: None,
                                   qq_settings=SimpleNamespace(self_member_id="999", allowed_group_ids=("100", "101"))),
        )
        monkeypatch.setattr("harness.cognitive.qq_memory.snapshot_owner", lambda value: snapshots[id(value.chat.memory)])
        worker = QQMemoryWorker(host, control_path=tmp_path / "control.json")

        async def no_maintenance(**kwargs):
            return None

        retried_jobs = []

        def stop_after_recovery(current, old, persona):
            retried_jobs.append(current.job_id)
            raise RuntimeError("test stops after proving restored source scheduling")

        worker.maintain = no_maintenance
        worker.make_proposal = stop_after_recovery
        await worker.tick()
        assert not retried_jobs
        assert worker.queue.get_job(job.job_id).status == "applied"
        # Restoring the factory/older copy replaces MemoryService without
        # changing the preset ID. Missing effects must be replayed from receipts.
        host.chat.memory = restored_memory
        await worker.tick()
        assert retried_jobs == [job.job_id]
        assert worker.queue.get_job(job.job_id).status == "retry"

    asyncio.run(exercise())


@pytest.mark.parametrize("backoff", [True, False])
def test_live_failed_window_cannot_starve_decay_or_adoption_and_delayed_counter_still_commits(
        tmp_path, monkeypatch, backoff):
    from tests.test_basic_persona_continuity import _host
    from tests.test_qq_memory_commit import _four_day_commit, _proposal

    sources = SourceFixture(tmp_path)
    committed = _four_day_commit(sources)
    for job_id in committed.qq_memory_state["receipts"]:
        sources.queue.acknowledge(job_id, set(committed.qq_memory_state["receipts"]))
    sources.add(at=NOW + timedelta(days=4), text="这次我不想解释具体步骤")
    retry = sources.jobs()[0]
    moment = NOW + timedelta(days=90)
    if backoff:
        sources.queue.fail(retry.job_id, "test_backoff", now=moment)
    monkeypatch.setattr("harness.cognitive.qq_memory.now_utc", lambda: moment)
    monkeypatch.setattr("harness.memory.qq_assimilation._now", lambda value=None: value or moment)

    async def exercise():
        host, _ = _host(tmp_path / "owner", monkeypatch, [])
        events = [event.model_copy(update={"tier": "mid", "expire_at": None, "strength": 0.21,
                  "strength_updated_at": NOW, "last_decay_review_at": NOW}) for event in committed.episodic]
        events[-1] = events[-1].model_copy(update={"strength": 0.8,
            "strength_updated_at": moment - timedelta(days=1), "last_decay_review_at": moment - timedelta(days=1)})
        current = snapshot_owner(host).model_copy(update={"episodic": events, "semantic": committed.semantic,
            "candidates": committed.candidates, "qq_memory_state": committed.qq_memory_state})
        install_committed(host, current)
        host.shared.store = sources.shared
        host.shared.sync_qq = lambda: None
        host.shared.qq_settings = SimpleNamespace(self_member_id="999", allowed_group_ids=("100", "101"))
        worker = host.qq_memory
        worker.control = QQMemoryControl(enabled=True, recall_enabled=True)
        worker.queue = sources.queue
        worker._queue_owner = host.chat.memory
        worker._queue_scope = ("misty", "999", ("100", "101"), ())
        impression_id = next(iter(committed.qq_memory_state["impressions"]))
        shown = events[-1].event_id
        worker._recall_pending["adopt-while-source-fails"] = {
            "ids": [impression_id], "episode_ids": [shown], "group_id": "100",
            "at": moment.isoformat(), "preset_id": "misty"}

        def fail_model(*args):
            raise ValueError("source window still needs model repair")

        worker.make_proposal = fail_model
        try:
            await worker.tick()
            assert worker.queue.get_job(retry.job_id).status == ("retry" if backoff else "skipped")
            persisted = host.chat.library.load("misty")
            maintained = {event.event_id: event for event in persisted.episodic}
            assert maintained[events[0].event_id].tier == "short"
            assert maintained[events[0].event_id].decay_demoted
            assert maintained[shown].hit_count == 1
            assert persisted.qq_memory_state["recalls"]["adopt-while-source-fails"] == [shown]
            assert persisted.qq_memory_state["last_review_at"] == moment.isoformat()
            await worker.tick()
            assert host.chat.library.load("misty").episodic[-1].hit_count == 1
            # Once the failed source is repaired, its real observation predates
            # the live maintenance clock; the retained strength must not rewind.
            repaired = build_commit(persisted, retry, _proposal(retry, stance="counter", target=impression_id),
                                    Settings(), now=moment)
            for event in repaired.episodic:
                if event.event_id in maintained:
                    assert event.strength_updated_at >= maintained[event.event_id].strength_updated_at
        finally:
            await host.close()

    asyncio.run(exercise())
