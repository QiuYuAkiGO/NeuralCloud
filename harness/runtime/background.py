"""Independent heartbeat scheduling and bounded threads for blocking work.

Coroutine orchestration and shared Runtime state stay on the owner event loop.
Only synchronous I/O/computation crosses a thread boundary.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from contextvars import copy_context
import json
from pathlib import Path
import sqlite3
import time

from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.queue import PriorityEventQueue, QueueClosedError


class ThreadLane:
    def __init__(self, name: str) -> None:
        self.name = name
        self._executor = None
        self._jobs: set[asyncio.Future] = set()
        self._closed = False

    @property
    def pending(self) -> int:
        return sum(not job.done() for job in self._jobs)

    async def run(self, function, /, *args, **kwargs):
        if self._closed:
            raise RuntimeError(f"{self.name} thread lane is closed")
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=self.name)
        context = copy_context()
        job = asyncio.get_running_loop().run_in_executor(
            self._executor, lambda: context.run(function, *args, **kwargs))
        self._jobs.add(job)

        def completed(done):
            self._jobs.discard(done)
            if not done.cancelled():
                done.exception()

        job.add_done_callback(completed)
        # Cancellation of an HTTP caller does not stop a Python worker. Keep
        # the future alive and drain it before closing its database/client.
        return await asyncio.shield(job)

    async def close(self) -> None:
        self._closed = True
        if self._jobs:
            await asyncio.gather(*tuple(self._jobs), return_exceptions=True)
        if self._executor is not None:
            self._executor.shutdown(wait=False)

    async def drain(self, timeout: float) -> bool:
        if not self._jobs:
            return True
        _, pending = await asyncio.wait(tuple(self._jobs), timeout=max(0, timeout))
        return not pending


def read_json_records(path, query: str):
    """Use a thread-owned read connection; never transfer the owner's SQLite handle."""
    with closing(sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        records = db.execute(query).fetchall()
    return [json.loads(row[0]) for row in records]


async def read_background_records(owner, path, query):
    lane = getattr(getattr(owner, "heartbeat", None), "thread", None)
    if lane is None:
        return await asyncio.to_thread(read_json_records, path, query)
    return await lane.run(read_json_records, path, query)


class HeartbeatWorker:
    def __init__(self, supervisor) -> None:
        self.supervisor = supervisor
        self.queue = PriorityEventQueue(capacity=2)
        self.thread = ThreadLane("nc-heartbeat")
        self.active = False
        self._closed = False
        self.task = None
        self.ticker = None
        self.current_event = None
        self.last_duration_ms = None
        self.last_error = None

    def start(self) -> None:
        if self.active:
            return
        if self._closed:
            if (self.task is not None and not self.task.done()) or self.thread.pending:
                raise RuntimeError("previous heartbeat worker has not drained")
            self.queue = PriorityEventQueue(capacity=2)
            self.thread = ThreadLane("nc-heartbeat")
            self._closed = False
        self.active = True
        self.task = asyncio.create_task(self._run(), name="runtime-heartbeat-queue")
        self.ticker = asyncio.create_task(self._tick(), name="runtime-heartbeat-scheduler")

    async def _tick(self) -> None:
        owner = self.supervisor
        while self.active:
            delay = owner.scheduler.seconds_until_deadline()
            await asyncio.sleep(min(0.1, max(0.001, delay)) if delay is not None else 0.1)
            if owner.lifecycle != RuntimeLifecycle.RUNNING:
                continue
            due = owner.scheduler.make_due_event()
            if due is not None:
                try:
                    await owner.submit_event(**due)
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    owner._append_journal("heartbeat_schedule_failed", data={"error_type": self.last_error})

    async def _run(self) -> None:
        while True:
            try:
                event = await self.queue.get(predicate=lambda _: self.supervisor.lifecycle == RuntimeLifecycle.RUNNING)
            except QueueClosedError:
                return
            self.current_event = event
            started = time.monotonic()
            owner = self.supervisor
            owner._append_journal("heartbeat_work_started", event=event,
                data={"queue_wait_ms": max(0, (utc_now() - event.received_at).total_seconds() * 1000)})
            try:
                result = await owner.dispatch(event)
                self.last_error = result.detail.get("error_type") or result.detail.get("error")
            except Exception as exc:
                self.last_error = type(exc).__name__
                owner._append_journal("heartbeat_work_failed", event=event, data={"error_type": self.last_error})
            finally:
                self.last_duration_ms = round((time.monotonic() - started) * 1000, 3)
                owner._append_journal("heartbeat_work_completed", event=event,
                    data={"elapsed_ms": self.last_duration_ms, "error_type": self.last_error})
                self.current_event = None

    def status(self) -> dict:
        stats = self.queue.statistics()
        return {"active": self.active, "pending": self.queue.qsize(),
                "running": self.current_event is not None, "coalesced": stats.coalesced,
                "thread_jobs": self.thread.pending, "last_duration_ms": self.last_duration_ms,
                "last_error": self.last_error}

    async def close(self, timeout: float) -> bool:
        deadline = asyncio.get_running_loop().time() + timeout
        self.active = False
        if self.ticker is not None:
            self.ticker.cancel()
            await asyncio.gather(self.ticker, return_exceptions=True)
        await self.queue.close()
        self._closed = True
        if self.task is not None and self.task is not asyncio.current_task():
            _, pending = await asyncio.wait({self.task}, timeout=timeout)
            if pending:
                return False
        if not await self.thread.drain(deadline - asyncio.get_running_loop().time()):
            return False
        await self.thread.close()
        return True
