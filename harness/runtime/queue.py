"""Bounded priority queue with Runtime-specific coalescing and shedding."""

from __future__ import annotations

import asyncio
import heapq
from collections.abc import Callable
from dataclasses import dataclass

from harness.runtime.models import (
    QueueStatistics,
    RuntimeEvent,
    RuntimeEventPriority,
    RuntimeEventType,
)


class QueueClosedError(RuntimeError):
    pass


class QueueCapacityError(RuntimeError):
    """The bounded queue contains only protected events and cannot shed load."""


@dataclass(order=True)
class _QueueItem:
    priority: int
    receive_seq: int
    event: RuntimeEvent


class PriorityEventQueue:
    def __init__(self, capacity: int = 1024, *, high_watermark_ratio: float = 0.8) -> None:
        if capacity < 2:
            raise ValueError("queue capacity must be at least 2")
        if not 0 < high_watermark_ratio <= 1:
            raise ValueError("high_watermark_ratio must be in (0, 1]")
        self.capacity = capacity
        self.high_watermark = max(1, int(capacity * high_watermark_ratio))
        self._heap: list[_QueueItem] = []
        self._condition = asyncio.Condition()
        self._closed = False
        self._coalesced = 0
        self._dropped = 0
        self._rejected = 0
        self._recently_shed: list[RuntimeEvent] = []

    def qsize(self) -> int:
        return len(self._heap)

    def empty(self) -> bool:
        return not self._heap

    def is_high(self) -> bool:
        return len(self._heap) >= self.high_watermark

    @staticmethod
    def _coalescing_key(event: RuntimeEvent) -> tuple[str, str] | None:
        if event.event_type == RuntimeEventType.HEARTBEAT:
            return event.event_type.value, "runtime"
        if event.event_type == RuntimeEventType.PERCEPTION:
            return event.event_type.value, event.source
        return None

    async def put(self, event: RuntimeEvent) -> bool:
        """Queue an event.

        Returns ``False`` when a droppable incoming event was intentionally
        discarded. Protected events either enter the queue or raise
        :class:`QueueCapacityError`.
        """

        async with self._condition:
            if self._closed:
                raise QueueClosedError("event queue is closed")

            key = self._coalescing_key(event)
            if key is not None:
                existing_index = self._find_coalescing_index(key)
                if existing_index is not None:
                    existing = self._heap[existing_index].event
                    merged = existing.coalesce_with(event)
                    self._heap[existing_index] = _QueueItem(int(merged.priority), merged.receive_seq, merged)
                    heapq.heapify(self._heap)
                    self._coalesced += event.coalesced_count
                    self._condition.notify_all()
                    return True

            # Keep one slot available for emergency/control events. A protected
            # user event remains durable in the journal and will be recovered,
            # but cannot consume the final control slot.
            if (
                event.priority == RuntimeEventPriority.USER
                and len(self._heap) >= self.capacity - 1
            ):
                shed_index = self._find_sheddable_index()
                if shed_index is not None:
                    self._recently_shed.append(self._remove_at(shed_index).event)
                    self._dropped += 1
                else:
                    self._rejected += 1
                    raise QueueCapacityError("queue control reserve is in use")

            if len(self._heap) >= self.capacity:
                shed_index = self._find_sheddable_index()
                if shed_index is not None and (
                    event.is_protected or int(event.priority) < self._heap[shed_index].priority
                ):
                    self._recently_shed.append(self._remove_at(shed_index).event)
                    self._dropped += 1
                elif event.priority >= RuntimeEventPriority.PERCEPTION:
                    self._recently_shed.append(event)
                    self._dropped += 1
                    return False
                else:
                    self._rejected += 1
                    raise QueueCapacityError("bounded queue contains only protected events")

            heapq.heappush(self._heap, _QueueItem(int(event.priority), event.receive_seq, event))
            self._condition.notify_all()
            return True

    async def get(
        self,
        *,
        timeout: float | None = None,
        predicate: Callable[[RuntimeEvent], bool] | None = None,
    ) -> RuntimeEvent | None:
        """Return the first eligible event, or ``None`` when *timeout* expires."""

        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + max(0.0, timeout)
        async with self._condition:
            while True:
                index = self._first_eligible_index(predicate)
                if index is not None:
                    return self._remove_at(index).event
                if self._closed:
                    if self._heap:
                        raise QueueClosedError("queue closed with ineligible events")
                    raise QueueClosedError("event queue is closed")
                if deadline is None:
                    await self._condition.wait()
                    continue
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return None
                try:
                    await asyncio.wait_for(self._condition.wait(), timeout=remaining)
                except TimeoutError:
                    return None

    async def wake(self) -> None:
        async with self._condition:
            self._condition.notify_all()

    async def close(self) -> None:
        async with self._condition:
            self._closed = True
            self._condition.notify_all()

    def has_higher_priority_than(self, priority: RuntimeEventPriority) -> bool:
        return bool(self._heap and self._heap[0].priority < int(priority))

    def snapshot(self) -> list[RuntimeEvent]:
        return [item.event for item in sorted(self._heap)]

    def statistics(self) -> QueueStatistics:
        counts: dict[str, int] = {}
        for item in self._heap:
            name = RuntimeEventPriority(item.priority).name
            counts[name] = counts.get(name, 0) + 1
        return QueueStatistics(
            size=len(self._heap),
            capacity=self.capacity,
            high_watermark=self.high_watermark,
            by_priority=counts,
            coalesced=self._coalesced,
            dropped=self._dropped,
            rejected=self._rejected,
        )

    def take_shedding_events(self) -> list[RuntimeEvent]:
        events = self._recently_shed
        self._recently_shed = []
        return events

    def _find_coalescing_index(self, key: tuple[str, str]) -> int | None:
        for index, item in enumerate(self._heap):
            if self._coalescing_key(item.event) == key:
                return index
        return None

    def _find_sheddable_index(self) -> int | None:
        candidates = [
            (item.priority, item.receive_seq, index)
            for index, item in enumerate(self._heap)
            if not item.event.is_protected
        ]
        if not candidates:
            return None
        # Highest numeric priority and newest receive sequence are least valuable.
        return max(candidates)[2]

    def _first_eligible_index(
        self, predicate: Callable[[RuntimeEvent], bool] | None
    ) -> int | None:
        candidates = [
            (item.priority, item.receive_seq, index)
            for index, item in enumerate(self._heap)
            if predicate is None or predicate(item.event)
        ]
        return min(candidates)[2] if candidates else None

    def _remove_at(self, index: int) -> _QueueItem:
        item = self._heap[index]
        last = self._heap.pop()
        if index < len(self._heap):
            self._heap[index] = last
            heapq.heapify(self._heap)
        return item
