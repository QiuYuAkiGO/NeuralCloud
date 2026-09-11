"""Post-click diagnostic evidence never becomes delivery or retry authority."""

from datetime import timedelta
import sqlite3
from types import SimpleNamespace

import pytest

from harness.qq import watcher as module
from harness.qq.decision import QQDeliveryReceipt
from harness.qq.watch_ledger import SendReservation, WatchLedgerError
from tests.test_qq_watch_ledger import NOW, WatchLedger, ledger, receipt_for, reserved
from tests.test_qq_watcher import setup as setup  # noqa: F401 - scenario dependency
from tests.test_qq_watcher_unattended import scenario as scenario


def test_postclick_refs_persist_without_mutating_proof_receipt_clock_or_attempts(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    watch.mark_uncertain(reservation.reservation_id, reason="synthetic_unknown", now=NOW)
    original = watch.get_send(reservation.reservation_id)
    state, attempts = watch.status(), watch.delivery_attempts(reservation.reservation_id)
    result = watch.record_postclick_evidence(
        reservation.reservation_id, evidence_refs=["test://initial", "test://initial", "test://reconcile-1"]
    )
    result = watch.record_postclick_evidence(
        reservation.reservation_id, evidence_refs=["test://reconcile-1", "test://reconcile-2"]
    )
    assert result.postclick_evidence_refs == ["test://initial", "test://reconcile-1", "test://reconcile-2"]
    assert result.model_copy(update={"postclick_evidence_refs": []}) == original
    assert watch.status() == state and watch.delivery_attempts(reservation.reservation_id) == attempts
    reopened = WatchLedger(watch.path, group_id=watch.group_id)
    assert reopened.get_send(reservation.reservation_id) == result
    with pytest.raises(WatchLedgerError):
        watch.start_send_attempt(reservation.reservation_id, now=NOW)


@pytest.mark.parametrize("completion", ["mark_sent", "reconcile_sent"])
def test_successful_receipt_refs_stay_exact_and_diagnostics_remain_separate(tmp_path, completion):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    watch.record_postclick_evidence(
        reservation.reservation_id, evidence_refs=["test://uncertain", "test://wrong-scene"]
    )
    watch.record_postclick_evidence(reservation.reservation_id, evidence_refs=["test://verified-after"])
    if completion == "mark_sent":
        ended = watch.mark_sent(reservation.reservation_id, evidence_refs=["test://verified-after"], now=NOW)
    else:
        watch.mark_uncertain(reservation.reservation_id, reason="synthetic_unknown", now=NOW)
        receipt = receipt_for(watch, reservation, evidence_refs=["test://verified-after"])
        ended = watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=NOW + timedelta(seconds=3))
    assert ended.evidence_refs == ["test://verified-after"]
    assert ended.postclick_evidence_refs == ["test://uncertain", "test://verified-after", "test://wrong-scene"]
    with pytest.raises(WatchLedgerError):
        watch.record_postclick_evidence(reservation.reservation_id, evidence_refs=["test://late"])


@pytest.mark.parametrize(
    "bad", [None, "test://not-list", ("test://tuple",), [None], [""], [" "], ["x" * 2049], ["test://same"] * 33]
)
def test_invalid_diagnostic_batch_is_rejected_atomically(tmp_path, bad):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    before = watch.mark_click_started(reservation.reservation_id, now=NOW)
    with pytest.raises(WatchLedgerError):
        watch.record_postclick_evidence(reservation.reservation_id, evidence_refs=bad)
    assert watch.get_send(reservation.reservation_id) == before


def test_total_cap_deduplicates_and_rejects_overflow_without_dropping_prior_evidence(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW)
    for offset in range(0, 128, 32):
        result = watch.record_postclick_evidence(
            reservation.reservation_id, evidence_refs=[f"test://frame-{index}" for index in range(offset, offset + 32)]
        )
    assert len(result.postclick_evidence_refs) == 128
    assert watch.record_postclick_evidence(reservation.reservation_id, evidence_refs=["test://frame-0"] * 32) == result
    with pytest.raises(WatchLedgerError, match="limit exceeded"):
        watch.record_postclick_evidence(reservation.reservation_id, evidence_refs=["test://overflow"])
    assert watch.get_send(reservation.reservation_id) == result


@pytest.mark.parametrize("phase", ["reserved", "uncertain_without_click", "previous_run"])
def test_preclick_and_previous_run_intents_cannot_acquire_postclick_diagnostics(tmp_path, phase):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    if phase == "uncertain_without_click":
        watch.mark_uncertain(reservation.reservation_id, reason="synthetic_unknown", now=NOW)
    elif phase == "previous_run":
        watch.mark_click_started(reservation.reservation_id, now=NOW)
        watch.start_run(now=NOW + timedelta(seconds=1))
    before = watch.get_send(reservation.reservation_id)
    with pytest.raises(WatchLedgerError):
        watch.record_postclick_evidence(reservation.reservation_id, evidence_refs=["test://historical"])
    assert watch.get_send(reservation.reservation_id) == before


def test_legacy_json_defaults_to_no_diagnostics_without_backfill(tmp_path):
    watch = ledger(tmp_path)
    reservation = reserved(watch)
    raw = reservation.model_dump()
    raw.pop("postclick_evidence_refs")
    assert SendReservation.model_validate(raw).postclick_evidence_refs == []


@pytest.mark.parametrize("successful_read", [None, 1, 3])
def test_watcher_retains_initial_unknown_and_each_read_including_final_success(scenario, monkeypatch, successful_read):
    state = scenario
    state.callback = lambda _: state.answer("speak", priority="direct", reply="fictional answer")
    monkeypatch.setattr(module.time, "sleep", state.clock.advance, raising=False)
    monkeypatch.setattr(module.QQDeadline, "after", classmethod(lambda cls, seconds: cls(state.clock.mono() + seconds)))

    def record_receipt(**kwargs):
        state.receipts.append(kwargs)
        return QQDeliveryReceipt.model_validate(
            {
                **{key: value for key, value in kwargs.items() if key != "memory_db"},
                "receipt_id": "synthetic-receipt",
                "origin": "decision",
            }
        )

    monkeypatch.setattr(module, "record_qq_delivery", record_receipt)
    original_delivery = state.desktop.deliver
    read_refs = []

    def delivery(*args, **kwargs):
        result = original_delivery(*args, **kwargs)
        result.status = "unknown"
        result.evidence_refs = ["test://initial-unknown"]
        return result

    def reconcile(_text, *, reservation_id, proof):
        ref = f"test://reconcile-{len(read_refs) + 1}"
        read_refs.append(ref)
        return SimpleNamespace(
            status="sent" if len(read_refs) == successful_read else "unknown",
            evidence_refs=[ref],
            reason_code="synthetic_read",
            delivered_at=state.clock.wall(),
        )

    state.desktop.deliver, state.desktop.reconcile_delivery = delivery, reconcile
    state.append("fictional direct question")
    state.watcher.cycle()
    with sqlite3.connect(state.watcher.settings.state_db) as connection:
        identifier = connection.execute("SELECT reservation_id FROM qq_watch_sends").fetchone()[0]
    result = state.watcher.ledger.get_send(identifier)
    assert result.postclick_evidence_refs == ["test://initial-unknown", *read_refs]
    assert state.desktop.clicks == state.desktop.sends == 1
    if successful_read is None:
        assert len(read_refs) == 3 and result.status == "uncertain"
        assert result.evidence_refs == [] and not state.receipts
    else:
        assert len(read_refs) == successful_read and result.status == "sent"
        assert result.evidence_refs == [read_refs[-1]]
        assert len(state.receipts) == 1 and state.receipts[0]["evidence_refs"] == [read_refs[-1]]
