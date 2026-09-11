"""QQ-only monotonic deadlines and bounded, content-free turn telemetry.

Wall time identifies observations; elapsed durations and expiry use monotonic
time. JSONL contains identifiers and measurements, never prompts or replies.
"""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import re
import threading
import time
from typing import Callable, Iterator, Literal
from uuid import uuid4

import httpx


TraceOutcome = Literal["ok", "error", "expired", "cancelled", "skipped", "late_result"]
_OUTCOMES = {"ok", "error", "expired", "cancelled", "skipped", "late_result"}
_ERRORS = {"deadline_expired", "model_timeout", "model_http", "model_invalid", "model_error",
           "storage_error", "cancelled", "late_result", "internal_error"}
_NAME = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9:_.-]{0,159}$")
_FLUSH_LOCK = threading.Lock()


class QQDeadlineExpired(TimeoutError):
    """A QQ turn no longer has time to produce an active reply intention."""


def _finite(value: float, *, nonnegative: bool = True) -> float:
    if isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value):
        raise ValueError("A finite timing value is required.")
    if nonnegative and value < 0:
        raise ValueError("A nonnegative timing value is required.")
    return float(value)


def _aware(value: datetime) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("An aware observation timestamp is required.")
    return value.astimezone(timezone.utc)


@dataclass(frozen=True)
class QQDeadline:
    deadline_mono: float
    clock: Callable[[], float] = time.monotonic

    def __post_init__(self):
        _finite(self.deadline_mono)

    @classmethod
    def after(cls, seconds: float, *, clock: Callable[[], float] = time.monotonic) -> QQDeadline:
        return cls(_finite(clock()) + _finite(seconds), clock)

    @classmethod
    def from_first_seen(
        cls, first_seen_at: datetime, seconds: float = 60, *,
        now_at: datetime | None = None, clock: Callable[[], float] = time.monotonic,
    ) -> QQDeadline:
        # Convert wall age once. Later NTP/local clock changes cannot renew it.
        age = max(0.0, (_aware(now_at or datetime.now(timezone.utc)) - _aware(first_seen_at)).total_seconds())
        return cls.after(max(0.0, _finite(seconds) - age), clock=clock)

    def remaining(self) -> float:
        return max(0.0, self.deadline_mono - _finite(self.clock()))

    @property
    def expired(self) -> bool:
        return self.remaining() <= 0

    def check(self) -> float:
        remaining = self.remaining()
        if remaining <= 0:
            raise QQDeadlineExpired("The QQ turn deadline has expired.")
        return remaining


def classify_error(error: BaseException) -> str:
    """Return a fixed category, never exception text, HTTP bodies or URLs."""
    if isinstance(error, QQDeadlineExpired):
        return "deadline_expired"
    if isinstance(error, (httpx.TimeoutException, TimeoutError)):
        return "model_timeout"
    if isinstance(error, httpx.HTTPError):
        return "model_http"
    if isinstance(error, (ValueError, TypeError)):
        return "model_invalid"
    return "model_error"


class QQTurnTrace:
    """A bounded in-memory trace; the caller chooses a private JSONL path.

    Repeated flushes are snapshots of one trace_id; report consumers should use
    the last snapshot per ID. Calls include failed attempts, not only responses.
    """

    def __init__(
        self, *, event_id: str | None = None, decision_id: str | None = None,
        first_seen_at: datetime | None = None, first_seen_mono: float | None = None,
        observation_sequence: int | None = None,
        clock: Callable[[], float] = time.monotonic, max_events: int = 256,
    ):
        if isinstance(max_events, bool) or not isinstance(max_events, int) or not 1 <= max_events <= 1024:
            raise ValueError("The trace event limit must be between 1 and 1024.")
        self.clock = clock
        self.first_seen_mono = _finite(clock() if first_seen_mono is None else first_seen_mono)
        if self.first_seen_mono > _finite(clock()):
            raise ValueError("First-seen monotonic time must not be in the future.")
        self.first_seen_at = _aware(first_seen_at or datetime.now(timezone.utc))
        self.trace_id = f"qq-trace:{uuid4().hex}"
        self.event_id = self._identifier(event_id)
        self.decision_id = self._identifier(decision_id)
        if observation_sequence is not None and (type(observation_sequence) is not int or observation_sequence < 0):
            raise ValueError("Observation sequence must be a nonnegative integer.")
        self.observation_sequence = observation_sequence
        self.max_events = max_events
        self._events: list[dict] = []
        self._counters: dict[str, int] = {}
        self._preparation_counters: dict[str, dict[str, int]] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _identifier(value: str | None) -> str | None:
        if value is not None and (not isinstance(value, str) or not _IDENTIFIER.fullmatch(value)):
            raise ValueError("Trace identifiers must be bounded opaque identifiers.")
        return value

    @staticmethod
    def _name(value: str) -> str:
        if not isinstance(value, str) or not _NAME.fullmatch(value):
            raise ValueError("Trace names must be bounded machine labels.")
        return value

    def bind_decision(self, decision_id: str) -> None:
        value = self._identifier(decision_id)
        with self._lock:
            if self.decision_id is not None and self.decision_id != value:
                raise ValueError("A turn trace cannot change decision identity.")
            self.decision_id = value

    def add_counter(self, name: str, amount: int = 1) -> None:
        self._name(name)
        if isinstance(amount, bool) or not isinstance(amount, int) or amount < 0:
            raise ValueError("Trace counter increments must be nonnegative integers.")
        with self._lock:
            if name not in self._counters and len(self._counters) >= 128:
                raise ValueError("Too many distinct trace counters.")
            self._counters[name] = self._counters.get(name, 0) + amount

    def merge_preparation(self, source: QQTurnTrace) -> bool:
        """Import one shared preparation, retaining its identity and clock origin.

        Event-local totals include the work. Physical-work consumers subtract
        preparation_counters from those totals and count each source ID once.
        """
        if not isinstance(source, QQTurnTrace) or source is self:
            raise ValueError("A distinct local preparation trace is required.")
        value = source.snapshot()
        if (value["event_id"] is not None or value["decision_id"] is not None
                or value.get("preparation_counters") or any(name.startswith("turn_") for name in value["counters"])):
            raise ValueError("Only an unbound preparation trace can be imported.")
        source_id = value["trace_id"]
        shift = (value["first_seen_mono"] - self.first_seen_mono) * 1000
        elapsed = (self.clock() - self.first_seen_mono) * 1000
        stages = []
        for index, stage in enumerate(value["stages"]):
            start, end = round(stage["start_offset_ms"] + shift, 3), round(stage["end_offset_ms"] + shift, 3)
            if start < 0 or end > elapsed + 1 or end < start:
                raise ValueError("Preparation work must lie inside the event's monotonic interval.")
            stages.append(dict(stage, start_offset_ms=start, end_offset_ms=end,
                source_preparation_id=source_id, source_stage_index=index,
                source_start_offset_ms=stage["start_offset_ms"], source_end_offset_ms=stage["end_offset_ms"]))
        with self._lock:
            if source_id in self._preparation_counters:
                return False
            if len(self._preparation_counters) >= 128 or len(set(self._counters) | set(value["counters"])) > 128:
                raise ValueError("Too many preparation measurements in this event.")
            self._preparation_counters[source_id] = dict(value["counters"])
            for name, count in value["counters"].items():
                self._counters[name] = self._counters.get(name, 0) + count
            available = self.max_events - len(self._events)
            self._events.extend(stages[:available])
            if len(stages) > available:
                self._counters["events_dropped"] = self._counters.get("events_dropped", 0) + len(stages) - available
            return True

    def record(
        self, name: str, *, outcome: TraceOutcome = "ok", error_type: str | None = None,
        started_mono: float | None = None,
    ) -> None:
        self._name(name)
        if outcome not in _OUTCOMES or (error_type is not None and error_type not in _ERRORS):
            raise ValueError("Trace outcomes and errors must use fixed categories.")
        ended = _finite(self.clock())
        started = ended if started_mono is None else _finite(started_mono)
        if started > ended or started < self.first_seen_mono:
            raise ValueError("A trace stage must fall inside its monotonic turn interval.")
        event = {"name": name, "outcome": outcome, "error_type": error_type,
                 "start_offset_ms": round((started - self.first_seen_mono) * 1000, 3),
                 "end_offset_ms": round((ended - self.first_seen_mono) * 1000, 3),
                 "duration_ms": round((ended - started) * 1000, 3)}
        with self._lock:
            if len(self._events) >= self.max_events:
                self._counters["events_dropped"] = self._counters.get("events_dropped", 0) + 1
            else:
                self._events.append(event)

    @contextmanager
    def stage(self, name: str) -> Iterator[None]:
        self._name(name)
        started = _finite(self.clock())
        try:
            yield
        except BaseException as exc:
            self.record(name, started_mono=started,
                        outcome="expired" if isinstance(exc, QQDeadlineExpired) else "error",
                        error_type=classify_error(exc))
            raise
        else:
            self.record(name, started_mono=started)

    def snapshot(self) -> dict:
        with self._lock:
            value = {"schema_version": "qq-turn-trace-v1", "trace_id": self.trace_id,
                    "event_id": self.event_id, "decision_id": self.decision_id,
                    "first_seen_at": self.first_seen_at.isoformat(), "first_seen_mono": self.first_seen_mono,
                    "recorded_at": datetime.now(timezone.utc).isoformat(),
                    "elapsed_ms": round(max(0.0, self.clock() - self.first_seen_mono) * 1000, 3),
                    "stages": [dict(event) for event in self._events], "counters": dict(self._counters)}
            if self._preparation_counters:
                value["preparation_counters"] = {key: dict(counters) for key, counters in self._preparation_counters.items()}
            if self.observation_sequence is not None:
                value["observation_sequence"] = self.observation_sequence
            return value

    def flush(self, path: Path | str, *, durable: bool = False) -> None:
        """Append one bounded JSONL snapshot; no implicit directory creation."""
        path = Path(path)
        if not path.is_absolute() or not path.parent.is_dir() or path.is_symlink():
            raise ValueError("Choose a file in an existing private absolute directory.")
        payload = (json.dumps(self.snapshot(), ensure_ascii=True, separators=(",", ":")) + "\n").encode("utf-8")
        with _FLUSH_LOCK:
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                with os.fdopen(descriptor, "ab") as stream:
                    stream.write(payload)
                    stream.flush()
                    if durable:
                        os.fsync(stream.fileno())
            except BaseException:
                # fdopen owns the descriptor, including exceptional exits.
                raise
