"""Real provider, coordinator and receipts; only GUI/sensor/model are synthetic."""
from types import SimpleNamespace as NS
from datetime import timedelta
import json
import sqlite3

import pytest

from harness.embodiment.providers.qq import QQCommunicationError
from harness.qq.decision import get_qq_delivery, get_qq_segment_deliveries
from tests import test_qq_homeostatic_watcher as fixtures


controller = fixtures.controller
model_setup = fixtures.model_setup
watcher_setup = fixtures.watcher_setup
isolated_static_parser_cache = fixtures.isolated_static_parser_cache


def gap_after_first_bubble(state, monkeypatch, *, seconds, stale_cache=False):
    observer = state.watch.observer
    latest, status = observer.latest, observer.status
    gap = NS(start=None, end=None)

    def after_sent(count):
        if count == 1:
            gap.start, gap.end = state.clock.mono(), state.clock.mono() + seconds

    def in_gap():
        return gap.end is not None and state.clock.mono() < gap.end

    def read_latest():
        if in_gap():
            return NS(polled_mono=state.clock.mono() - 6) if stale_cache else None
        return latest()

    def read_status():
        return status() | (dict(gap_started_at="synthetic-gap", gap_seconds=1,
                              last_error_type="request_timeout") if in_gap() and not stale_cache else {})

    state.desktop.after_sent = after_sent
    monkeypatch.setattr(observer, "latest", read_latest)
    monkeypatch.setattr(observer, "status", read_status)
    state.client.chat_routes = {"": "第一段。\n\n第二段。"}
    return gap


@pytest.mark.parametrize("stale_cache", [False, True])
def test_second_segment_waits_for_sensor_recovery_within_original_deadline(controller, monkeypatch, stale_cache):
    state = controller
    gap = gap_after_first_bubble(state, monkeypatch, seconds=2, stale_cache=stale_cache)
    state.watch.cycle()
    parent = fixtures.reservations(state)[0]
    assert parent["status"] == "sent"
    children = state.watch.ledger.segments(parent["reservation_id"])
    assert len(state.desktop.clicked) == 2
    assert all(child.status == "sent" and child.receipt_id for child in children)
    assert state.clock.mono() >= gap.end
    assert children[1].click_started_at >= children[0].click_acknowledged_at
    assert children[1].deadline_at == children[0].deadline_at
    assert state.watch.status["verified_segments_sent"] == 2
    assert state.watch.status["partial_deliveries"] == 0
    assert state.watch.status["deliveries"] == 1


def test_unrecovered_sensor_expires_suffix_without_repeating_confirmed_bubble(controller, monkeypatch):
    state = controller
    gap_after_first_bubble(state, monkeypatch, seconds=120)
    state.watch.cycle()
    parent = fixtures.reservations(state)[0]
    children = state.watch.ledger.segments(parent["reservation_id"])
    assert parent["status"] == "partial" and parent["reason"] == "partial_expired"
    assert [child.status for child in children] == ["sent", "cancelled"]
    assert len(state.desktop.clicked) == 1 and not state.desktop.reconciles
    assert state.clock.wall() >= children[0].deadline_at
    assert state.watch._pending_turn is None
    assert state.watch.status["verified_segments_sent"] == 1
    assert state.watch.status["partial_deliveries"] == 1
    assert state.watch.status["deliveries"] == 0
    assert state.watch.status["last_delivery_at"] is not None
    # These are durable group totals, including after status/restart rebuilding.
    receipt = get_qq_segment_deliveries(memory_db=state.watch.session.memory_db,
        group_id=state.watch.settings.group_id, decision_id=parent["decision_id"])[0]
    state.watch.status.update(verified_segments_sent=0, partial_deliveries=0, last_delivery_at=None)
    state.watch._refresh_segment_delivery_status()
    assert state.watch.status["verified_segments_sent"] == state.watch.status["partial_deliveries"] == 1
    assert state.watch.status["last_delivery_at"] == receipt.delivered_at.isoformat()


def test_restart_delivery_time_uses_single_verified_receipt_not_later_ledger_update(controller):
    state = controller
    state.watch.cycle()
    parent = fixtures.reservations(state)[0]
    assert parent["status"] == "sent" and parent["segment_count"] == 0
    receipt = get_qq_delivery(memory_db=state.watch.session.memory_db,
        group_id=state.watch.settings.group_id, decision_id=parent["decision_id"])
    parent["updated_at"] = (receipt.delivered_at + timedelta(hours=1)).isoformat()
    with sqlite3.connect(state.watch.ledger.path) as db:
        db.execute("UPDATE qq_watch_sends SET record_json=? WHERE reservation_id=?",
                   (json.dumps(parent), parent["reservation_id"]))
    state.watch.status.update(last_delivery_at=None, deliveries=0)
    state.watch._refresh_segment_delivery_status()
    assert state.watch.status["last_delivery_at"] == receipt.delivered_at.isoformat()
    assert state.watch.status["deliveries"] == 0


def test_status_time_requires_sent_ledger_bound_to_exact_verified_receipt(controller):
    state = controller
    state.watch.cycle()
    parent = fixtures.reservations(state)[0]
    parent["receipt_id"] = "unmatched-receipt"
    with sqlite3.connect(state.watch.ledger.path) as db:
        db.execute("UPDATE qq_watch_sends SET record_json=? WHERE reservation_id=?",
                   (json.dumps(parent), parent["reservation_id"]))
    state.watch._refresh_segment_delivery_status()
    assert state.watch.status["last_delivery_at"] is None


def test_new_message_while_waiting_supersedes_remaining_segment(controller, monkeypatch):
    state = controller
    gap = gap_after_first_bubble(state, monkeypatch, seconds=2)
    changes = state.watch.observer.changes_after

    def changed(sequence):
        if gap.start is not None and state.clock.mono() >= gap.start + .4:
            return NS(observations=[NS(digest="new-incoming")], overflowed=False,
                      latest_sequence=sequence + 1)
        return changes(sequence)

    monkeypatch.setattr(state.watch.observer, "changes_after", changed)
    state.watch.cycle()
    parent = fixtures.reservations(state)[0]
    assert len(state.desktop.clicked) == 1 and parent["reason"] == "partial_superseded"
    assert gap.start + .4 <= state.clock.mono() < gap.end


def test_new_sensor_gap_at_provider_guard_waits_without_calling_desktop_twice(controller, monkeypatch):
    state = controller
    state.client.chat_routes = {"": "第一段。\n\n第二段。"}
    original_ready = state.watch._qq_send_observer_ready
    gap = NS(end=None, injected=False)
    monkeypatch.setattr(state.watch, "_qq_send_observer_ready", lambda:
        False if gap.end is not None and state.clock.mono() < gap.end else original_ready())

    def after_sent(count):
        if count != 1:
            return
        provider = state.watch._qq_provider
        original_guard = provider._guard

        async def guarded(*args, **kwargs):
            if not gap.injected:
                gap.injected = True
                gap.end = state.clock.mono() + .5
                raise QQCommunicationError("capability_unavailable")
            return await original_guard(*args, **kwargs)

        monkeypatch.setattr(provider, "_guard", guarded)

    state.desktop.after_sent = after_sent
    state.watch.cycle()
    assert gap.injected and state.clock.mono() >= gap.end
    assert len(state.desktop.clicked) == len(state.desktop.calls) == 2
    assert fixtures.reservations(state)[0]["status"] == "sent"


def test_provider_gate_error_keeps_fixed_diagnostic_without_suffix_retry(controller, monkeypatch):
    state = controller
    state.client.chat_routes = {"": "第一段。\n\n第二段。"}
    original = state.watch._qq_wait_for_send_observer

    def wait(turn):
        if state.desktop.clicked:
            raise QQCommunicationError("capability_unavailable")
        return original(turn)

    monkeypatch.setattr(state.watch, "_qq_wait_for_send_observer", wait)
    state.watch.cycle()
    parent = fixtures.reservations(state)[0]
    assert len(state.desktop.clicked) == 1 and parent["reason"] == "partial_capability_unavailable"
    assert state.watch.status["last_error_type"] == "partial_capability_unavailable"
