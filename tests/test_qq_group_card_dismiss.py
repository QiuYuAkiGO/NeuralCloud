from dataclasses import replace
from types import SimpleNamespace
import time

import pytest

from harness.qq.desktop import DesktopError
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace
from tests import test_qq_desktop as fixtures

backend = fixtures.backend


def prepared(desktop, monkeypatch):
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    item = replace(fixtures.frame(desktop, "card"), a11y_tree={"nodes":[]})
    profile = {"kind":"group", "display_name":"虚构群", "group_id":"10001", "member_id":None,
               "card":fixtures.Rect(x=200,y=120,width=320,height=330),"close":None,"uncertainties":[]}
    reading = fixtures.screen(item, profile=profile, scene="group_profile")
    desktop._a11y_frames.add(item.capture_id)
    desktop._screens[item.capture_id] = reading
    monkeypatch.setattr(desktop,"_fresh_profile_for_escape",lambda *args:item)
    return item, reading


def test_one_blank_click_closes_group_card_without_escape_or_pointer_mutation(backend, monkeypatch):
    desktop, calls = backend
    item, reading = prepared(desktop, monkeypatch)
    after = fixtures.frame(desktop,"after")
    output = fixtures.screen(after).model_copy(update={"visible_draft":"synthetic retained draft"})
    monkeypatch.setattr(desktop,"capture",lambda:after)
    monkeypatch.setattr(desktop,"inspect",lambda _:output)
    result, projected = desktop._dismiss(item,reading)
    assert result is after and projected.visible_draft == "synthetic retained draft"
    assert calls == [{"kind":"click","x":640,"y":45,"button":"left","count":1}]


@pytest.mark.parametrize("fault", ["wrong_group","member","blocked","unknown","not_ax","focus","interactive","card_overlap","nonblank","missing_title"])
def test_blank_target_requires_numeric_group_and_current_ui_evidence(backend, monkeypatch, fault):
    desktop, calls = backend
    item, reading = prepared(desktop, monkeypatch)
    if fault == "wrong_group":
        reading = reading.model_copy(update={"opened_profile": reading.opened_profile.model_copy(update={"group_id": "99999"})})
    elif fault == "member":
        reading = reading.model_copy(update={"scene":"member_profile"})
    elif fault == "blocked":
        reading = reading.model_copy(update={"blocked":True})
    elif fault == "unknown":
        reading = reading.model_copy(update={"uncertainties":["synthetic_unknown"]})
    elif fault == "not_ax":
        desktop._a11y_frames.clear()
    elif fault == "focus":
        item = replace(item, active_window_id="0x999")
    elif fault == "interactive":
        item.a11y_tree["nodes"].append({"role":"push button","rect":{"x":630,"y":38,"width":20,"height":14}})
    elif fault == "card_overlap":
        reading = reading.model_copy(update={"opened_profile": reading.opened_profile.model_copy(update={"card": fixtures.Rect(x=600,y=30,width=80,height=50)})})
    elif fault == "nonblank":
        item = replace(item,png=fixtures.png(change=(630,38,640,52)))
    elif fault == "missing_title":
        reading = reading.model_copy(update={"group_title":None})
    desktop._screens[item.capture_id] = reading
    monkeypatch.setattr(desktop,"_fresh_profile_for_escape",lambda *args:item)
    assert desktop._dismiss_group_via_blank_bar(item,reading) is False
    assert calls == []


def test_card_remaining_after_blank_click_never_repeats_the_click(backend, monkeypatch):
    desktop, calls = backend
    item, reading = prepared(desktop, monkeypatch)
    monkeypatch.setattr(desktop,"capture",lambda:item)
    monkeypatch.setattr(desktop,"inspect",lambda _:reading)
    with pytest.raises(DesktopError,match="unambiguous_group_chat_required"):
        desktop._dismiss(item,reading)
    assert len(calls) == 1 and calls[0]["kind"] == "click"


def test_expired_group_dismiss_does_not_click(backend, monkeypatch):
    desktop, calls = backend
    item, reading = prepared(desktop, monkeypatch)
    with desktop.turn(deadline=QQDeadline.after(0),trace=QQTurnTrace()):
        with pytest.raises(QQDeadlineExpired):
            desktop._dismiss_group_via_blank_bar(item,reading)
    assert calls == []


def test_failed_precheck_retains_the_latest_verified_frame(backend, monkeypatch):
    desktop, _ = backend
    args = fixtures.delivery_setup(desktop,monkeypatch)
    failed = fixtures.frame(desktop,"failure-evidence", image=fixtures.png(change=(30, 32, 34, 36)))
    def stop():
        desktop._remember_capture(failed, time.monotonic())
        raise DesktopError("unambiguous_group_chat_required")
    monkeypatch.setattr(desktop,"verify_self",stop)
    result = desktop.deliver(**args)
    assert result.status == "not_sent" and failed.evidence_ref in result.evidence_refs


def test_invalid_attempt_does_not_adopt_previous_intent_capture(backend, monkeypatch):
    desktop, _ = backend
    args = fixtures.delivery_setup(desktop,monkeypatch)
    previous = fixtures.frame(desktop,"previous-intent", image=fixtures.png(change=(30, 32, 34, 36)))
    desktop._remember_capture(previous, time.monotonic())
    args["text"] = ""
    result = desktop.deliver(**args)
    assert result.status == "not_sent" and result.reason_code == "invalid_delivery_request"
    assert previous.evidence_ref not in result.evidence_refs


@pytest.mark.parametrize("failure", ["health_after_action_invalidation", "next_capture"])
def test_attempt_diagnostic_frame_survives_cache_invalidation(backend, monkeypatch, failure):
    desktop, calls = backend
    args = fixtures.delivery_setup(desktop, monkeypatch)
    old = fixtures.frame(desktop, "previous-intent", image=fixtures.png(change=(10, 12, 14, 16)))
    failed = fixtures.frame(desktop, "this-attempt-frame", image=fixtures.png(change=(30, 32, 34, 36)))
    desktop._remember_capture(old, time.monotonic())

    def broken_health():
        raise DesktopError("dedicated_agent_unavailable")

    def broken_body_run(coroutine):
        coroutine.close()
        raise OSError("synthetic Body capture failure")

    def stop():
        desktop._remember_capture(failed, time.monotonic())
        if failure == "health_after_action_invalidation":
            desktop._act({"kind": "move_pointer", "x": 640, "y": 45})
        else:
            desktop._capture_unlocked()

    monkeypatch.setattr(desktop, "_agent_health", broken_health)
    monkeypatch.setattr(desktop, "_body_runner", SimpleNamespace(run=broken_body_run, close=lambda: None))
    monkeypatch.setattr(desktop, "verify_self", stop)
    result = desktop.deliver(**args)
    assert result.status == "not_sent"
    assert result.reason_code == ("dedicated_agent_unavailable" if failure == "health_after_action_invalidation"
                                  else "verified_frame_unavailable")
    assert failed.evidence_ref in result.evidence_refs
    assert old.evidence_ref not in result.evidence_refs
    assert desktop._latest_capture is None
    assert desktop._last_verified_capture is failed
    assert calls == []

    # The next invalid intent must not adopt even this retained diagnostic.
    result = desktop.deliver(**dict(args, text=""))
    assert result.evidence_refs == []


def test_diagnostic_capture_is_never_a_reusable_action_frame(backend, monkeypatch):
    desktop, _ = backend
    desktop.config = replace(desktop.config, accessibility_enabled=True)
    old = replace(fixtures.frame(desktop, "diagnostic"), a11y_tree={"nodes": []},
                  a11y_evidence_ref="test://synthetic-ax")
    desktop._a11y_frames.add(old.capture_id)
    desktop._screens[old.capture_id] = fixtures.screen(old)
    desktop._remember_capture(old, time.monotonic())
    desktop._latest_capture = None
    desktop._latest_capture_started_mono = None
    actual = fixtures.frame(desktop, "new-live-capture")
    captures = []

    def capture():
        captures.append(1)
        return actual

    monkeypatch.setattr(desktop, "capture", capture)
    assert desktop._current_ax_frame_or_capture() is actual
    assert captures == [1] and desktop._last_verified_capture is old
