from dataclasses import replace

import pytest

from harness.qq.desktop import DesktopError
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired
from tests import test_qq_desktop as fixtures

backend = fixtures.backend


def profile_screen(frame, *, member_id="20001", scene="member_profile"):
    profile = {"kind": "member", "display_name": "synthetic", "member_id": member_id, "group_id": None,
               "card": fixtures.Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    return fixtures.screen(frame, profile=profile if scene == "member_profile" else None, scene=scene)


@pytest.mark.parametrize("chat_reads", [0, 1, 2, 3])
def test_delayed_profile_reads_are_bounded_and_do_not_click(backend, monkeypatch, chat_reads):
    desktop, actions = backend
    frames = [fixtures.frame(desktop, f"transition-{index}") for index in range(4)]
    reads = []
    waits = []
    monkeypatch.setattr(desktop, "capture", lambda: reads.append(frames[len(reads)]) or reads[-1])
    monkeypatch.setattr(desktop, "inspect", lambda item: fixtures.screen(item) if frames.index(item) < chat_reads else profile_screen(item))
    monkeypatch.setattr("harness.qq.desktop.time.sleep", waits.append)
    result_frame, result = desktop._capture_profile_transition("虚构群")
    assert len(reads) == min(chat_reads + 1, 3)
    assert result_frame is reads[-1] and len(waits) == min(chat_reads, 2)
    assert result.scene == ("group_chat" if chat_reads == 3 else "member_profile")
    assert not actions


@pytest.mark.parametrize("scene", ["member_profile", "login", "uncertain"])
def test_wrong_card_or_unknown_scene_never_waits_for_a_better_identity(backend, monkeypatch, scene):
    desktop, actions = backend
    item = fixtures.frame(desktop, "first")
    reads = []
    monkeypatch.setattr(desktop, "capture", lambda: reads.append(True) or item)
    monkeypatch.setattr(desktop, "inspect", lambda frame: profile_screen(frame, member_id="29999", scene=scene))
    monkeypatch.setattr("harness.qq.desktop.time.sleep", lambda *_: pytest.fail("must not wait"))
    _frame, reading = desktop._capture_profile_transition("虚构群")
    assert reads == [True] and reading.scene == scene
    if reading.opened_profile:
        assert reading.opened_profile.member_id == "29999"
    assert not actions


@pytest.mark.parametrize("fault", ["title", "window"])
def test_different_chat_binding_stops_transition(backend, monkeypatch, fault):
    desktop, actions = backend
    item = fixtures.frame(desktop, "changed")
    reading = fixtures.screen(item)
    if fault == "title":
        reading = reading.model_copy(update={"group_title": reading.group_title.model_copy(update={"display_name": "other"})})
    else:
        item = replace(item, active_window_id="0x999")
    monkeypatch.setattr(desktop, "capture", lambda: item)
    monkeypatch.setattr(desktop, "inspect", lambda _: reading)
    monkeypatch.setattr("harness.qq.desktop.time.sleep", lambda *_: pytest.fail("must not wait"))
    with pytest.raises(DesktopError):
        desktop._capture_profile_transition("虚构群")
    assert not actions


@pytest.mark.parametrize("stop", [False, True])
def test_transition_keeps_original_deadline_and_pause(backend, monkeypatch, stop):
    desktop, actions = backend
    clock = [100.0]
    paused = [False]
    desktop.deadline = QQDeadline(100.1, clock=lambda: clock[0])
    desktop.should_stop = lambda: paused[0]
    item = fixtures.frame(desktop, "waiting")
    reads = []
    monkeypatch.setattr(desktop, "capture", lambda: reads.append(True) or item)
    monkeypatch.setattr(desktop, "inspect", lambda item: fixtures.screen(item))

    def wait(seconds):
        assert seconds <= .1 + 1e-9
        clock[0] = 100.2
        paused[0] = stop

    monkeypatch.setattr("harness.qq.desktop.time.sleep", wait)
    with pytest.raises(InterruptedError if stop else QQDeadlineExpired):
        desktop._capture_profile_transition("虚构群")
    assert reads == [True] and not actions


def test_group_verification_waits_for_card_but_clicks_title_once(backend, monkeypatch):
    desktop, actions = backend
    source, fresh, delayed, card, returned = [fixtures.frame(desktop, name) for name in ["source", "fresh", "delayed", "card", "returned"]]
    profile = {"kind": "group", "display_name": "虚构群", "member_id": None, "group_id": "10001",
               "card": fixtures.Rect(x=300, y=140, width=500, height=360), "close": None, "uncertainties": []}
    readings = {frame.capture_id: fixtures.screen(frame) for frame in [source, fresh, delayed, returned]}
    readings[card.capture_id] = fixtures.screen(card, profile=profile, scene="group_profile")
    captures = iter([source, fresh, delayed, card])
    monkeypatch.setattr(desktop, "capture", lambda: next(captures))
    monkeypatch.setattr(desktop, "inspect", lambda frame: readings[frame.capture_id])
    monkeypatch.setattr(desktop, "_dismiss", lambda *args: (returned, readings[returned.capture_id]))
    monkeypatch.setattr("harness.qq.desktop.time.sleep", lambda *_: None)
    assert desktop.verify_group().group_id == "10001"
    assert len(actions) == 1 and actions[0]["kind"] == "click"
