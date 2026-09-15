"""Evidence accumulation and confidence updates (01 §5)."""

from __future__ import annotations

from harness.config import Settings, get_settings


def update_confidence(
    current: float,
    *,
    n_new_evidence: int = 1,
    increment: float | None = None,
    settings: Settings | None = None,
) -> float:
    settings = settings or get_settings()
    step = increment if increment is not None else settings.confidence_increment
    value = current
    for _ in range(max(0, n_new_evidence)):
        value = value + step * (1.0 - value)
    return min(1.0, max(0.0, value))


def should_consolidate(confidence: float, settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    return confidence >= settings.consolidation_threshold
