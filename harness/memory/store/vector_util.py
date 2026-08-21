"""Vector helpers used by both in-memory and postgres pool ranking."""

from __future__ import annotations

from datetime import datetime

import numpy as np

from harness.core.models import EpisodicEvent


def cosine(a: list[float] | None, b: list[float] | None) -> float:
    if not a or not b:
        return 0.0
    va = np.asarray(a, dtype=np.float64)
    vb = np.asarray(b, dtype=np.float64)
    denom = float(np.linalg.norm(va) * np.linalg.norm(vb))
    if denom == 0:
        return 0.0
    return float(np.dot(va, vb) / denom)


def event_in_filters(
    event: EpisodicEvent,
    *,
    since: datetime | None,
    until: datetime | None,
    channels: list[str] | None,
    entities: list[str] | None,
) -> bool:
    if since is not None and event.time < since:
        return False
    if until is not None and event.time > until:
        return False
    if channels and event.channel not in channels:
        return False
    if entities:
        lowered = {item.lower() for item in event.participants}
        if not any(entity.lower() in lowered for entity in entities):
            return False
    return True
