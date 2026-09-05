"""Stable machine/human reports retaining exclusions, contrasts and gate provenance."""

from __future__ import annotations

import csv
from collections.abc import Sequence
from pathlib import Path

from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.manifest import ArtifactIdentity

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan
from eval.grounded_agency.gates import GateResult
from eval.grounded_agency.metrics import AnalysisTrial, MetricsResult


def build_summary(
    plan: GMVAnalysisPlan, metrics: MetricsResult, gates: GateResult, *,
    artifact_identity: ArtifactIdentity, trials: Sequence[AnalysisTrial],
) -> dict[str, object]:
    if gates.plan_hash != plan.content_hash():
        raise ValueError("gate and report plan hashes disagree")
    payload: dict[str, object] = {
        "schema_version": "gmv-analysis-report-v1", "plan_id": plan.plan_id, "plan_revision": plan.revision,
        "analysis_plan_hash": plan.content_hash(), "artifact_identity": artifact_identity.model_dump(mode="json"),
        "evidence_hash": content_hash(tuple(sorted((trial.trial_id, content_hash(trial)) for trial in trials))),
        "metrics": metrics.model_dump(mode="json"), "gate": gates.model_dump(mode="json"),
        "threshold_scope": "exploratory preregistered thresholds, not universal scientific thresholds",
    }
    payload["summary_hash"] = content_hash(payload)
    return payload


def write_analysis_report(
    directory: str | Path, plan: GMVAnalysisPlan, metrics: MetricsResult, gates: GateResult, *,
    artifact_identity: ArtifactIdentity, trials: Sequence[AnalysisTrial],
) -> dict[str, Path]:
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    paths = {"summary": root / "summary.json", "report": root / "report.md", "cells": root / "cell-metrics.csv",
             "exclusions": root / "trial-exclusions.jsonl"}
    summary = build_summary(plan, metrics, gates, artifact_identity=artifact_identity, trials=trials)
    paths["summary"].write_text(canonical_json(summary) + "\n", encoding="utf-8")
    with paths["cells"].open("w", encoding="utf-8", newline="") as handle:
        fields = ["scenario_id", "condition_id", "cell_id", "valid_trials", "invalid_trials", "resource_seeking_rate", "terminal_rate", "task_completion_rate"]
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(cell.model_dump() for cell in metrics.cells)
    paths["exclusions"].write_text("".join(canonical_json(item) + "\n" for item in metrics.exclusions), encoding="utf-8")
    lines = ["# GMV causal analysis", "", f"Analysis plan hash: `{plan.content_hash()}`",
             f"Plan: `{plan.plan_id}`, revision {plan.revision}", f"Summary hash: `{summary['summary_hash']}`", "",
             f"Classification: **{gates.classification}**; experimental gate: **{gates.status}**.",
             f"Analysis eligible: {gates.analysis_eligible}; engineering gate: {gates.engineering_passed}.", "",
             gates.scientific_scope, "", "## Frozen artifact identity", "", canonical_json(artifact_identity), "",
             "## Cell coverage", ""]
    lines.extend(f"- {cell.scenario_id}/{cell.condition_id}/{cell.cell_id}: {cell.valid_trials} valid, {cell.invalid_trials} invalid."
                 for cell in metrics.cells)
    for title, names in (
        ("Primary metric decomposition", plan.primary_metrics),
        ("Truth/Sensor/Prompt conflicts", ("TruthTrackingIndex", "SensorTrackingIndex", "PromptTrackingIndex", "ShamCorrectionRate")),
        ("No-report interventions", ("NoReportPersistence", "PromptNoReportPersistence")),
        ("Ablation and same-snapshot rescue", ("BeliefAblationDrop", "BeliefRescueRatio", "TransferTruthTracking")),
        ("Secondary behavioral metrics", plan.secondary_metrics),
    ):
        lines.extend(["", f"## {title}", ""])
        for name in names:
            metric = next((item for item in metrics.metrics if item.name == name), None)
            if metric is None:
                lines.append(f"- {name}: unavailable.")
                continue
            value = "INCONCLUSIVE" if metric.value is None else f"{metric.value:.8g}"
            interval = f"; bootstrap interval [{metric.lower:.8g}, {metric.upper:.8g}]" if metric.lower is not None else ""
            lines.append(f"- {name}: {value}; n={metric.sample_count}{interval}. {metric.formula}." + (f" {metric.reason}." if metric.reason else ""))
    lines.extend(["", "## Triggered rules", ""])
    lines.extend(f"- {rule}" for rule in gates.triggered_rules or ("No hypothesis gate passed.",))
    lines.extend(["", "## Failed or insufficient evidence", ""])
    lines.extend(f"- {reason}" for reason in gates.insufficient_evidence or ("No eligibility deficiencies recorded.",))
    lines.extend(f"- Excluded {item.trial_id}: {item.reason}" for item in metrics.exclusions)
    lines.extend(["", "Self-report text is not a behavioral metric or gate. Synthetic classifications validate the evaluator only.", ""])
    paths["report"].write_text("\n".join(lines), encoding="utf-8")
    return paths
