"""Describe all frozen outcomes without treating unvalidated model scores as truth."""

import argparse
from collections import Counter
import json
from pathlib import Path
from statistics import mean

from .common import digest, file_hash, read, save


def percentile(values, fraction):
    values = sorted(values)
    if not values:
        return None
    index = (len(values) - 1) * fraction
    lower = int(index)
    return values[lower] + (values[min(lower + 1, len(values) - 1)] - values[lower]) * (index - lower)


def generation(root):
    manifest = read(root / "manifest.json")
    scenarios = {s["id"]: s for s in read(root / "scenarios.json")}
    rows = []
    for scenario in manifest["scenarios"]:
        for repeat in range(1, manifest["repeats"] + 1):
            for arm in manifest["arms"]:
                name = f"{scenario}-r{repeat}-{arm}"
                path = root / "generation" / "live" / name / "turn-1.json"
                if not path.exists():
                    rows.append(
                        {
                            "id": name,
                            "scenario_id": scenario,
                            "repeat": repeat,
                            "arm": arm,
                            "status": "missing_record",
                            "reply": None,
                            "case": scenarios[scenario],
                        }
                    )
                    continue
                row = read(path)
                events = [
                    json.loads(line)
                    for line in path.with_name("turn-1.http.jsonl").read_text(encoding="utf-8").splitlines()
                ]
                assert file_hash(path.with_name("turn-1.http.jsonl")) == row["http_evidence_sha256"]
                responses = [e for e in events if e.get("record_type") == "http_response_received"]
                main = read(root / "generation-config.json")["main"]["model"]
                drafts = [e["response_content"] for e in responses if e.get("response_model") == main]
                critics = []
                for event in responses:
                    try:
                        value = json.loads(event.get("response_content") or "null")
                    except (TypeError, ValueError):
                        continue
                    if isinstance(value, dict) and "coverage_complete" in value:
                        critics.append(value)
                rows.append(
                    {
                        **{
                            k: row.get(k)
                            for k in (
                                "scenario_id",
                                "arm",
                                "repeat",
                                "status",
                                "reply",
                                "latency_ms",
                                "model_requests",
                                "response_models",
                                "fixture_tool_requests",
                                "input_sha256",
                                "source_manifest_sha256",
                                "error",
                            )
                        },
                        "id": name,
                        "case": scenarios[scenario],
                        "record_sha256": digest(row),
                        "drafts": drafts,
                        "critics": critics,
                        "outcome": (row.get("decision") or {}).get("outcome"),
                        "error_type": (row.get("decision") or {}).get("error_type"),
                    }
                )
    metrics = {}
    for arm in manifest["arms"]:
        selected = [r for r in rows if r["arm"] == arm]
        complete = [r for r in selected if r["status"] != "missing_record"]
        metrics[arm] = {
            "expected": len(selected),
            "recorded": len(complete),
            "statuses": dict(Counter(r["status"] for r in selected)),
            "ready": sum(r["status"] == "ok" for r in selected),
            "mean_requests": mean(r["model_requests"] for r in complete) if complete else None,
            "requests_total": sum(r["model_requests"] for r in complete),
            "p95_ms_all_recorded": percentile([r["latency_ms"] for r in complete], 0.95),
            "mean_ms_all_recorded": mean(r["latency_ms"] for r in complete) if complete else None,
            "drafts_with_critic_retry": sum(len(r.get("critics", [])) > 1 for r in selected),
            "categories": {
                category: {
                    "n": len(subset := [r for r in selected if r["case"]["category"] == category]),
                    "ready": sum(r["status"] == "ok" for r in subset),
                }
                for category in sorted({r["case"]["category"] for r in selected})
            },
            "scenarios": {
                scenario: {
                    "n": len(subset := [r for r in selected if r["scenario_id"] == scenario]),
                    "ready": sum(r["status"] == "ok" for r in subset),
                }
                for scenario in manifest["scenarios"]
            },
        }
    matched = all(
        len(
            {
                r["input_sha256"]
                for r in rows
                if r["scenario_id"] == scenario and r["repeat"] == repeat and r.get("input_sha256")
            }
        )
        == 1
        for scenario in manifest["scenarios"]
        for repeat in (1, 2)
    )
    return {
        "manifest_sha256": file_hash(root / "manifest.json"),
        "manifest": manifest,
        "same_input_hash_across_arms": matched,
        "metrics": metrics,
        "rows": rows,
    }


def analyze(root):
    factorial = generation(root / "factorial")
    receipts = generation(root / "receipts")
    critic = [read(p) for p in sorted((root / "factorial/critic").glob("*/*.json"))]
    critic_metrics = {}
    for arm in ("control", "cards"):
        selected = [r for r in critic if r["arm"] == arm]
        passed = [r for r in selected if r["case"]["expected_pass"]]
        rejected = [r for r in selected if not r["case"]["expected_pass"]]
        critic_metrics[arm] = {
            "expected": 48,
            "recorded": len(selected),
            "correct": sum(r.get("correct", False) for r in selected),
            "false_rejects": sum(r["status"] == "ok" and r["validation"]["verdict"] != "pass" for r in passed),
            "false_accepts": sum(r["status"] == "ok" and r["validation"]["verdict"] == "pass" for r in rejected),
            "positive_n": len(passed),
            "negative_n": len(rejected),
            "unscored": sum(r["status"] != "ok" for r in selected),
        }
    controls = [read(p) for p in sorted((root / "screen-v2/grader-controls").glob("*.json"))]
    scored = [r for r in controls if r["status"] == "scored"]
    fields = list(scored[0]["gold"]) if scored else []
    screen = {
        "policy": factorial["manifest"]["screening_policy"],
        "result": read(root / "screen-v2/screening-control-result.json"),
        "controls": controls,
        "field_accuracy": {
            k: {"correct": sum(r["grade"][k] == r["gold"][k] for r in scored), "n": len(scored)} for k in fields
        },
        "failed_v1_unscored": len(list((root / "factorial/grader-controls").glob("*.json"))),
        "generation_quality_metrics": None,
        "reason": "Screening failed its preregistered controls; no automatic naturalness or safety win rate is reported.",
    }
    return {
        "version": "misty-uncertainty-research-report-v1",
        "factorial": factorial,
        "critic": {"metrics": critic_metrics, "rows": critic},
        "screening": screen,
        "receipts": receipts,
        "limitations": [
            "Development fixtures, two repeats; not a held-out release evaluation.",
            "Fixed previous v6 research source, not the concurrently changed online release.",
            "Vision and web outputs were frozen; no public-image vision endpoint evaluation in this pilot.",
            "Generation readiness is an operational outcome, not a quality or factual accuracy score.",
            "Model-based screening was disqualified by its controls; examples are qualitative implementation-agent review.",
            "No independent human blind review; repeated versions of the same case are not independent evidence.",
            "Latency is descriptive under experimental concurrency, not a production P95 acceptance test.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    result = analyze(args.root)
    save(args.output, result)
    print(
        json.dumps(
            {
                "factorial": result["factorial"]["metrics"],
                "critic": result["critic"]["metrics"],
                "receipts": result["receipts"]["metrics"],
                "screen": result["screening"]["field_accuracy"],
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
