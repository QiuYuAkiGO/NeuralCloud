"""Known-card sensor recovery using synthetic desktops; never GUI or network."""
from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from harness.qq import watcher as module
from harness.qq.desktop import DesktopError
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
                group_title=NS(display_name="Synthetic group"),
                opened_profile=NS(kind="member", member_id="30001", uncertainties=[],
                                  verified_self_indicator="own_space_link"))
    desktop._group = NS(group_id="10001", display_name="Synthetic group")
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

    def dismiss(frame, screen):
        assert frame is desktop.current and desktop.active_deadline is not None
        actions.append("dismiss")
        clock[0] += state.verify_delay
        desktop.active_deadline.check()
        return frame, NS(scene="group_chat", blocked=False, opened_profile=None,
                         group_title=NS(display_name="Synthetic group"))

    desktop.turn, desktop.capture, desktop.inspect, desktop._dismiss = turn, capture, inspect, dismiss
    desktop.invalidate_identity_cache = lambda: None
    desktop._require_chat = lambda frame, reading: None
    if pending:
        watcher._observer_baselined = True
        watcher._pending_turn.deadline = QQDeadline(clock[0] + 20, clock=lambda: clock[0])
    return state


@pytest.mark.parametrize("reason", ["startup", "attribution", "precheck", "pending", "superseded", "natural_wait", "idle"])
def test_known_profile_gap_recovers_in_startup_and_actor_failure_contexts(setup, monkeypatch, reason):
    state = gap_case(setup, monkeypatch, pending=reason == "pending")
    watcher = state.watcher
    if reason != "startup":
        watcher._observer_baselined = True
    if reason == "attribution":
        watcher._attribution_retry = True
    if reason == "precheck":
        watcher.set_status(last_turn_outcome="failed", last_error_type="synthetic_precheck_failure")
    if reason in {"superseded", "natural_wait", "idle"}:
        watcher.set_status(state="watching", last_turn_outcome=None if reason == "idle" else reason)
        assert watcher._pending_turn is None and not watcher._attribution_retry
    original = watcher._pending_turn.deadline if watcher._pending_turn else None
    watcher.cycle()
    assert state.actions == ["capture", "inspect", "dismiss"]
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
    state.watcher._observer_baselined = True
    state.watcher.set_status(last_turn_outcome="superseded")
    state.sensor.gap.update(changes)
    state.watcher.cycle()
    assert not state.actions and state.watcher._observer_gap_recovery_attempts == 0


@pytest.mark.parametrize("scene,blocked,uncertain", [
    ("other", False, False), ("login", False, False), ("uncertain", False, False),
    ("member_profile", True, False), ("member_profile", False, True), ("group_chat", False, False),
])
def test_only_positive_profile_classification_permits_dismissal(setup, monkeypatch, scene, blocked, uncertain):
    state = gap_case(setup, monkeypatch)
    state.watcher._observer_baselined = True
    state.watcher.set_status(last_turn_outcome="natural_wait")
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
    state.watcher._observer_baselined = True
    state.watcher.set_status(last_turn_outcome="superseded")
    state.watcher.cycle()
    state.clock[0] += 2.99
    state.watcher.cycle()
    assert state.actions.count("dismiss") == 1
    state.clock[0] += .01
    state.watcher.cycle()
    assert state.actions.count("dismiss") == 2
    state.clock[0] += 3
    # Sensor can observe short actor-created transitions; no fresh usable snapshot
    # means those transitions must not renew the recovery budget.
    state.sensor.gap["gap_started_at"] = (NOW + timedelta(seconds=2)).isoformat()
    state.watcher.cycle()
    assert state.actions.count("dismiss") == 2
    assert state.watcher.status["state"] == "observer_recovery_exhausted"


def test_idle_recovery_control_pause_after_inspection_prevents_card_action(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    state.watcher._observer_baselined = True
    state.watcher.set_status(last_turn_outcome="superseded")
    control_checks = []

    def control():
        control_checks.append(True)
        if len(control_checks) == 3:
            raise InterruptedError("synthetic operator pause")

    monkeypatch.setattr(state.watcher, "check_control", control)
    with pytest.raises(InterruptedError):
        state.watcher.cycle()
    assert state.actions == ["capture", "inspect"]
    assert not state.receipts and state.desktop.clicks == 0
    assert state.watcher._pending_turn is None


def test_idle_recovery_rejects_wrong_target_group_without_deciding_or_sending(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    state.watcher._observer_baselined = True
    state.watcher.set_status(last_turn_outcome="natural_wait")
    state.screen.scene = "group_profile"
    state.screen.opened_profile = NS(kind="group", group_id="10002", display_name="Synthetic group", uncertainties=[])
    state.watcher.cycle()
    assert state.watcher.status["state"] == "observer_gap_recovery_failed"
    assert state.watcher.status["last_error_type"] == "observer_recovery_profile_scope_mismatch"
    assert state.actions == ["capture", "inspect"]
    assert state.watcher._pending_turn is None and not state.receipts and state.desktop.clicks == 0


def test_idle_self_preparation_failure_can_recover_after_one_transient_gate_busy(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    watcher = state.watcher
    watcher._observer_baselined = True
    watcher.set_status(last_turn_outcome="superseded", preparation={
        "phase": "self_verification", "outcome": "error", "last_error_type": "observation_gate_busy"})
    assert watcher._pending_turn is None and not watcher._attribution_retry
    dismiss = state.desktop._dismiss
    attempts = []

    def transient_dismiss(frame, screen):
        attempts.append(True)
        if len(attempts) == 1:
            raise DesktopError("observation_gate_busy")
        return dismiss(frame, screen)

    monkeypatch.setattr(state.desktop, "_dismiss", transient_dismiss)
    watcher.cycle()
    assert watcher.status["state"] == "observer_gap_recovery_failed"
    assert watcher.status["last_error_type"] == "observation_gate_busy"
    assert len(attempts) == 1 and state.budgets == [15]
    state.clock[0] += 2.99
    watcher.cycle()
    assert len(attempts) == 1
    state.clock[0] += .01
    watcher.cycle()
    assert len(attempts) == 2 and state.budgets == [15, 15]
    assert watcher.status["state"] == "waiting_for_observer" and watcher.status["last_error_type"] is None
    assert watcher.status["last_turn_outcome"] == "superseded" and watcher._pending_turn is None
    assert watcher._observer_gap_recovery_attempts == 2
    state.clock[0] += 3
    watcher.cycle()
    assert watcher.status["state"] == "observer_recovery_exhausted" and len(attempts) == 2
    assert not state.calls and not state.receipts and state.desktop.clicks == 0


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
