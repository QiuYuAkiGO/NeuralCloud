"""Offline child-send coordination with real SQLite receipts and ledger.

The desktop and observer are synthetic; no GUI, HTTP or model is used.
"""
from contextlib import nullcontext
from datetime import timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest

from harness.qq import segment_delivery as segments
from harness.qq import watcher as module
from harness.qq.accessibility import ChatChangeSignature
from harness.qq.decision import QQReplyDecision, get_qq_delivery, get_qq_segment_deliveries
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_watcher import Desktop, NOW, setup as setup


class Clock:
    seconds = 3.0

    def mono(self):
        return 1000 + self.seconds

    def wall(self):
        return NOW + timedelta(seconds=self.seconds)

    def sleep(self, value):
        self.seconds += value


class SegmentDesktop(Desktop):
    supports_click_acknowledgement = True

    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.calls = []
        self.clicked = []
        self.advances = []
        self.reconciles = []
        self.cleaned = []
        self.failures = []
        self.after_sent = lambda _: None
        self.before_attempt = lambda _: None
        self.reconcile_result = "unknown"
        self._owned_draft = None
        self._identity_epoch = "epoch-1"

    def turn(self, **_):
        return nullcontext()

    def deliver(self, text, *, reservation_id, expected_content_digest, expected_context_frame,
                group_verification, before_click, after_click):
        self.before_attempt(len(self.calls))
        assert expected_context_frame.content_digest == expected_content_digest
        self.calls.append((reservation_id, text, expected_context_frame))
        self._owned_draft = (text, "10001", "test-window")
        outcome = self.failures.pop(0) if self.failures else "sent"
        if outcome == "precheck":
            return NS(status="not_sent", reason_code="target_changed", retryable=True,
                      phase="precheck", evidence_refs=["test://precheck"])
        self.last_preclick_proof = dict(group_id="10001", self_member_id="30001",
            reply_sha256=hashlib.sha256(text.encode()).hexdigest(),
            reservation_id=reservation_id, frame_evidence_ref=self.current.evidence_ref,
            context_signature=self.current.content_digest, verified_at=self.clock.wall(),
            before_outgoing_count=len(self.clicked), window_id="test-window", body_instance_id="test-body",
            environment_revision=2, title_pixels_sha256="a" * 64, evidence_refs=[self.current.evidence_ref])
        try:
            before_click()
        except Exception as exc:
            from harness.qq.desktop import PreClickIdentityInvalidated
            return NS(status="not_sent", reason_code="identity_cache_invalidated_before_click" if isinstance(exc, PreClickIdentityInvalidated) else "callback_refused",
                      retryable=isinstance(exc, PreClickIdentityInvalidated), phase="precheck", evidence_refs=[])
        self.clicked.append((reservation_id, text))
        self.clock.sleep(.05)
        self.ack = self.clock.wall()
        if outcome != "lost_ack":
            after_click(self.ack)
        self._owned_draft = None
        self.current = NS(capture_id=f"own-{len(self.clicked)}", evidence_ref=f"test://own-{len(self.clicked)}",
            content_digest=f"pixel-{len(self.clicked)}", a11y_tree={"signature": f"own-{len(self.clicked)}"},
            captured_at=self.clock.wall())
        self.after_sent(len(self.clicked))
        return self.segment_result("unknown" if outcome in {"unknown", "lost_ack"} else "sent")

    def segment_result(self, status):
        return NS(status=status, reason_code="verified" if status == "sent" else "outcome_unavailable",
            evidence_refs=list(dict.fromkeys([*self.last_preclick_proof["evidence_refs"], self.current.evidence_ref])), delivered_at=self.clock.wall(),
            click_acknowledged_at=self.ack, click_observed_at=None,
            post_send_frame=self.current, post_send_verification=NS(chat_frame=self.current))

    def advance_segment_context(self, previous, result, text):
        self.advances.append((previous, text))
        if getattr(self, "external_after_send", False):
            raise RuntimeError("segment_context_changed")
        return result.post_send_frame, result.post_send_verification

    def reconcile_delivery(self, text, *, reservation_id, proof):
        self.reconciles.append((reservation_id, text))
        self.clock.sleep(.1)
        result = self.segment_result(self.reconcile_result)
        if self.reconcile_result == "sent":
            result.click_acknowledged_at = None
            result.click_observed_at = self.clock.wall()
            result.click_observed_evidence_ref = self.current.evidence_ref
        return result

    def discard_owned_draft(self, text):
        assert self._owned_draft[0] == text
        self.cleaned.append(text)
        self._owned_draft = None
        return True


@pytest.fixture
def case(setup, monkeypatch):
    settings, _, _, _, _ = setup
    clock = Clock()
    desktop = SegmentDesktop(clock)
    monkeypatch.setattr(module, "now", clock.wall)
    monkeypatch.setattr(module, "time", NS(monotonic=clock.mono, sleep=clock.sleep))
    monkeypatch.setattr(module, "QQTurnTrace", lambda **kwargs: QQTurnTrace(**kwargs, clock=clock.mono))
    monkeypatch.setattr(module, "QQDeadline", type("Deadline", (QQDeadline,), {
        "__init__": lambda self, deadline_mono, *args, **kwargs: QQDeadline.__init__(self, deadline_mono, clock=clock.mono)}))
    monkeypatch.setattr(segments, "chat_change_signature", lambda tree: ChatChangeSignature(True, tree["signature"]))
    state = NS(clock=clock, desktop=desktop, texts=["第一段，中文。", "第二段。", "第三段。"], decision_calls=0)

    def decide(**_):
        state.decision_calls += 1
        source = state.watcher.store.reply_context(group_id="10001", member_id="20001").observations[-1]
        result = QQReplyDecision(decision_id="synthetic-segment-decision", input_hash="synthetic-hash",
            created_at=clock.wall(), action="speak", reason="synthetic", reply="\n\n".join(state.texts),
            reply_segments=state.texts, decision_source="model", group_id="10001", self_member_id="30001",
            trigger_member_id="20001", participants=["20001"], source_record_ids=[source.record_id],
            new_observation_record_ids=[source.record_id], evidence_refs=["test://source"], delivery_refs=[],
            model="synthetic", persona_version="synthetic", preset_id="synthetic", outcome="ready")
        from harness.qq.decision import _connect
        with _connect(state.watcher.session.memory_db) as connection:
            connection.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
                (result.decision_id, result.input_hash, result.group_id, "completed", result.model_dump_json()))
        return result

    state.watcher = module.QQWatcher(settings, desktop=desktop, decide=decide)
    state.watcher.cycle()
    return state


def parent(case):
    with sqlite3.connect(case.watcher.settings.state_db) as connection:
        raw = connection.execute("SELECT record_json FROM qq_watch_sends").fetchone()[0]
    return json.loads(raw)


def children(case):
    return case.watcher.ledger.segments(parent(case)["reservation_id"])


def receipts(case):
    return get_qq_segment_deliveries(memory_db=case.watcher.session.memory_db,
        group_id="10001", decision_id="synthetic-segment-decision")


def trace(case):
    return json.loads(case.watcher._trace_file.read_text().splitlines()[-1])


def test_three_actual_bubbles_have_distinct_proofs_and_one_aggregate(case):
    case.watcher.cycle()
    assert [text for _, text in case.desktop.clicked] == case.texts
    assert len({key for key, _ in case.desktop.clicked}) == 3
    assert [item.status for item in children(case)] == ["sent"] * 3
    assert [item.segment_index for item in receipts(case)] == [0, 1, 2]
    for item in children(case):
        assert item.proof.reservation_id == item.child_id
        assert item.click_acknowledged_at is not None
        assert item.proof.reply_sha256 == hashlib.sha256(item.text.encode()).hexdigest()
    aggregate = get_qq_delivery(memory_db=case.watcher.session.memory_db, group_id="10001",
                                decision_id="synthetic-segment-decision")
    assert aggregate.delivered_segments == case.texts
    assert aggregate.segment_receipt_ids == [item.receipt_id for item in receipts(case)]
    assert parent(case)["status"] == "sent" and case.watcher.status["deliveries"] == 1
    assert case.decision_calls == 1 and len(case.desktop.advances) == 2
    assert case.watcher.ledger.next_allowed_at(priority="direct") == children(case)[-1].click_acknowledged_at + timedelta(seconds=10)
    assert trace(case)["counters"]["turn_sent"] == 1
    assert trace(case)["counters"]["segments_sent"] == 3


@pytest.mark.parametrize("stop", ["external", "expired", "paused"])
def test_sent_prefix_survives_termination_without_sending_the_suffix(case, stop):
    def after(_):
        if stop == "external":
            case.desktop.external_after_send = True
        elif stop == "expired":
            case.clock.sleep(61)
        else:
            module.atomic_json(case.watcher.settings.control_file, {"paused": True})
    case.desktop.after_sent = after
    if stop == "paused":
        with pytest.raises(InterruptedError):
            case.watcher.cycle()
    else:
        case.watcher.cycle()
    assert len(case.desktop.clicked) == len(receipts(case)) == 1
    assert [item.status for item in children(case)] == ["sent", "cancelled", "cancelled"]
    assert parent(case)["status"] == "partial"
    assert case.watcher.status["deliveries"] == 0
    counters = trace(case)["counters"]
    assert counters["turn_failed"] == 1 and not counters.get("turn_superseded")
    assert get_qq_delivery(memory_db=case.watcher.session.memory_db, group_id="10001",
                           decision_id="synthetic-segment-decision") is None


def test_unknown_second_click_only_reconciles_that_segment_then_pauses(case):
    case.desktop.failures = ["sent", "unknown"]
    case.watcher.cycle()
    assert len(case.desktop.clicked) == 2
    assert [text for _, text in case.desktop.reconciles] == [case.texts[1]] * 3
    assert len({key for key, _ in case.desktop.reconciles}) == 1
    assert [item.status for item in children(case)] == ["sent", "uncertain", "cancelled"]
    assert len(receipts(case)) == 1 and parent(case)["status"] == "uncertain"
    assert case.watcher.should_stop() and case.watcher.status["deliveries"] == 0


def test_reconciled_lost_ack_uses_observed_bound_and_can_continue(case):
    case.desktop.failures = ["lost_ack", "sent", "sent"]
    case.desktop.reconcile_result = "sent"
    case.watcher.cycle()
    assert len(case.desktop.clicked) == 3 and len(case.desktop.reconciles) == 1
    first = children(case)[0]
    assert first.click_observed_at is not None and first.click_acknowledged_at is None
    assert parent(case)["status"] == "sent"


def test_late_reconciliation_confirms_prefix_but_never_renews_send_deadline(case):
    case.desktop.failures = ["unknown"]
    case.desktop.reconcile_result = "sent"
    case.desktop.after_sent = lambda _: case.clock.sleep(61)
    case.watcher.cycle()
    assert len(case.desktop.clicked) == 1 and len(case.desktop.reconciles) == 1
    assert children(case)[0].status == "sent"
    assert parent(case)["status"] == "partial" and "expired" in parent(case)["reason"]
    assert trace(case)["elapsed_ms"] >= 61000


def test_second_segment_precheck_retry_keeps_first_receipt_and_original_deadline(case):
    case.desktop.failures = ["sent", "precheck", "precheck", "sent", "sent"]
    case.watcher.cycle()
    turn = case.watcher._pending_turn
    original = turn.deadline
    assert len(case.desktop.clicked) == len(receipts(case)) == 1
    for _ in range(2):
        case.clock.sleep(2.1)
        case.watcher.cycle()
    assert len(case.desktop.clicked) == 3
    assert case.desktop.calls[1][0] == case.desktop.calls[2][0] == case.desktop.calls[3][0]
    assert children(case)[1].attempt_count == 3 and turn.deadline is original
    assert case.decision_calls == 1 and parent(case)["status"] == "sent"


def test_exhausted_retry_cleans_only_current_segment_text(case):
    case.desktop.failures = ["sent", "precheck", "precheck", "precheck"]
    case.watcher.cycle()
    for _ in range(2):
        case.clock.sleep(2.1)
        case.watcher.cycle()
    assert len(case.desktop.clicked) == 1
    assert case.desktop.cleaned == [case.texts[1]]
    assert parent(case)["status"] == "partial" and case.watcher.status["deliveries"] == 0


def test_confirmed_own_digests_can_lag_but_any_intermediate_external_change_stops(case):
    turn = NS(segment_texts=tuple(case.texts), context_digest="own-2",
              confirmed_context_digests={"initial", "own-1", "own-2"})
    case.watcher._pending_turn = turn
    batch = NS(overflowed=False, observations=[NS(digest="initial"), NS(digest="own-1")], latest_sequence=2)
    pending = {"pending_digest": "own-1"}
    case.watcher.observer = NS(status=lambda: pending, changes_after=lambda _: batch)
    assert case.watcher._newer_visible_context() is False
    batch.observations = [NS(digest="external"), NS(digest="own-2")]
    batch.latest_sequence = 4
    assert case.watcher._newer_visible_context() is True
    assert case.watcher._observer_sequence == 2
    pending["pending_digest"] = "external"
    batch.observations = []
    assert case.watcher._newer_visible_context() is True


def test_restart_joins_receipts_without_clicking_or_replaying_suffix(case):
    case.desktop.failures = ["sent", "precheck"]
    case.watcher.cycle()
    assert len(case.desktop.clicked) == 1
    restarted_desktop = SegmentDesktop(case.clock)
    restarted = module.QQWatcher(case.watcher.settings, desktop=restarted_desktop)
    restarted._recover_existing_receipts()
    assert restarted_desktop.clicked == [] and restarted_desktop.calls == []
    assert parent(case)["status"] == "partial"
    assert [item.status for item in children(case)] == ["sent", "cancelled", "cancelled"]


def test_segment_receipt_storage_failure_never_attempts_next_child(case, monkeypatch):
    monkeypatch.setattr(segments, "record_qq_segment_delivery", lambda **_: (_ for _ in ()).throw(OSError("synthetic")))
    case.watcher.cycle()
    assert len(case.desktop.clicked) == 1 and not receipts(case)
    assert parent(case)["status"] == "uncertain" and case.watcher.should_stop()
