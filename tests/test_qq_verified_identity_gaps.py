"""Fresh numerical verification can satisfy an older closed sensor gap."""
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from harness.qq import accessibility, watcher as module
from tests import test_qq_homeostatic_watcher as fixtures
from tests.test_qq_watcher import NOW
from tests.test_qq_watcher_identity_gaps import bare_watcher, closed_gap, open_gap

controller = fixtures.controller
model_setup = fixtures.model_setup
watcher_setup = fixtures.watcher_setup
isolated_static_parser_cache = fixtures.isolated_static_parser_cache


@pytest.fixture
def proof(monkeypatch):
    watch, invalidations = bare_watcher()
    watch.settings = NS(identity_cache_enabled=True, group_id="10001", self_member_id="30001", window_id="qq-window")
    frame = NS(capture_id="verified-home", captured_at=NOW + timedelta(seconds=12), evidence_ref="test://home",
               window_id="qq-window", active_window_id="qq-window", a11y_tree={})
    group = NS(group_id="10001", evidence_refs=["test://numeric-group"], chat_frame=frame,
               chat_screen=NS(frame_id=frame.capture_id, evidence_ref=frame.evidence_ref,
                              scene="group_chat", blocked=False, opened_profile=None))
    account = NS(member_id="30001", observed_at=frame.captured_at, profile_evidence_refs=["test://numeric-self"],
                 attribution_method="qq_visual_avatar_profile_v1")
    status = closed_gap(12)
    status["recent_gaps"][0]["finished_at"] = frame.captured_at.isoformat()
    status.update(running=True, last_error_type=None)
    snapshot = NS(digest="home-chat", polled_mono=12)
    watch.observer = NS(status=lambda: status, latest=lambda: snapshot)
    monkeypatch.setattr(module, "now", lambda: frame.captured_at)
    monkeypatch.setattr(module, "time", NS(monotonic=lambda: 12))
    monkeypatch.setattr(accessibility, "chat_change_signature", lambda _: NS(chat_visible=True, chat_digest="home-chat"))
    return NS(watch=watch, invalidations=invalidations, frame=frame, group=group,
              account=account, status=status, snapshot=snapshot)


def acknowledge(state):
    state.watch._acknowledge_verified_identity_gaps(state.group, state.account, started_at=NOW)
    state.watch._invalidate_identity_after_observer_gap(state.status)


def test_fresh_profile_and_recovered_matching_sensor_satisfy_closed_gap(proof):
    acknowledge(proof)
    assert proof.watch._identity_prepared and not proof.invalidations
    assert len(proof.watch._identity_cache_invalidated_gaps) == 1
    assert proof.status["recent_gaps"][0]["duration_seconds"] == 12


@pytest.mark.parametrize("broken", [
    "cached", "old_profile", "wrong_account", "wrong_group", "wrong_window", "inactive_window",
    "no_profile_evidence", "no_group_evidence", "open_profile", "unbound_frame", "stale_sensor",
    "unmatched_sensor", "stopped_sensor", "errored_sensor", "missing_gap_end", "future_gap_end",
])
def test_incomplete_or_unrelated_proof_still_invalidates(proof, broken):
    if broken == "cached":
        proof.account.attribution_method = "qq_session_cached_self_v1"
    elif broken == "old_profile":
        proof.account.observed_at = NOW - timedelta(seconds=1)
    elif broken == "wrong_account":
        proof.account.member_id = "99999"
    elif broken == "wrong_group":
        proof.group.group_id = "1060979317"
    elif broken in {"wrong_window", "inactive_window"}:
        setattr(proof.frame, "window_id" if broken == "wrong_window" else "active_window_id", "foreign-window")
    elif broken == "no_profile_evidence":
        proof.account.profile_evidence_refs = []
    elif broken == "no_group_evidence":
        proof.group.evidence_refs = []
    elif broken == "open_profile":
        proof.group.chat_screen.opened_profile = NS(kind="member")
    elif broken == "unbound_frame":
        proof.group.chat_screen.frame_id = "older-frame"
    elif broken == "stale_sensor":
        proof.snapshot.polled_mono = 0
    elif broken == "unmatched_sensor":
        proof.snapshot.digest = "foreign-chat"
    elif broken == "stopped_sensor":
        proof.status["running"] = False
    elif broken == "errored_sensor":
        proof.status["last_error_type"] = "request_timeout"
    elif broken == "missing_gap_end":
        del proof.status["recent_gaps"][0]["finished_at"]
    elif broken == "future_gap_end":
        proof.status["recent_gaps"][0]["finished_at"] = (proof.frame.captured_at + timedelta(seconds=1)).isoformat()
    acknowledge(proof)
    assert proof.invalidations == [True] and not proof.watch._identity_prepared


def test_open_gap_is_never_acknowledged_by_profile_or_controlled_action(proof):
    proof.status.update(open_gap(12))
    acknowledge(proof)
    assert proof.invalidations == [True] and not proof.watch._identity_prepared


def test_new_profile_survives_six_seconds_of_dismissal_and_sensor_recovery(proof):
    # The numerical profile is new in this verification. Dismissing it and
    # recovering the independent sensor may take longer than a frame's TTL;
    # the final bound chat frame and sensor snapshot must still be fresh.
    proof.account.observed_at = NOW + timedelta(seconds=6)
    acknowledge(proof)
    assert proof.watch._identity_prepared and not proof.invalidations
    assert len(proof.watch._identity_cache_invalidated_gaps) == 1


def test_gap_beginning_after_profile_is_new_loss_of_identity_evidence(proof):
    proof.frame.captured_at = NOW + timedelta(seconds=24)
    proof.status["recent_gaps"][0].update(started_at=(NOW + timedelta(seconds=13)).isoformat(),
        finished_at=proof.frame.captured_at.isoformat(), duration_seconds=11)
    acknowledge(proof)
    assert proof.invalidations == [True] and not proof.watch._identity_prepared


def test_later_gap_invalidates_after_prior_gap_was_acknowledged(proof):
    acknowledge(proof)
    proof.watch._invalidate_identity_after_observer_gap(open_gap(12, sequence=1, start=NOW + timedelta(seconds=13)))
    assert proof.invalidations == [True] and not proof.watch._identity_prepared


def test_self_verification_gap_does_not_disconnect_speak_before_delivery(controller, monkeypatch):
    state = controller
    watch, desktop, clock = state.watch, state.desktop, state.clock
    watch.settings = watch.settings.model_copy(update={"identity_cache_enabled": True})
    prior_status, verify_group = watch.observer.status, desktop.verify_group
    completed_gaps, invalidations = [], []
    watch.observer.status = lambda: prior_status() | {
        "gap_sequence": len(completed_gaps), "recent_gaps": list(completed_gaps)}
    desktop.invalidate_identity_cache = lambda: invalidations.append(clock.wall())
    def group(*, frame=None):
        verified = verify_group(frame=frame)
        verified.group_id = watch.settings.group_id
        return verified
    monkeypatch.setattr(desktop, "verify_group", group)
    def verify_self():
        started = clock.wall()
        clock.sleep(12)
        latest = desktop.capture()
        verified = desktop.verify_group(frame=latest)
        desktop._last_verified_capture = latest
        desktop._screens = {latest.capture_id: verified.chat_screen}
        completed_gaps.append({"started_at": started.isoformat(), "finished_at": clock.wall().isoformat(),
                               "sequence": len(completed_gaps) + 1, "duration_seconds": 12.0})
        return NS(member_id=watch.settings.self_member_id, observed_at=clock.wall(),
                  profile_evidence_refs=["test://fresh-account-profile"],
                  attribution_method="qq_visual_avatar_profile_v1")
    monkeypatch.setattr(desktop, "verify_self", verify_self)
    watch.cycle()
    assert len(state.calls) == 1 and watch.status["last_decision_action"] == "speak"
    assert not invalidations and watch._identity_prepared
    assert len(desktop.clicked) == 1
    assert fixtures.reservations(state)[0]["status"] == "sent"
    assert watch.status["last_turn_outcome"] == "sent"
