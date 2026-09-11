"""Independent synthetic cohort/storage recovery checks with real SQLite state."""
from contextlib import closing

import pytest

from harness.qq import decision
from harness.qq import watcher as module
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_watcher import setup as setup
from tests.test_qq_watcher_unattended import events, scenario as scenario, traces


def use_real_receipt_storage(state, monkeypatch):
    def stored_decision(_):
        source_ids = [item.record_id for item in state.watcher.store.reply_context(
            group_id="10001", member_id="20001").observations]
        value = decision.QQReplyDecision(
            decision_id="qq-decision:synthetic-durable", input_hash="synthetic-input-hash",
            created_at=state.clock.wall(), completed_at=state.clock.wall(), action="speak", reason="synthetic",
            reply="虚构的持久回执测试回复", decision_source="model", outcome="ready", group_id="10001",
            self_member_id="30001", trigger_member_id="20001", participants=["20001"],
            source_record_ids=source_ids, new_observation_record_ids=source_ids,
            evidence_refs=["test-evidence://synthetic-input"], delivery_refs=[], model="synthetic",
            persona_version="synthetic", preset_id="synthetic")
        with closing(decision._connect(state.watcher.session.memory_db)) as connection, connection:
            connection.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
                (value.decision_id, value.input_hash, value.group_id, "completed", value.model_dump_json()))
        return value

    state.callback = stored_decision
    monkeypatch.setattr(module, "record_qq_delivery", decision.record_qq_delivery)


def test_cold_cohort_is_locked_once_when_profile_write_fails_before_commit(scenario, monkeypatch):
    state = scenario
    original_attribute = state.desktop.attribute
    original_profile = state.watcher.store.record_profile
    attempts = []

    def newly_seen(message, frame):
        result = original_attribute(message, frame)
        result.member_id = "20002"
        return result

    def transient_profile_failure(profile):
        attempts.append(profile.member_id)
        if len(attempts) == 1:
            raise OSError("synthetic profile write interruption")
        return original_profile(profile)

    state.desktop.attribute = newly_seen
    monkeypatch.setattr(state.watcher.store, "record_profile", transient_profile_failure)
    state.callback = lambda _: state.answer("speak", reply="虚构的新成员回复")
    state.append("虚构的新成员第一条")
    state.watcher.cycle()
    assert not state.calls and events(state)[0]["state"] == "observed"
    state.clock.advance(1)
    state.watcher.cycle()
    assert attempts == ["20002", "20002"] and state.desktop.clicks == 1
    assert traces(state)[0]["counters"]["cold_path"] == 1
    assert traces(state)[0]["counters"]["turn_sent"] == 1


@pytest.mark.parametrize("interruption", ["ledger_after_receipt", "terminal_trace_flush"])
def test_durable_receipt_recovery_keeps_unique_persistence_time_and_terminal(scenario, monkeypatch, interruption):
    state = scenario
    use_real_receipt_storage(state, monkeypatch)
    failures = []
    if interruption == "ledger_after_receipt":
        original = state.watcher.ledger.reconcile_sent

        def interrupted_ledger(*args, **kwargs):
            failures.append(1)
            if len(failures) == 1:
                raise OSError("synthetic ledger interruption after receipt commit")
            return original(*args, **kwargs)

        monkeypatch.setattr(state.watcher.ledger, "reconcile_sent", interrupted_ledger)
    else:
        original = QQTurnTrace.flush

        def interrupted_flush(self, path, **kwargs):
            if not self.snapshot()["counters"].get("turn_sent"):
                return original(self, path, **kwargs)
            failures.append(1)
            if len(failures) == 1:
                raise OSError("synthetic terminal trace flush interruption")
            return original(self, path, **kwargs)

        monkeypatch.setattr(QQTurnTrace, "flush", interrupted_flush)
    state.append("虚构的发送后恢复验证")
    state.watcher.cycle()
    assert state.desktop.clicks == 1 and state.watcher._pending_turn is None
    assert events(state)[0]["state"] == "sent" and not state.watcher.ledger.has_uncertain_send()
    receipt = decision.get_qq_delivery(memory_db=state.watcher.session.memory_db, group_id="10001",
                                       decision_id="qq-decision:synthetic-durable")
    assert receipt is not None and receipt.delivered_text == state.desktop.sent_texts[0]
    trace = traces(state)[0]
    assert len([stage for stage in trace["stages"] if stage["name"] == "receipt_persisted" and stage["outcome"] == "ok"]) == 1
    assert trace["counters"]["turn_sent"] == 1 and not trace["counters"].get("turn_failed")
    assert trace["counters"]["receipt_recoveries"] == 1
    assert state.watcher.status["last_turn_outcome"] == "sent"
