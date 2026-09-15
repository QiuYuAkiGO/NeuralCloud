"""Read-only audit contracts over synthetic authoritative production receipts."""
from datetime import timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

import pytest

from eval.qq_brief.postrelease_audit import AuditError, MAX_CURRENT_REQUEST_CHARACTERS, collect_audit, main, _redact
from harness.qq import decision
from tests.test_qq_segment_ledger import setup, NOW, GROUP, SELF


VERSION = "audit-test-release"


def frozen(scenario, identity, *, version=VERSION):
    value = scenario.decision.model_copy(update={"decision_id": identity, "input_hash": identity,
                                                "prompt_version": version})
    with sqlite3.connect(scenario.memory) as connection:
        connection.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
                           (identity, identity, GROUP, "completed", value.model_dump_json()))
    return value


def sent(scenario, value, index, seconds):
    return decision.record_qq_segment_delivery(memory_db=scenario.memory, group_id=GROUP,
        self_member_id=SELF, decision_id=value.decision_id, segment_index=index,
        segment_count=len(value.reply_segments), delivered_text=value.reply_segments[index],
        evidence_refs=[f"test://{value.decision_id}/{index}"], verification="qq_ui_verified",
        source_record_ids=[scenario.source.record_id], delivered_at=NOW + timedelta(seconds=seconds))


def full(scenario, value, receipts, seconds):
    return decision.record_qq_delivery(memory_db=scenario.memory, group_id=GROUP, self_member_id=SELF,
        decision_id=value.decision_id, delivered_text=value.reply,
        evidence_refs=sorted({ref for receipt in receipts for ref in receipt.evidence_refs}),
        verification="qq_ui_verified", source_record_ids=[scenario.source.record_id],
        delivered_at=NOW + timedelta(seconds=seconds))


def audit(scenario, **kwargs):
    return collect_audit(scenario.memory, since=NOW, as_of=NOW + timedelta(minutes=5),
                         prompt_versions=[VERSION], **kwargs)


def test_missing_receipt_and_wrong_version_drafts_never_count(tmp_path):
    scenario = setup(tmp_path, ["第一句", "第二句"])
    frozen(scenario, "unsent")
    other = frozen(scenario, "other-version", version="older")
    sent(scenario, other, 0, 5)
    before = scenario.memory.read_bytes()
    result = audit(scenario)
    assert result["aggregate"]["verified_turns"] == 0
    assert result["turns"] == result["issues"] == []
    assert scenario.memory.read_bytes() == before


def test_partial_full_and_all_segments_without_aggregate_are_distinct(tmp_path):
    scenario = setup(tmp_path, ["第一句", "第二句"])
    partial = frozen(scenario, "partial")
    sent(scenario, partial, 0, 5)
    complete = frozen(scenario, "complete")
    receipts = [sent(scenario, complete, index, 10 + index) for index in range(2)]
    full(scenario, complete, receipts, 15)
    no_aggregate = frozen(scenario, "no-aggregate")
    for index in range(2):
        sent(scenario, no_aggregate, index, 20 + index)
    before = scenario.memory.read_bytes()
    result = audit(scenario)
    assert [item["coverage"] for item in result["turns"]] == [
        "partial_prefix", "aggregate_verified", "all_segments_verified_without_aggregate"]
    assert [item["delivered_segment_count"] for item in result["turns"]] == [1, 2, 2]
    assert len(result["turns"][1]["receipt_ids"]) == 3
    assert result["turns"][0]["content"] == "第一句"
    assert result["aggregate"]["verified_turns"] == 3
    assert scenario.memory.read_bytes() == before


@pytest.mark.parametrize("damage", ["receipt_hash", "missing_prefix", "missing_aggregate_child"])
def test_invalid_receipts_are_reported_and_never_counted(tmp_path, damage):
    scenario = setup(tmp_path, ["第一句", "第二句"])
    value = frozen(scenario, "bad")
    receipts = [sent(scenario, value, index, 5 + index) for index in range(2)]
    if damage == "missing_aggregate_child":
        full(scenario, value, receipts, 10)
    with sqlite3.connect(scenario.memory) as connection:
        if damage == "receipt_hash":
            connection.execute("UPDATE qq_reply_segment_deliveries SET receipt_hash='bad' WHERE receipt_id=?",
                               (receipts[0].receipt_id,))
        else:
            connection.execute("DELETE FROM qq_reply_segment_deliveries WHERE receipt_id=?", (receipts[0].receipt_id,))
    before = scenario.memory.read_bytes()
    result = audit(scenario)
    assert result["aggregate"]["verified_turns"] == 0
    assert result["aggregate"]["integrity_issues"] == 1
    assert scenario.memory.read_bytes() == before


def test_first_delivery_order_and_activation_cutoff_not_draft_creation(tmp_path):
    scenario = setup(tmp_path, ["第一句", "第二句"])
    late = frozen(scenario, "a-created-first")
    early = frozen(scenario, "z-created-later")
    old = frozen(scenario, "old-prefix")
    sent(scenario, late, 0, 20)
    sent(scenario, early, 0, 10)
    sent(scenario, old, 0, -10)
    sent(scenario, old, 1, 3)
    result = audit(scenario, limit=1)
    assert [item["decision_id"] for item in result["turns"]] == [early.decision_id]
    assert result["aggregate"]["first_cohort_complete"]


def test_candidate_bound_and_no_database_creation(tmp_path):
    with pytest.raises(AuditError):
        collect_audit(tmp_path / "missing.sqlite3", since=NOW, prompt_versions=[VERSION])
    assert not (tmp_path / "missing.sqlite3").exists()
    scenario = setup(tmp_path, ["第一句"])
    for index in range(3):
        value = frozen(scenario, f"turn-{index}")
        sent(scenario, value, 0, 5 + index)
    result = audit(scenario, limit=1, max_candidates=1)
    assert result["aggregate"]["candidate_limit_reached"]
    assert result["aggregate"]["scanned_candidate_turns"] == 1


def test_secret_redaction_does_not_change_source_metric_calculation():
    text = "key sk-" + "A" * 30 + " Bearer abc.def password=secretvalue"
    safe, changed = _redact(text)
    assert changed and "A" * 30 not in safe and "secretvalue" not in safe and "abc.def" not in safe


def test_cli_stdout_is_aggregate_only_and_artifact_never_overwrites(tmp_path, capsys):
    scenario = setup(tmp_path, ["私有正文 sk-" + "A" * 30])
    value = frozen(scenario, "cli-check")
    sent(scenario, value, 0, 5)
    output = tmp_path / "audit.json"
    source_root = Path(decision.__file__).resolve().parents[2]
    args = ["--release-root", str(source_root), "--database", str(scenario.memory),
            "--since", NOW.isoformat(), "--prompt-version", VERSION, "--output", str(output)]
    assert main(args) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["verified_turns"] == 1
    assert "私有正文" not in captured.out and "turns" not in json.loads(captured.out)
    report = json.loads(output.read_text(encoding="utf-8"))
    assert report["turns"][0]["content_redacted"]
    assert "A" * 30 not in report["turns"][0]["content"]
    before = output.read_bytes()
    assert main(args) == 2
    assert output.read_bytes() == before


def test_review_query_is_redacted_and_keeps_intent_detail_and_provenance(tmp_path):
    scenario = setup(tmp_path, ["详细说明" * 20])
    value = frozen(scenario, "review-query")
    query = "请详细说明。api_key=sk-" + "B" * 30
    value = decision.QQReplyDecision.model_validate({**value.model_dump(), "effective_query": query,
        "query_source": "explicit", "addressing": "direct",
        "addressing_source_record_ids": [scenario.source.record_id], "addressing_evidence_refs": ["test://chat"],
        "reply_plan": {"intent": "answer", "detail_level": "expanded"}})
    with sqlite3.connect(scenario.memory) as connection:
        connection.execute("UPDATE qq_reply_decisions SET record_json=? WHERE decision_id=?",
                           (value.model_dump_json(), value.decision_id))
    sent(scenario, value, 0, 5)
    turn = audit(scenario)["turns"][0]
    assert "B" * 30 not in turn["current_request"]
    assert turn["current_request_redacted"] and not turn["current_request_truncated"]
    assert turn["current_request_sha256"] == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert turn["query_source"] == "explicit" and turn["addressing"] == "direct"
    assert turn["reply_plan"] == {"intent": "answer", "detail_level": "expanded"}
    assert turn["reply_plan_available"]
    assert not turn["style"]["brief_length_exceeded"]
    assert turn["new_observation_record_ids"] == [scenario.source.record_id]


@pytest.mark.parametrize("query,derived", [("短的查询", True), ("字" * 2001, False)])
def test_review_query_marks_both_prior_and_local_truncation(tmp_path, query, derived):
    scenario = setup(tmp_path, ["正文"])
    value = frozen(scenario, "truncated-query").model_copy(update={
        "effective_query": query, "derived_query_truncated": derived})
    with sqlite3.connect(scenario.memory) as connection:
        connection.execute("UPDATE qq_reply_decisions SET record_json=? WHERE decision_id=?",
                           (value.model_dump_json(), value.decision_id))
    sent(scenario, value, 0, 5)
    turn = audit(scenario)["turns"][0]
    assert turn["current_request_truncated"]
    assert len(turn["current_request"]) <= MAX_CURRENT_REQUEST_CHARACTERS
    assert turn["current_request_sha256"] == hashlib.sha256(query.encode("utf-8")).hexdigest()
    assert turn["reply_plan"] == {"intent": "other", "detail_level": "brief"}
    assert not turn["reply_plan_available"]
