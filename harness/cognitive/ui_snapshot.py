"""Disposable, bounded operator hot data. Never used as model memory."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import time


class UISnapshotCache:
    """One background reader regardless of refresh callers or cancellations.

    Ordinary updates may lag by refresh_after seconds. Authority changes must
    change key (or invalidate), which immediately withholds the old value.
    No cached state is persisted or written back to the authoritative stores.
    """

    def __init__(self, *, refresh_after=2.0, max_age=15.0, cold_wait=.25, clock=time.monotonic):
        self.refresh_after, self.max_age, self.cold_wait = refresh_after, max_age, cold_wait
        self.clock = clock
        self._key = None
        self._generation = 0
        self._value = None
        self._task = None
        self._next_refresh = 0.0
        self._sampled = 0.0
        self._as_of = None
        self._error = None
        self._closed = False
        self.refresh_count = 0
        self.last_duration_ms = None

    def invalidate(self):
        self._generation += 1
        self._key, self._value, self._as_of, self._error = None, None, None, None
        self._next_refresh = 0.0

    async def _refresh(self, key, generation, reader):
        started = self.clock()
        as_of = datetime.now(timezone.utc).isoformat()
        self.refresh_count += 1
        try:
            value = await asyncio.to_thread(reader)
        except Exception as exc:
            if generation == self._generation:
                self._value = None
                self._error = type(exc).__name__  # Never include private text.
        else:
            if key == self._key and generation == self._generation:
                self._value, self._sampled, self._as_of = value, started, as_of
                self._error = None
        finally:
            if generation == self._generation:
                self.last_duration_ms = round((self.clock()-started)*1000, 2)
                # Failures and long reads cannot cause a hot retry loop.
                self._next_refresh = self.clock() + self.refresh_after

    async def get(self, key, reader):
        if key != self._key:
            self.invalidate()
            self._key = key
        generation = self._generation
        now = self.clock()
        if (not self._closed and (self._task is None or self._task.done()) and now >= self._next_refresh
                and (self._value is None or now-self._sampled >= self.refresh_after)):
            self._task = asyncio.create_task(self._refresh(key, generation, reader))
        if self._value is None and self._task is not None and not self._task.done():
            # wait() leaves the sole reader alive if a browser disconnects.
            await asyncio.wait({self._task}, timeout=self.cold_wait)
        valid = key == self._key and generation == self._generation and not self._closed
        age = max(0, self.clock()-self._sampled) if valid and self._value is not None else None
        value = self._value if age is not None and age <= self.max_age else None
        refreshing = bool(self._task and not self._task.done()) and not self._closed
        return (deepcopy(value) if value is not None else {}), {
            "status": ("refreshing" if refreshing else "ready") if value is not None else
                      "loading" if refreshing else "unavailable",
            "as_of": self._as_of if valid and value is not None else None,
            "age_seconds": round(age, 3) if age is not None else None,
            "refreshing": refreshing, "last_error_type": self._error if valid else None,
            "refresh_count": self.refresh_count, "last_duration_ms": self.last_duration_ms,
        }

    async def close(self):
        self._closed = True
        self.invalidate()
        if self._task is not None:
            await asyncio.shield(self._task)
