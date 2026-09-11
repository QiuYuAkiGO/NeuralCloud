"""Deterministic unattended coordinator regressions; no live GUI or model calls."""
from datetime import timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace as NS

import httpx
import pytest

from harness.qq import watcher as module
from harness.qq.observer import ScopedQQObserver
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_watcher import Desktop, NOW, message, setup as setup


class Clock:
    def __init__(self):
        self.seconds = 0.0

    def wall(self):
        return NOW + timedelta(seconds=self.seconds)

    def mono(self):
        return 1_000_000.0 + self.seconds

    def advance(self, seconds):
        self.seconds += seconds


class MutableDesktop(Desktop):
    """The sensor and GUI snapshots are independently sampled from this screen."""
    def __init__(self, clock):
        super().__init__()
        self.clock = clock
        self.lines = ["旧消息甲", "旧消息乙"]
        self.fail_verification = 0
        self.fail_attribution = 0
        self.attributions = 0
        self.sent_texts = []

    def capture(self):
        self.captures += 1
        self.current = NS(capture_id=f"synthetic-capture-{self.captures}",
                          content_digest=hashlib.sha256(json.dumps(self.lines).encode()).hexdigest(),
                          captured_at=self.clock.wall(), evidence_ref=f"test-evidence://frame-{self.captures}")
        return self.current

    def verify_group(self, *, frame=None):
        self.verifications += 1
        if self.fail_verification:
            self.fail_verification -= 1
            raise RuntimeError("synthetic_group_verification_failure")
        return NS(chat_frame=frame or self.current, chat_screen=NS(messages=[message(text) for text in self.lines]),
                  evidence_refs=["test-evidence://group"])

    def attribute(self, message, frame):
        self.attributions += 1
        if self.fail_attribution:
            self.fail_attribution -= 1
            raise RuntimeError("synthetic_attribution_failure")
        return super().attribute(message, frame)

    def deliver(self, text, *, reservation_id, expected_content_digest, expected_context_frame,
                before_click, group_verification=None):
        assert expected_context_frame.content_digest == expected_content_digest
        self.last_preclick_proof = {"group_id": "10001", "self_member_id": "30001",
            "reply_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "frame_evidence_ref": self.current.evidence_ref, "context_signature": self.current.content_digest,
            "verified_at": self.clock.wall(), "before_outgoing_count": len(self.sent_texts)}
        self.sends += 1
        before_click()
        self.clicks += 1
        self.sent_texts.append(text)
        return NS(status="sent", evidence_refs=[f"test-evidence://outgoing-{self.clicks}"],
                  delivered_at=self.clock.wall(), reason_code="synthetic_verified")


@pytest.fixture
def scenario(setup, monkeypatch):
    settings, _, _, _, receipts = setup
    clock = Clock()
    desktop = MutableDesktop(clock)
    calls = []

    def forbidden(*args, **kwargs):
        pytest.fail("The synthetic watcher test attempted network access")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(module, "now", clock.wall)
    monkeypatch.setattr(module, "time", NS(monotonic=clock.mono))

    class ControlledDeadline(QQDeadline):
        def __init__(self, deadline_mono, **kwargs):
            super().__init__(deadline_mono, clock=clock.mono)

    monkeypatch.setattr(module, "QQDeadline", ControlledDeadline)
    monkeypatch.setattr(module, "QQTurnTrace", lambda **kwargs: QQTurnTrace(**kwargs, clock=clock.mono))
    settings = settings.model_copy(update={"accessibility_enabled": True})

    def answer(action="wait", *, outcome=None, priority="ambient", reply=None, cached=False):
        return NS(action=action, reply=reply if action == "speak" else None,
                  decision_id=f"synthetic-decision-{len(calls)}", new_decision=not cached, replayed=cached,
                  outcome=outcome or ("ready" if action == "speak" else "natural_wait"),
                  decision_source="model", addressing=priority)

    def decide(**kwargs):
        calls.append(kwargs)
        return state.callback(kwargs)

    observer = ScopedQQObserver(read_fn=lambda: {"lines": list(desktop.lines)},
        signature_fn=lambda tree: hashlib.sha256(json.dumps(tree["lines"]).encode()).hexdigest(),
        clock=clock.mono, wall_clock=clock.wall)
    watcher = module.QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.observer = observer
    state = NS(watcher=watcher, observer=observer, desktop=desktop, clock=clock, calls=calls,
               receipts=receipts, answer=answer, callback=lambda _: answer())

    def append(text, *, after=1):
        clock.advance(after)
        desktop.lines.append(text)
        return observer.poll_once()

    state.append = append
    observer.poll_once()
    watcher.cycle()
    assert not calls and desktop.sends == 0
    yield state
    observer.close()


def events(state):
    with sqlite3.connect(state.watcher.settings.state_db) as connection:
        return [json.loads(row[0]) for row in connection.execute(
            "SELECT record_json FROM qq_watch_events WHERE group_id='10001' ORDER BY rowid")]


def traces(state):
    path = state.watcher._trace_file
    if not path.exists():
        return []
    latest = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        latest[value["trace_id"]] = value
    return list(latest.values())


def test_verification_failure_does_not_commit_cursor_or_renew_arrival(scenario):
    state = scenario
    arrival = state.append("虚构的新提问")
    state.desktop.fail_verification = 1
    with pytest.raises(RuntimeError, match="synthetic_group"):
        state.watcher.cycle()
    assert state.watcher._observer_sequence == 0 and not state.calls
    assert events(state) == []
    state.clock.advance(3)
    state.watcher.cycle()
    assert len(state.calls) == 1 and state.watcher._observer_sequence == arrival.sequence
    trace = state.calls[0]["trace"]
    assert trace.first_seen_at == arrival.first_seen_at
    assert trace.first_seen_mono == arrival.first_seen_mono
    assert state.calls[0]["deadline"].deadline_mono == arrival.first_seen_mono + 60
    assert events(state)[0]["state"] == "decided_wait"


def test_buffered_batch_keeps_earliest_arrival_even_when_latest_is_newer(scenario):
    state = scenario
    first = state.append("虚构的第一条")
    latest = state.append("虚构的第二条", after=7)
    state.clock.advance(4)
    state.watcher.cycle()
    assert len(state.calls) == 1
    trace = state.calls[0]["trace"]
    assert trace.first_seen_at == first.first_seen_at != latest.first_seen_at
    assert trace.first_seen_mono == first.first_seen_mono
    assert state.calls[0]["deadline"].remaining() == pytest.approx(49)
    records = events(state)
    assert len(records) == 2 and {record["first_seen_at"] for record in records} == {first.first_seen_at.isoformat().replace("+00:00", "Z")}
    assert state.watcher._observer_sequence == latest.sequence
    assert len(state.watcher.store.reply_context(group_id="10001", member_id="20001").observations) == 2


@pytest.mark.parametrize("first_action", ["speak", "wait"])
def test_new_message_during_gate_supersedes_without_deadlock_or_stale_send(scenario, first_action):
    state = scenario
    first = state.append("虚构的初始问题")

    def during_gate(_):
        if len(state.calls) == 1:
            state.append("虚构的补充信息", after=2)
            return state.answer(first_action, reply="这份旧草稿不能发送")
        return state.answer()

    state.callback = during_gate
    state.watcher.cycle()
    assert state.desktop.sends == 0 and state.watcher._pending_turn is None
    assert events(state)[0]["state"] == "superseded"
    state.watcher.cycle()
    assert len(state.calls) == 2 and state.desktop.sends == 0
    assert state.calls[1]["trace"].first_seen_at == first.first_seen_at
    assert state.calls[1]["deadline"].deadline_mono == first.first_seen_mono + 60
    assert [event["state"] for event in events(state)] == ["superseded", "decided_wait"]
    assert not any(trace["counters"].get("turn_failed") for trace in traces(state))


def test_attribution_retry_uses_same_event_and_keeps_cursor_stable(scenario):
    state = scenario
    first = state.append("虚构的身份待核验消息")
    state.desktop.fail_attribution = 1
    state.watcher.cycle()
    original = events(state)[0]
    assert not state.calls and original["state"] == "observed"
    assert state.watcher._observer_sequence == first.sequence
    state.clock.advance(2)
    state.watcher.cycle()
    current = events(state)
    assert len(current) == 1 and current[0]["event_id"] == original["event_id"]
    assert state.desktop.attributions == 2 and len(state.calls) == 1
    assert state.watcher._observer_sequence == first.sequence
    assert state.calls[0]["trace"].first_seen_mono == first.first_seen_mono
    assert len(state.watcher.store.reply_context(group_id="10001", member_id="20001").observations) == 1


@pytest.mark.parametrize("outcome", ["expired", "model_error", "model_invalid", "pending"])
def test_unsuccessful_decision_never_becomes_natural_wait(scenario, outcome):
    state = scenario
    state.append("虚构的待处理问题")
    state.callback = lambda _: state.answer(outcome=outcome)
    state.watcher.cycle()
    assert state.desktop.sends == 0 and not state.receipts
    assert events(state)[0]["state"] == ("expired" if outcome == "expired" else "failed")
    recorded = traces(state)
    assert len(recorded) == 1 and recorded[0]["counters"]["turn_failed"] == 1
    assert not recorded[0]["counters"].get("turn_natural_wait")


def test_late_ready_decision_is_expired_before_any_send_intent(scenario):
    state = scenario
    state.append("虚构的超时提问")

    def late(_):
        state.clock.advance(61)
        return state.answer("speak", reply="迟到草稿")

    state.callback = late
    state.watcher.cycle()
    assert state.desktop.sends == 0 and not state.receipts
    assert events(state)[0]["state"] == "expired"
    assert traces(state)[0]["counters"]["turn_failed"] == 1


@pytest.mark.parametrize("priority,seconds,final_action,cached", [
    ("direct", 10, "speak", False), ("ambient", 60, "speak", False),
    ("direct", 10, "wait", False), ("direct", 10, "speak", True),
])
def test_cadence_rechecks_model_and_never_sends_old_or_cached_draft(scenario, priority, seconds, final_action, cached):
    state = scenario

    def cadence_reply(_):
        if len(state.calls) == 1:
            return state.answer("speak", priority=priority, reply="虚构的第一次回复")
        if len(state.calls) == 2:
            return state.answer("speak", priority=priority, reply="冷却前旧草稿")
        return state.answer(final_action, priority=priority, reply="冷却后新回复", cached=cached)

    state.callback = cadence_reply
    state.append("虚构的第一问")
    state.watcher.cycle()
    sent_at = state.clock.wall()
    assert state.desktop.sent_texts == ["虚构的第一次回复"]
    second = state.append("虚构的第二问")
    state.watcher.cycle()
    assert len(state.calls) == 2 and len(state.desktop.sent_texts) == 1
    assert state.watcher._pending_turn.retry_at == sent_at + timedelta(seconds=seconds)
    assert state.watcher.ledger.next_allowed_at(priority="direct") == sent_at + timedelta(seconds=10)
    assert state.watcher.ledger.next_allowed_at(priority="ambient") == sent_at + timedelta(seconds=60)
    state.clock.advance(seconds - 1.01)
    state.watcher.cycle()
    assert len(state.calls) == 2  # The cooldown has not quite finished.
    state.clock.advance(.01)
    state.watcher.cycle()
    assert len(state.calls) == 3 and state.calls[2]["request"]
    assert state.calls[2]["trace"].first_seen_at == second.first_seen_at
    assert state.calls[2]["deadline"].deadline_mono == second.first_seen_mono + 60
    assert "冷却前旧草稿" not in state.desktop.sent_texts
    if final_action == "speak" and not cached:
        assert state.desktop.sent_texts == ["虚构的第一次回复", "冷却后新回复"]
    else:
        assert state.desktop.sent_texts == ["虚构的第一次回复"]
    assert state.watcher._pending_turn is None
