"""A turn's immutable model deadline, propagated through worker context."""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime, timezone
import time


_model_deadline: ContextVar[float | None] = ContextVar("model_deadline", default=None)


def remaining_model_seconds() -> float | None:
    deadline = _model_deadline.get()
    if deadline is None:
        return None
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("model_turn_deadline_exceeded")
    return remaining


@contextmanager
def model_deadline(deadline_at: datetime | None):
    if deadline_at is None:
        yield
        return
    if deadline_at.tzinfo is None:
        raise ValueError("model_deadline_requires_timezone")
    end = time.monotonic() + (deadline_at - datetime.now(timezone.utc)).total_seconds()
    previous = _model_deadline.get()
    token = _model_deadline.set(min(end, previous) if previous is not None else end)
    try:
        remaining_model_seconds()
        yield
        remaining_model_seconds()
    finally:
        _model_deadline.reset(token)


def call_before_deadline(deadline_at, function, *args, **kwargs):
    with model_deadline(deadline_at):
        return function(*args, **kwargs)
