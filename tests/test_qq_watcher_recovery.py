"""Automatic recovery with synthetic GUI observations and real ledger transitions."""
from datetime import timedelta
from types import SimpleNamespace as NS

import pytest

from harness.qq.decision import QQDeliveryReceipt
from harness.qq.watcher import QQWatcher
from tests.test_qq_watcher import NOW, setup as setup


def receipt(**kwargs):
    return QQDeliveryReceipt(receipt_id="synthetic-recovery-receipt", origin="decision",
                             **{key: value for key, value in kwargs.items() if key != "memory_db"})


def test_preclick_retry_keeps_same_decision_and_clicks_once(setup, monkeypatch):
    settings, desktop, decide, calls, receipts = setup
    actual = desktop.deliver

    def transient(*args, **kwargs):
        result = actual(*args, **kwargs)
        result.phase, result.retryable = "precheck", True
        return result

    desktop.deliver = transient
    desktop.result = "not_sent"
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    watcher.cycle()
    pending = watcher._pending_turn
    reservation_id = pending.reservation_id
    assert watcher.ledger.get_send(reservation_id).status == "retry_wait"
    assert watcher.ledger.next_allowed_at(priority="direct") is None
    monkeypatch.setattr("harness.qq.watcher.now", lambda: NOW + timedelta(seconds=6))
    desktop.result = "sent"
    watcher.cycle()
    assert len(calls) == len(receipts) == desktop.clicks == 1
    assert desktop.sends == 2
    assert watcher.ledger.get_send(reservation_id).status == "sent"
    assert len(watcher.ledger.delivery_attempts(reservation_id)) == 2


def test_uncertain_click_uses_read_only_reconciliation_without_another_click(setup, monkeypatch):
    settings, desktop, decide, calls, _ = setup
    desktop.result = "unknown"
    reads = []

    def reconcile(text, *, reservation_id, proof):
        reads.append(reservation_id)
        assert proof["reply_sha256"] == desktop.last_preclick_proof["reply_sha256"]
        return NS(status="sent", evidence_refs=["test-evidence://new-confirmed-bubble"],
                  delivered_at=NOW + timedelta(seconds=5))

    desktop.reconcile_delivery = reconcile
    monkeypatch.setattr("harness.qq.watcher.record_qq_delivery", receipt)
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    watcher.cycle()
    assert len(calls) == len(reads) == desktop.clicks == desktop.sends == 1
    assert not watcher.ledger.has_uncertain_send()
    assert watcher.status["automatic_send"] is True
    assert watcher.status["last_turn_outcome"] == "sent"


@pytest.mark.parametrize("interrupted_after_write", [True, False])
def test_receipt_write_interruption_never_reissues_click(setup, monkeypatch, interrupted_after_write):
    settings, desktop, decide, _, _ = setup
    durable = []

    def persist(**kwargs):
        if interrupted_after_write:
            durable.append(receipt(**kwargs))
        raise OSError("synthetic-write-interruption")

    monkeypatch.setattr("harness.qq.watcher.record_qq_delivery", persist)
    monkeypatch.setattr("harness.qq.watcher.get_qq_delivery", lambda **_: durable[0] if durable else None)
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    if interrupted_after_write:
        watcher.cycle()
        assert watcher.status["last_turn_outcome"] == "sent"
        assert watcher.status["automatic_send"] is True
    else:
        with pytest.raises(OSError):
            watcher.cycle()
    assert watcher.ledger.has_uncertain_send() is not interrupted_after_write
    assert desktop.sends == desktop.clicks == 1
