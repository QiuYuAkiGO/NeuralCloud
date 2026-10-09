"""Describe all preregistered trials; preserve failures and distinguish fixed labels from preferences."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean

from eval.qq_brief.common import digest, file_hash, read, save


def percentile(values, fraction=.95):
    ordered = sorted(values)
    if not ordered:
        return None
    index = (len(ordered) - 1) * fraction
    lower = int(index)
    return ordered[lower] + (ordered[min(lower + 1, len(ordered) - 1)] - ordered[lower]) * (index - lower)


def events_for(path, expected):
    if not path.exists():
        if expected:
            raise ValueError("http_log_missing")
        return []
    if file_hash(path) != expected:
        raise ValueError("http_log_hash_mismatch")
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def critic(root):
    manifest, cases = read(root / "manifest.json"), read(root / "cases.json")
    rows = []
    for case in cases:
        for repeat in range(1, manifest["repeats"] + 1):
            for arm in manifest["arms"]:
                for state in manifest["plan_states"]:
                    key = f"{case['id']}-r{repeat}-{state}-{arm}"
                    job = root / "trials" / key
                    if not (job / "result.json").exists():
                        rows.append({"key": key, "case_id": case["id"], "arm": arm, "plan": state,
                                     "repeat": repeat, "expected_pass": case["expected_pass"], "status": "missing"})
                        continue
                    result = read(job / "result.json")
                    unavailable = "semantic_review_unavailable" in result.get("validation", {}).get("violations", [])
                    result["critic_available"] = result["status"] == "ok" and not unavailable
                    result["operational_decision_matches_label"] = result.get("correct")
                    if not result["critic_available"]:
                        # A fail-closed rejection is not a successful assessment
                        # of a bad candidate, even though delivery was prevented.
                        result["correct"] = None
                    events = events_for(job / "http.jsonl", result["http_sha256"])
                    requests = [e for e in events if e.get("record_type") == "http_request_prepared"]
                    reports = []
                    for e in events:
                        if e.get("record_type") == "http_response_received":
                            try:
                                value = json.loads(e.get("response_content") or "null")
                            except (ValueError, TypeError):
                                continue
                            if isinstance(value, dict) and "coverage_complete" in value:
                                reports.append(value)
                    rows.append(result | {"key": key, "first_wire_hash": requests[0]["request_body_sha256"] if requests else None,
                                          "reports": reports})
    metrics = {}
    for arm in manifest["arms"]:
        metrics[arm] = {}
        for state in manifest["plan_states"]:
            selected = [r for r in rows if r["arm"] == arm and r["plan"] == state]
            ok = [r for r in selected if r.get("critic_available")]
            metrics[arm][state] = {
                "expected": len(selected), "scored": len(ok), "correct": sum(bool(r.get("correct")) for r in selected),
                "good_expected": sum(r["expected_pass"] for r in selected),
                "bad_expected": sum(not r["expected_pass"] for r in selected),
                "false_reject": sum(r["expected_pass"] and not r["correct"] for r in ok),
                "false_accept": sum(not r["expected_pass"] and not r["correct"] for r in ok),
                "blocked_good": sum(r["expected_pass"] and r.get("validation", {}).get("verdict") == "revise" for r in selected),
                "accepted_bad": sum(not r["expected_pass"] and r.get("validation", {}).get("verdict") == "pass" for r in selected),
                "unscored": len(selected) - len(ok), "requests": sum(r.get("model_requests", 0) for r in selected),
                "p95_ms": percentile([r["latency_ms"] for r in selected if "latency_ms" in r]),
            }
        pairs = []
        for case in cases:
            for repeat in range(1, manifest["repeats"] + 1):
                pair = [r for r in rows if r["arm"] == arm and r["case_id"] == case["id"] and r["repeat"] == repeat]
                pairs.append({"case_id": case["id"], "repeat": repeat,
                              "both_scored": all(r.get("critic_available") for r in pair),
                              "wire_same": len({r.get("first_wire_hash") for r in pair}) == 1 and bool(pair[0].get("first_wire_hash")),
                              "flip": len({r.get("validation", {}).get("verdict", "unscored") for r in pair}) != 1})
        metrics[arm]["pairs"] = {"expected": len(pairs), "both_scored": sum(p["both_scored"] for p in pairs),
                                 "wire_identical": sum(p["wire_same"] for p in pairs),
                                 "verdict_flips": sum(p["flip"] and p["both_scored"] for p in pairs),
                                 "operational_flips": sum(p["flip"] for p in pairs), "rows": pairs}
    return {"manifest": manifest, "manifest_sha256": file_hash(root / "manifest.json"), "cases": cases,
            "metrics": metrics, "rows": rows,
            "fixed_inputs_match": all(len({r.get("fixed_input_hash") for r in rows if r["case_id"] == c["id"]}) == 1 for c in cases)}


def body(root):
    manifest, config = read(root / "manifest.json"), read(root / "config.json")
    rows = []
    for key in manifest["cases"]:
        capture = read(root / "captures" / (key + ".json"))
        for arm in manifest["arms"]:
            job = root / "trials" / (key + "-" + arm)
            if not (job / "result.json").exists():
                rows.append({"key": key, "arm": arm, "status": "missing", "input": capture["input"]})
                continue
            result = read(job / "result.json")
            events = events_for(job / "http.jsonl", result["http_sha256"])
            drafts = [e["response_content"] for e in events
                      if e.get("record_type") == "http_response_received" and e.get("response_model") == config["main"]["model"]]
            rows.append(result | {"input": capture["input"], "drafts": drafts,
                                  "plan": capture["snapshot"]["choice"].get("reply_plan")})
    metrics = {}
    for arm in manifest["arms"]:
        selected = [r for r in rows if r["arm"] == arm]
        recorded = [r for r in selected if r["status"] != "missing"]
        metrics[arm] = {"expected": len(selected), "statuses": dict(Counter(r["status"] for r in selected)),
            "requests": sum(r.get("model_requests", 0) for r in selected),
            "mean_requests": mean(r["model_requests"] for r in recorded) if recorded else None,
            "p95_ms": percentile([r["latency_ms"] for r in recorded]), "quality_score": None}
    return {"manifest": manifest, "manifest_sha256": file_hash(root / "manifest.json"), "metrics": metrics, "rows": rows,
            "same_captures": all(len({r.get("capture_sha256") for r in rows if r["key"] == key}) == 1 for key in manifest["cases"])}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--mechanism", type=Path, required=True)
    p.add_argument("--followup", type=Path)
    p.add_argument("--body", type=Path)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    result = {"mechanism": critic(args.mechanism), "human_blind_preference": None,
              "quality_judge": "Prior grader failed controls; do not use it to select a winner."}
    if args.followup:
        result["followup"] = critic(args.followup)
    if args.body:
        result["body"] = body(args.body)
    save(args.output, result)
    print(json.dumps({"output": str(args.output), "sha256": digest(result),
                      "rows": {k: len(v["rows"]) for k, v in result.items() if isinstance(v, dict)}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
