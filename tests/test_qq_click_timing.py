"""Synthetic response clocks and temporary ledgers; no desktop or live API."""
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.qq.watch_ledger import WatchLedgerError
from harness.qq.watcher import QQWatcher
from tests.test_qq_desktop import (
    NOW as DESKTOP_NOW, TEXT, backend as backend, delivery_setup, frame, message, screen,
)
from tests.test_qq_watch_ledger import NOW, ledger, receipt_for, reserved
from tests.test_qq_watcher import NOW as WATCHER_NOW, setup as setup


@pytest.fixture
def local_clock(monkeypatch):
    current = [DESKTOP_NOW]

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0] if tz is not None else current[0].replace(tzinfo=None)

    monkeypatch.setattr("harness.qq.desktop.datetime", Clock)
    return current


def started(watch):
    reservation = reserved(watch)
    watch.mark_click_started(reservation.reservation_id, now=NOW + timedelta(seconds=2),
                             require_click_timing=True)
    return reservation


@pytest.mark.parametrize("source", ["acknowledged", "observed"])
@pytest.mark.parametrize("priority,seconds", [("direct", 10), ("ambient", 60)])
def test_cadence_uses_first_local_upper_bound_and_receipts_never_renew_it(tmp_path, source, priority, seconds):
    watch = ledger(tmp_path)
    reservation = started(watch)
    assert watch.next_allowed_at(priority=priority) is None
    bound = NOW + timedelta(seconds=7)
    method = getattr(watch, "mark_click_" + source)
    arguments = {source + "_at": bound}
    if source == "observed":
        arguments["evidence_ref"] = "test://fresh-capture"
    stored = method(reservation.reservation_id, **arguments, now=bound)
    assert getattr(stored, "click_" + source + "_at") == bound
    assert watch.next_allowed_at(priority=priority) == bound + timedelta(seconds=seconds)
    later = bound + timedelta(seconds=20)
    arguments[source + "_at"] = later
    assert method(reservation.reservation_id, **arguments, now=later) == stored
    watch.start_run(now=later)
    receipt = receipt_for(watch, reservation, delivered_at=later)
    completed = watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=later)
    assert getattr(completed, "click_" + source + "_at") == bound
    assert watch.next_allowed_at(priority=priority) == bound + timedelta(seconds=seconds)
    assert watch.reconcile_sent(reservation.reservation_id, receipt=receipt, now=later + timedelta(hours=1)) == completed
    assert watch.next_allowed_at(priority=priority) == bound + timedelta(seconds=seconds)


@pytest.mark.parametrize("method", ["mark_sent", "reconcile_sent"])
def test_missing_ack_and_visual_bound_cannot_complete_even_with_matching_receipt(tmp_path, method):
    watch = ledger(tmp_path)
    reservation = started(watch)
    arguments = ({"receipt": receipt_for(watch, reservation)} if method == "reconcile_sent"
                 else {"evidence_refs": ["test://after"]})
    with pytest.raises(WatchLedgerError, match="upper bound is required"):
        getattr(watch, method)(reservation.reservation_id, **arguments, now=NOW + timedelta(seconds=10))
    watch.mark_uncertain(reservation.reservation_id, reason="missing_ack", now=NOW + timedelta(seconds=10))
    assert watch.has_uncertain_send()
    assert watch.get_send(reservation.reservation_id).click_acknowledged_at is None


@pytest.mark.parametrize("invalid", [None, "2026-09-11", NOW.replace(tzinfo=None), NOW,
                                    NOW + timedelta(seconds=100)])
def test_bound_is_explicit_aware_after_attempt_and_not_in_future(tmp_path, invalid):
    watch = ledger(tmp_path)
    reservation = started(watch)
    with pytest.raises(WatchLedgerError):
        watch.mark_click_acknowledged(reservation.reservation_id, acknowledged_at=invalid,
                                      now=NOW + timedelta(seconds=10))
    assert watch.next_allowed_at() is None


def test_old_run_cannot_acquire_new_ack_or_fake_capture_bound(tmp_path):
    watch = ledger(tmp_path)
    reservation = started(watch)
    watch.start_run(now=NOW + timedelta(seconds=3))
    with pytest.raises(WatchLedgerError, match="this run"):
        watch.mark_click_acknowledged(reservation.reservation_id, acknowledged_at=NOW + timedelta(seconds=5),
                                      now=NOW + timedelta(seconds=5))
    with pytest.raises(WatchLedgerError):
        watch.mark_click_observed(reservation.reservation_id, observed_at=NOW + timedelta(seconds=5),
                                 evidence_ref=None, now=NOW + timedelta(seconds=5))


def test_action_ack_time_follows_health_and_http_but_precedes_body_parsing(backend, monkeypatch, local_clock):
    desktop, _ = backend

    def health():
        local_clock[0] += timedelta(seconds=3)

    def post(_url, *, json, timeout):
        local_clock[0] += timedelta(seconds=2)

        def body():
            local_clock[0] += timedelta(seconds=5)
            return {"success": True, "action_id": json["action_id"], "step_id": json["step_id"]}

        return SimpleNamespace(raise_for_status=lambda: None, json=body)

    monkeypatch.setattr(desktop, "_agent_health", health)
    monkeypatch.setattr(desktop.agent, "post", post)
    acknowledged_at = desktop._act({"kind": "click", "x": 100, "y": 100}, reservation_id="synthetic")
    assert acknowledged_at == DESKTOP_NOW + timedelta(seconds=5)
    assert local_clock[0] == DESKTOP_NOW + timedelta(seconds=10)


@pytest.mark.parametrize("callback_fails", [False, True])
def test_after_click_is_after_one_action_and_failure_never_repeats_click(backend, monkeypatch, local_clock, callback_fails):
    desktop, actions = backend
    seen = []

    def after(acknowledged_at):
        seen.append((acknowledged_at, len(actions)))
        if callback_fails:
            raise OSError("synthetic persistence interruption")

    arguments = delivery_setup(desktop, monkeypatch)
    result = desktop.deliver(**arguments, after_click=after)
    assert result.status == ("unknown" if callback_fails else "sent")
    assert result.click_acknowledged_at == DESKTOP_NOW
    assert seen == [(DESKTOP_NOW, 3)]
    assert desktop.deliver(**arguments, after_click=after).status == "unknown"
    assert len(actions) == 3


@pytest.mark.parametrize("ack_lost", [False, True])
def test_readonly_recovery_keeps_ack_or_the_first_verified_capture_completion(backend, monkeypatch, local_clock, ack_lost):
    desktop, actions = backend
    arguments = delivery_setup(desktop, monkeypatch, post_missing=True)
    assert desktop.deliver(**arguments).status == "unknown"
    proof = dict(desktop.last_preclick_proof)
    if ack_lost:
        desktop._click_acknowledgements.clear()
    observed_frame = frame(desktop, "reconcile")
    reading = screen(observed_frame, messages=[message()])
    captures = []

    def capture():
        local_clock[0] += timedelta(seconds=2)
        captures.append(local_clock[0])
        return observed_frame

    def inspect(value):
        assert value is observed_frame
        local_clock[0] += timedelta(seconds=5)
        return reading

    monkeypatch.setattr(desktop, "capture", capture)
    monkeypatch.setattr(desktop, "inspect", inspect)
    first = desktop.reconcile_delivery(TEXT, reservation_id=arguments["reservation_id"], proof=proof)
    second = desktop.reconcile_delivery(TEXT, reservation_id=arguments["reservation_id"], proof=proof)
    assert first.status == second.status == "sent"
    assert len(actions) == 3
    if ack_lost:
        assert first.click_acknowledged_at is second.click_acknowledged_at is None
        assert first.click_observed_at == second.click_observed_at == captures[0]
        assert first.click_observed_at < first.delivered_at
        assert first.click_observed_evidence_ref == observed_frame.evidence_ref
    else:
        assert first.click_acknowledged_at == second.click_acknowledged_at == DESKTOP_NOW
        assert first.click_observed_at is second.click_observed_at is None


def test_watcher_retries_only_ack_storage_after_the_one_click(setup, monkeypatch):
    from harness.qq.decision import QQDeliveryReceipt

    settings, desktop, decide, _, receipts = setup

    def receipt(**kwargs):
        receipts.append(kwargs)
        return QQDeliveryReceipt(receipt_id="synthetic-click-receipt", origin="decision", **{
            key: value for key, value in kwargs.items() if key != "memory_db"})

    monkeypatch.setattr("harness.qq.watcher.record_qq_delivery", receipt)
    desktop.supports_click_acknowledgement = True
    original_delivery = desktop.deliver
    acknowledged_at = WATCHER_NOW + timedelta(seconds=3)

    def delivery(text, *, after_click, **kwargs):
        result = original_delivery(text, **kwargs)
        result.click_acknowledged_at = acknowledged_at
        try:
            after_click(acknowledged_at)
        except OSError:
            result.status = "unknown"
        return result

    desktop.deliver = delivery
    desktop.reconcile_delivery = lambda *_args, **_kwargs: SimpleNamespace(
        status="sent", evidence_refs=["test://fresh-reconciliation"], reason_code="synthetic_reconciled",
        delivered_at=WATCHER_NOW + timedelta(seconds=5), click_acknowledged_at=acknowledged_at)
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    original_ack = watcher.ledger.mark_click_acknowledged
    attempts = []

    def acknowledge(*args, **kwargs):
        attempts.append(kwargs["acknowledged_at"])
        if len(attempts) == 1:
            raise OSError("synthetic first ACK write failed")
        return original_ack(*args, **kwargs)

    monkeypatch.setattr(watcher.ledger, "mark_click_acknowledged", acknowledge)
    watcher.cycle()
    watcher.cycle()
    assert desktop.clicks == desktop.sends == len(receipts) == 1
    assert attempts == [acknowledged_at, acknowledged_at]
    assert not watcher.ledger.has_uncertain_send()
    assert watcher.ledger.next_allowed_at(priority="direct") == acknowledged_at + timedelta(seconds=10)
    trace = next(iter(watcher._traces.values())).snapshot()
    assert len([stage for stage in trace["stages"] if stage["name"] == "send_click_acknowledged"]) == 1
