"""Real-time deadline scheduler; it never catches up missed heartbeats."""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import UTC, datetime

from harness.runtime.models import RuntimeEventPriority, RuntimeEventType


class RealTimeScheduler:
    def __init__(
        self,
        heartbeat_seconds: float = 60.0,
        *,
        monotonic: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], datetime] | None = None,
    ) -> None:
        if heartbeat_seconds <= 0:
            raise ValueError("heartbeat_seconds must be positive")
        self.heartbeat_seconds = float(heartbeat_seconds)
        self._monotonic = monotonic
        self._wall_clock = wall_clock or (lambda: datetime.now(UTC))
        self._next_heartbeat: float | None = None

    def start(self) -> None:
        self._next_heartbeat = self._monotonic() + self.heartbeat_seconds

    def reset_from_now(self) -> None:
        self.start()

    def suspend(self) -> None:
        self._next_heartbeat = None

    def next_deadline(self) -> float | None:
        return self._next_heartbeat

    def seconds_until_deadline(self) -> float | None:
        if self._next_heartbeat is None:
            return None
        return max(0.0, self._next_heartbeat - self._monotonic())

    def make_due_event(self) -> dict[str, object] | None:
        if self._next_heartbeat is None or self._monotonic() < self._next_heartbeat:
            return None
        now = self._wall_clock()
        # Advance once from the current time, not from the missed deadline.
        self._next_heartbeat = self._monotonic() + self.heartbeat_seconds
        return {
            "event_type": RuntimeEventType.HEARTBEAT,
            "priority": RuntimeEventPriority.HEARTBEAT,
            "source": "scheduler",
            "source_time": now,
            "payload": {"scheduled_at": now.isoformat()},
        }
