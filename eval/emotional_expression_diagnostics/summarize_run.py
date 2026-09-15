"""Read-only descriptive run summary; never produce or import human ratings.

Usage: python -m eval.emotional_expression_diagnostics.summarize_run --run PATH
The command prints aggregate JSON only; no response/prompt text or identity files.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import statistics
from typing import Any

from .models import digest, read_jsonl


def _mean(values: list[float]) -> float | None:
    return statistics.mean(values) if values else None


def summarize_run(run_dir: Path) -> dict[str, Any]:
    manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    if digest(manifest["spec"]) != manifest.get("spec_sha256"):
        raise ValueError("run manifest does not match its frozen specification hash")
    result_path = run_dir / "run-result.json"
    if not result_path.exists():
        raise ValueError("seal the run before descriptive analysis")
    result = json.loads(result_path.read_text(encoding="utf-8"))
    if result.get("spec_sha256") != manifest["spec_sha256"]:
        raise ValueError("run result and manifest identify different frozen specifications")
    rows = read_jsonl(run_dir / "requests.jsonl")
    request_ids = [row["request_id"] for row in rows if row.get("request_id")]
    if len(request_ids) != len(set(request_ids)):
        raise ValueError("duplicate request records would inflate descriptive statistics")
    grouped: dict[tuple[str, str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["route"], row["family"], row["condition"], row["kind"])].append(row)
    groups = []
    for (route, family, condition, kind), items in sorted(grouped.items()):
        responses = [row for row in items if isinstance(row.get("result"), dict)]
        valid = [row for row in responses if row["status"] == "completed"]
        scores = [row["probe_evaluation"] for row in responses if isinstance(row.get("probe_evaluation"), dict)]
        field_keys = sorted({key for score in scores for key in score.get("field_correctness", {})})
        # First average repeated outputs within cases and then variants within clusters.
        cases: dict[tuple[str, str], list[float]] = defaultdict(list)
        for row in valid:
            if kind == "probe":
                score = row.get("probe_evaluation", {})
                if score.get("total_fields"):
                    cases[(row["cluster_id"], row["case_id"])].append(score["correct_fields"] / score["total_fields"])
        clusters: dict[str, list[float]] = defaultdict(list)
        for (cluster, _case), values in cases.items():
            clusters[cluster].append(statistics.mean(values))
        groups.append({
            "route": route, "family": family, "condition": condition, "kind": kind,
            "recorded": len(items), "response_records": len(responses), "completed": len(valid),
            "status_counts": dict(Counter(row["status"] for row in items)),
            "independent_clusters": len({row["cluster_id"] for row in items}),
            "case_variants": len({row["case_id"] for row in items}),
            "response_models": sorted({row["result"]["response_model"] for row in responses if row["result"].get("response_model")}),
            "mean_response_elapsed_ms": _mean([row["result"]["elapsed_ms"] for row in responses if isinstance(row["result"].get("elapsed_ms"), (int, float))]),
            "mean_completed_response_characters": _mean([len(row.get("final_content") or "") for row in valid]),
            "prompt_tokens": sum(row["result"].get("usage", {}).get("prompt_tokens", 0) for row in responses if isinstance(row["result"].get("usage"), dict)),
            "completion_tokens": sum(row["result"].get("usage", {}).get("completion_tokens", 0) for row in responses if isinstance(row["result"].get("usage"), dict)),
            "probe_valid_json_count": sum(bool(score.get("valid")) for score in scores),
            "probe_exact_contract_count": sum(score.get("valid") and score.get("correct_fields") == score.get("total_fields") for score in scores),
            "probe_category_accuracy": {key: _mean([int(score.get("field_correctness", {}).get(key, False)) for score in scores]) for key in field_keys},
            "probe_cluster_mean_category_accuracy": _mean([statistics.mean(values) for values in clusters.values()]),
        })
    observed_counts = {
        "recorded": len(rows),
        "completed": sum(row["status"] == "completed" for row in rows),
        "unobservable_skipped": sum(row["status"] == "unobservable_contrast" for row in rows),
        "responses": sum(isinstance(row.get("result"), dict) for row in rows),
    }
    expected_counts = result.get("counts", {})
    mismatches = [key for key in ("completed", "unobservable_skipped") if observed_counts[key] != expected_counts.get(key)]
    return {
        "schema_version": "expression-descriptive-summary-v1", "spec_sha256": manifest["spec_sha256"],
        "live": manifest.get("live", False), "sealed_run_status": result.get("status"),
        "status": "descriptive_only", "behavioral_confirmation": "inconclusive", "release_eligible": False,
        "human_scores_used": False, "reported_counts": expected_counts, "observed_counts": observed_counts,
        "count_mismatches": mismatches, "groups": groups,
        "limitations": [
            "Probe scores are the immutable evaluations recorded during generation, not a new semantic or human assessment.",
            "Five probe categories measure ownership, numeric direction, trend, explicit uncertainty and unsupported causes.",
            "Single-state probes do not directly test field-meaning paraphrases, intensity interpretation or two-state magnitude comparison.",
            "Output length and tokens are descriptive costs, never emotional-expression quality scores.",
            "Pilot families and repeated responses cannot establish confirmation or release readiness.",
        ],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run", type=Path, required=True)
    args = parser.parse_args(argv)
    result = summarize_run(args.run)
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if result["count_mismatches"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
