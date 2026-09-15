"""Negotiated QQ actor integration using only synthetic temporary SQLite."""
from datetime import timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest

from harness.embodiment.capabilities import QQ_COMMUNICATION_SEND
from harness.embodiment.providers.qq import QQCommunicationError
from harness.qq import embodied
from harness.qq.desktop import DeliveryResult
from harness.qq.group_observation import QQGroupObservationMixin
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace
from tests import test_qq_segment_ledger as fixtures


class Actor(QQGroupObservationMixin, embodied.QQEmbodiedMixin):
    def set_status(self, **kwargs):
        self.status.update(kwargs)

    def should_stop(self):
        return self.paused

    def check_control(self):
        if self.paused:
            raise InterruptedError("synthetic pause")

    def _newer_visible_context(self):
        # This port fixture has no incoming-message stream. The coordinator's
        # arrival/continuity tests exercise that independent scheduling gate.
        return False


def saved_frame(label, moment):
    return NS(capture_id=label, captured_at=moment, evidence_ref=f"test://{label}",
        content_digest=hashlib.sha256(label.encode()).hexdigest(), a11y_evidence_ref=f"test://{label}-ax",
        window_id="0x100", active_window_id="0x100")


def saved_screen(frame, **changes):
    return NS(**(dict(frame_id=frame.capture_id, evidence_ref=frame.evidence_ref,
        scene="group_chat", blocked=False, opened_profile=None,
        group_title=NS(display_name="synthetic group"), messages=[]) | changes))


@pytest.fixture
def case(tmp_path, monkeypatch):
    data = fixtures.setup(tmp_path, ["合成第一段", "合成第二段"])
    # The shared ledger fixture predates the transport contract's SHA256 field.
    data.decision = data.decision.model_copy(update={"input_hash": "b" * 64})
    with sqlite3.connect(data.memory) as db:
        db.execute("UPDATE qq_reply_decisions SET input_hash=?,record_json=?", (
            data.decision.input_hash, data.decision.model_dump_json()))
    moment = [fixtures.at(3)]
    def mono():
        return 1000 + (moment[0] - fixtures.NOW).total_seconds()
    def sleep(seconds):
        moment[0] += timedelta(seconds=seconds)
    monkeypatch.setattr(embodied, "_runtime", lambda: NS(now=lambda: moment[0],
        time=NS(monotonic=mono, sleep=sleep)))
    actor = Actor()
    actor.settings = NS(qq_body_enabled=True, homeostatic_idle_enabled=False,
        expected_instance_id="fictional-body", expected_revision=2, window_id="0x100",
        group_id=fixtures.GROUP, self_member_id=fixtures.SELF, automatic_send=True,
        group_roster=None, state_db=data.watch.path)
    actor.ledger = data.watch
    actor.session = NS(memory_db=data.memory)
    actor.status, actor.paused, actor._identity_prepared = {}, False, True
    observer_state = dict(running=True, last_error_type=None, gap_started_at=None, gap_seconds=0)
    actor.observer = NS(latest=lambda: NS(polled_mono=1000 + (moment[0] - fixtures.NOW).total_seconds()),
                        status=lambda: dict(observer_state))
    frame = saved_frame("synthetic-current", moment[0])
    screen = saved_screen(frame)
    actor.desktop = NS(_last_verified_capture=frame, _screens={frame.capture_id: screen})
    actor._init_group_observation()
    actor._init_embodied()
    actor._qq_accept_read(frame, screen)
    data.actor, data.moment, data.observer_state = actor, moment, observer_state
    data.frame, data.screen = frame, screen
    data.turn = NS(reservation_id=data.parent.reservation_id, decision=data.decision,
                   event=data.watch.get_event(data.event.event_id), frame=frame,
                   deadline=QQDeadline(1000 + (data.parent.deadline_at - fixtures.NOW).total_seconds(), clock=mono),
                   trace=QQTurnTrace(clock=mono))
    data.clicks, data.persistence, data.reconciles = [], [], []
    yield data
    actor._close_embodied()


def execute(case, index=0):
    case.clicks.append(index)
    seconds = (case.moment[0] - fixtures.NOW).total_seconds()
    fixtures.click(case, index, seconds)
    case.moment[0] += timedelta(seconds=.6)
    return DeliveryResult(status="sent", reason_code="synthetic_verified", phase="postcheck",
        evidence_refs=[f"test://preclick-{index}", f"test://after-{index}"], delivered_at=case.moment[0],
        click_acknowledged_at=fixtures.at(seconds+.2))


def persist(case, result, index=0, *, interrupt=False):
    from harness.qq.decision import record_qq_segment_delivery
    case.persistence.append(index)
    receipt = record_qq_segment_delivery(memory_db=case.memory, group_id=fixtures.GROUP,
        self_member_id=fixtures.SELF, decision_id=case.decision.decision_id, segment_index=index,
        segment_count=len(case.texts), delivered_text=case.texts[index], evidence_refs=result.evidence_refs,
        source_record_ids=[case.source.record_id], delivered_at=result.delivered_at, verification="qq_ui_verified")
    if interrupt:
        raise OSError("synthetic persistence interruption after durable receipt")
    case.watch.reconcile_segment_sent(case.segments[index].child_id, receipt=receipt, now=case.moment[0])
    return receipt


def action(case, index=0, *, persist_fn=None, execute_fn=None, reconcile=False):
    return case.actor._qq_action(case.turn, segment=case.watch.segments(case.parent.reservation_id)[index],
        execute=execute_fn or (lambda: execute(case, index)),
        persist=persist_fn or (lambda result: persist(case, result, index)), reconcile=reconcile)


def test_body_port_binds_real_parent_child_decision_and_persists_each_segment_once(case):
    first = action(case)
    assert first.status == "sent"
    assert case.actor._qq_was_persisted(case.turn, 0)
    assert case.watch.segments(case.parent.reservation_id)[0].status == "sent"
    assert action(case).status == "sent"  # Reads existing receipt; no second actor entry.
    assert case.clicks == case.persistence == [0]
    assert action(case, 1).status == "sent"
    assert case.clicks == case.persistence == [0, 1]
    assert all(item.status == "sent" for item in case.watch.segments(case.parent.reservation_id))


def test_started_click_never_reenters_executor_even_without_durable_receipt(case):
    fixtures.click(case, 0)
    result = action(case)
    assert result.status == "unknown" and not case.clicks and not case.persistence


def test_persistence_interruption_recovers_receipt_readonly_without_another_click(case):
    result = action(case, persist_fn=lambda result: persist(case, result, interrupt=True))
    assert result.status == "unknown" and case.clicks == [0]
    assert not case.actor._qq_was_persisted(case.turn)
    case.watch.mark_segment_uncertain(case.segments[0].child_id, reason="storage_interrupted",
                                      cancel_remaining=False, now=case.moment[0])
    recovered = action(case, reconcile=True, execute_fn=lambda: pytest.fail("durable receipt needs no GUI reconcile"))
    assert recovered.status == "sent" and case.clicks == [0]
    # The outer coordinator may still finish the existing receipt's ledger link.
    assert not case.actor._qq_was_persisted(case.turn)
    persist(case, recovered)
    assert case.watch.segments(case.parent.reservation_id)[0].status == "sent"


@pytest.mark.parametrize("field,value", [("window_id", "0x999"), ("body_instance_id", "different-body"),
    ("environment_revision", 9), ("source_record_ids", []), ("reservation_id", "wrong-child")])
def test_receipt_loader_cannot_copy_request_scope_over_conflicting_stored_proof(case, field, value):
    result = execute(case)
    persist(case, result)
    child = case.watch.segments(case.parent.reservation_id)[0]
    raw = child.model_dump(mode="json")
    raw["proof"][field] = value
    with sqlite3.connect(case.watch.path) as db:
        db.execute("UPDATE qq_watch_send_segments SET record_json=? WHERE child_id=?",
                   (json.dumps(raw), child.child_id))
    with pytest.raises((QQCommunicationError, ValueError), match="receipt|callback"):
        action(case)
    assert case.clicks == [0]


@pytest.mark.parametrize("field,value", [("reply_sha256", "c" * 64), ("run_id", "old-run"),
    ("event_id", "wrong-event"), ("decision_id", "wrong-decision")])
def test_frozen_plan_rejects_unrelated_or_changed_parent(case, field, value):
    parent = case.watch.get_send(case.parent.reservation_id)
    raw = parent.model_dump(mode="json") | {field: value}
    with sqlite3.connect(case.watch.path) as db:
        db.execute("UPDATE qq_watch_sends SET record_json=? WHERE reservation_id=?",
                   (json.dumps(raw), parent.reservation_id))
    with pytest.raises((QQCommunicationError, ValueError)):
        action(case)
    assert not case.clicks


@pytest.mark.parametrize("change", [{"new_decision": False}, {"replayed": True}, {"source_record_ids": []}])
def test_ready_text_without_current_decision_or_original_source_cannot_send(case, change):
    raw = case.decision.model_dump(mode="json") | change
    with sqlite3.connect(case.memory) as db:
        db.execute("UPDATE qq_reply_decisions SET record_json=?", (json.dumps(raw),))
    with pytest.raises(QQCommunicationError):
        action(case)
    assert not case.clicks


def test_initial_self_verification_reuses_real_latest_chat_before_sensor_resamples(case):
    old = saved_frame("before-self", case.moment[0] - timedelta(seconds=12))
    old_screen = saved_screen(old)
    case.observer_state.update(gap_started_at=case.moment[0].isoformat(), gap_seconds=1)
    frame, screen = case.actor._qq_accept_read(old, old_screen)
    assert frame is case.frame and screen is case.screen
    assert frame.captured_at == case.moment[0]  # Original capture timestamp, never replaced.
    body = case.actor.status["qq_body"]
    assert QQ_COMMUNICATION_SEND not in body["embodiment"]["effective_capabilities"]
    with pytest.raises(QQDeadlineExpired):
        action(case)
    assert not case.clicks


@pytest.mark.parametrize("changes", [{"scene": "member_profile"}, {"blocked": True},
    {"group_title": NS(display_name="another group")}, {"frame_id": "unrelated"}])
def test_latest_unknown_or_mismatched_scene_never_replaces_expired_chat(case, changes):
    old = saved_frame("old", case.moment[0] - timedelta(seconds=12))
    case.actor.desktop._screens[case.frame.capture_id] = saved_screen(case.frame, **changes)
    with pytest.raises(QQCommunicationError, match="stale"):
        case.actor._qq_accept_read(old, saved_screen(old))
    assert not case.clicks


def test_single_segment_protocol_loads_its_child_receipt_not_missing_aggregate(case):
    # A separate synthetic one-child reservation exercises segment_count == 1.
    # Reuse no historical entries or production state.
    old = case.actor
    single_path = case.memory.parent / "single"
    single_path.mkdir()
    single = fixtures.setup(single_path, ["正式的单段回应。"])
    single.decision = single.decision.model_copy(update={"input_hash": "e" * 64, "reply_style": "formal"})
    with sqlite3.connect(single.memory) as db:
        db.execute("UPDATE qq_reply_decisions SET input_hash=?,record_json=?", (
            single.decision.input_hash, single.decision.model_dump_json()))
    old._qq_provider = None
    old.ledger, old.session = single.watch, NS(memory_db=single.memory)
    single.actor, single.moment, single.frame = old, case.moment, case.frame
    single.turn = NS(reservation_id=single.parent.reservation_id, decision=single.decision,
                     event=single.watch.get_event(single.event.event_id), frame=case.frame,
                     deadline=case.turn.deadline, trace=case.turn.trace)
    single.clicks, single.persistence = [], []
    assert action(single).status == "sent"
    assert single.clicks == single.persistence == [0]


def test_formal_multiline_legacy_text_is_not_silently_rewritten_or_sent(case):
    case.turn.decision = NS(decision_id=case.decision.decision_id, reply="正式第一段\n第二行")
    with pytest.raises(ValueError):
        case.actor._qq_action(case.turn, execute=lambda: pytest.fail("invalid text entered actor"), persist=lambda _: None)
