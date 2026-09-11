from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import hashlib
import json
import sqlite3

import pytest

from harness.qq.watch_ledger import VisibleItem, WatchLedger as CoreWatchLedger, WatchLedgerError, plan_visible_transition


NOW = datetime(2026, 9, 11, 2, tzinfo=timezone.utc)
GROUP = "10001"
SELF = "29999"
REPLY = "synthetic reply"
REPLY_HASH = hashlib.sha256(REPLY.encode()).hexdigest()


def click_proof(moment=NOW, **changes):
    return dict(group_id=GROUP, self_member_id=SELF, reply_sha256=REPLY_HASH,
                frame_evidence_ref="test://preclick", context_signature="synthetic-context",
                verified_at=moment, before_outgoing_count=0) | changes


class WatchLedger(CoreWatchLedger):
    """Supply common synthetic identity/proof inputs for the existing scenarios."""

    def reserve_send(self, *args, **kwargs):
        kwargs.setdefault("reply_sha256", REPLY_HASH)
        kwargs.setdefault("self_member_id", SELF)
        return super().reserve_send(*args, **kwargs)

    def mark_click_started(self, *args, **kwargs):
        kwargs.setdefault("proof", click_proof(kwargs.get("now", NOW)))
        return super().mark_click_started(*args, **kwargs)


def item(text, *, direction="incoming", kind="text", author="visual-avatar-a"):
    return VisibleItem(text=text, direction=direction, kind=kind, author_key=author)


def plan(previous, current, *, frame="frame-2", previous_frame="frame-1"):
    return plan_visible_transition(previous, current, frame_signature=frame, previous_frame_signature=previous_frame)


def ledger(tmp_path):
    result = WatchLedger(tmp_path / "private-watch.sqlite3", group_id=GROUP)
    result.start_run(now=NOW)
    return result


def appended(watch, text="new message", *, signature="frame-2", prefix=None, moment=NOW):
    initial = prefix or [item("stable anchor")]
    watch.observe(frame_signature="frame-1", items=initial, observed_at=moment)
    return watch.observe(frame_signature=signature, items=initial + [item(text)], observed_at=moment + timedelta(seconds=1))


def ready(watch, event, decision="decision-1", *, fresh=True, replayed=False):
    watch.bind_event(event.event_id, f"source:{event.event_id}")
    return watch.attach_decision(event.event_id, decision, "speak", new_decision=fresh, replayed=replayed)


def reserved(watch):
    event = appended(watch).events[-1]
    ready(watch, event)
    return watch.reserve_send(event.event_id, "decision-1", now=NOW, cooldown_seconds=60)


@pytest.mark.parametrize("outcome", ["expired", "failed", "rate_limited", "superseded"])
def test_unreserved_failure_is_not_a_natural_wait(tmp_path, outcome):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    result = watch.finish_event(event.event_id, outcome=outcome, reason="synthetic_failure")
    assert result.state == outcome and result.decision_id is None
    assert watch.finish_event(event.event_id, outcome=outcome, reason="same").state == outcome
    with pytest.raises(WatchLedgerError):
        watch.attach_decision(event.event_id, "old", "wait", new_decision=True)


def test_finish_event_never_rewrites_reserved_send(tmp_path):
    watch = ledger(tmp_path)
    send = reserved(watch)
    with pytest.raises(WatchLedgerError):
        watch.finish_event(send.event_id, outcome="failed", reason="failed")
    assert watch.get_send(send.reservation_id).status == "reserved"


def test_preclick_proof_cannot_bind_another_reservation(tmp_path):
    watch = ledger(tmp_path)
    send = reserved(watch)
    with pytest.raises(WatchLedgerError, match="pre-click proof"):
        watch.mark_click_started(send.reservation_id, now=NOW,
                                 proof=click_proof(reservation_id="other-send"))
    assert watch.get_send(send.reservation_id).status == "reserved"
    clicked = watch.mark_click_started(send.reservation_id, now=NOW,
                                      proof=click_proof(reservation_id=send.reservation_id))
    assert clicked.preclick_proof.reservation_id == send.reservation_id


def test_rate_limited_redecision_once_preserves_first_seen_and_old_decision_audit(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event, "old-decision")
    watch.finish_event(event.event_id, outcome="rate_limited", reason="cooldown")
    reopened = watch.reopen_rate_limited(event.event_id, now=NOW + timedelta(seconds=20))
    assert reopened.state == "bound" and reopened.decision_id is None
    assert reopened.first_seen_at == event.first_seen_at
    assert reopened.decision_audit[0].decision_id == "old-decision"
    watch.attach_decision(event.event_id, "new-decision", "speak", new_decision=True)
    watch.finish_event(event.event_id, outcome="rate_limited", reason="cooldown")
    assert watch.reopen_rate_limited(event.event_id, now=NOW + timedelta(seconds=21)) is None
    assert watch.get_event(event.event_id).decision_id == "new-decision"


@pytest.mark.parametrize("change", ["expired", "superseded", "restart"])
def test_rate_limited_old_context_never_reopens(tmp_path, change):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event)
    watch.finish_event(event.event_id, outcome="rate_limited", reason="cooldown")
    if change == "restart":
        watch.start_run(now=NOW + timedelta(seconds=10))
        with pytest.raises(WatchLedgerError):
            watch.reopen_rate_limited(event.event_id, now=NOW + timedelta(seconds=20))
        return
    if change == "superseded":
        watch.observe(frame_signature="new-window", items=[item("different")], observed_at=NOW + timedelta(seconds=10))
    moment = NOW + timedelta(seconds=61 if change == "expired" else 20)
    assert watch.reopen_rate_limited(event.event_id, now=moment) is None
    assert watch.get_event(event.event_id).state == change


def test_first_screen_and_identical_frame_cannot_emit_events():
    assert plan(None, [item("old history")]).status == "baseline"
    same = plan([item("old")], [item("old"), item("OCR changed")], frame="same", previous_frame="same")
    assert same.status == "unchanged" and not same.new_indices
    identical_items = plan([item("same")], [item("same")])
    assert identical_items.status == "unchanged"


def test_unique_ordered_anchor_preserves_repeated_text_occurrences():
    result = plan([item("anchor"), item("好")], [item("anchor"), item("好"), item("好")])
    assert result.status == "new" and result.overlap == 2 and result.new_indices == [2]
    # The same text can also occur far earlier; no global content-hash suppression.
    assert plan([item("好"), item("topic")], [item("topic"), item("好")]).new_indices == [1]


@pytest.mark.parametrize("previous,current", [
    (["好"], ["好", "好"]),
    (["好", "好"], ["好", "好", "新"]),
    (["a", "b", "a"], ["a", "new"]),
    (["a", "b", "a"], ["a", "b", "a", "new"]),
])
def test_repeated_overlap_ambiguity_never_emits(previous, current):
    result = plan([item(text) for text in previous], [item(text) for text in current])
    assert result.status == "ambiguous" and not result.new_indices


@pytest.mark.parametrize("previous,current,reason", [
    (["a"], ["b"], "no_overlap"),
    ([], ["b"], "empty_window"),
    (["a"], [], "empty_window"),
    (["a", "b"], ["b"], "window_contracted"),
])
def test_unknown_continuity_only_sets_baseline(previous, current, reason):
    result = plan([item(text) for text in previous], [item(text) for text in current])
    assert result.status == "baseline" and result.reason == reason and not result.new_indices


def test_signatures_distinguish_direction_sticker_and_visual_author():
    incoming = item("same")
    outgoing = item("same", direction="outgoing")
    sticker = item("same", kind="sticker")
    different_author = item("same", author="visual-avatar-b")
    assert len({entry.model_dump_json() for entry in [incoming, outgoing, sticker, different_author]}) == 4
    result = plan([incoming], [incoming, outgoing, sticker, different_author])
    assert result.new_indices == [1, 2, 3]
    assert "message_id" not in incoming.model_dump()


def test_ledger_records_ordered_events_and_never_reemits_same_frame(tmp_path):
    watch = ledger(tmp_path)
    first = watch.observe(frame_signature="frame-1", items=[item("anchor")], observed_at=NOW)
    assert first.plan.status == "baseline" and first.events == []
    items = [item("anchor"), item("hi"), item("[sticker]", kind="sticker"), item("mine", direction="outgoing")]
    batch = watch.observe(frame_signature="frame-2", items=items, observed_at=NOW)
    assert [event.item_index for event in batch.events] == [1, 2, 3]
    assert [event.state for event in batch.events] == ["observed", "observed", "ignored"]
    assert len({event.event_id for event in batch.events}) == 3
    assert all(event.observed_at == NOW and event.group_id == GROUP for event in batch.events)
    again = watch.observe(frame_signature="frame-2", items=items, observed_at=NOW)
    assert not again.events and again.screen_id == batch.screen_id
    with sqlite3.connect(watch.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM qq_watch_events").fetchone()[0] == 3
        assert connection.execute("SELECT COUNT(*) FROM qq_watch_screens").fetchone()[0] == 2


def test_returning_to_old_frame_is_baseline_even_after_intervening_screens(tmp_path):
    watch = ledger(tmp_path)
    initial = [item("anchor")]
    watch.observe(frame_signature="old-1", items=initial)
    visible = initial + [item("was new")]
    event = watch.observe(frame_signature="old-2", items=visible).events[-1]
    watch.observe(frame_signature="old-1", items=initial)
    revisit = watch.observe(frame_signature="old-2", items=visible)
    assert revisit.plan.reason == "previously_seen_frame" and not revisit.events
    assert watch.get_event(event.event_id).item.text == "was new"


def test_restart_discards_backlog_and_first_new_screen_is_history(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event)
    reopened = WatchLedger(watch.path, group_id=GROUP)
    reopened.start_run(now=NOW + timedelta(hours=1))
    first = reopened.observe(frame_signature="later", items=[item("stable anchor"), item("new message"), item("during downtime")])
    assert first.plan.status == "baseline" and first.events == []
    assert reopened.get_event(event.event_id).reason == "process_restarted_no_backlog"
    with pytest.raises(WatchLedgerError, match="active run"):
        watch.reserve_send(event.event_id, "decision-1")
    with pytest.raises(WatchLedgerError, match="earlier run"):
        reopened.reserve_send(event.event_id, "decision-1")


@pytest.mark.parametrize("clicked", [False, True])
def test_restart_quarantines_pending_and_blocks_future_send(tmp_path, clicked):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    if clicked:
        watch.mark_click_started(reservation.reservation_id, now=NOW)
    reopened = WatchLedger(watch.path, group_id=GROUP)
    reopened.start_run(now=NOW + timedelta(hours=1))
    assert reopened.has_uncertain_send()
    assert reopened.get_reservation(reservation.reservation_id).status == "uncertain"
    new_event = appended(reopened, moment=NOW + timedelta(hours=2)).events[-1]
    ready(reopened, new_event, "new-decision")
    assert reopened.reserve_send(new_event.event_id, "new-decision", now=NOW + timedelta(hours=2)) is None
    assert reopened.get_event(new_event.event_id).reason == "unresolved_send_blocks_delivery"


@pytest.mark.parametrize("fresh,replayed", [(False, True), (False, False), (True, True)])
def test_cached_speak_is_discarded_and_cannot_later_queue(tmp_path, fresh, replayed):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    attached = ready(watch, event, fresh=fresh, replayed=replayed)
    assert attached.state == "ignored" and attached.reason == "cached_speak_not_queued"
    assert watch.reserve_send(event.event_id, "decision-1", now=NOW) is None
    watch.attach_decision(event.event_id, "decision-1", "speak", new_decision=True)
    assert watch.reserve_send(event.event_id, "decision-1", now=NOW + timedelta(days=1)) is None


def test_wait_and_unknown_attribution_are_terminal_without_send(tmp_path):
    watch = ledger(tmp_path)
    events = appended(watch, prefix=[item("anchor")]).events
    event = events[-1]
    with pytest.raises(WatchLedgerError, match="bound"):
        watch.attach_decision(event.event_id, "d", "speak", new_decision=True)
    watch.mark_ignored(event.event_id, reason="author_not_verified")
    with pytest.raises(WatchLedgerError, match="unbound"):
        watch.bind_event(event.event_id, "source")
    more = watch.observe(frame_signature="frame-3", items=[item("anchor"), item("new message"), item("question")]).events[-1]
    watch.bind_event(more.event_id, "source-2")
    assert watch.attach_decision(more.event_id, "d2", "wait", new_decision=True).state == "decided_wait"
    assert watch.reserve_send(more.event_id, "d2") is None


def test_atomic_reservation_allows_only_one_concurrent_caller(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event)
    with ThreadPoolExecutor(max_workers=6) as executor:
        results = list(executor.map(lambda _: watch.reserve_send(event.event_id, "decision-1", now=NOW), range(6)))
    assert sum(result is not None for result in results) == 1
    assert watch.get_event(event.event_id).state == "reserved"


def test_preclick_transition_allows_only_one_concurrent_caller(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)

    def attempt(_):
        try:
            watch.mark_click_started(reservation.reservation_id, now=NOW)
            return True
        except WatchLedgerError:
            return False

    with ThreadPoolExecutor(max_workers=6) as executor:
        assert sum(executor.map(attempt, range(6))) == 1


def test_sent_requires_preclick_durable_state_and_verified_evidence(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    with pytest.raises(WatchLedgerError, match="state transition"):
        watch.mark_sent(reservation.reservation_id, evidence_refs=["test://bubble"], now=NOW)
    with pytest.raises(WatchLedgerError, match="references"):
        watch.mark_sent(reservation.reservation_id, evidence_refs=[], now=NOW)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    sent = watch.mark_sent(reservation.reservation_id, evidence_refs=["test://bubble"], now=NOW)
    assert sent.status == "sent" and watch.get_event(reservation.event_id).state == "sent"
    assert watch.mark_sent(reservation.reservation_id, evidence_refs=["test://bubble"], now=NOW) == sent
    with pytest.raises(WatchLedgerError, match="cannot be replaced"):
        watch.mark_sent(reservation.reservation_id, evidence_refs=["test://different"], now=NOW)
    with pytest.raises(WatchLedgerError, match="cannot be ignored"):
        watch.mark_ignored(reservation.event_id, "try again")


def test_cooldown_persists_and_discards_instead_of_delaying_reply(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    watch.mark_sent(reservation.reservation_id, evidence_refs=["test://sent"], now=NOW + timedelta(seconds=10))
    reopened = WatchLedger(watch.path, group_id=GROUP)
    reopened.start_run(now=NOW + timedelta(seconds=20))
    assert not reopened.has_uncertain_send()
    event = appended(reopened, moment=NOW + timedelta(seconds=20)).events[-1]
    ready(reopened, event, "decision-2")
    assert reopened.reserve_send(event.event_id, "decision-2", now=NOW + timedelta(seconds=55)) is None
    assert reopened.get_event(event.event_id).reason == "cooldown_discarded_no_queue"
    assert reopened.reserve_send(event.event_id, "decision-2", now=NOW + timedelta(hours=1)) is None
    current = [item("stable anchor"), item("new message"), item("genuinely new")]
    next_event = reopened.observe(frame_signature="frame-3", items=current, observed_at=NOW + timedelta(seconds=71)).events[-1]
    ready(reopened, next_event, "decision-3")
    assert reopened.reserve_send(next_event.event_id, "decision-3", now=NOW + timedelta(seconds=71)) is not None


def test_one_decision_cannot_send_twice_for_different_visual_events(tmp_path):
    watch = ledger(tmp_path)
    first = reserved(watch)
    watch.mark_not_sent(first.reservation_id, reason="target_changed", now=NOW)
    event = watch.observe(frame_signature="frame-3", items=[item("stable anchor"), item("new message"), item("another")], observed_at=NOW + timedelta(hours=1)).events[-1]
    ready(watch, event, "decision-1")
    assert watch.reserve_send(event.event_id, "decision-1", now=NOW + timedelta(hours=1)) is None
    assert watch.get_event(event.event_id).reason == "decision_already_reserved"


def test_not_sent_only_before_click_and_uncertain_never_becomes_sent(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    with pytest.raises(WatchLedgerError, match="state transition"):
        watch.mark_not_sent(reservation.reservation_id, reason="backend_returned_not_sent")
    watch.mark_uncertain(reservation.reservation_id, reason="bubble_not_verified", now=NOW)
    assert watch.has_uncertain_send()
    with pytest.raises(WatchLedgerError, match="state transition"):
        watch.mark_sent(reservation.reservation_id, evidence_refs=["test://late"], now=NOW)


def test_not_sent_does_not_become_uncertain_on_restart(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    assert watch.mark_not_sent(reservation.reservation_id, reason="no_click", now=NOW).status == "not_sent"
    watch.start_run(now=NOW)
    assert not watch.has_uncertain_send()
    assert watch.get_reservation(reservation.reservation_id).status == "not_sent"


def test_newer_stream_discards_old_speak_and_preclick_checks_reservation(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event)
    items = [item("stable anchor"), item("new message"), item("next")]
    new_event = watch.observe(frame_signature="frame-3", items=items, observed_at=NOW).events[-1]
    assert watch.reserve_send(event.event_id, "decision-1", now=NOW) is None
    assert watch.get_event(event.event_id).reason == "newer_screen_discarded_old_speak"
    ready(watch, new_event, "decision-2")
    reservation = watch.reserve_send(new_event.event_id, "decision-2", now=NOW)
    watch.observe(frame_signature="frame-4", items=items + [item("newest")])
    with pytest.raises(WatchLedgerError, match="stream changed"):
        watch.mark_click_started(reservation.reservation_id, now=NOW)
    assert watch.mark_not_sent(reservation.reservation_id, reason="changed_before_click").status == "not_sent"


def test_changing_only_frame_decoration_keeps_current_event_eligible(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event)
    changed = watch.observe(frame_signature="clock-decoration-updated", items=[item("stable anchor"), item("new message")])
    assert changed.plan.status == "unchanged"
    assert watch.reserve_send(event.event_id, "decision-1", now=NOW) is not None


def test_database_scope_start_requirement_and_metadata_do_not_leak_text(tmp_path):
    path = tmp_path / "private.sqlite3"
    watch = WatchLedger(path, group_id=GROUP)
    with pytest.raises(WatchLedgerError, match="start_run"):
        watch.observe(frame_signature="x", items=[])
    with pytest.raises(WatchLedgerError, match="different group"):
        WatchLedger(path, group_id="10002")
    watch.start_run(now=NOW)
    appended(watch, "PRIVATE_VISIBLE_TEXT")
    assert "PRIVATE_VISIBLE_TEXT" not in json.dumps(watch.status())
    with pytest.raises(WatchLedgerError, match="timezone"):
        watch.start_run(now=datetime(2026, 9, 11))


def test_existing_other_tables_are_preserved(tmp_path):
    path = tmp_path / "shared.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE unrelated_private_data(value TEXT)")
        connection.execute("INSERT INTO unrelated_private_data VALUES('untouched')")
    watch = WatchLedger(path, group_id=GROUP)
    watch.start_run(now=NOW)
    with sqlite3.connect(path) as connection:
        assert connection.execute("SELECT value FROM unrelated_private_data").fetchone()[0] == "untouched"


def test_deferred_attribution_retries_the_same_event_without_new_arrival(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    deferred = watch.defer_attribution(event.event_id, "attribution_GroundingError", now=NOW + timedelta(seconds=2))
    assert deferred.state == "observed" and deferred.attribution_attempts == 1
    assert deferred.event_id == event.event_id and deferred.observed_at == event.observed_at
    assert deferred.attribution_audit[0].reason == "attribution_GroundingError"
    with pytest.raises(WatchLedgerError, match="matching current screen"):
        watch.bind_event(event.event_id, "unverified-retry")
    unchanged = watch.observe(frame_signature="frame-2", items=[item("stable anchor"), item("new message")])
    assert unchanged.events == []
    pending = watch.pending_attribution(items=[item("stable anchor"), item("new message")], frame_signature="frame-2", now=NOW + timedelta(seconds=3))
    assert len(pending) == 1 and pending[0].event_id == event.event_id
    assert pending[0].attribution_attempts == 1 and not pending[0].attribution_recheck_required
    assert watch.bind_event(event.event_id, "verified-source").state == "bound"
    assert watch.pending_attribution(items=[item("stable anchor"), item("new message")], frame_signature="frame-2", now=NOW + timedelta(seconds=4)) == []
    with sqlite3.connect(watch.path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM qq_watch_events").fetchone()[0] == 1


def test_pending_attribution_requires_entire_current_window_and_current_frame(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    watch.defer_attribution(event.event_id, "attribution_GroundingError", now=NOW)
    original = [item("stable anchor"), item("new message")]
    for items, signature in [([item("new message")], "frame-2"), (original, "unknown-frame"), (list(reversed(original)), "frame-2")]:
        assert watch.pending_attribution(items=items, frame_signature=signature, now=NOW + timedelta(seconds=3)) == []
    changed = [item("different anchor"), item("new message")]
    watch.observe(frame_signature="changed", items=changed)
    assert watch.pending_attribution(items=changed, frame_signature="changed", now=NOW + timedelta(seconds=3)) == []
    watch.observe(frame_signature="frame-2", items=original)
    pending = watch.pending_attribution(items=original, frame_signature="frame-2", now=NOW + timedelta(seconds=3))
    assert pending[0].event_id == event.event_id
    assert pending[0].stream_revision > event.stream_revision
    watch.bind_event(event.event_id, "verified-source")


def test_pending_attribution_requires_unique_visible_occurrence(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch, text="repeat", prefix=[item("anchor"), item("repeat")]).events[-1]
    watch.defer_attribution(event.event_id, "attribution_GroundingError", now=NOW)
    assert watch.pending_attribution(items=[item("anchor"), item("repeat"), item("repeat")], frame_signature="frame-2", now=NOW + timedelta(seconds=3)) == []


def test_pending_attribution_age_is_original_observation_not_retry_time(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    watch.defer_attribution(event.event_id, "attribution_GroundingError", now=NOW + timedelta(seconds=850))
    items = [item("stable anchor"), item("new message")]
    assert watch.pending_attribution(items=items, frame_signature="frame-2", now=NOW + timedelta(seconds=902)) == []
    assert watch.pending_attribution(items=items, frame_signature="frame-2", now=NOW) == []
    assert watch.pending_attribution(items=items, frame_signature="frame-2", now=NOW + timedelta(seconds=900))


def test_restart_never_automatically_retries_deferred_attribution(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    watch.defer_attribution(event.event_id, "attribution_GroundingError", now=NOW)
    watch.start_run(now=NOW + timedelta(seconds=3))
    items = [item("stable anchor"), item("new message")]
    assert watch.observe(frame_signature="frame-2", items=items).plan.status == "baseline"
    assert watch.pending_attribution(items=items, frame_signature="frame-2", now=NOW + timedelta(seconds=4)) == []
    stored = watch.get_event(event.event_id)
    assert stored.state == "ignored" and stored.reason == "attribution_GroundingError"
    assert stored.attribution_audit[0].action == "deferred"


def test_explicit_cross_run_legacy_repair_preserves_audit_and_requires_current_baseline(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    watch.mark_ignored(event.event_id, "attribution_GroundingError")
    old_run = watch.run_id
    watch.start_run(now=NOW + timedelta(seconds=3))
    restored = watch.recover_unbound_attribution_event(event.event_id, now=NOW + timedelta(seconds=4))
    assert restored.event_id == event.event_id and restored.screen_id == event.screen_id
    assert restored.observed_at == event.observed_at and restored.run_id == watch.run_id != old_run
    assert restored.attribution_audit[0].previous_run_id == old_run
    assert restored.attribution_audit[0].reason == "attribution_GroundingError"
    items = [item("stable anchor"), item("new message")]
    assert watch.pending_attribution(items=items, frame_signature="frame-2", now=NOW + timedelta(seconds=4)) == []
    with pytest.raises(WatchLedgerError, match="matching current screen"):
        watch.bind_event(event.event_id, "too-early")
    watch.observe(frame_signature="fresh-repair-frame", items=items)
    pending = watch.pending_attribution(items=items, frame_signature="fresh-repair-frame", now=NOW + timedelta(seconds=5))
    assert [entry.event_id for entry in pending] == [event.event_id]
    watch.bind_event(event.event_id, "verified-new-profile-evidence")


@pytest.mark.parametrize("state", ["ordinary_ignored", "bound", "ready", "reserved", "click_started", "uncertain", "sent", "not_sent", "expired"])
def test_repair_never_recovers_other_ignored_work_or_any_decision_send(tmp_path, state):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    moment = NOW + timedelta(seconds=4)
    if state in {"ordinary_ignored", "expired"}:
        watch.mark_ignored(event.event_id, "other_reason" if state == "ordinary_ignored" else "attribution_GroundingError")
        if state == "expired":
            moment += timedelta(seconds=901)
    else:
        watch.bind_event(event.event_id, "source")
        if state != "bound":
            watch.attach_decision(event.event_id, "d", "speak", new_decision=True)
        if state not in {"bound", "ready"}:
            reservation = watch.reserve_send(event.event_id, "d", now=NOW)
            if state in {"click_started", "uncertain", "sent"}:
                watch.mark_click_started(reservation.reservation_id, now=NOW)
            if state == "uncertain":
                watch.mark_uncertain(reservation.reservation_id, reason="unknown", now=NOW)
            elif state == "sent":
                watch.mark_sent(reservation.reservation_id, evidence_refs=["test://bubble"], now=NOW)
            elif state == "not_sent":
                watch.mark_not_sent(reservation.reservation_id, reason="no-click", now=NOW)
        elif state == "bound":
            watch.mark_ignored(event.event_id, "attribution_GroundingError")
    with pytest.raises(WatchLedgerError, match="explicitly recovered"):
        watch.recover_unbound_attribution_event(event.event_id, now=moment)
    with pytest.raises(WatchLedgerError, match="defer attribution"):
        watch.defer_attribution(event.event_id, "attribution_again", now=moment)


def test_old_watch_event_json_parses_attribution_defaults(tmp_path):
    from harness.qq.watch_ledger import WatchEvent

    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    old = event.model_dump(exclude={"attribution_attempts", "attribution_recheck_required", "attribution_audit"})
    parsed = WatchEvent.model_validate(old)
    assert parsed.attribution_attempts == 0 and parsed.attribution_audit == []


def test_first_seen_deadline_is_not_renewed_by_later_capture_or_retry(tmp_path):
    watch = ledger(tmp_path)
    watch.observe(frame_signature="initial", items=[item("anchor")], observed_at=NOW)
    event = watch.observe(frame_signature="later-capture", items=[item("anchor"), item("new")],
                          observed_at=NOW + timedelta(seconds=25), first_seen_at=NOW).events[-1]
    ready(watch, event)
    reservation = watch.reserve_send(event.event_id, "decision-1", now=NOW + timedelta(seconds=30))
    assert reservation.deadline_at == NOW + timedelta(seconds=60)
    watch.start_send_attempt(reservation.reservation_id, now=NOW + timedelta(seconds=31))
    watch.defer_preclick(reservation.reservation_id, reason="transient_geometry", now=NOW + timedelta(seconds=35))
    assert watch.get_send(reservation.reservation_id).deadline_at == reservation.deadline_at
    assert watch.pending_preclick(now=NOW + timedelta(seconds=60)) == []
    assert watch.get_send(reservation.reservation_id).status == "expired"
    assert watch.get_event(event.event_id).first_seen_at == NOW
    assert watch.next_allowed_at() is None


def test_expired_event_cannot_create_new_intent_from_fresh_decision(tmp_path):
    watch = ledger(tmp_path)
    event = appended(watch).events[-1]
    ready(watch, event)
    assert watch.reserve_send(event.event_id, "decision-1", now=NOW + timedelta(seconds=61)) is None
    assert watch.get_event(event.event_id).state == "expired"
    assert watch.unsettled_sends() == []


def test_preclick_failures_allow_exactly_two_retries_same_intent_without_cooldown(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    assert watch.next_allowed_at() is None
    for number, seconds in [(1, 0), (2, 4), (3, 8)]:
        attempt = watch.start_send_attempt(reservation.reservation_id, now=NOW + timedelta(seconds=seconds))
        assert attempt.attempt_number == number
        result = watch.defer_preclick(reservation.reservation_id, reason="verified_frame_unavailable", now=NOW + timedelta(seconds=seconds + 1))
        assert result.reservation_id == reservation.reservation_id
        assert watch.next_allowed_at(priority="direct") is None
        if number < 3:
            assert result.status == "retry_wait"
            assert watch.pending_preclick(now=NOW + timedelta(seconds=seconds + 2)) == []
            assert watch.pending_preclick(now=NOW + timedelta(seconds=seconds + 3))[0].reservation_id == reservation.reservation_id
        else:
            assert result.status == "not_sent"
    assert [a.phase for a in watch.delivery_attempts(reservation.reservation_id)] == ["precheck_failed"] * 3
    with pytest.raises(WatchLedgerError, match="cannot start"):
        watch.start_send_attempt(reservation.reservation_id, now=NOW + timedelta(seconds=12))
    assert watch.unsettled_sends() == []


def test_retry_mutex_is_independent_of_cooldown_and_explicit_cancel_releases_it(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    watch.defer_preclick(reservation.reservation_id, reason="temporary", now=NOW)
    assert watch.next_allowed_at() is None
    assert len(watch.unsettled_sends()) == 1
    cancelled = watch.supersede_send(reservation.reservation_id, reason="new_message", now=NOW)
    assert cancelled.status == "superseded" and watch.unsettled_sends() == []
    assert watch.pending_preclick(now=NOW + timedelta(seconds=3)) == []
    with pytest.raises(WatchLedgerError, match="state transition"):
        watch.mark_click_started(reservation.reservation_id, now=NOW)


@pytest.mark.parametrize("priority,interval", [("direct", 10), ("ambient", 60)])
def test_cooldown_is_from_actual_activity_and_candidate_priority(tmp_path, priority, interval):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    assert watch.status()["cooldown_until"] is None
    watch.mark_click_started(reservation.reservation_id, now=NOW + timedelta(seconds=2))
    assert watch.next_allowed_at(priority=priority) == NOW + timedelta(seconds=2 + interval)
    watch.mark_sent(reservation.reservation_id, evidence_refs=["test://after"], now=NOW + timedelta(seconds=3))
    assert watch.next_allowed_at(priority=priority) == NOW + timedelta(seconds=2 + interval)
    watch.start_run(now=NOW + timedelta(seconds=4))
    assert watch.next_allowed_at(priority=priority) == NOW + timedelta(seconds=2 + interval)
    event = appended(watch, moment=NOW + timedelta(seconds=8)).events[-1]
    ready(watch, event, "next-decision")
    assert watch.reserve_send(event.event_id, "next-decision", priority=priority, now=NOW + timedelta(seconds=11)) is None
    assert watch.get_event(event.event_id).state == "rate_limited"
    assert watch.reserve_send(event.event_id, "next-decision", priority=priority, now=NOW + timedelta(seconds=50)) is None


def test_direct_can_send_ten_seconds_after_ambient_despite_ambient_sixty_seconds(tmp_path):
    watch = ledger(tmp_path)
    first = reserved(watch)
    watch.mark_click_started(first.reservation_id, now=NOW)
    watch.mark_sent(first.reservation_id, evidence_refs=["test://after"], now=NOW)
    watch.start_run(now=NOW + timedelta(seconds=10))
    event = appended(watch, moment=NOW + timedelta(seconds=10)).events[-1]
    ready(watch, event, "direct-new")
    reservation = watch.reserve_send(event.event_id, "direct-new", priority="direct", now=NOW + timedelta(seconds=11))
    assert reservation is not None and reservation.cooldown_seconds == 10
    with pytest.raises(WatchLedgerError, match="policy"):
        watch.reserve_send(event.event_id, "direct-new", priority="ambient", cooldown_seconds=0, now=NOW)


@pytest.mark.parametrize("change", [
    {"group_id": "10002"}, {"self_member_id": "29998"}, {"reply_sha256": "0" * 64},
    {"verified_at": NOW - timedelta(seconds=16)}, {"verified_at": NOW + timedelta(seconds=3)},
    {"source_record_ids": ["wrong-source"]},
])
def test_preclick_proof_must_bind_original_group_self_reply_source_and_fresh_time(tmp_path, change):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    with pytest.raises(WatchLedgerError, match="proof does not match"):
        watch.mark_click_started(reservation.reservation_id, proof=click_proof(**change), now=NOW)
    assert watch.get_send(reservation.reservation_id).status == "reserved"
    assert watch.next_allowed_at() is None


def test_explicit_attempt_persists_proof_and_cannot_retry_after_click(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    proof = click_proof(window_id="0x123", body_instance_id="synthetic-body", environment_revision=2,
                        title_pixels_sha256="a" * 64, ordered_baseline_signatures=["anchor-signature"],
                        source_record_ids=[watch.get_event(reservation.event_id).source_record_id])
    clicked = watch.mark_click_started(reservation.reservation_id, proof=proof, now=NOW)
    assert clicked.preclick_proof.window_id == "0x123"
    attempt = watch.delivery_attempts(reservation.reservation_id)[0]
    assert attempt.phase == "click_started" and attempt.proof == clicked.preclick_proof
    with pytest.raises(WatchLedgerError, match="only an active pre-click"):
        watch.defer_preclick(reservation.reservation_id, reason="retry", now=NOW)
    with pytest.raises(WatchLedgerError, match="already started"):
        watch.mark_click_started(reservation.reservation_id, now=NOW)


def receipt_for(watch, reservation, **changes):
    from harness.qq.decision import QQDeliveryReceipt

    values = dict(receipt_id="synthetic-receipt", group_id=GROUP, self_member_id=SELF,
                  decision_id=reservation.decision_id, delivered_text=REPLY, evidence_refs=["test://after"],
                  verification="qq_ui_verified", origin="decision", delivered_at=NOW + timedelta(seconds=3),
                  source_record_ids=[watch.get_event(reservation.event_id).source_record_id])
    return QQDeliveryReceipt.model_validate(values | changes)


def test_existing_verified_receipt_reconciles_cross_run_uncertainty_idempotently(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    watch.start_run(now=NOW + timedelta(seconds=4))
    assert watch.has_uncertain_send()
    receipt = receipt_for(watch, reservation)
    completed = watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=NOW + timedelta(seconds=5))
    assert completed.status == "sent" and completed.receipt_id == receipt.receipt_id
    assert not watch.has_uncertain_send() and watch.unsettled_sends() == []
    assert watch.delivery_attempts(reservation.reservation_id)[0].phase == "sent"
    assert watch.next_allowed_at(priority="direct") == NOW + timedelta(seconds=10)
    assert watch.reconcile_sent(reservation.reservation_id, receipt=receipt.model_dump(), now=NOW + timedelta(hours=1)) == completed
    assert watch.next_allowed_at(priority="direct") == NOW + timedelta(seconds=10)
    with pytest.raises(WatchLedgerError):
        watch.mark_click_started(reservation.reservation_id, now=NOW)


@pytest.mark.parametrize("method", ["mark_sent", "reconcile_sent", "reconcile_after_restart"])
@pytest.mark.parametrize("priority,interval", [("direct", 10), ("ambient", 60)])
def test_delayed_confirmation_never_renews_actual_click_cooldown(tmp_path, method, priority, interval):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    clicked_at = NOW + timedelta(seconds=2)
    clicked = watch.mark_click_started(reservation.reservation_id, now=clicked_at)
    assert clicked.click_started_at == clicked_at
    original_activity = watch.status()["last_actual_activity_at"]
    if method == "reconcile_after_restart":
        watch.start_run(now=NOW + timedelta(seconds=5))
    if method == "mark_sent":
        completed = watch.mark_sent(reservation.reservation_id, evidence_refs=["test://after"], now=NOW + timedelta(seconds=32))
    else:
        receipt = receipt_for(watch, reservation, delivered_at=NOW + timedelta(seconds=32))
        completed = watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=NOW + timedelta(seconds=33))
    assert completed.click_started_at == clicked_at
    assert watch.status()["last_actual_activity_at"] == original_activity
    assert watch.next_allowed_at(priority=priority) == clicked_at + timedelta(seconds=interval)


@pytest.mark.parametrize("keep_activity", [True, False])
def test_legacy_missing_click_clock_does_not_invent_one_from_confirmation(tmp_path, keep_activity):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    watch.mark_uncertain(reservation.reservation_id, reason="legacy_unknown", now=NOW)
    with sqlite3.connect(watch.path) as connection:
        record = json.loads(connection.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0])
        record.pop("click_started_at")
        connection.execute("UPDATE qq_watch_sends SET record_json=?", (json.dumps(record),))
        if not keep_activity:
            connection.execute("UPDATE qq_watch_state SET last_actual_activity_at=NULL, cooldown_until=NULL")
    before = watch.status()
    receipt = receipt_for(watch, reservation, delivered_at=NOW + timedelta(seconds=30))
    completed = watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=NOW + timedelta(seconds=31))
    assert completed.click_started_at is None
    assert watch.status()["last_actual_activity_at"] == before["last_actual_activity_at"]
    assert watch.status()["cooldown_until"] == before["cooldown_until"]
    assert watch.next_allowed_at(priority="direct") == (NOW + timedelta(seconds=10) if keep_activity else None)


def test_reconciling_an_older_click_preserves_the_newer_activity_clock(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    later = NOW + timedelta(seconds=20)
    with sqlite3.connect(watch.path) as connection:
        connection.execute("UPDATE qq_watch_state SET last_actual_activity_at=?,cooldown_until=?", (later.isoformat(), (later + timedelta(seconds=10)).isoformat()))
    before = watch.status()
    receipt = receipt_for(watch, reservation, delivered_at=NOW + timedelta(seconds=30))
    watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=NOW + timedelta(seconds=31))
    assert watch.status()["last_actual_activity_at"] == before["last_actual_activity_at"]
    assert watch.status()["cooldown_until"] == before["cooldown_until"]
    assert watch.next_allowed_at(priority="direct") == later + timedelta(seconds=10)


@pytest.mark.parametrize("change", [
    {"group_id": "10002"}, {"self_member_id": "29998"}, {"decision_id": "different"},
    {"delivered_text": "different reply"}, {"source_record_ids": []},
    {"source_record_ids": ["unrelated"]}, {"delivered_at": NOW - timedelta(seconds=1)},
])
def test_unrelated_receipt_never_resolves_uncertainty(tmp_path, change):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    watch.mark_uncertain(reservation.reservation_id, reason="transport_unknown", now=NOW)
    with pytest.raises(WatchLedgerError, match="receipt does not match"):
        watch.reconcile_sent(reservation.reservation_id, receipt=receipt_for(watch, reservation, **change), now=NOW + timedelta(seconds=5))
    assert watch.has_uncertain_send()


def test_not_sent_history_cannot_be_rewritten_by_receipt(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_not_sent(reservation.reservation_id, reason="no_click", now=NOW)
    with pytest.raises(WatchLedgerError, match="possibly sent"):
        watch.reconcile_sent(reservation.reservation_id, receipt=receipt_for(watch, reservation), now=NOW + timedelta(seconds=4))
    assert watch.get_send(reservation.reservation_id).status == "not_sent"


def test_incremental_migration_preserves_legacy_record_and_conservative_cooldown(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_not_sent(reservation.reservation_id, reason="legacy_no_click", now=NOW)
    with sqlite3.connect(watch.path) as connection:
        row = connection.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0]
        legacy = json.loads(row)
        for field in ("priority", "self_member_id", "reply_sha256", "deadline_at", "attempt_count", "next_retry_at", "click_started_at", "preclick_proof", "receipt_id", "reconciled_at"):
            legacy.pop(field)
        legacy_json = json.dumps(legacy)
        connection.execute("UPDATE qq_watch_sends SET record_json=?", (legacy_json,))
        connection.execute("ALTER TABLE qq_watch_state DROP COLUMN last_actual_activity_at")
        connection.execute("ALTER TABLE qq_watch_state DROP COLUMN legacy_cooldown_until")
        connection.execute("UPDATE qq_watch_state SET cooldown_until=?", ((NOW + timedelta(seconds=60)).isoformat(),))
    migrated = WatchLedger(watch.path, group_id=GROUP)
    migrated.start_run(now=NOW + timedelta(seconds=5))
    assert migrated.get_send(reservation.reservation_id).status == "not_sent"
    assert migrated.next_allowed_at(priority="direct") == NOW + timedelta(seconds=60)
    with sqlite3.connect(watch.path) as connection:
        assert connection.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0] == legacy_json


def test_new_stream_supersedes_preclick_retry_without_clicking_or_new_intent(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    watch.defer_preclick(reservation.reservation_id, reason="temporary", now=NOW)
    watch.observe(frame_signature="new-context", items=[item("stable anchor"), item("new message"), item("newer")], observed_at=NOW + timedelta(seconds=3))
    assert watch.pending_preclick(now=NOW + timedelta(seconds=4)) == []
    assert watch.get_send(reservation.reservation_id).status == "superseded"
    assert watch.next_allowed_at() is None


def test_restart_does_not_resume_preclick_retry_or_renew_deadline(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    watch.defer_preclick(reservation.reservation_id, reason="temporary", now=NOW)
    watch.start_run(now=NOW + timedelta(seconds=5))
    assert watch.pending_preclick(now=NOW + timedelta(seconds=6)) == []
    old = watch.get_send(reservation.reservation_id)
    assert old.status == "uncertain" and old.deadline_at == reservation.deadline_at
    with pytest.raises(WatchLedgerError, match="cannot start"):
        watch.start_send_attempt(reservation.reservation_id, now=NOW + timedelta(seconds=6))
