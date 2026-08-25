"""Per-successful-call JSON dump: one file for one top-level invocation."""

from __future__ import annotations

import json
import threading
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from harness.config import SCHEMA_VERSION, Settings
from harness.core.ids import new_id
from infra.trace.recorder import Tracer
from infra.trace.serialize import dump_value, serialize_stage

_current: ContextVar[CallSession | None] = ContextVar("nc_call_log", default=None)
_pipeline_emit: ContextVar[bool] = ContextVar("nc_call_log_pipeline_emit", default=False)


@dataclass
class CallSession:
    call_id: str
    kind: str
    input: dict[str, Any]
    ts: str
    started: float
    steps: list[dict[str, Any]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def append_step(self, step: dict[str, Any]) -> None:
        with self._lock:
            self.steps.append(step)


class CallScope:
    def __init__(
        self,
        log: CallLog,
        kind: str,
        payload: dict[str, Any],
        *,
        call_id: str | None = None,
    ) -> None:
        self._log = log
        self._kind = kind
        self._payload = payload
        self._call_id = call_id
        self._nested = False
        self._session: CallSession | None = None
        self._token: Any = None
        self._result: Any = None

    def set_result(self, result: Any) -> None:
        self._result = result

    def __enter__(self) -> CallScope:
        if _current.get() is not None:
            self._nested = True
            return self
        if not self._log.enabled:
            return self
        session = CallSession(
            call_id=self._call_id or new_id("call"),
            kind=self._kind,
            input=dump_value(self._payload) or {},
            ts=datetime.now(UTC).isoformat(),
            started=time.perf_counter(),
        )
        self._session = session
        self._token = _current.set(session)
        return self

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, tb: Any) -> bool:
        if self._nested or self._session is None:
            return False
        try:
            if exc_type is None:
                self._log.flush(self._session, self._result)
        finally:
            _current.reset(self._token)
        return False


class CallLog:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._write_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return bool(self.settings.call_log_enabled)

    def active(self) -> CallSession | None:
        return _current.get()

    def scope(self, kind: str, payload: dict[str, Any], *, call_id: str | None = None) -> CallScope:
        return CallScope(self, kind, payload, call_id=call_id)

    def add_step(self, kind: str, data: Any, extra: dict[str, Any] | None = None) -> None:
        session = _current.get()
        if session is None:
            return
        step: dict[str, Any] = {"kind": kind, "data": dump_value(data)}
        extra = extra or {}
        if "latency_ms" in extra:
            step["latency_ms"] = extra["latency_ms"]
        meta = {key: value for key, value in extra.items() if key != "latency_ms"}
        if meta:
            step["meta"] = dump_value(meta)
        session.append_step(step)

    def flush(self, session: CallSession, result: Any) -> None:
        record = {
            "call_id": session.call_id,
            "kind": session.kind,
            "ok": True,
            "ts": session.ts,
            "latency_ms": round((time.perf_counter() - session.started) * 1000, 3),
            "schema_version": self.settings.schema_version or SCHEMA_VERSION,
            "model_version": self.settings.model_version,
            "prompt_version": self.settings.prompt_version,
            "input": session.input,
            "steps": list(session.steps),
            "result": dump_value(result),
        }
        path = call_log_path(self.settings, session.call_id, ts=session.ts)
        payload = json.dumps(record, ensure_ascii=False, indent=2, default=str) + "\n"
        with self._write_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(payload, encoding="utf-8")


class CallAwareTracer:
    """Forward to an inner Tracer and, when a call session is open, append a step."""

    def __init__(self, inner: Tracer, call_log: CallLog) -> None:
        self.inner = inner
        self.call_log = call_log

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        self.inner.record(kind, payload)
        if _pipeline_emit.get():
            return
        self.call_log.add_step(kind, payload)


@contextmanager
def pipeline_emit_guard() -> Iterator[None]:
    token = _pipeline_emit.set(True)
    try:
        yield
    finally:
        _pipeline_emit.reset(token)


def call_log_dir(settings: Settings) -> Path:
    return Path(settings.trace_dir) / "calls"


def call_log_path(settings: Settings, call_id: str, *, ts: str | None = None) -> Path:
    stamp = ts or datetime.now(UTC).isoformat()
    day = stamp[:10].replace("-", "")
    if len(day) != 8 or not day.isdigit():
        day = datetime.now(UTC).strftime("%Y%m%d")
    return call_log_dir(settings) / day / f"{_safe_call_id(call_id)}.json"


def list_call_logs(settings: Settings | Path) -> list[Path]:
    root = call_log_dir(settings) if isinstance(settings, Settings) else Path(settings) / "calls"
    if not root.exists():
        return []
    return sorted(root.rglob("*.json"))


def emit_pipeline_step(call_log: CallLog, kind: str, result: Any, payload: dict[str, Any]) -> None:
    call_log.add_step(kind, serialize_stage(kind, result), extra=payload)


def _safe_call_id(call_id: str) -> str:
    cleaned = "".join(ch if ch.isalnum() or ch in "-_." else "_" for ch in call_id)
    return cleaned or "call"
