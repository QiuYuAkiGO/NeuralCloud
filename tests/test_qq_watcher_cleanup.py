"""Owned-draft recovery stays bounded without extending a send deadline."""
from contextlib import contextmanager
from datetime import timedelta
import json
from types import SimpleNamespace as NS

import pytest

from harness.qq import watcher as module
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired
from tests.test_qq_watcher import NOW, setup as setup


def pending_retry(setup):
    settings, desktop, decide, calls, receipts = setup
    desktop.result = "not_sent"
    actual = desktop.deliver

    def precheck(*args, **kwargs):
        result = actual(*args, **kwargs)
        desktop._owned_draft = (args[0], "10001", "synthetic-window")
        result.phase, result.retryable = "precheck", True
        return result

    desktop.deliver = precheck
    watcher = module.QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    watcher.cycle()
    assert watcher._pending_turn is not None
    return watcher, desktop, calls, receipts


def install_cleanup(desktop, monkeypatch, *, result=True, elapsed=0, raises=None):
    clock = [1000.0]
    calls = []
    desktop.active_deadline = None
    monkeypatch.setattr(module, "time", NS(monotonic=lambda: clock[0]))

    @contextmanager
    def turn(*, deadline=None, trace=None):
        previous = desktop.active_deadline
        desktop.active_deadline = deadline
        try:
            yield
        finally:
            desktop.active_deadline = previous

    def cleanup(text):
        deadline = desktop.active_deadline
        assert deadline is not None
        calls.append((deadline, deadline.remaining(), text))
        clock[0] += elapsed
        if raises:
            raise raises("private diagnostic must not enter status")
        deadline.check()
        if result:
            desktop._owned_draft = None
        return result

    desktop.turn = turn
    desktop.discard_owned_draft = cleanup
    return calls


@pytest.mark.parametrize("terminal", ["superseded", "expired", "precheck_failed", "callback_failed", "attempt_expired"])
def test_terminal_cleanup_uses_fresh_budget_and_preserves_send_deadline(setup, monkeypatch, terminal):
    watcher, desktop, calls, receipts = pending_retry(setup)
    pending = watcher._pending_turn
    original = pending.deadline
    cleanup_calls = install_cleanup(desktop, monkeypatch)
    if terminal == "superseded":
        assert watcher._supersede_pending() is True
    elif terminal == "expired":
        pending.deadline = QQDeadline(1, clock=lambda: 2)
        original = pending.deadline
        monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=65))
        watcher.cycle()
    elif terminal == "attempt_expired":
        monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=65))
        watcher._attempt_delivery(pending)
    else:
        monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=6))
        desktop.deliver = lambda *args, **kwargs: NS(
            status="not_sent" if terminal == "precheck_failed" else "unknown",
            phase="precheck", retryable=False, reason_code="synthetic_precheck_failure")
        watcher._attempt_delivery(pending)
    assert len(cleanup_calls) == 1
    cleanup_deadline, budget, _ = cleanup_calls[0]
    assert cleanup_deadline is not original and budget == pytest.approx(15)
    assert pending.deadline is original
    assert desktop.active_deadline is None and desktop._owned_draft is None
    assert watcher._pending_turn is None and not watcher.should_stop()
    assert watcher.status["state"] != "cleaning_owned_draft"
    assert len(calls) == 1 and desktop.clicks == 0 and not receipts
    assert watcher.ledger.next_allowed_at(priority="direct") is None


@pytest.mark.parametrize("failure", ["false", "timeout", "exception"])
@pytest.mark.parametrize("terminal", ["superseded", "expired", "precheck_failed"])
def test_cleanup_failure_pauses_persistently_without_sending_new_context(setup, monkeypatch, failure, terminal):
    watcher, desktop, calls, receipts = pending_retry(setup)
    cleanup_calls = install_cleanup(desktop, monkeypatch, result=failure != "false",
                                    elapsed=15 if failure == "timeout" else 0,
                                    raises=OSError if failure == "exception" else None)
    monkeypatch.setattr(watcher, "_newer_visible_context", lambda: terminal == "superseded")
    if terminal == "expired":
        watcher._pending_turn.deadline = QQDeadline(1, clock=lambda: 2)
        monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=65))
    elif terminal == "precheck_failed":
        monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=6))
        desktop.deliver = lambda *args, **kwargs: NS(
            status="not_sent", phase="precheck", retryable=False, reason_code="synthetic_precheck_failure")
    watcher.cycle()
    assert len(cleanup_calls) == 1
    assert watcher._pending_turn is None and watcher._carry_first_seen is None
    assert watcher.should_stop() and watcher.status["automatic_send"] is False
    assert watcher.status["last_error_type"] == "owned_draft_cleanup_required"
    assert watcher.status["state"] == "draft_cleanup_required"
    assert watcher.status["last_turn_outcome"] == "failed"
    assert json.loads(watcher.settings.control_file.read_text())["paused"] is True
    with pytest.raises(InterruptedError):
        watcher.cycle()
    restarted = module.QQWatcher(watcher.settings, desktop=desktop, decide=watcher.decide)
    assert restarted.should_stop()
    assert len(calls) == 1 and desktop.clicks == 0 and not receipts


def test_pause_write_failure_still_stops_in_process(setup, monkeypatch):
    watcher, desktop, _, _ = pending_retry(setup)
    install_cleanup(desktop, monkeypatch, result=False)
    monkeypatch.setattr(module, "atomic_json", lambda *_: (_ for _ in ()).throw(PermissionError("synthetic")))
    assert watcher._supersede_pending() is False
    assert watcher.should_stop() and watcher._heartbeat_failed
    assert watcher.status["last_error_type"] == "owned_draft_cleanup_required"
    with pytest.raises(InterruptedError):
        watcher.cycle()


def test_no_owned_draft_needs_no_cleanup_or_pause(setup, monkeypatch):
    watcher, desktop, _, _ = pending_retry(setup)
    desktop._owned_draft = None
    cleanup_calls = install_cleanup(desktop, monkeypatch, result=False)
    assert watcher._supersede_pending() is True
    assert not cleanup_calls and not watcher.should_stop()


def test_late_cleanup_success_is_not_accepted(setup, monkeypatch):
    watcher, desktop, _, _ = pending_retry(setup)
    clock = [1000.0]
    monkeypatch.setattr(module, "time", NS(monotonic=lambda: clock[0]))

    def late_success(_):
        clock[0] += 16
        return True

    desktop.discard_owned_draft = late_success
    assert watcher._supersede_pending() is False
    assert watcher.should_stop()
    lines = [json.loads(line) for line in watcher._trace_file.read_text().splitlines()]
    cleanup = next(stage for stage in lines[-1]["stages"] if stage["name"] == "owned_draft_cleanup")
    assert cleanup["outcome"] == "expired"


def test_preclick_exception_cleanup_failure_preserves_recovery_reason(setup, monkeypatch):
    watcher, desktop, _, _ = pending_retry(setup)
    install_cleanup(desktop, monkeypatch, raises=QQDeadlineExpired)
    monkeypatch.setattr(module, "now", lambda: NOW + timedelta(seconds=6))
    desktop.deliver = lambda *args, **kwargs: (_ for _ in ()).throw(OSError("synthetic_preclick"))
    with pytest.raises(InterruptedError, match="owned_draft_cleanup_required"):
        watcher._attempt_delivery(watcher._pending_turn)
    assert watcher.should_stop() and watcher._pending_turn is None
    assert watcher.status["last_error_type"] == "owned_draft_cleanup_required"
    assert desktop.clicks == 0 and not watcher.ledger.has_uncertain_send()


def test_clicked_unknown_never_cleans_or_reissues_input(setup, monkeypatch):
    settings, desktop, decide, _, _ = setup
    desktop.result = "unknown"
    cleanup_calls = []
    desktop.discard_owned_draft = lambda text: cleanup_calls.append(text)
    desktop._owned_draft = ("synthetic-draft", "10001", "synthetic-window")
    watcher = module.QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    watcher.cycle()
    assert watcher.ledger.has_uncertain_send() and not cleanup_calls
    assert desktop.clicks == 1
