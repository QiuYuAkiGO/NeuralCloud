"""Long-running perception guard with pause-time observation aggregation."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel

from harness.embodiment.ports import PerceptionBackend
from harness.runtime.models import ResumeObservation, RuntimeEvent, RuntimeEventPriority, RuntimeEventType


SubmitPerception = Callable[..., Awaitable[RuntimeEvent | None]]


class PauseObservationBuffer:
    def __init__(self, *, significant_limit: int = 16) -> None:
        self.significant_limit = significant_limit
        self.clear()

    def clear(self) -> None:
        self.paused_at: datetime | None = None
        self.first_receive_seq: int | None = None
        self.last_receive_seq: int | None = None
        self.count = 0
        self.latest_world: dict[str, Any] = {}
        self.significant_changes: list[dict[str, Any]] = []

    def begin(self, paused_at: datetime) -> None:
        self.clear()
        self.paused_at = paused_at

    def add(self, event: RuntimeEvent) -> None:
        if self.paused_at is None:
            self.paused_at = event.received_at
        first, last = event.sequence_range
        self.first_receive_seq = first if self.first_receive_seq is None else min(self.first_receive_seq, first)
        self.last_receive_seq = last if self.last_receive_seq is None else max(self.last_receive_seq, last)
        self.count += event.coalesced_count
        self.latest_world = dict(event.payload)
        if event.event_type == RuntimeEventType.ENVIRONMENT_CHANGE and len(self.significant_changes) < self.significant_limit:
            self.significant_changes.append(dict(event.payload))

    def build(self, *, resumed_at: datetime) -> ResumeObservation | None:
        if self.count == 0 or self.paused_at is None:
            return None
        assert self.first_receive_seq is not None
        assert self.last_receive_seq is not None
        return ResumeObservation(
            latest_world=self.latest_world,
            significant_changes=self.significant_changes,
            first_receive_seq=self.first_receive_seq,
            last_receive_seq=self.last_receive_seq,
            observation_count=self.count,
            paused_at=self.paused_at,
            resumed_at=resumed_at,
            duration_seconds=max(0.0, (resumed_at - self.paused_at).total_seconds()),
        )


class PerceptionGuard:
    def __init__(
        self,
        backend: PerceptionBackend,
        submit: SubmitPerception,
        *,
        interval_seconds: float = 5.0,
        source: str = "perception",
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.backend = backend
        self.submit = submit
        self.interval_seconds = interval_seconds
        self.source = source
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def start(self) -> asyncio.Task[None]:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self.run(), name="runtime-perception-guard")
        return self._task

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            try:
                await asyncio.wait_for(self._task, timeout=self.interval_seconds + 1)
            except TimeoutError:
                self._task.cancel()
                await asyncio.gather(self._task, return_exceptions=True)

    async def run(self) -> None:
        while not self._stop.is_set():
            moment = datetime.now(UTC)
            try:
                observation = await self.backend.observe(now=moment)
                payload = self._normalise(observation)
                await self.submit(
                    event_type=RuntimeEventType.PERCEPTION,
                    priority=RuntimeEventPriority.PERCEPTION,
                    source=self.source,
                    source_time=moment,
                    payload=payload,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.submit(
                    event_type=RuntimeEventType.DIAGNOSTIC,
                    priority=RuntimeEventPriority.CONTROL,
                    source=self.source,
                    source_time=moment,
                    payload={"perception_error": str(exc)},
                )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval_seconds)
            except TimeoutError:
                pass

    @staticmethod
    def _normalise(observation: Any) -> dict[str, Any]:
        if isinstance(observation, BaseModel):
            return observation.model_dump(mode="json")
        if isinstance(observation, dict):
            return dict(observation)
        return {"value": observation}
