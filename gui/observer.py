"""Stage observers: serialize pipeline results and fan them out to WebSocket clients."""

from __future__ import annotations

import asyncio
from typing import Any

from harness.pipeline.pipeline import StageObserver

__all__ = ["QueueObserver", "RecordingObserver", "StageObserver", "serialize_stage"]


def serialize_stage(kind: str, result: Any) -> Any:
    """Turn a stage result into JSON-safe data. Gate tuples keep request + complexity only."""
    if kind == "gate" and isinstance(result, tuple) and len(result) >= 2:
        situation, request = result[0], result[1]
        complexity = getattr(situation, "complexity", None)
        return {
            "request": _dump(request),
            "complexity": _dump(complexity),
        }
    return _dump(result)


def _dump(value: Any) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_dump(item) for item in value]
    if isinstance(value, tuple):
        return [_dump(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _dump(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


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
