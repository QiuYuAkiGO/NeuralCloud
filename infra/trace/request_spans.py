"""Request telemetry with causal context that survives copied worker contexts.

Only operational metadata is recorded: prompts, replies, headers and exception
messages are deliberately absent. Each request has an append-only start and end
record, so interrupted requests and late background completions stay visible.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator
from urllib.parse import urlsplit, urlunsplit
from uuid import uuid4

from harness.config import Settings

logger = logging.getLogger(__name__)
_context: ContextVar[dict[str, Any] | None] = ContextVar("nc_trace_context", default=None)
_write_lock = threading.Lock()
_CONTEXT_FIELDS = {
    "correlation_id", "event_id", "command_id", "phase", "stage",
    "span_id", "parent_span_id", "model_role",
    "model_attempt", "model_budget_seconds",
}


def current_trace_context() -> dict[str, Any]:
    """Return detached metadata, safe to copy into a durable record."""
    return dict(_context.get() or {})


def new_span_id(kind: str = "span") -> str:
    return f"{kind}_{uuid4().hex}"


@contextmanager
def trace_scope(**metadata: Any) -> Iterator[dict[str, Any]]:
    """Bind metadata for a command, stage, or routed model call.

    ``asyncio.to_thread`` copies this context automatically. Executor.submit
    callers must explicitly submit ``copy_context().run`` to preserve it.
    """
    unknown = metadata.keys() - _CONTEXT_FIELDS
    if unknown:
        raise ValueError(f"unknown trace context fields: {sorted(unknown)}")
    merged = current_trace_context()
    merged.update(metadata)
    token = _context.set(merged)
    try:
        yield dict(merged)
    finally:
        _context.reset(token)


def safe_endpoint(value: str) -> str:
    """Keep endpoint identity without URL credentials, query or fragment."""
    parsed = urlsplit(value)
    return urlunsplit((parsed.scheme, parsed.netloc.rsplit("@", 1)[-1], parsed.path, "", ""))


def _append(path: Path, payload: dict[str, Any]) -> None:
    try:
        line = json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n"
        with _write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception as exc:
        # Diagnostics must never change model success/failure or expose secrets
        # from an OS/HTTP exception in a secondary logger.
        logger.warning("model request telemetry write failed (%s)", type(exc).__name__)


@contextmanager
def model_request_span(settings: Settings, *, role: str) -> Iterator[dict[str, Any]]:
    """Yield mutable safe result fields; always append a terminal request row."""
    metadata = current_trace_context()
    span_id = new_span_id("model_request")
    started_at = datetime.now(UTC).isoformat()
    started = time.perf_counter()
    base = {
        "schema_version": "model-request-span-v1",
        **metadata,
        "span_id": span_id,
        "parent_span_id": metadata.get("span_id"),
        "role": metadata.get("model_role") or role,
        "model": settings.llm_model,
        "endpoint": safe_endpoint(settings.llm_base_url),
        "started_at": started_at,
        "attempt": metadata.get("model_attempt", 1),
        "retries": max(0, metadata.get("model_attempt", 1) - 1),
    }
    path = Path(settings.trace_dir) / "model_requests.jsonl"
    enabled = settings.call_log_enabled
    if enabled:
        _append(path, {**base, "record_type": "model_request_started", "status": "running"})
    result: dict[str, Any] = {"http_status": None, "usage": None, "usage_source": "unavailable"}
    status = "success"
    error = None
    try:
        yield result
    except BaseException as exc:
        status = "error"
        error = {"type": type(exc).__name__, "message": "model request failed"}
        raise
    finally:
        if enabled:
            _append(path, {
                **base,
                **result,
                "record_type": "model_request_finished",
                "completed_at": datetime.now(UTC).isoformat(),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                "status": status,
                "error": error,
            })
