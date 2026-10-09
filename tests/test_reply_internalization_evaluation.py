"""Guard the denominators and isolate evaluation artifacts from live rollout evidence."""
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

from eval.reply_internalization.acceptance import assess
from eval.reply_internalization.common import save
from eval.reply_internalization.scenarios import scenarios
from tools.diagnostics.qq_expression_observe import collect


def test_frozen_suite_covers_domains_turns_and_eight_deliveries():
    cases = scenarios()
    assert Counter(c["category"] for c in cases) == {"vision": 12, "memory": 4, "emotion": 4, "tool": 4}
    assert len({c["id"] for c in cases}) == 24
    assert all(len(c["turns"]) == 3 for c in cases)
    assert len(next(c for c in cases if c["id"] == "v09_repeated_boundary")["delivered_history"]) == 16
    from harness.cognitive.web import normalize_web_context
    from harness.qq.vision_tool import VisionAnswer
    for case in cases:
        for turn in case["turns"]:
            if "web" in turn:
                normalize_web_context(turn["web"])
            if "image" in turn and "error" not in turn["image"]:
                VisionAnswer.model_validate({"images": [{"image_id": "image_1", **turn["image"]}]})
    success = next(c for c in cases if c["id"] == "t01_success")["turns"][0]["web"]
    partial = next(c for c in cases if c["id"] == "t03_partial")["turns"][0]["web"]
    assert success["sources"][0]["content"]
    assert "content" not in partial["sources"][0] and partial["sources"][0]["snippet"]


def test_missing_or_unrated_results_cannot_pass(tmp_path):
    save(tmp_path / "manifest.json", {"ablation": False})
    save(tmp_path / "scenarios.json", scenarios())
    save(tmp_path / "generation-config.json", {"production_snapshot_sha256": "frozen"})
    result = assess(tmp_path, "live")
    assert result["offline_accepted"] is False
    assert result["unscored_per_arm"] == {"baseline": 144, "candidate": 144}
    assert len(result["missing"]) == 288
    assert not result["gates"]["necessary_qualification_100pct"]
    assert not result["gates"]["human_review_complete"]


def test_simulated_database_cannot_count_as_live_sample(tmp_path):
    path = tmp_path / "fixture.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE expression_evaluation_marker (simulation_only INTEGER)")
    with pytest.raises(ValueError, match="evaluation_database"):
        collect(memory_db=path, account="29999", conversation="10001", since="2026-10-09T00:00:00+00:00", mode="observe")


def test_phase_collector_requires_complete_scoped_receipts_and_bound_reviews(tmp_path):
    from harness.core.expression import EXPRESSION_VERSION
    from harness.qq.decision import QQDeliveryReceipt, QQReplyDecision
    path = tmp_path / "receipts.sqlite3"
    now = datetime(2026, 10, 9, 1, tzinfo=timezone.utc)
    decisions, receipts, reviews = [], [], {}
    for index in range(32):
        text = f"完整回复{index}"
        decision = QQReplyDecision(decision_id=f"d{index}", input_hash="hash", created_at=now,
            action="speak", reason="测试", reply=text, decision_source="model", group_id="10001",
            self_member_id="29999", participants=[], source_record_ids=[], new_observation_record_ids=[],
            evidence_refs=[], delivery_refs=[], model="test", persona_version="test", preset_id="test",
            prompt_version=f"gate:{EXPRESSION_VERSION}:observe", outcome="ready",
            model_call_counts={"vision": int(index < 10)})
        receipt = QQDeliveryReceipt(receipt_id=f"r{index}", group_id="10001", self_member_id="29999",
            decision_id=decision.decision_id, delivered_text=text, evidence_refs=["receipt-proof"],
            verification="qq_ui_verified", delivered_at=now, origin="decision")
        if index == 30:
            receipt = receipt.model_copy(update={"delivered_text": "只送达了一部分"})
        if index == 31:
            decision = decision.model_copy(update={"self_member_id": "29998"})
            receipt = receipt.model_copy(update={"self_member_id": "29998"})
        decisions.append(decision)
        receipts.append(receipt)
        reviews[decision.decision_id] = {"reply_sha256": hashlib.sha256(text.encode()).hexdigest(),
            "category": "vision" if index < 10 else "memory", "new_factual_violation": False,
            "missing_necessary_qualification": False}
    # Duplicate durable rows cannot turn a single delivery into two samples.
    receipts.append(receipts[0].model_copy(update={"receipt_id": "duplicate"}))
    with sqlite3.connect(path) as db:
        for table, records in (("qq_reply_decisions", decisions), ("qq_reply_deliveries", receipts)):
            db.execute(f"CREATE TABLE {table} (group_id TEXT, record_json TEXT)")
            db.executemany(f"INSERT INTO {table} VALUES (?, ?)", [(r.group_id, r.model_dump_json()) for r in records])
    kwargs = dict(memory_db=path, account="29999", conversation="10001", since="2026-10-09T00:00:00+00:00", mode="observe")
    result = collect(**kwargs, reviews=reviews)
    assert result["complete_deliveries"] == 30 and result["vision_deliveries"] == 10
    assert result["phase_review_passed"] and not result["promotion_allowed"]
    reviews["d0"]["missing_necessary_qualification"] = True
    result = collect(**kwargs, reviews=reviews)
    assert result["disable_required"] and not result["phase_review_passed"]
    reviews["d0"]["reply_sha256"] = "different-reply"
    result = collect(**kwargs, reviews=json.loads(json.dumps(reviews)))
    assert not result["human_review_complete"] and not result["phase_review_passed"]


