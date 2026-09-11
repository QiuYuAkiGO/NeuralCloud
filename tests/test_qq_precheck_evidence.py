"""Pre-click failure evidence survives retries without becoming a receipt."""
from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pytest

from harness.qq.watch_ledger import DeliveryAttempt, SendReservation, WatchLedgerError
from tests.test_qq_watch_ledger import NOW, WatchLedger, ledger, receipt_for, reserved
from tests.test_qq_watcher import setup as setup
from tests.test_qq_watcher_unattended import scenario as scenario


def test_precheck_refs_accumulate_per_reservation_but_attempts_keep_own_frames(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    refs = [["test://frame-a", "test://frame-a"], ["test://frame-b", "test://frame-a"], ["test://frame-c"]]
    for index, evidence in enumerate(refs):
        moment = NOW + timedelta(seconds=index * 4)
        watch.start_send_attempt(reservation.reservation_id, now=moment)
        result = watch.defer_preclick(reservation.reservation_id, reason="synthetic_geometry",
                                      evidence_refs=evidence, now=moment)
        assert result.attempt_count == index + 1
        assert result.status == ("retry_wait" if index < 2 else "not_sent")
        assert result.reason == "synthetic_geometry"
    expected = ["test://frame-a", "test://frame-b", "test://frame-c"]
    assert result.evidence_refs == result.precheck_evidence_refs == expected
    assert watch.next_allowed_at() is None
    reopened = WatchLedger(watch.path, group_id=watch.group_id)
    assert reopened.get_send(reservation.reservation_id).precheck_evidence_refs == expected
    attempts = reopened.delivery_attempts(reservation.reservation_id)
    assert [a.evidence_refs for a in attempts] == [sorted(set(values)) for values in refs]
    assert [a.phase for a in attempts] == ["precheck_failed"] * 3


@pytest.mark.parametrize("completion", ["mark_sent", "reconcile_sent"])
def test_success_keeps_failed_prechecks_separate_from_exact_receipt_evidence(tmp_path, completion):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    watch.defer_preclick(reservation.reservation_id, reason="synthetic_geometry", evidence_refs=["test://failed-frame"], now=NOW)
    moment = NOW + timedelta(seconds=3)
    watch.start_send_attempt(reservation.reservation_id, now=moment)
    watch.mark_click_started(reservation.reservation_id, now=moment)
    if completion == "mark_sent":
        completed = watch.mark_sent(reservation.reservation_id, evidence_refs=["test://after"], now=moment)
    else:
        receipt = receipt_for(watch, reservation, delivered_at=moment)
        completed = watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=moment)
    assert completed.evidence_refs == ["test://after"]
    assert completed.precheck_evidence_refs == ["test://failed-frame"]
    attempts = watch.delivery_attempts(reservation.reservation_id)
    assert attempts[0].evidence_refs == ["test://failed-frame"]
    assert attempts[1].evidence_refs == []
    assert [a.phase for a in attempts] == ["precheck_failed", "sent"]


@pytest.mark.parametrize("bad", ["test://not-a-list", ("test://tuple",), [None], [""], [" "], ["x" * 2049], ["test://a"] * 33])
def test_invalid_refs_reject_atomically_using_existing_identifier_bounds(tmp_path, bad):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    with pytest.raises(WatchLedgerError):
        watch.defer_preclick(reservation.reservation_id, reason="synthetic_failure", evidence_refs=bad, now=NOW)
    assert watch.get_send(reservation.reservation_id).status == "reserved"
    attempt = watch.delivery_attempts(reservation.reservation_id)[0]
    assert attempt.phase == "prechecking" and attempt.evidence_refs == []


def test_expiry_retains_frame_without_changing_terminal_reason(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.start_send_attempt(reservation.reservation_id, now=NOW)
    ended = watch.defer_preclick(reservation.reservation_id, reason="synthetic_slow_precheck",
        evidence_refs=["test://last-frame"], now=reservation.deadline_at)
    assert ended.status == "expired" and ended.reason == "event_deadline_expired"
    assert ended.precheck_evidence_refs == ["test://last-frame"]
    attempt = watch.delivery_attempts(reservation.reservation_id)[0]
    assert attempt.reason == "synthetic_slow_precheck" and attempt.evidence_refs == ended.evidence_refs


def test_legacy_records_and_calls_default_to_empty_failure_evidence(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    attempt = watch.start_send_attempt(reservation.reservation_id, now=NOW)
    old_reservation = reservation.model_dump()
    old_reservation.pop("precheck_evidence_refs")
    old_attempt = attempt.model_dump()
    old_attempt.pop("evidence_refs")
    assert SendReservation.model_validate(old_reservation).precheck_evidence_refs == []
    assert DeliveryAttempt.model_validate(old_attempt).evidence_refs == []
    result = watch.defer_preclick(reservation.reservation_id, reason="legacy_failure", now=NOW)
    assert result.precheck_evidence_refs == result.evidence_refs == []


@pytest.mark.parametrize("status,reason", [("not_sent", "synthetic_precheck"), ("unknown", "callback_not_committed")])
def test_watcher_passes_result_refs_even_when_callback_did_not_commit(scenario, status, reason):
    s = scenario
    s.callback = lambda _: s.answer("speak", priority="direct", reply="fictional reply")

    def deliver(*args, **kwargs):
        return SimpleNamespace(status=status, evidence_refs=["test://failure-frame", "test://failure-frame"],
                               reason_code="synthetic_precheck", retryable=False, phase="precheck")

    s.desktop.deliver = deliver
    s.append("fictional direct question")
    s.watcher.cycle()
    with sqlite3.connect(s.watcher.settings.state_db) as connection:
        ident = connection.execute("SELECT reservation_id FROM qq_watch_sends").fetchone()[0]
    reservation = s.watcher.ledger.get_send(ident)
    assert reservation.status == "not_sent" and reservation.reason == reason
    assert reservation.evidence_refs == reservation.precheck_evidence_refs == ["test://failure-frame"]
    attempt = s.watcher.ledger.delivery_attempts(ident)[0]
    assert attempt.evidence_refs == ["test://failure-frame"]
    assert s.desktop.clicks == 0 and not s.receipts
