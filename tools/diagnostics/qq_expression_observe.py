"""Read-only phase collector. Counts complete receipts, never drafts or simulated eval output."""
import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime
import hashlib
import json
from pathlib import Path
import sqlite3


def collect(*, memory_db, account, conversation, since, mode, reviews=None):
    from harness.core.expression import EXPRESSION_VERSION
    from harness.qq.decision import QQDeliveryReceipt, QQReplyDecision
    start = datetime.fromisoformat(since.replace("Z", "+00:00"))
    if start.tzinfo is None:
        raise ValueError("since_requires_timezone")
    with closing(sqlite3.connect(Path(memory_db).resolve().as_uri()+"?mode=ro", uri=True)) as db:
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='expression_evaluation_marker'").fetchone():
            raise ValueError("evaluation_database_cannot_supply_live_deliveries")
        decisions = [QQReplyDecision.model_validate_json(row[0]) for row in db.execute(
            "SELECT record_json FROM qq_reply_decisions WHERE group_id=?", (conversation,))]
        receipts = [QQDeliveryReceipt.model_validate_json(row[0]) for row in db.execute(
            "SELECT record_json FROM qq_reply_deliveries WHERE group_id=?", (conversation,))]
    token = ":" + EXPRESSION_VERSION + ":" + mode
    scoped = {d.decision_id: d for d in decisions if d.self_member_id == account
              and d.created_at >= start and token in d.prompt_version}
    delivered = {}
    for receipt in receipts:
        d = scoped.get(receipt.decision_id)
        if (d and d.outcome == "ready" and receipt.self_member_id == account and receipt.origin == "decision"
                and receipt.delivered_at >= start and receipt.delivered_text == d.reply
                and not any(ref.startswith("fixture:") for ref in receipt.evidence_refs)):
            delivered[d.decision_id] = receipt
    samples = []
    for key, d in scoped.items():
        receipt = delivered.get(key)
        reply_hash = hashlib.sha256((d.reply or "").encode()).hexdigest()
        review = (reviews or {}).get(key)
        if review and review.get("reply_sha256") != reply_hash:
            review = None
        samples.append({"decision_id": key, "outcome": d.outcome, "created_at": d.created_at.isoformat(),
            "receipt_id": receipt.receipt_id if receipt else None, "delivered": bool(receipt),
            "reply": d.reply, "reply_sha256": reply_hash, "diagnostics": d.expression_diagnostics,
            "vision": d.model_call_counts.get("vision", 0) > 0,
            "model_call_counts": d.model_call_counts, "timings_ms": d.timings_ms, "review": review})
    actual = [s for s in samples if s["delivered"]]
    valid_reviews = [s for s in actual if s["review"] and s["review"].get("category") in {"vision", "memory", "emotion", "tool"}
                     and type(s["review"].get("new_factual_violation")) is bool
                     and type(s["review"].get("missing_necessary_qualification")) is bool]
    rollback = any(s["review"]["new_factual_violation"] or s["review"]["missing_necessary_qualification"] for s in valid_reviews)
    sample_gate = len(actual) >= 30 and sum(s["vision"] for s in actual) >= 10
    return {"version": EXPRESSION_VERSION, "mode": mode, "scope": account+"/"+conversation,
        "complete_deliveries": len(actual), "vision_deliveries": sum(s["vision"] for s in actual),
        "outcomes": dict(Counter(s["outcome"] for s in samples)),
        "diagnostic_status": dict(Counter((s["diagnostics"] or {}).get("status", "unavailable") for s in actual)),
        "classified_reviews": dict(Counter(s["review"]["category"] for s in valid_reviews)),
        "sample_threshold_met": sample_gate, "human_review_complete": len(valid_reviews) == len(actual) and bool(actual),
        "disable_required": rollback, "phase_review_passed": sample_gate and len(valid_reviews) == len(actual) and not rollback,
        "promotion_allowed": False, "samples": samples,
        "note": "Phase evidence only. Offline gates and public-image E2E are required separately. This tool never changes settings."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-db", type=Path, required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--conversation", required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--mode", choices=("observe", "enforce"), required=True)
    parser.add_argument("--reviews", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    reviews = json.loads(args.reviews.read_text(encoding="utf-8")) if args.reviews else None
    result = collect(memory_db=args.memory_db, account=args.account, conversation=args.conversation,
                     since=args.since, mode=args.mode, reviews=reviews)
    if args.output.exists():
        raise ValueError("output_exists_preserve_prior_evidence")
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"samples", "note"}}))


if __name__ == "__main__":
    main()
