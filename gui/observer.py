"""Stage observers: serialize pipeline results and fan them out to WebSocket clients."""

from __future__ import annotations

import asyncio
from typing import Any

from harness.pipeline.pipeline import StageObserver
from infra.trace.serialize import serialize_stage

__all__ = ["QueueObserver", "RecordingObserver", "StageObserver", "serialize_stage"]


class RecordingObserver:
    """Collects stage events in-process. Used by tests and as a fallback."""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []
        self.run_id: str | None = None

    def bind(self, run_id: str) -> None:
        self.run_id = run_id

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        self.events.append(
            {
                "type": "final" if kind == "final" else "stage",
                "run_id": self.run_id,
                "stage": kind,
                "latency_ms": payload.get("latency_ms", 0.0),
                "data": serialize_stage(kind, result),
                "meta": {key: value for key, value in payload.items() if key != "latency_ms"},
            }
        )

    def emit_state(self, snapshot: dict[str, Any], run_id: str | None = None) -> None:
        self.events.append({"type": "state", "run_id": run_id or self.run_id, "data": snapshot})

    def emit_error(self, message: str, run_id: str | None = None) -> None:
        self.events.append(
            {"type": "error", "run_id": run_id or self.run_id, "data": {"message": message}}
        )


class QueueObserver:
    """Broadcast serialized stage events onto asyncio queues from a worker thread."""

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        subscribers: set[asyncio.Queue[dict[str, Any]]],
    ) -> None:
        self.loop = loop
        self.subscribers = subscribers
        self.run_id: str | None = None

    def bind(self, run_id: str) -> None:
        self.run_id = run_id

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        self._broadcast(
            {
                "type": "final" if kind == "final" else "stage",
                "run_id": self.run_id,
                "stage": kind,
                "latency_ms": payload.get("latency_ms", 0.0),
                "data": serialize_stage(kind, result),
                "meta": {key: value for key, value in payload.items() if key != "latency_ms"},
            }
        )

    def emit_state(self, snapshot: dict[str, Any], run_id: str | None = None) -> None:
        self._broadcast({"type": "state", "run_id": run_id or self.run_id, "data": snapshot})

    def emit_error(self, message: str, run_id: str | None = None) -> None:
        self._broadcast(
            {"type": "error", "run_id": run_id or self.run_id, "data": {"message": message}}
        )

    def _broadcast(self, event: dict[str, Any]) -> None:
        def _put() -> None:
            for queue in list(self.subscribers):
                try:
                    queue.put_nowait(event)
                except asyncio.QueueFull:
                    continue

        try:
            if self.loop.is_running():
                self.loop.call_soon_threadsafe(_put)
            else:
                _put()
        except RuntimeError:
            _put()
