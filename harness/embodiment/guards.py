"""Independent body-state polling guard."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

from harness.embodiment.contracts import BodyStateSnapshot, captured_age_seconds
from harness.embodiment.ports import BodyStateProvider
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType

SubmitEvent = Callable[..., Awaitable[RuntimeEvent | None]]
Clock = Callable[[], datetime]


class BodyStateGuard:
    def __init__(
        self,
        provider: BodyStateProvider,
        submit: SubmitEvent,
        *,
        interval_seconds: float = 5.0,
        max_age_seconds: float = 10.0,
        expected_body_instance_id: str | None = None,
        source: str = "body_state",
        clock: Clock | None = None,
    ) -> None:
        if interval_seconds <= 0 or max_age_seconds <= 0:
            raise ValueError("body-state intervals must be positive")
        self.provider = provider
        self.submit = submit
        self.interval_seconds = interval_seconds
        self.max_age_seconds = max_age_seconds
        self.expected_body_instance_id = expected_body_instance_id
        self.source = source
        self.clock = clock or (lambda: datetime.now(UTC))
        self._last_revision: int | None = None
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="runtime-body-state-guard")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval_seconds + 1.0)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    async def capture_once(self) -> BodyStateSnapshot:
        snapshot = await self.provider.snapshot()
        now = self.clock()
        if self.expected_body_instance_id and snapshot.body_instance_id != self.expected_body_instance_id:
            raise ValueError("body-state snapshot belongs to a different body instance")
        if self._last_revision is not None and snapshot.revision < self._last_revision:
            raise ValueError("body-state revision moved backwards")
        age = captured_age_seconds(snapshot.captured_at, now)
        if age < -1.0:
            raise ValueError("body-state snapshot is from the future")
        if age > self.max_age_seconds:
            raise ValueError("body-state snapshot is stale")
        self._last_revision = snapshot.revision
        return snapshot

    async def run(self) -> None:
        while not self._stop.is_set():
            moment = self.clock()
            try:
                snapshot = await self.capture_once()
                await self.submit(
                    event_type=RuntimeEventType.BODY_STATE,
                    priority=RuntimeEventPriority.PERCEPTION,
                    source=self.source,
                    source_time=snapshot.captured_at,
                    idempotency_key=(
                        f"body-state:{snapshot.body_instance_id}:{snapshot.revision}"
                    ),
                    payload={"body_state": snapshot.model_dump(mode="json")},
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.submit(
                    event_type=RuntimeEventType.DIAGNOSTIC,
                    priority=RuntimeEventPriority.CONTROL,
                    source=self.source,
                    source_time=moment,
                    payload={"body_state_error": str(exc)},
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass
