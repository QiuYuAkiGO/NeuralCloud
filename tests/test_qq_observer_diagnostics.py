"""Fixed diagnostic labels for blind sensor intervals; no GUI or live reads."""
import json

import pytest

from harness.qq.accessibility import AccessibilityUnavailable
from harness.qq.observer import ObserverReadError, ScopedQQObserver
from tests.test_qq_observer import Clock, signature
from tests.test_qq_watcher import setup as setup
from tests.test_qq_watcher_unattended import scenario as scenario


@pytest.mark.parametrize("reason,expected", [
    ("a11y_text_unreadable", "a11y_text_unreadable"),
    ("a11y_message_text_missing", "a11y_message_text_missing"),
    ("a11y_private_fictional_chat", "accessibility_unavailable"),
    ("fictional member: confidential message", "accessibility_unavailable"),
    (["fictional untrusted node"], "accessibility_unavailable"),
])
def test_only_allowlisted_signature_failure_is_exposed_then_cleared(reason, expected):
    clock = Clock()
    failure = [False]

    def parse(tree):
        if failure[0]:
            raise AccessibilityUnavailable(reason)
        return signature(tree)

    observer = ScopedQQObserver(read_fn=lambda: {"chat": "fictional baseline"},
        signature_fn=parse, clock=clock, wall_clock=clock.wall)
    baseline = observer.poll_once()
    failure[0] = True
    clock.value += 1
    assert observer.poll_once() is None
    status = observer.status()
    assert status["last_error_type"] == "accessibility_unavailable"
    assert status["signature_error_type"] == expected
    assert "fictional" not in json.dumps(status)
    clock.value += 2
    observer.poll_once()
    assert observer.status()["gap_seconds"] == 2
    failure[0] = False
    recovered = observer.poll_once()
    assert recovered.sequence == baseline.sequence == 0
    assert recovered.first_seen_mono == baseline.first_seen_mono
    assert observer.status()["signature_error_type"] is None
    assert observer.status()["last_error_type"] is None


def test_read_failure_does_not_impersonate_a_signature_failure():
    def read():
        raise AccessibilityUnavailable("a11y_text_unreadable")

    observer = ScopedQQObserver(read_fn=read)
    assert observer.poll_once() is None
    assert observer.status()["last_error_type"] == "accessibility_unavailable"
    assert observer.status()["signature_error_type"] is None


@pytest.mark.parametrize("error,expected", [
    (AccessibilityUnavailable("a11y_text_unreadable"), "a11y_text_unreadable"),
    (AccessibilityUnavailable("fictional secret message"), "accessibility_unavailable"),
    (ObserverReadError("request_timeout"), "request_timeout"),
    (ValueError("fictional secret message"), "observer_error"),
])
def test_watcher_reports_sensor_failure_without_gui_and_clears_on_recovery(scenario, error, expected):
    s = scenario
    original = s.observer._signature_fn

    def fail(tree):
        raise error

    s.observer._signature_fn = fail
    s.clock.advance(1)
    assert s.observer.poll_once() is None
    captures = s.desktop.captures
    s.watcher.cycle()
    assert s.watcher.status["state"] == "observer_unavailable"
    assert s.watcher.status["last_error_type"] == expected
    assert s.watcher.status["automatic_send"] is False
    assert s.watcher.status["automatic_screen_capture"] is False
    assert s.desktop.captures == captures and not s.calls and not s.desktop.sends
    assert "secret message" not in json.dumps(s.watcher.status)
    s.observer._signature_fn = original
    s.observer.poll_once()
    s.watcher.cycle()
    assert s.watcher.status["state"] == "watching"
    assert s.watcher.status["last_error_type"] is None
    assert s.watcher.status["observer"]["signature_error_type"] is None
    assert s.desktop.captures == captures and not s.calls
