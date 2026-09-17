from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from harness.cognitive.daily_revision import DailyRevisionWorker
from harness.cognitive.qq_memory import QQMemoryWorker, foreground_pending, wait_foreground_idle
from tests.test_cognitive_incremental_store import _owner
from tests.test_daily_revision_worker import setup_host


def test_both_background_workers_defer_before_claim_when_foreground_is_pending(tmp_path):
    async def run():
        release = asyncio.Event()
        command = asyncio.create_task(release.wait())
        host = SimpleNamespace(adapter=SimpleNamespace(command_tasks={command}, _maintenance_paused=False))
        qq = object.__new__(QQMemoryWorker)
        qq.host, qq.stopping = host, asyncio.Event()
        daily = DailyRevisionWorker(host, config_path=tmp_path / "config.json", queue_path=tmp_path / "queue.db")
        try:
            # The minimal host intentionally lacks memory/providers. Deferral
            # must happen before source sync, snapshotting or claiming work.
            assert await qq.tick() is None
            assert await daily.tick() is False
            daily.poke()
            assert daily._work_task is None
        finally:
            release.set()
            await command
    asyncio.run(run())


def test_foreground_wait_uses_owned_tasks_after_rpc_cancellation_and_is_stoppable(tmp_path):
    async def run():
        adapter, _, _ = _owner(tmp_path)
        host = SimpleNamespace(adapter=adapter)
        release, started, stopping = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def operation():
            started.set()
            await release.wait()
        waiter = asyncio.create_task(adapter._run_command(operation))
        await started.wait()
        waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiter
        assert foreground_pending(host)
        background = asyncio.create_task(wait_foreground_idle(host, stopping))
        await asyncio.sleep(0)
        assert not background.done()
        stopping.set()
        assert await asyncio.wait_for(background, .5) is False
        assert foreground_pending(host) and not next(iter(adapter.command_tasks)).cancelled()
        release.set()
        await asyncio.gather(*adapter.command_tasks)
        assert not foreground_pending(host)
        assert await wait_foreground_idle(host, asyncio.Event()) is True
        await adapter.close()
    asyncio.run(run())


def test_waiter_cancellation_cannot_cancel_foreground_work():
    async def run():
        release = asyncio.Event()
        command = asyncio.create_task(release.wait())
        host = SimpleNamespace(adapter=SimpleNamespace(command_tasks={command}))
        background = asyncio.create_task(wait_foreground_idle(host, asyncio.Event()))
        await asyncio.sleep(0)
        background.cancel()
        with pytest.raises(asyncio.CancelledError):
            await background
        assert not command.done()
        release.set()
        await command
    asyncio.run(run())


def test_foreground_arriving_during_daily_claim_delays_model_without_consuming_retry(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch)
        worker = host.daily_revision
        release, started = asyncio.Event(), asyncio.Event()
        loop = asyncio.get_running_loop()
        async def foreground():
            started.set()
            await release.wait()
        def start_foreground():
            task = asyncio.create_task(foreground())
            host.adapter.command_tasks.add(task)
            task.add_done_callback(host.adapter.command_tasks.discard)
        original = worker.queue.claim
        def claim(**kwargs):
            job = original(**kwargs)
            loop.call_soon_threadsafe(start_foreground)
            return job
        work = None
        try:
            run_id = (await worker.enqueue(day=day))["run_ids"][0]
            monkeypatch.setattr(worker.queue, "claim", claim)
            work = asyncio.create_task(worker.tick())
            await asyncio.wait_for(started.wait(), 2)
            assert worker.provider.calls == 0
            assert worker.queue.get(run_id)["attempts"] == 1
            release.set()
            await asyncio.wait_for(work, 5)
            assert worker.provider.calls == 1
            row = worker.queue.get(run_id)
            assert row["status"] == "applied" and row["attempts"] == 1
        finally:
            release.set()
            if work is not None:
                await work
            await host.close()
    asyncio.run(run())
