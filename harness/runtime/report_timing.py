"""Request-local report timings, with fixed labels and no report content."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
import inspect
from threading import Lock
from time import perf_counter


_LABELS = frozenset({
    "report", "archive", "monitor_wait", "monitor", "samples", "runs", "events",
    "resource_rows", "actions", "homeostasis", "revision", "qq_tokens",
    "qq_idle", "resources", "operations", "goal_budget", "assembly",
    "cognitive", "insights", "timeline", "failures", "run_rows",
    "run_health", "encode", "render",
})
_CURRENT: ContextVar[ReportTiming | None] = ContextVar("daily_report_timing", default=None)


class ReportTiming:
    """Inclusive wall time per stage; nested stages must not be added together."""

    def __init__(self):
        self._stages: dict[str, dict[str, float | int]] = {}
        self._lock = Lock()
        self.total_ms = 0.0

    def record(self, label, seconds):
        if label not in _LABELS:
            raise ValueError("unknown_report_timing_label")
        with self._lock:
            row = self._stages.setdefault(label, {"ms": 0.0, "calls": 0})
            row["ms"] += max(0.0, seconds * 1000)
            row["calls"] += 1

    def snapshot(self):
        with self._lock:
            return {
                "total_ms": round(self.total_ms, 3),
                "stages": {name: {"ms": round(row["ms"], 3), "calls": row["calls"]}
                           for name, row in sorted(self._stages.items())},
                "basis": "inclusive_wall_time",
            }

    def header(self):
        value = self.snapshot()
        return ", ".join([
            f'report_total;dur={value["total_ms"]:.3f}',
            *(f'{name};dur={row["ms"]:.3f}' for name, row in value["stages"].items()),
        ])


@contextmanager
def capture_report_timing():
    timing = ReportTiming()
    token = _CURRENT.set(timing)
    started = perf_counter()
    try:
        yield timing
    finally:
        timing.total_ms = (perf_counter() - started) * 1000
        _CURRENT.reset(token)


@contextmanager
def report_phase(label):
    timing = _CURRENT.get()
    if timing is None:
        yield
        return
    if label not in _LABELS:
        raise ValueError("unknown_report_timing_label")
    started = perf_counter()
    try:
        yield
    finally:
        timing.record(label, perf_counter() - started)


def report_timed(label):
    if label not in _LABELS:
        raise ValueError("unknown_report_timing_label")

    def decorate(function):
        if inspect.iscoroutinefunction(function):
            @wraps(function)
            async def asynchronous(*args, **kwargs):
                with report_phase(label):
                    return await function(*args, **kwargs)
            return asynchronous

        @wraps(function)
        def synchronous(*args, **kwargs):
            with report_phase(label):
                return function(*args, **kwargs)
        return synchronous
    return decorate
