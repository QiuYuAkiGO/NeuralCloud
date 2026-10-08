"""Preserve interpretation maturity without promoting it to verified fact."""
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal


InterpretationStatus = Literal["provisional", "stable_interpretation", "disputed"]
_INTERPRETATION_AUTHORITIES = frozenset({
    "misty_tentative_interpretation", "daily_evidence_bound_interpretation", "source_anchored_interpretation",
})


def interpretation_status(*states: str | None, has_counterevidence: bool = False) -> InterpretationStatus:
    """Only explicit maturity establishes stability; active disputes win."""
    if any(state in {"disputed", "contested"} for state in states):
        return "disputed"
    if any(state in {"stable", "stable_interpretation"} for state in states):
        # The writer can regain stability while retaining historical counters.
        # Keep those counters in the payload; do not undo its explicit maturity.
        return "stable_interpretation"
    if has_counterevidence:
        return "disputed"
    return "provisional"


def derived_evidence_status(entry: Mapping) -> str:
    status = entry.get("status")
    if (entry.get("authority") in _INTERPRETATION_AUTHORITIES
            or entry.get("kind") in {"person", "relationship", "impression"}
            or status in {"provisional", "stable_interpretation", "disputed"}):
        return interpretation_status(status, has_counterevidence=bool(entry.get("counterevidence")))
    return "retained_fact"
