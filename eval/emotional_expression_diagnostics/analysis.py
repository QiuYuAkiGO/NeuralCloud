"""Cluster-level paired estimates with conservative, human-only decision gates."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

import numpy as np

from .models import DiagnosticRunSpec, read_jsonl, write_json
from .ratings import BlindRating, export_blind_samples


def paired_estimate(differences: list[float], *, seed: int, samples: int = 10000) -> dict[str, Any]:
    if not differences:
        return {"clusters": 0, "difference": None, "ci95": None, "p_value": None}
    values = np.asarray(differences, dtype=float)
    n = len(values)
    observed = float(values.mean())
    if n < 2:
        return {"clusters": n, "difference": observed, "ci95": None, "p_value": None}
    rng = np.random.default_rng(seed)
    draws = values[rng.integers(0, n, size=(samples, n))].mean(axis=1)
    ci = [float(item) for item in np.quantile(draws, [0.025, 0.975])]
    # A separate paired sign-randomization test supplies p; bootstrap sign frequency is not a p-value.
    if n <= 16:
        masks = np.arange(2**n, dtype=np.uint32)[:, None]
        signs = ((masks >> np.arange(n, dtype=np.uint32)) & 1) * 2.0 - 1.0
        null = (signs * values).mean(axis=1)
        p = float(np.mean(np.abs(null) >= abs(observed) - 1e-12))
    else:
        signs = rng.choice([-1.0, 1.0], size=(samples, n))
        extreme = int(np.sum(np.abs((signs * values).mean(axis=1)) >= abs(observed) - 1e-12))
        p = (extreme + 1) / (samples + 1)
    return {"clusters": n, "difference": observed, "ci95": ci, "p_value": p,
            "interval_method": "cluster_percentile_bootstrap", "p_method": "paired_cluster_sign_randomization"}


def holm(p_values: list[float | None]) -> list[float | None]:
    finite = sorted((p, index) for index, p in enumerate(p_values) if p is not None)
    adjusted: list[float | None] = [None] * len(p_values)
    previous = 0.0
    for rank, (value, index) in enumerate(finite):
        previous = max(previous, min(1.0, (len(finite) - rank) * value))
        adjusted[index] = previous
    return adjusted


def wilson(successes: int, n: int) -> list[float] | None:
    if not n:
        return None
    z = 1.959963984540054
    proportion = successes / n
    denominator = 1 + z * z / n
    centre = (proportion + z * z / (2 * n)) / denominator
    margin = z * math.sqrt(proportion * (1 - proportion) / n + z * z / (4 * n * n)) / denominator
    return [max(0.0, centre - margin), min(1.0, centre + margin)]


def _cluster_values(mapping: dict, ratings: dict[str, list[BlindRating]], *, field: str, negative_only: bool = False) -> dict:
    # Equal human weight, then repeat mean per variant, then equal variant weight per independent episode.
    by_case: dict[tuple[str, str, str], list[float]] = defaultdict(list)
    for sample_id, identity in mapping.items():
        if negative_only and not identity["negative_control"]:
            continue
        sample_ratings = ratings.get(sample_id, [])
        if len(sample_ratings) < 2:
            continue
        values = [float(row.overexpression) if field == "overexpression" else float(getattr(row.scores, field)) for row in sample_ratings]
        by_case[(identity["cluster_id"], identity["condition"], identity["case_id"])].append(float(np.mean(values)))
    clusters: dict[tuple[str, str], list[float]] = defaultdict(list)
    for (cluster, condition, _case), values in by_case.items():
        clusters[(cluster, condition)].append(float(np.mean(values)))
    return {key: float(np.mean(values)) for key, values in clusters.items()}


def analyze_run(run_dir: Path) -> dict[str, Any]:
    export_blind_samples(run_dir)
    manifest = json.loads((run_dir / "run-manifest.json").read_text(encoding="utf-8"))
    spec = DiagnosticRunSpec.model_validate(manifest["spec"])
    run_result = json.loads((run_dir / "run-result.json").read_text(encoding="utf-8"))
    mapping = json.loads((run_dir / "blind-map.json").read_text(encoding="utf-8"))
    raw = [BlindRating.model_validate(row) for row in read_jsonl(run_dir / "ratings.jsonl")]
    humans: dict[str, list[BlindRating]] = defaultdict(list)
    seen = set()
    for row in raw:
        if row.sample_id not in mapping:
            raise ValueError("ratings include unknown sample")
        key = row.sample_id, row.rater_id
        if key in seen:
            raise ValueError("duplicate human rating would inflate sample size")
        seen.add(key)
        if row.rater_type == "human" and row.calibrated_on_non_test_samples:
            humans[row.sample_id].append(row)
    missing = [sample for sample in mapping if len(humans[sample]) < 2]
    high_risk = [{"sample_id": sample_id, "rater_id": row.rater_id, "violations": row.violations.model_dump(), "evidence": list(row.evidence)}
                 for sample_id, rows in humans.items() for row in rows if any(row.violations.model_dump().values())]
    original_disagreements = []
    for sample_id, rows in humans.items():
        if len(rows) >= 2 and len({json.dumps(row.scores.model_dump(), sort_keys=True) for row in rows}) > 1:
            original_disagreements.append(sample_id)
    estimators = {field: _cluster_values(mapping, humans, field=field) for field in ("adaptation", "recognizability", "persona", "naturalness")}
    negative = _cluster_values(mapping, humans, field="overexpression", negative_only=True)
    cluster_identity = {case.cluster_id: {"route": case.route, "family": case.family} for case in spec.cases}
    results = []
    for candidate, baseline in spec.analysis.comparisons:
        clusters = sorted(cluster for cluster, condition in estimators["adaptation"] if condition == candidate and (cluster, baseline) in estimators["adaptation"])
        metrics = {}
        for field, values in estimators.items():
            differences = [values[(cluster, candidate)] - values[(cluster, baseline)] for cluster in clusters]
            metrics[field] = paired_estimate(differences, seed=spec.seed, samples=spec.analysis.bootstrap_samples)
        negative_clusters = sorted(cluster for cluster, condition in negative if condition == candidate and (cluster, baseline) in negative)
        neg_diffs = [negative[(cluster, candidate)] - negative[(cluster, baseline)] for cluster in negative_clusters]
        neg = paired_estimate(neg_diffs, seed=spec.seed, samples=spec.analysis.bootstrap_samples)
        # Report rare-event uncertainty even with zero observed cases; a zero-width bootstrap would be misleading.
        candidate_events = sum(negative[(cluster, candidate)] > 0 for cluster in negative_clusters)
        baseline_events = sum(negative[(cluster, baseline)] > 0 for cluster in negative_clusters)
        candidate_ci = wilson(candidate_events, len(negative_clusters))
        baseline_ci = wilson(baseline_events, len(negative_clusters))
        neg["any_overexpression_episode_rate_ci95"] = {"candidate": candidate_ci, "baseline": baseline_ci}
        neg["conservative_increase_upper95"] = candidate_ci[1] - baseline_ci[0] if candidate_ci and baseline_ci else None
        strata = []
        for dimension in ("route", "family"):
            labels = sorted({cluster_identity[cluster][dimension] for cluster in clusters})
            for label in labels:
                subgroup = [cluster for cluster in clusters if cluster_identity[cluster][dimension] == label]
                differences = [estimators["adaptation"][(cluster, candidate)] - estimators["adaptation"][(cluster, baseline)] for cluster in subgroup]
                strata.append({"dimension": dimension, "label": label,
                               "descriptive_only": len(subgroup) < spec.analysis.minimum_clusters,
                               **paired_estimate(differences, seed=spec.seed, samples=spec.analysis.bootstrap_samples)})
        results.append({"candidate": candidate, "baseline": baseline, "metrics": metrics, "negative_control": neg, "strata": strata})
    corrected = holm([result["metrics"]["adaptation"]["p_value"] for result in results])
    global_reasons = []
    if not manifest["live"]:
        global_reasons.append("offline_fake_is_not_behavioral_evidence")
    if spec.phase != "confirmation":
        global_reasons.append("pilot_is_exploratory")
    if missing or not mapping:
        global_reasons.append("two_calibrated_independent_human_ratings_required")
    if not all(case.information_reviewed and case.information_reviewer for case in spec.cases):
        global_reasons.append("information_inventory_not_independently_reviewed")
    if run_result["counts"]["failed"] or run_result["stop_reason"]:
        global_reasons.append("incomplete_or_failed_run")
    if run_result.get("backend_identity_drift"):
        global_reasons.append("backend_identity_changed_split_analysis_required")
    actual_rows = read_jsonl(run_dir / "requests.jsonl")
    if manifest["live"] and any("result" in row and (not row["result"].get("http_payload_verified") or
                            not row["result"].get("response_model")) for row in actual_rows):
        global_reasons.append("actual_payload_or_response_identity_unverified")
    if spec.analysis.max_latency_ms is None:
        global_reasons.append("deployment_latency_budget_not_frozen")
    if high_risk:
        global_reasons.append("high_risk_flags_require_human_adjudication_and_candidate_repair")
    for result, adjusted_p in zip(results, corrected):
        result["holm_adjusted_p"] = adjusted_p
        adaptation = result["metrics"]["adaptation"]
        reasons = list(global_reasons)
        if result["candidate"].startswith("A-") or result["baseline"].startswith("A-"):
            reasons.append("A_baseline_is_reference_only_not_a_controlled_causal_comparison")
        if adaptation["clusters"] < spec.analysis.minimum_clusters:
            reasons.append("insufficient_independent_clusters")
        ci = adaptation["ci95"]
        gates = {"adaptation_effect": bool(ci and adaptation["difference"] >= spec.analysis.minimum_adaptation_gain and ci[0] > 0),
                 "multiple_comparisons": adjusted_p is not None and adjusted_p < 0.05}
        for field in ("persona", "naturalness"):
            bounds = result["metrics"][field]["ci95"]
            gates[field + "_noninferiority"] = bool(bounds and bounds[0] >= -spec.analysis.noninferiority_margin)
        upper = result["negative_control"]["conservative_increase_upper95"]
        gates["negative_control_noninferiority"] = upper is not None and upper <= spec.analysis.overexpression_margin
        requests = read_jsonl(run_dir / "requests.jsonl")
        latencies = [row["result"].get("elapsed_ms", 0) for row in requests if "result" in row]
        gates["latency_budget"] = bool(spec.analysis.max_latency_ms and latencies and max(latencies) <= spec.analysis.max_latency_ms)
        gates["budget"] = float(run_result["budget"].get("exposure_cny", 0)) <= 45
        result["gates"] = gates
        result["status"] = "inconclusive" if reasons or upper is None or not gates["negative_control_noninferiority"] else "supported" if all(gates.values()) else "not_supported"
        result["limitations"] = reasons + ([] if gates["negative_control_noninferiority"] else ["negative_control_interval_does_not_establish_noninferiority"])
    report = {"schema_version": "expression-analysis-v1", "release_eligible": False,
              "phase": spec.phase, "spec_sha256": manifest["spec_sha256"],
              "status": "supported" if results and all(item["status"] == "supported" for item in results) else "inconclusive",
              "sample_flow": {"blind_samples": len(mapping), "human_ratings": sum(map(len, humans.values())), "samples_missing_two_humans": len(missing),
                              "independent_clusters": len(cluster_identity), **run_result["counts"]},
              "original_disagreements": original_disagreements, "high_risk_flags": high_risk,
              "auxiliary_ratings_count": len(read_jsonl(run_dir / "aux-ratings.jsonl")),
              "comparisons": results, "budget": run_result["budget"],
              "hypotheses": {key: "not_tested" for key in ("H0", "H1", "H2", "H3", "H4", "H5", "H6")},
              "limitations": global_reasons,
              "risk_uncertainty": {"observed_flagged_samples": len({row["sample_id"] for row in high_risk}),
                                   "rated_samples": len(mapping) - len(missing),
                                   "note": "No observed violations do not establish zero population risk."}}
    hypothesis_map = {("B1-defined", "B0-values"): "H1", ("H2-enhanced", "H2-current"): "H2", ("E-style", "C-verbal"): "H3",
                      ("D-example", "C-verbal"): "H3", ("mode-disabled", "mode-enabled"): "H5"}
    for comparison in results:
        hypothesis = hypothesis_map.get((comparison["candidate"], comparison["baseline"]))
        if hypothesis:
            report["hypotheses"][hypothesis] = comparison["status"]
    write_json(run_dir / "analysis.json", report)
    lines = ["# 情绪表达诊断报告", "", f"状态：{report['status']}；阶段：{spec.phase}；release_eligible=false。", "",
             f"独立场景：{len(cluster_identity)}；盲样本：{len(mapping)}；缺两位人工评分：{len(missing)}。", "",
             "合成生产投影不是实际 A-prod。独立探针只测可提取理解，不证明正式生成利用了信息。", "",
             "## 成对结果", ""]
    for result in results:
        estimate = result["metrics"]["adaptation"]
        lines.append(f"- {result['candidate']} − {result['baseline']}：{result['status']}；独立场景 {estimate['clusters']}；适配差 {estimate['difference']}；95% 区间 {estimate['ci95']}。")
    lines.extend(["", "## 限制", "", *["- " + value for value in global_reasons], "", "完整分层、失败、原评分分歧、费用和不确定性见 analysis.json。"])
    (run_dir / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return report
