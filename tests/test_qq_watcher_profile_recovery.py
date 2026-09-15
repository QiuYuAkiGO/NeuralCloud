"""Observation recovery must close a known card without reopening it."""
from types import SimpleNamespace as NS

import pytest

from harness.qq.desktop import DesktopError
from tests.test_qq_watcher import setup as setup
from tests.test_qq_watcher_observer_recovery import gap_case


@pytest.mark.parametrize("kind", ["group", "member"])
def test_recovery_dismisses_verified_card_without_reopening_or_reusing_identity(setup, monkeypatch, kind):
    state = gap_case(setup, monkeypatch)
    state.screen.scene = kind + "_profile"
    state.screen.group_title = NS(display_name="Synthetic group (3)")
    state.screen.opened_profile = NS(kind=kind, uncertainties=[], group_id="10001", member_id="30001",
                                   display_name="Synthetic group", verified_self_indicator="own_space_link")
    state.desktop._group = NS(group_id="10001", display_name="Synthetic group")
    state.watcher._identity_prepared = True
    state.watcher._self_aliases = ["synthetic prior alias"]
    calls = []

    def dismiss(frame, screen):
        calls.append("dismiss")
        return frame, NS(scene="group_chat", blocked=False, opened_profile=None,
                         group_title=NS(display_name="Synthetic group (3)"))

    def reopen(**kwargs):
        calls.append("reopen")
        raise DesktopError("group_profile_not_verified")

    state.desktop._dismiss = dismiss
    state.desktop._require_chat = lambda frame, reading: calls.append("bound_chat")
    state.desktop.invalidate_identity_cache = lambda: calls.append("invalidate")
    state.desktop.verify_group = reopen
    state.watcher.cycle()

    assert state.watcher.status["state"] == "waiting_for_observer"
    assert calls == ["invalidate", "dismiss", "bound_chat"]
    assert state.watcher._identity_prepared is False
    assert state.watcher._self_aliases == []
    assert state.watcher.status["automatic_send"] is False
    assert not state.receipts and state.desktop.clicks == 0


@pytest.mark.parametrize("change", ["wrong_group", "group_title_mismatch", "wrong_member", "missing_self_marker",
                                    "wrong_prior_group", "missing_prior_group", "missing_title"])
def test_profile_scope_mismatch_never_dismisses(setup, monkeypatch, change):
    state = gap_case(setup, monkeypatch)
    if change in {"wrong_group", "group_title_mismatch"}:
        state.screen.scene = "group_profile"
        state.screen.opened_profile = NS(kind="group", group_id="10001", display_name="Synthetic group", uncertainties=[])
        if change == "wrong_group":
            state.screen.opened_profile.group_id = "10002"
        else:
            state.screen.opened_profile.display_name = "Different group"
    elif change == "wrong_member":
        state.screen.opened_profile.member_id = "30002"
    elif change == "missing_self_marker":
        state.screen.opened_profile.verified_self_indicator = "none"
    elif change == "wrong_prior_group":
        state.desktop._group.group_id = "10002"
    elif change == "missing_prior_group":
        state.desktop._group = None
    else:
        state.screen.group_title = None
    state.watcher.cycle()
    assert state.actions == ["capture", "inspect"]
    assert state.watcher.status["last_error_type"] == "observer_recovery_profile_scope_mismatch"
    assert not state.receipts and state.desktop.clicks == 0


def test_dismissed_card_returning_to_different_group_does_not_mark_recovery_success(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    state.desktop._dismiss = lambda frame, screen: (
        frame, NS(group_title=NS(display_name="Different group")))
    state.watcher.cycle()
    assert state.watcher.status["last_error_type"] == "observer_recovery_group_changed"
    assert state.watcher.status["automatic_send"] is False
    assert state.watcher._identity_prepared is False


def test_next_preparation_reverifies_group_and_self_after_dismissal(setup, monkeypatch):
    state = gap_case(setup, monkeypatch)
    state.watcher._identity_prepared = True
    state.watcher.cycle()
    assert state.watcher._identity_prepared is False
    state.clock[0] += 3
    state.sensor.snapshot = NS(sequence=0)
    state.sensor.gap["gap_started_at"] = None
    verified = []

    def verify_group():
        verified.append("group")
        return NS(chat_frame=state.desktop.current, chat_screen=NS(messages=[]), evidence_refs=[])

    state.desktop.verify_group = verify_group
    state.desktop.verify_self = lambda: verified.append("self")
    state.watcher.cycle()
    assert verified == ["group", "self"]
    assert state.watcher._identity_prepared is True
    assert not state.receipts and state.desktop.clicks == 0
