"""Known-card sensor recovery using synthetic desktops; never GUI or network."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from harness.qq import watcher as module
from harness.qq.telemetry import QQDeadline
from tests.test_qq_watcher import NOW, setup as setup
from tests.test_qq_watcher_cleanup import pending_retry


def gap_case(setup, monkeypatch, *, pending=False):
    settings, desktop, decide, calls, receipts = setup
    if pending:
        watcher, desktop, calls, receipts = pending_retry(setup)
        desktop._owned_draft = None
    else:
        watcher = module.QQWatcher(settings, desktop=desktop, decide=decide)
    clock = [1000.0]
    monkeypatch.setattr(module, "time", NS(monotonic=lambda: clock[0]))
    monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=3 + clock[0] - 1000))
    gap = {"running": True, "last_error_type": None, "gap_started_at": NOW.isoformat(),
           "gap_seconds": 10, "evidence_in_progress": False, "pending_digest": None}
    sensor = NS(snapshot=None, gap=gap)
    watcher.observer = NS(latest=lambda: sensor.snapshot, status=lambda: dict(sensor.gap),
                          changes_after=lambda _: NS(observations=[], overflowed=False))
    actions, budgets = [], []
    desktop.active_deadline = None
    screen = NS(scene="member_profile", blocked=False, uncertainties=[],
                opened_profile=NS(kind="member", uncertainties=[]))
    state = NS(watcher=watcher, desktop=desktop, sensor=sensor, clock=clock, actions=actions,
               budgets=budgets, screen=screen, calls=calls, receipts=receipts, verify_delay=0)

    @contextmanager
    def turn(*, deadline=None, trace=None):
        previous = desktop.active_deadline
        desktop.active_deadline = deadline
        try:
            yield
        finally:
            desktop.active_deadline = previous

    original_capture = desktop.capture

    def capture():
        actions.append("capture")
        budgets.append(desktop.active_deadline.check())
        return original_capture()

    def inspect(frame):
        assert frame is desktop.current
        actions.append("inspect")
        return state.screen

    def verify_group(*, frame):
        assert frame is desktop.current and desktop.active_deadline is not None
        actions.append("verify_group")
        clock[0] += state.verify_delay
        desktop.active_deadline.check()
        return NS(group_id="10001", chat_frame=frame)

    desktop.turn, desktop.capture, desktop.inspect, desktop.verify_group = turn, capture, inspect, verify_group
    if pending:
        watcher._observer_baselined = True
        watcher._pending_turn.deadline = QQDeadline(clock[0] + 20, clock=lambda: clock[0])
    return state


@pytest.mark.parametrize("reason", ["startup", "attribution", "precheck", "pending"])
def test_known_profile_gap_recovers_in_startup_and_actor_failure_contexts(setup, monkeypatch, reason):
    state = gap_case(setup, monkeypatch, pending=reason == "pending")
    watcher = state.watcher
    if reason != "startup":
        watcher._observer_baselined = True
    if reason == "attribution":
        watcher._attribution_retry = True
    if reason == "precheck":
        watcher.set_status(last_turn_outcome="failed", last_error_type="synthetic_precheck_failure")
    original = watcher._pending_turn.deadline if watcher._pending_turn else None
    watcher.cycle()
    assert state.actions == ["capture", "inspect", "verify_group"]
    assert state.budgets == [15]
    assert watcher.status["captures"] >= 1 and watcher.status["automatic_send"] is False
    assert state.desktop.active_deadline is None
    if original:
        assert watcher._pending_turn.deadline is original
    assert not state.receipts and state.desktop.clicks == 0


@pytest.mark.parametrize("changes", [
    {"last_error_type": "request_timeout"}, {"last_error_type": "accessibility_unavailable"},
    {"evidence_in_progress": True}, {"pending_digest": "synthetic-new-message"},
    {"gap_started_at": None}, {"running": False},
])
def test_transport_failure_and_evidence_pending_never_trigger_ui_recovery(setup, monkeypatch, changes):
    state = gap_case(setup, monkeypatch)
    state.sensor.gap.update(changes)
    state.watcher.cycle()
    assert not state.actions and state.watcher._observer_gap_recovery_attempts == 0


@pytest.mark.parametrize("scene,blocked,uncertain", [
    ("other", False, False), ("login", False, False), ("uncertain", False, False),
    ("member_profile", True, False), ("member_profile", False, True), ("group_chat", False, False),
])
def test_only_positive_profile_classification_permits_verify_group(setup, monkeypatch, scene, blocked, uncertain):
    state = gap_case(setup, monkeypatch)
    state.screen.scene, state.screen.blocked = scene, blocked
    if scene == "group_chat":
        state.screen.opened_profile = None
    if uncertain:
        state.screen.opened_profile.uncertainties = ["synthetic uncertain card"]
    state.watcher.cycle()
    assert state.actions == ["capture", "inspect"]
    assert not state.receipts and state.desktop.clicks == 0


def test_two_attempt_limit_and_three_second_resampling_wait(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    state.watcher.cycle()
    state.clock[0] += 2.99
    state.watcher.cycle()
    assert state.actions.count("verify_group") == 1
    state.clock[0] += .01
    state.watcher.cycle()
    assert state.actions.count("verify_group") == 2
    state.clock[0] += 3
    # Sensor can observe short actor-created transitions; no fresh usable snapshot
    # means those transitions must not renew the recovery budget.
    state.sensor.gap["gap_started_at"] = (NOW + timedelta(seconds=2)).isoformat()
    state.watcher.cycle()
    assert state.actions.count("verify_group") == 2
    assert state.watcher.status["state"] == "observer_recovery_exhausted"


def test_success_waits_even_if_sensor_publishes_chat_immediately(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    state.watcher.cycle()
    state.sensor.snapshot = NS(sequence=0)
    state.sensor.gap["gap_started_at"] = None
    state.watcher._observer_baselined = True
    state.watcher.cycle()
    assert state.actions.count("capture") == 1
    state.clock[0] += 3
    state.watcher.cycle()
    assert state.watcher._observer_gap_recovery_attempts == 0
    assert state.watcher.status["state"] == "watching"


def test_pending_recovery_cannot_extend_candidate_deadline(setup, monkeypatch):
    state = gap_case(setup, monkeypatch, pending=True)
    turn = state.watcher._pending_turn
    turn.deadline = QQDeadline(state.clock[0] + 4, clock=lambda: state.clock[0])
    original = turn.deadline
    state.verify_delay = 4
    state.watcher.cycle()
    assert state.budgets == [4]
    assert original.expired and turn.deadline is original
    assert state.watcher._pending_turn is None
    assert state.watcher.status["last_turn_outcome"] == "failed"
    assert state.desktop.clicks == 0 and not state.receipts


def test_missing_sensor_snapshot_does_not_keep_expired_pending_alive(setup, monkeypatch):
    state = gap_case(setup, monkeypatch, pending=True)
    turn = state.watcher._pending_turn
    state.clock[0] += 70
    state.sensor.gap["last_error_type"] = "request_timeout"
    state.watcher.cycle()
    assert state.watcher._pending_turn is None and turn.deadline.expired
    assert state.watcher.ledger.get_send(turn.reservation_id).status == "expired"
    assert not state.actions and not state.receipts and state.desktop.clicks == 0
