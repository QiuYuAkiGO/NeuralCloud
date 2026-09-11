"""Cache invalidation from observation gaps; no real GUI, model or services."""
from collections import deque
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from harness.qq.watcher import QQWatcher
from tests.test_qq_watcher import NOW, setup as setup  # noqa: F401 - scenario dependency
from tests.test_qq_watcher_unattended import scenario as scenario  # noqa: F401 - fixture


def bare_watcher(enabled=True):
    invalidations = []
    watcher = QQWatcher.__new__(QQWatcher)
    watcher.settings = NS(identity_cache_enabled=enabled)
    watcher.desktop = NS(invalidate_identity_cache=lambda: invalidations.append(True))
    watcher._identity_prepared = True
    watcher._self_aliases = ["synthetic-verified-self"]
    watcher._identity_cache_invalidated_gaps = deque(maxlen=64)
    return watcher, invalidations


def open_gap(duration=10, *, sequence=0, start=NOW):
    return {"gap_seconds": duration, "gap_started_at": start.isoformat(),
            "gap_sequence": sequence, "recent_gaps": []}


def closed_gap(duration=10, *, sequence=1, start=NOW):
    return {"gap_seconds": 0, "gap_started_at": None, "gap_sequence": sequence,
            "recent_gaps": [{"started_at": start.isoformat(), "sequence": sequence,
                             "duration_seconds": duration}]}


def test_current_gap_crosses_threshold_once_and_closure_keeps_same_key():
    watcher, invalidations = bare_watcher()
    for seconds in (0, 9.999):
        watcher._invalidate_identity_after_observer_gap(open_gap(seconds))
    assert not invalidations and watcher._identity_prepared
    watcher._invalidate_identity_after_observer_gap(open_gap(10))
    assert invalidations == [True]
    assert watcher._identity_prepared is False and watcher._self_aliases == []
    for state in (open_gap(11), open_gap(20), closed_gap(20)):
        watcher._invalidate_identity_after_observer_gap(state)
    assert invalidations == [True]
    # ISO spelling changes are not a new gap.
    state = closed_gap(20)
    state["recent_gaps"][0]["started_at"] = NOW.isoformat().replace("+00:00", "Z")
    watcher._invalidate_identity_after_observer_gap(state)
    assert invalidations == [True]


def test_each_distinct_long_gap_invalidates_once_and_storage_is_bounded():
    watcher, invalidations = bare_watcher()
    for sequence in range(1, 71):
        state = closed_gap(sequence=sequence, start=NOW + timedelta(seconds=sequence * 20))
        watcher._invalidate_identity_after_observer_gap(state)
        watcher._invalidate_identity_after_observer_gap(state)
    assert len(invalidations) == 70
    assert len(watcher._identity_cache_invalidated_gaps) == 64


@pytest.mark.parametrize("status", [
    None, [], {}, {"recent_gaps": None}, {"recent_gaps": [None, []]},
    {"gap_started_at": NOW.isoformat(), "gap_seconds": 15},
    {"gap_started_at": "invalid", "gap_sequence": 0, "gap_seconds": 15},
    {"gap_started_at": "2026-09-11T00:00:00", "gap_sequence": 0, "gap_seconds": 15},
    {"gap_started_at": NOW.isoformat(), "gap_sequence": False, "gap_seconds": 15},
    open_gap(float("nan")), open_gap(float("inf")), open_gap(True), open_gap("15"),
    open_gap(-1), closed_gap(9.999),
])
def test_malformed_or_short_gap_neither_throws_nor_changes_identity(status):
    watcher, invalidations = bare_watcher()
    watcher._invalidate_identity_after_observer_gap(status)
    assert not invalidations
    assert watcher._identity_prepared is True and watcher._self_aliases == ["synthetic-verified-self"]


def test_default_cache_disabled_does_not_call_desktop_or_touch_flags():
    watcher, invalidations = bare_watcher(enabled=False)
    watcher._invalidate_identity_after_observer_gap(open_gap(100))
    watcher._invalidate_identity_after_observer_gap(closed_gap(100))
    assert not invalidations and watcher._identity_prepared
    assert watcher._self_aliases == ["synthetic-verified-self"]


def test_cycle_invalidates_current_gap_before_returning_for_unavailable_observer(scenario):
    state = scenario
    state.watcher.settings = state.watcher.settings.model_copy(update={"identity_cache_enabled": True})
    invalidations = []
    state.desktop.invalidate_identity_cache = lambda: invalidations.append(True)
    state.watcher._identity_prepared = True
    state.watcher._self_aliases = ["synthetic-alias"]
    state.observer._mark_gap(state.clock.wall(), state.clock.mono(), "request_timeout")
    state.clock.advance(10)
    captures = state.desktop.captures
    state.watcher.cycle()
    assert invalidations == [True] and not state.watcher._identity_prepared
    assert state.watcher._self_aliases == []
    assert state.desktop.captures == captures and not state.calls and not state.receipts


def test_completed_gap_during_model_wait_invalidates_without_claiming_new_context(scenario):
    state = scenario
    state.watcher.settings = state.watcher.settings.model_copy(update={"identity_cache_enabled": True})
    invalidations = []
    state.desktop.invalidate_identity_cache = lambda: invalidations.append(True)
    state.watcher._identity_prepared = True
    state.watcher._self_aliases = ["synthetic-alias"]

    def decide(_):
        state.observer._mark_gap(state.clock.wall(), state.clock.mono(), None)
        state.clock.advance(10)
        state.observer._close_gap(state.clock.wall(), state.clock.mono())
        return state.answer("wait")

    state.callback = decide
    arrival = state.append("synthetic new query")
    first_seen = arrival.first_seen_mono
    state.watcher.cycle()
    assert invalidations == [True]
    assert state.watcher._identity_prepared is False and state.watcher._self_aliases == []
    assert state.watcher._newer_visible_context() is False
    assert invalidations == [True]
    assert arrival.first_seen_mono == first_seen
    assert len(state.calls) == 1 and state.watcher.status["last_decision_action"] == "wait"
    assert not state.receipts and state.desktop.clicks == 0
