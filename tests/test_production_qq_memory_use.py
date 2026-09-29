"""Actual QQ gate/body decisions audit selected SQL memory use, not recall alone."""
from dataclasses import replace
import json
import sqlite3

import pytest

from harness.cognitive.qq_memory import QQMemoryControl, QQMemoryWorker
from harness.qq import decision
from harness.qq.telemetry import QQDeadline
from tests.test_production_memory import experience
from tests.test_production_memory_live_chain import production_qq as production_qq, _resume
from tests.test_qq_decision import setup as setup
from tests.test_qq_prepared_turn import _binding


@pytest.mark.parametrize("action", ["speak", "wait"])
@pytest.mark.parametrize("declared_use", [True, False])
def test_real_gate_body_and_replay_record_only_declared_visible_memory(setup, production_qq, action, declared_use):
    host, owner = production_qq
    store = host.shared.production_store
    shown = store.recall(audience="qq:group:10001")["entries"][:2]
    assert len(shown) == 2
    store.ingest([experience("unshown-memory", audience="qq:group:10001", text="retained but not presented")])
    assert store.validate_references(["unshown-memory"], audience="qq:group:10001")
    owner = replace(owner, derived_memory={"schema_version": "production-memory-v1", "entries": shown})
    selected = shown[0]["record_id"]
    answer = {"action": action, "reason": "Choose from the current conversation"}
    if declared_use:
        answer["used_memory_ids"] = [selected, "unshown-memory", "invented-id", selected]
    setup[1].choice = json.dumps(answer)
    binding = _binding()
    from harness.qq.prepared_evidence import capture_used_sources
    prepared = decision.prepare_qq_turn(**setup[3], trigger_member_id="20002", shared_owner=owner,
        turn_binding=binding, deadline=QQDeadline.after(60),
        used_sources_provider=lambda payload: capture_used_sources(host, payload))
    expected = [selected] if declared_use else []
    if action == "speak":
        assert prepared["kind"] == "prepared"
        snapshot = decision.get_qq_turn_checkpoint(memory_db=setup[0].path, prepare_id=prepared["prepare_id"])["snapshot"]
        assert snapshot["choice"]["used_memory_ids"] == expected
        assert snapshot["decision"].get("used_memory_ids", []) == expected
        result = _resume(setup, host, owner, binding, prepared)
        replay = _resume(setup, host, owner, binding, prepared)
        assert len(setup[1].chat_calls) == 1
        assessment = decision.combined_user_payload(setup[1].chat_calls[0])["assessment"]
        assert assessment["used_memory_ids"] == expected
    else:
        assert prepared["kind"] == "completed"
        result = decision.QQReplyDecision.model_validate(prepared["decision"])
        replay = decision.QQReplyDecision.model_validate(prepared["decision"])
        assert not setup[1].chat_calls
    assert result.used_memory_ids == replay.used_memory_ids == expected
    assert result.recalled_memory_ids == [entry["record_id"] for entry in shown]
    payload = decision.combined_user_payload(setup[1].gate_messages[0])
    assert payload["production_memory_use_schema"]["maxItems"] == 8
    assert "不要自动列出全部记忆" in setup[1].gate_messages[0][0]["content"]
    worker = object.__new__(QQMemoryWorker)
    worker.control, worker.host, worker._recall_pending = QQMemoryControl(recall_enabled=False), host, {}
    before = store.recall(audience="qq:group:10001")
    worker.note_recall(result)
    worker.note_recall(replay)
    with sqlite3.connect(store.path) as db:
        receipts = [(kind, json.loads(payload)) for kind, payload in db.execute(
            "SELECT kind,payload FROM pm_receipts WHERE event_id IN (?,?)",
            ("qq-exposure:" + result.decision_id, "qq-decision-use:" + result.decision_id))]
    assert len([row for row in receipts if row[0] == "context_exposure"]) == 1
    uses = [row[1] for row in receipts if row[0] == "decision_use"]
    assert uses == ([{"audience": "qq:group:10001", "ids": [selected]}] if declared_use else [])
    assert store.recall(audience="qq:group:10001") == before


def test_legacy_gate_and_empty_decision_wire_keep_original_shape(setup):
    assert "used_memory_ids" not in decision._GateDecision.model_json_schema()["properties"]
    result = decision.decide_qq_reply(**setup[3])
    assert result.outcome == "ready"
    assert result.used_memory_ids == []
    wire = result.model_dump_json()
    assert "used_memory_ids" not in wire
    assert decision.QQReplyDecision.model_validate_json(wire).model_dump_json() == wire
    assert "production_memory_use_schema" not in str(setup[1].gate_messages)
    assert "used_memory_ids" not in str(setup[1].gate_messages)
