"""Temporary production limits never exhaust durable memory jobs' retries."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import sqlite3
from types import SimpleNamespace

import pytest

from harness.memory.qq_assimilation import QQAssimilationQueue
from harness.memory.revision.queue import DailyRevisionQueue
from tests.test_daily_revision_worker import setup_host as daily_host
from tests.test_production_cognition import context
from tests.test_qq_memory_worker import setup_host as qq_host


def _clock(monkeypatch):
    now = [datetime.now(UTC) + timedelta(days=1)]
    monkeypatch.setattr("harness.memory.revision.queue._now", lambda: now[0].isoformat())
    monkeypatch.setattr("harness.memory.qq_assimilation._now", lambda value=None: value or now[0])
    return now


def _job(worker, kind, run_id):
    if kind == "daily":
        return worker.queue.get(run_id)
    with sqlite3.connect(worker.queue.path) as db:
        identity = db.execute("SELECT job_id FROM qq_memory_jobs").fetchone()[0]
    return worker.queue.get_job(identity).model_dump(mode="json")


async def _fixture(kind, tmp_path, monkeypatch):
    host, day = (daily_host if kind == "daily" else qq_host)(tmp_path, monkeypatch)
    host.chat.settings.production_memory_enabled = True
    current = context(autonomous=True)
    current["budget"] = {"remaining": 12}
    host.adapter.production_meter = SimpleNamespace(context=lambda **kwargs: current)
    worker = host.daily_revision if kind == "daily" else host.qq_memory
    run_id = (await worker.enqueue(day=day))["run_ids"][0] if kind == "daily" else None
    return host, worker, current, run_id


@pytest.mark.parametrize("kind", ["qq", "daily"])
@pytest.mark.parametrize("restriction", ["paused", "resting", "exhausted", "disconnected", "budget"])
def test_repeated_temporary_limits_preserve_job_and_resume_after_queue_reopen(tmp_path, monkeypatch, kind, restriction):
    async def run():
        now = _clock(monkeypatch)
        host, worker, current, run_id = await _fixture(kind, tmp_path, monkeypatch)
        if restriction == "paused":
            current["runtime_lifecycle"] = "PAUSED"
        elif restriction == "budget":
            current["budget"]["remaining"] = 0
        else:
            current["restricted_dialogue"] = True
            current["body"]["available"] = restriction != "disconnected"
            current["body"]["snapshot"].update(resting=restriction == "resting", exhausted=restriction == "exhausted")
        proposer = worker.provider if kind == "daily" else worker.proposer
        try:
            frozen = None
            # More than either queue's failure limit: these are eligibility
            # checks, so no job failure or model attempt should accumulate.
            for _ in range(10):
                await worker.tick()
                row = _job(worker, kind, run_id)
                assert row["status"] == "retry", (worker.last_error, row)
                assert row["attempts"] == 0
                assert proposer.calls == 0
                originals = row["sources"]
                if frozen is None:
                    frozen = originals
                assert originals == frozen
                now[0] += timedelta(seconds=31)
            if kind == "daily":
                worker.queue = DailyRevisionQueue(worker.queue.path)
            else:
                worker.queue = QQAssimilationQueue(worker.queue.path, owner_id="misty", preset_id="misty",
                    self_id="29999", allowed_group_ids=("10001",))
            assert _job(worker, kind, run_id)["attempts"] == 0
            current.update(runtime_lifecycle="RUNNING", restricted_dialogue=False)
            current["body"]["available"] = True
            current["body"]["snapshot"].update(resting=False, exhausted=False)
            current["budget"]["remaining"] = 12
            await worker.tick()
            row = _job(worker, kind, run_id)
            assert row["status"] == "applied", (worker.last_error, row)
            assert row["attempts"] == 1
            assert row["sources"] == frozen
            assert proposer.calls == 1
            await worker.tick()
            assert proposer.calls == 1
        finally:
            await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["qq", "daily"])
def test_pause_after_proposal_preserves_cached_result_for_resume(tmp_path, monkeypatch, kind):
    async def run():
        now = _clock(monkeypatch)
        host, worker, _, run_id = await _fixture(kind, tmp_path, monkeypatch)
        proposer = worker.provider if kind == "daily" else worker.proposer
        original = proposer.propose

        def pause_after_proposal(*args, **kwargs):
            result = original(*args, **kwargs)
            host.adapter._maintenance_paused = True
            return result

        proposer.propose = pause_after_proposal
        try:
            await worker.tick()
            row = _job(worker, kind, run_id)
            assert row["status"] == "retry", (worker.last_error, row)
            assert row["attempts"] == 0
            saved = row["stages"]["proposal"] if kind == "daily" else row["proposal"]
            assert saved and proposer.calls == 1
            # Duplicate defer cannot refund older failures or a future claim.
            identity = run_id if kind == "daily" else row["job_id"]
            assert not worker.queue.defer(identity, "maintenance_paused")
            host.adapter._maintenance_paused = False
            now[0] += timedelta(seconds=31)
            await worker.tick()
            assert _job(worker, kind, run_id)["status"] == "applied", worker.last_error
            assert proposer.calls == 1
        finally:
            await host.close()

    asyncio.run(run())


@pytest.mark.parametrize("kind", ["qq", "daily"])
def test_deferral_keeps_prior_real_failure_count(tmp_path, monkeypatch, kind):
    async def run():
        now = _clock(monkeypatch)
        host, worker, current, run_id = await _fixture(kind, tmp_path, monkeypatch)
        proposer = worker.provider if kind == "daily" else worker.proposer
        original = proposer.propose

        def fail(*args, **kwargs):
            raise OSError("synthetic model failure")

        proposer.propose = fail
        try:
            await worker.tick()
            assert _job(worker, kind, run_id)["attempts"] == 1
            proposer.propose = original
            current["restricted_dialogue"] = True
            now[0] += timedelta(seconds=31)
            await worker.tick()
            row = _job(worker, kind, run_id)
            assert row["status"] == "retry" and row["attempts"] == 1
            current["restricted_dialogue"] = False
            now[0] += timedelta(seconds=31)
            await worker.tick()
            row = _job(worker, kind, run_id)
            assert row["status"] == "applied" and row["attempts"] == 2
            assert proposer.calls == 1
        finally:
            await host.close()

    asyncio.run(run())
