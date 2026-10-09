"""Read-only canary evidence collector. No transport, configuration writes or auto-promotion."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import closing
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3


def read(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def moment(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def p95(values):
    return sorted(values)[math.ceil(len(values) * .95) - 1] if values else None


def collect(*, memory_db, trace_dir, runtime_trace, since, account, group):
    start = moment(since)
    with closing(sqlite3.connect(Path(memory_db).resolve().as_uri() + "?mode=ro", uri=True)) as db:
        decisions = [read_row[0] for read_row in db.execute(
            "SELECT record_json FROM qq_reply_decisions WHERE group_id=?", (group,))]
        decisions = [json.loads(value) for value in decisions]
        decisions = [d for d in decisions if d.get("self_member_id") == account]
        receipts = {row[0] for row in db.execute(
            "SELECT decision_id FROM qq_reply_deliveries WHERE group_id=? AND decision_id IS NOT NULL", (group,))}
    decisions.sort(key=lambda d: moment(d["created_at"]))
    current = {d["decision_id"]: d for d in decisions if moment(d["created_at"]) >= start}
    previous = {d["decision_id"]: d for d in decisions if moment(d["created_at"]) < start and d["decision_id"] in receipts}
    previous = dict(list(previous.items())[-30:])
    timings, terminations = {}, {}
    if Path(runtime_trace).exists():
        with Path(runtime_trace).open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                key = row.get("decision_id")
                if key in current or key in previous:
                    timings[key] = row.get("elapsed_ms")
                    terminations[key] = row.get("termination")
    by_source = {}
    for key, decision in current.items():
        for source_id in decision.get("new_observation_record_ids", []):
            by_source.setdefault(source_id, set()).add(key)
    samples, unmapped = {}, []
    for folder in sorted((Path(trace_dir) / "calls").glob("*")):
        if folder.name < start.strftime("%Y%m%d"):
            continue
        for path in sorted(folder.glob("*.json")):
            try:
                call = read(path)
            except (ValueError, OSError):
                continue
            if call.get("kind") not in {"prepare_qq_response", "prepare"} or moment(call["ts"]) < start:
                continue
            compiled = next((step["data"] for step in call.get("steps", []) if step["kind"] == "compile"), {})
            task = compiled.get("response_semantics") or {}
            if task.get("audience") != "qq:group:" + group:
                continue
            matches = set.union(*(by_source.get(source, set()) for source in task.get("trigger_source_ids", []))) if task.get("trigger_source_ids") else set()
            if len(matches) != 1:
                unmapped.append(call["call_id"])
                continue
            key = matches.pop()
            candidates, validations = [], []
            for step in call.get("steps", []):
                if step["kind"] == "generate":
                    candidates.append(step["data"].get("text"))
                elif step["kind"] == "validate":
                    validations.append(step["data"])
            if not candidates:
                continue
            decision = current[key]
            samples[key] = {"decision_id": key, "call_id": call["call_id"], "created_at": decision["created_at"],
                "mode": task.get("mode"), "policy_version": task.get("version"), "task": task,
                "original_evidence": compiled.get("memory_evidence"), "candidates": candidates,
                "validations": validations, "outcome": decision["outcome"], "reply": decision.get("reply"),
                "complete_delivery_receipt": key in receipts, "end_to_end_latency_ms": timings.get(key),
                "termination": terminations.get(key), "human_review": None}
    available = [r for r in samples.values() if any(v.get("alignment", {}).get("status") in
        {"aligned", "misaligned", "uncertain", "not_applicable"} for v in r["validations"] if v.get("alignment"))]
    before = p95([timings[k] for k in previous if timings.get(k) is not None])
    after = p95([timings[k] for k in samples if timings.get(k) is not None])
    return {"collected_at": datetime.now(timezone.utc).isoformat(), "since": since,
        "scope": account + "/" + group, "unique_candidate_turns": len(samples),
        "candidate_turns_with_alignment": len(available), "twenty_candidate_threshold_met": len(available) >= 20,
        "actual_complete_replies": sum(k in receipts for k in current),
        "outcomes": dict(Counter(d["outcome"] for d in current.values())),
        "ready_without_complete_receipt": sum(d["outcome"] == "ready" and k not in receipts for k, d in current.items()),
        "prior_delivered_latency_samples": sum(timings.get(k) is not None for k in previous),
        "current_candidate_latency_samples": sum(timings.get(k) is not None for k in samples),
        "prior_p95_ms": before, "current_p95_ms": after,
        "p95_ratio": after / before if before and after else None,
        "unmapped_calls": unmapped, "samples": list(samples.values()),
        "human_review_complete": False, "enforce_allowed": False,
        "note": "Candidate count is per decision, not per rewrite. A missing delivery receipt is pending or incomplete, not automatically a send failure. Latency compares current candidates with the last 30 delivered replies in the same account and group; workloads may differ. Promotion additionally requires offline acceptance and explicit human review."}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--memory-db", type=Path, required=True)
    parser.add_argument("--trace-dir", type=Path, required=True)
    parser.add_argument("--runtime-trace", type=Path, required=True)
    parser.add_argument("--since", required=True)
    parser.add_argument("--account", required=True)
    parser.add_argument("--group", required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = collect(**{k: v for k, v in vars(args).items() if k != "output"})
    args.output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = args.output.with_name(args.output.name + f".{os.getpid()}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.replace(temporary, args.output)
    print(json.dumps({k: v for k, v in data.items() if k not in {"samples", "unmapped_calls", "note"}}))


if __name__ == "__main__":
    main()
