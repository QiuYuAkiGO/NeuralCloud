"""Persistent evidence-trigger coalescing and budgets; never goal selection."""

from __future__ import annotations

from datetime import datetime

from harness.agency.models import AgencyConfig, GoalTrigger
from harness.agency.store import GoalStore
from harness.research.grounded_agency.hashes import content_hash


class GoalScheduler:
    def __init__(self, store: GoalStore, config: AgencyConfig) -> None:
        self.store, self.config = store, config

    def _state(self):
        return self.store.get("scheduler", "state") or {"pending": {}, "seen": [], "calls": [], "paused": False}

    def enqueue(self, trigger: GoalTrigger) -> bool:
        if not self.config.enabled or trigger.kind in {"body_tick", "static_observation"}:
            return False
        if trigger.kind == "opaque_change" and trigger.change_symbol not in self.config.significant_symbol_changes:
            return False
        state = self._state()
        # Timestamp is deliberately excluded: the same observed state must not
        # become a new model call merely because it was polled again.
        key = content_hash([trigger.kind, trigger.goal_id, trigger.signature, sorted(trigger.evidence_refs)])
        if key in state["seen"] or key in state["pending"]:
            return False
        if len(state["pending"]) >= 256:
            raise ValueError("bounded pending agency trigger capacity exceeded")
        state["pending"][key] = trigger.model_dump(mode="json")
        self.store.put("scheduler", "state", state)
        return True

    def next_due(self, now: datetime) -> tuple[GoalTrigger, ...]:
        state = self._state()
        timestamp = now.timestamp()
        calls = [value for value in state["calls"] if timestamp - value < 3600]
        if not self.config.enabled or state["paused"] or not state["pending"]:
            return ()
        if calls and timestamp - calls[-1] < self.config.minimum_reconsideration_interval:
            return ()
        if len(calls) >= self.config.maximum_model_calls_per_hour:
            return ()
        ready = {key: raw for key, raw in state["pending"].items()
                 if GoalTrigger.model_validate(raw).observed_at <= now}
        if not ready:
            return ()
        state["seen"] = [*state["seen"], *ready][-4096:]
        state["pending"] = {key: raw for key, raw in state["pending"].items() if key not in ready}
        state["calls"] = [*calls, timestamp]
        self.store.put("scheduler", "state", state)
        return tuple(GoalTrigger.model_validate(raw) for raw in ready.values())

    def set_paused(self, paused: bool) -> None:
        state = self._state()
        state["paused"] = paused
        self.store.put("scheduler", "state", state)

    @property
    def pending_count(self) -> int:
        return len(self._state()["pending"])
