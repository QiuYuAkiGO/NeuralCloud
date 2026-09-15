"""Real controller, negotiated QQ provider, and temporary durable stores.

Only the screen sensor/GUI primitives and LLM are synthetic. No real API runs.
"""
from datetime import datetime, timedelta, timezone
import json
import hashlib
import sqlite3
from dataclasses import dataclass
from types import SimpleNamespace as NS

import pytest

from harness.qq import accessibility, decision, telemetry, watcher as module
from harness.qq import segment_delivery
from harness.qq.accessibility import ChatChangeSignature
from harness.qq.watch_ledger import VisibleItem
from harness.embodiment.providers.qq import QQCommunicationError
from tests import test_qq_decision as decision_fixtures, test_qq_watcher as watcher_fixtures
from tests.test_qq_decision import isolated_static_parser_cache as isolated_static_parser_cache
from tests.test_qq_idle_ledger import idle
from tests.test_qq_segment_watcher import Clock, SegmentDesktop
from tests.test_qq_watcher import message


model_setup = decision_fixtures.setup
watcher_setup = watcher_fixtures.setup


class Observer:
    def __init__(self, clock):
        self.clock, self.sequence, self.digest = clock, 1, "synthetic-chat"
        self.error = None

    def latest(self):
        return NS(sequence=self.sequence, digest=self.digest, first_seen_at=self.clock.wall(),
                  first_seen_mono=self.clock.mono(), polled_mono=self.clock.mono())

    def status(self):
        return dict(running=True, last_error_type=self.error, open_gap=False,
                    gap_started_at=None, gap_seconds=0, pending_digest=None)

    def changes_after(self, sequence):
        return NS(observations=[] if sequence >= self.sequence else [self.latest()],
                  overflowed=False, missed_changes=0, latest_sequence=self.sequence)

    def arrive(self):
        self.sequence += 1
        self.digest = "synthetic-new-message"


class IdleDesktop(SegmentDesktop):
    def capture(self):
        self.captures += 1
        self.current = NS(capture_id=f"idle-capture-{self.captures}", captured_at=self.clock.wall(),
            content_digest="a" * 64, evidence_ref="test://current", a11y_tree={"signature": "synthetic-chat"},
            window_id="test-window", active_window_id="test-window")
        return self.current

    def verify_group(self, *, frame=None):
        self.verifications += 1
        self.current = frame or self.current
        return Verification(chat_frame=self.current, chat_screen=NS(messages=[message("synthetic anchor")],
            frame_id=self.current.capture_id, evidence_ref=self.current.evidence_ref, scene="group_chat",
            blocked=False, opened_profile=None, group_title=NS(display_name="synthetic group")),
            evidence_refs=["test://group"])

    def verify_self(self):
        return NS(member_id="30001")

    def segment_result(self, status):
        self.current.window_id = self.current.active_window_id = "test-window"
        self.current.content_digest = hashlib.sha256(self.current.capture_id.encode()).hexdigest()
        return super().segment_result(status)


@dataclass
class Verification:
    chat_frame: object
    chat_screen: object
    evidence_refs: list


@pytest.fixture
def controller(watcher_setup, model_setup, monkeypatch):
    settings, _, _, _, _ = watcher_setup
    store, client, _, model_options, records = model_setup
    config = json.loads(settings.session_config_path.read_text())
    config.update(memory_db=str(store.path), model_config_path=str(model_options["config_path"]),
                  preset_path=str(model_options["preset_path"]))
    settings.session_config_path.write_text(json.dumps(config))
    settings = settings.model_copy(update={"qq_body_enabled": True, "expected_instance_id": "test-body",
        "expected_revision": 2, "window_id": "test-window"})
    clock, calls, facts = Clock(), [], []
    clock.seconds = 0.0
    started_at = datetime.now(timezone.utc)
    clock.wall = lambda: started_at + timedelta(seconds=clock.seconds)
    desktop = IdleDesktop(clock)
    monkeypatch.setattr(module, "now", clock.wall)
    monkeypatch.setattr(module, "time", NS(monotonic=clock.mono, sleep=clock.sleep))
    original_deadline, original_trace = telemetry.QQDeadline, telemetry.QQTurnTrace
    monkeypatch.setattr(telemetry, "QQDeadline", lambda mono: original_deadline(mono, clock=clock.mono))
    monkeypatch.setattr(telemetry, "QQTurnTrace", lambda **kwargs: original_trace(**kwargs, clock=clock.mono))
    def signature(tree):
        return ChatChangeSignature(True, tree["signature"])
    monkeypatch.setattr(accessibility, "chat_change_signature", signature)
    monkeypatch.setattr(segment_delivery, "chat_change_signature", signature)
    monkeypatch.setattr(module, "record_qq_delivery", decision.record_qq_delivery)
    monkeypatch.setattr("harness.qq.feedback.recover_recent_feedback", lambda *args, **kwargs: 0)
    state = NS(clock=clock, desktop=desktop, client=client, calls=calls, facts=facts, records=records,
               before_model=lambda _: None, after_model=lambda _: None)

    def decide(**options):
        calls.append(options)
        state.before_model(options)
        result = decision.decide_qq_reply(**options)
        state.after_model(result)
        return result

    watch = module.QQWatcher(settings, desktop=desktop, decide=decide)
    state.watch = watch
    watch.observer = Observer(clock)
    watch.ledger.observe(frame_signature="a"*64, items=[VisibleItem(direction="incoming", text="synthetic anchor")], observed_at=clock.wall())
    watch.last_digest = "a"*64
    watch.last_items = [VisibleItem(direction="incoming", text="synthetic anchor")]
    watch._observer_sequence, watch._observer_baselined, watch._identity_prepared = 1, True, True
    watch._homeostasis = NS(summary=idle().homeostatic_interoception,
        eligible=lambda **_: True, tick=lambda: None, refresh=lambda: None, status=lambda: {"enabled": True},
        enqueue_fact=lambda **values: facts.append(values), close=lambda: None)
    yield state
    watch._close_embodied()


def events(state):
    with sqlite3.connect(state.watch.settings.state_db) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT record_json FROM qq_watch_events")]


def reservations(state):
    with sqlite3.connect(state.watch.settings.state_db) as db:
        return [json.loads(row[0]) for row in db.execute("SELECT record_json FROM qq_watch_sends")]


def test_idle_single_send_through_negotiated_body_and_existing_two_model_stages(controller):
    state = controller
    state.watch.cycle()
    assert len(state.desktop.clicked) == 1
    assert len(state.calls) == 1 and state.calls[0]["trigger_member_id"] is None
    assert state.calls[0]["idle_intent"].homeostatic_interoception == state.calls[0]["homeostatic_interoception"]
    assert len(state.client.gate_messages) == 1
    parent = reservations(state)[0]
    event = events(state)[0]
    assert event["origin"] == "homeostasis" and event["source_record_id"] is None and event["item"] is None
    assert parent["status"] == "sent" and parent["preclick_proof"]["source_record_ids"] == []
    receipt = decision.get_qq_delivery(memory_db=state.watch.session.memory_db,
        group_id="10001", decision_id=parent["decision_id"])
    assert receipt and receipt.source_record_ids == []
    assert state.watch.status["deliveries"] == 1
    assert state.watch.status["idle_latency_ms"]["samples"] == 1
    assert "latency_ms" not in state.watch.status
    assert len(state.facts) == 1 and state.facts[0]["kind"] == "send_attempt"
    assert state.facts[0]["fact_id"] == receipt.receipt_id
    state.watch.cycle()
    assert len(state.desktop.clicked) == state.watch.status["deliveries"] == 1


def test_idle_segments_have_no_fake_sources_and_feedback_only_per_actual_bubble(controller):
    state = controller
    state.client.chat_routes = {"": "第一段。\n\n第二段。"}
    state.watch.cycle()
    parent = reservations(state)[0]
    assert len(state.desktop.clicked) == 2 and parent["status"] == "sent"
    children = state.watch.ledger.segments(parent["reservation_id"])
    assert all(child.proof.source_record_ids == [] and child.receipt_id for child in children)
    assert len(state.facts) == 2 and len({fact["fact_id"] for fact in state.facts}) == 2
    assert state.watch.status["deliveries"] == 1


@pytest.mark.parametrize("action", ["wait", "speak"])
def test_new_message_during_gate_supersedes_idle_without_reusing_its_clock(controller, action):
    state = controller
    state.client.choice = json.dumps(dict(action=action, reason="synthetic"))
    state.after_model = lambda _: state.watch.observer.arrive()
    state.watch.cycle()
    assert events(state)[0]["state"] == "superseded"
    assert state.desktop.clicked == [] and reservations(state) == []
    assert state.watch._carry_first_seen is None and state.watch._pending_turn is None
    assert state.facts == []


def test_new_message_before_click_invalidates_idle_and_cannot_send_old_intent(controller):
    state = controller
    state.desktop.before_attempt = lambda _: state.watch.observer.arrive()
    state.watch.cycle()
    assert state.desktop.clicked == []
    assert reservations(state)[0]["status"] == "not_sent"
    assert state.facts == [] and state.watch._carry_first_seen is None


def test_homeostasis_revision_change_before_click_rejects_idle(controller):
    state = controller
    state.desktop.before_attempt = lambda _: setattr(state.watch._homeostasis, "summary",
        state.watch._homeostasis.summary.model_copy(update={"snapshot_revision": 4}))
    state.watch.cycle()
    assert state.desktop.clicked == [] and reservations(state)[0]["status"] == "not_sent"
    assert state.facts == []


def test_model_expiry_and_natural_wait_do_not_create_send_reservations(controller):
    state = controller
    state.before_model = lambda _: state.clock.sleep(61)
    state.watch.cycle()
    assert events(state)[0]["state"] == "expired"
    assert not reservations(state) and not state.desktop.clicked and not state.facts


def test_natural_wait_consumes_opportunity_without_gui_or_positive_feedback(controller):
    state = controller
    state.client.choice = '{"action":"wait","reason":"现在先等等。"}'
    state.watch.cycle()
    assert events(state)[0]["state"] == "decided_wait" and not state.desktop.clicked
    state.clock.sleep(61)
    state.watch.cycle()
    assert len(state.calls) == 1 and len(events(state)) == 1 and not state.facts


def test_disabled_negotiated_send_cannot_fall_through_to_gui(controller, monkeypatch):
    state = controller
    original = state.watch._qq_driver_status

    def disabled():
        result = original()
        if state.watch._qq_operation is not None:
            return result.model_copy(update={"send_ready": False})
        return result

    monkeypatch.setattr(state.watch, "_qq_driver_status", disabled)
    with pytest.raises(QQCommunicationError, match="capability_unavailable"):
        state.watch.cycle()
    assert state.desktop.clicked == [] and state.desktop.calls == []
    assert reservations(state)[0]["status"] == "not_sent"
    assert not state.facts


def test_post_persist_provider_error_cannot_count_one_delivery_twice(controller, monkeypatch):
    state = controller
    original = state.watch._qq_verified_receipt
    failures = []

    def unavailable_once(request):
        # Fail after controller persistence/trace completion, before the port's
        # independent acknowledgement. The existing receipt must only rejoin.
        if state.watch.status["deliveries"] and not failures:
            failures.append(True)
            raise OSError("synthetic independent receipt read failure")
        return original(request)

    monkeypatch.setattr(state.watch, "_qq_verified_receipt", unavailable_once)
    state.watch.cycle()
    assert len(state.desktop.clicked) == 1 and reservations(state)[0]["status"] == "sent"
    assert state.watch.status["deliveries"] == 1
    assert len(state.facts) == 1
    assert state.watch.status["idle_latency_ms"]["samples"] == 1


def test_partial_idle_reply_stops_on_new_message_and_only_actual_segment_gets_feedback(controller):
    state = controller
    state.client.chat_routes = {"": "第一段。\n\n第二段。"}
    state.desktop.after_sent = lambda _: state.watch.observer.arrive()
    state.watch.cycle()
    parent = reservations(state)[0]
    assert parent["status"] == "partial" and len(state.desktop.clicked) == 1
    assert [item.status for item in state.watch.ledger.segments(parent["reservation_id"])] == ["sent", "cancelled"]
    assert state.watch.status["deliveries"] == 0 and len(state.facts) == 1
    assert decision.get_qq_delivery(memory_db=state.watch.session.memory_db,
        group_id="10001", decision_id=parent["decision_id"]) is None


def test_body_status_requires_activation_and_recovers_health_without_capturing(controller):
    state = controller
    state.watch.set_status(automatic_send=True)
    assert state.watch.status["automatic_send"] is False
    state.client.choice = '{"action":"wait","reason":"等待。"}'
    state.watch.cycle()
    state.watch.cycle()
    assert state.watch.status["automatic_send"] is True
    captures = state.desktop.captures
    state.watch.observer.error = "request_timeout"
    state.watch.cycle()
    assert state.watch.status["automatic_send"] is False
    state.watch.observer.error = None
    state.watch.cycle()
    assert state.watch.status["automatic_send"] is True
    assert state.desktop.captures == captures
    state.watch._qq_provider.manager.set_terminal("synthetic terminal")
    state.watch.cycle()
    assert state.watch.status["automatic_send"] is False


def test_incoming_message_receives_summary_but_never_idle_permission(controller):
    state = controller
    state.client.choice = '{"action":"wait","reason":"听对方说完。"}'
    state.watch._tick_homeostasis()
    frame = state.desktop.capture()
    verification = state.desktop.verify_group(frame=frame)
    batch = state.watch.ledger.observe(frame_signature="new-message-frame", items=[
        VisibleItem(direction="incoming", text="synthetic anchor"),
        VisibleItem(direction="incoming", text="synthetic question")], observed_at=state.clock.wall())
    original = batch.events[0]
    state.watch.ledger.bind_event(original.event_id, state.records[0].record_id)
    event = state.watch.ledger.get_event(original.event_id)
    turn = module.PendingTurn(event, "20001", state.records[0].record_id, frame, verification,
        decision.QQDeadline.after(60, clock=state.clock.mono),
        decision.QQTurnTrace(first_seen_at=state.clock.wall(), clock=state.clock.mono),
        context_digest="synthetic-chat", homeostatic_interoception=state.watch._homeostasis.summary)
    state.watch._pending_turn = turn
    state.watch._decide_turn(turn)
    assert "idle_intent" not in state.calls[0]
    assert state.calls[0]["homeostatic_interoception"] == state.watch._homeostasis.summary
    assert turn.decision.initiated_by == "message" and turn.decision.idle_intent_id is None
    assert state.records[0].record_id in turn.decision.new_observation_record_ids
    assert state.desktop.clicked == []


def test_idle_cooldown_is_checked_before_any_gui_acquisition(controller):
    state = controller
    state.client.choice = '{"action":"wait","reason":"等待。"}'
    state.watch.cycle()
    captures = state.desktop.captures
    state.clock.sleep(61)
    state.watch.cycle()
    assert state.desktop.captures == captures
    assert len(state.calls) == 1


def test_feedback_read_failure_prevents_idle_even_with_marked_summary(controller, monkeypatch):
    state = controller
    def fail(*args, **kwargs):
        raise OSError("synthetic queue read interruption")
    monkeypatch.setattr("harness.qq.feedback.recover_recent_feedback", fail)
    state.watch.cycle()
    assert state.calls == [] and state.desktop.clicked == []
    assert state.watch.status["homeostasis_feedback_error"] == "OSError"


def test_idle_reverifies_identity_after_long_gap_without_waiting_for_new_message(controller):
    state = controller
    state.watch._identity_prepared = False
    state.client.choice = '{"action":"wait","reason":"等待。"}'
    state.watch._tick_homeostasis()
    state.watch._maybe_homeostatic_idle(state.watch.observer.latest())
    assert state.watch._identity_prepared is True
    assert len(state.calls) == 1
    assert state.watch.status["qq_body"]["embodiment"]["state"] == "available"


def test_failed_idle_identity_check_never_restores_prepared_flag(controller, monkeypatch):
    state = controller
    state.watch._identity_prepared = False
    def fail():
        raise RuntimeError("synthetic identity failure")
    monkeypatch.setattr(state.desktop, "verify_self", fail)
    with pytest.raises(RuntimeError, match="synthetic identity failure"):
        state.watch._tick_homeostasis()
        state.watch._maybe_homeostatic_idle(state.watch.observer.latest())
    assert state.watch._identity_prepared is False
    assert not state.calls and not state.desktop.clicked


def test_nominal_quiet_group_rebuilds_identity_without_waiting_for_an_idle_need(controller, monkeypatch):
    state = controller
    state.watch._identity_prepared = False
    state.watch._homeostasis.eligible = lambda **_: False
    verify = state.desktop.verify_group
    def empty_group(**kwargs):
        value = verify(**kwargs)
        value.chat_screen.messages = []
        return value
    monkeypatch.setattr(state.desktop, "verify_group", empty_group)
    state.watch.cycle()
    assert state.watch._identity_prepared is True
    assert state.desktop.captures > 0 and state.desktop.verifications > 0
    assert not state.calls and not state.desktop.clicked
    state.watch.cycle()
    assert state.watch.status["automatic_send"] is True
