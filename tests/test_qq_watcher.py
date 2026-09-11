"""Synthetic desktop integration checks; never touch QQ, WSL or a live model."""
from datetime import datetime, timezone, timedelta
import json
import hashlib
from types import SimpleNamespace as NS

import pytest

from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore
from harness.qq.watcher import QQWatcher, WatcherSettings, atomic_json


NOW = datetime(2026, 9, 11, tzinfo=timezone.utc)


def message(text, direction="incoming"):
    return NS(text=text, direction=direction, kind="text", display_name="虚构测试甲")


def frame(number):
    return NS(capture_id=f"synthetic-{number}", content_digest=f"synthetic-digest-{number}",
              captured_at=NOW + timedelta(seconds=number), evidence_ref=f"test-evidence://frame-{number}")


class Desktop:
    def __init__(self):
        self.frames = [frame(1), frame(2), frame(2)]
        self.screens = [NS(messages=[message("旧消息甲"), message("旧消息乙")]),
                        NS(messages=[message("旧消息甲"), message("旧消息乙"), message("Misty，你好")])]
        self.captures = self.verifications = self.sends = self.clicks = 0
        self.result = "sent"

    def capture(self):
        self.current = self.frames[min(self.captures, len(self.frames) - 1)]
        self.captures += 1
        return self.current

    def verify_group(self):
        screen = self.screens[min(self.verifications, len(self.screens) - 1)]
        self.verifications += 1
        return NS(chat_frame=self.current, chat_screen=screen, evidence_refs=["test-evidence://group"])

    def attribute(self, message, frame):
        return NS(member_id="20001", display_name="虚构测试甲", capture_id=frame.capture_id + "-profile",
                  observed_at=frame.captured_at, profile_evidence_refs=["test-evidence://profile"],
                  evidence_refs=[frame.evidence_ref, "test-evidence://profile"])

    def deliver(self, text, *, reservation_id, expected_content_digest, expected_context_frame, before_click, group_verification=None):
        assert expected_context_frame.content_digest == expected_content_digest
        assert expected_context_frame is self.current
        self.last_preclick_proof = {"group_id": "10001", "self_member_id": "30001",
            "reply_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "frame_evidence_ref": self.current.evidence_ref, "context_signature": self.current.content_digest,
            "verified_at": NOW + timedelta(seconds=3), "before_outgoing_count": 0}
        self.sends += 1
        if self.result != "not_sent":
            before_click()
            self.clicks += 1
        return NS(status=self.result, evidence_refs=["test-evidence://actual-new-bubble"],
                  delivered_at=NOW + timedelta(seconds=5), reason_code="synthetic_result")

    def close(self):
        pass


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr("harness.qq.watcher.now", lambda: NOW + timedelta(seconds=3))
    memory = tmp_path / "memory.sqlite3"
    QQVisualMemoryStore(memory).record_profile(QQVerifiedProfileInput(
        group_id="10001", member_id="20001", display_name="虚构测试甲", display_name_source="qq_profile",
        verification="qq_ui_verified", capture_id="synthetic-original-profile", observed_at=NOW,
        group_evidence=["test-evidence://group"], member_evidence=["test-evidence://profile"],
    ))
    token = tmp_path / "token"
    token.write_text("synthetic-only-" + "x" * 32)
    session = tmp_path / "session.json"
    session.write_text(json.dumps({"memory_db": str(memory), "token_file": str(token),
                                   "allowed_group_ids": ["10001"], "self_member_id": "30001"}))
    settings = WatcherSettings(session_config_path=session, state_db=tmp_path / "watch.sqlite3",
                               status_file=tmp_path / "status.json", control_file=tmp_path / "control.json",
                               body_token_file=token, agent_token_file=token, group_id="10001", self_member_id="30001",
                               automatic_send=True)
    desktop = Desktop()
    calls, receipts = [], []

    def decide(**kwargs):
        calls.append(kwargs)
        return NS(action="speak", reply="你好呀", decision_id="synthetic-new-decision", new_decision=True, replayed=False, outcome="ready")

    monkeypatch.setattr("harness.qq.watcher.record_qq_delivery", lambda **kwargs: receipts.append(kwargs))
    return settings, desktop, decide, calls, receipts


def test_first_screen_is_baseline_then_one_new_event_is_remembered_and_sent_once(setup):
    settings, desktop, decide, calls, receipts = setup
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    assert not calls and not receipts and desktop.sends == 0
    watcher.cycle()
    watcher.cycle()
    assert len(calls) == len(receipts) == desktop.sends == desktop.clicks == 1
    assert watcher.status["deliveries"] == 1
    observations = watcher.store.reply_context(group_id="10001", member_id="20001").observations
    assert [x.text for x in observations] == ["Misty，你好"]
    assert observations[0].message_id is None
    assert receipts[0]["source_record_ids"] == [observations[0].record_id]


def test_wait_is_a_real_gate_result_with_memory_and_no_gui_send(setup):
    settings, desktop, _, _, receipts = setup
    watcher = QQWatcher(settings, desktop=desktop, decide=lambda **_: NS(
        action="wait", reply=None, decision_id="synthetic-wait", new_decision=True, replayed=False, outcome="ready"))
    watcher.cycle()
    watcher.cycle()
    assert watcher.status["last_decision_action"] == "wait" and desktop.sends == 0 and not receipts
    assert len(watcher.store.reply_context(group_id="10001", member_id="20001").observations) == 1


def test_cached_speak_is_not_sent(setup):
    settings, desktop, _, _, _ = setup
    watcher = QQWatcher(settings, desktop=desktop, decide=lambda **_: NS(
        action="speak", reply="旧草稿", decision_id="synthetic-cached", new_decision=False, replayed=True))
    watcher.cycle()
    watcher.cycle()
    assert desktop.sends == 0


def test_pause_during_model_call_prevents_staging_or_click(setup):
    settings, desktop, decide, _, receipts = setup

    def paused(**kwargs):
        atomic_json(settings.control_file, {"paused": True})
        return decide(**kwargs)

    watcher = QQWatcher(settings, desktop=desktop, decide=paused)
    watcher.cycle()
    with pytest.raises(InterruptedError):
        watcher.cycle()
    assert desktop.sends == 0 and not receipts


@pytest.mark.parametrize("result,uncertain", [("unknown", True), ("not_sent", False)])
def test_delivery_failure_never_retries_the_event(setup, result, uncertain):
    settings, desktop, decide, _, receipts = setup
    desktop.result = result
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    watcher.cycle()
    watcher.cycle()
    assert watcher.ledger.has_uncertain_send() is uncertain
    assert desktop.sends == 1 and not receipts


def test_receipt_storage_failure_after_click_blocks_future_sends(setup, monkeypatch):
    settings, desktop, decide, _, _ = setup
    monkeypatch.setattr("harness.qq.watcher.record_qq_delivery", lambda **_: (_ for _ in ()).throw(OSError("synthetic")))
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    with pytest.raises(OSError):
        watcher.cycle()
    assert desktop.clicks == 1 and watcher.ledger.has_uncertain_send()
    assert watcher.status["automatic_send"] is False


def test_scope_mismatch_and_nonloopback_endpoints_rejected(setup):
    settings, _, _, _, _ = setup
    with pytest.raises(ValueError, match="scope"):
        QQWatcher(settings.model_copy(update={"group_id": "99999"}))
    with pytest.raises(ValueError, match="loopback"):
        WatcherSettings.model_validate(settings.model_dump() | {"body_url": "http://example.org"})


def test_malformed_control_fails_closed(setup):
    settings, desktop, decide, _, _ = setup
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    settings.control_file.write_text("not JSON")
    assert watcher.should_stop()


def test_restart_does_not_reply_to_stopped_period_backlog(setup):
    settings, desktop, decide, calls, _ = setup
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    restarted_desktop = Desktop()
    restarted_desktop.frames = [frame(3)]
    restarted_desktop.screens = [desktop.screens[1]]
    restarted = QQWatcher(settings, desktop=restarted_desktop, decide=decide)
    restarted.cycle()
    assert not calls and restarted_desktop.sends == 0


def test_transient_attribution_failure_retries_same_real_event_without_duplicate_send(setup):
    from harness.qq.grounding import GroundingError
    settings, desktop, decide, calls, receipts = setup
    original = desktop.attribute
    attempts = []

    def flaky(message, frame):
        attempts.append(frame.capture_id)
        if len(attempts) == 1:
            raise GroundingError("qq_geometry_schema_value_error")
        return original(message, frame)

    desktop.attribute = flaky
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    watcher.cycle()
    watcher.cycle()
    assert not calls and not receipts
    watcher.cycle()
    watcher.cycle()
    assert len(attempts) == 2
    assert desktop.clicks == len(calls) == len(receipts) == 1
    assert len(watcher.store.reply_context(group_id="10001", member_id="20001").observations) == 1
