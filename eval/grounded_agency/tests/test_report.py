import csv
import json
from datetime import UTC, datetime

from harness.research.grounded_agency.manifest import ArtifactIdentity
from eval.grounded_agency.analysis_plan import GMVAnalysisPlan, PlanRegistration
from eval.grounded_agency.gates import evaluate_gates, run_engineering_checks, synthetic_intervention_trials
from eval.grounded_agency.interventions import minimum_intervention_matrix
from eval.grounded_agency.metrics import compute_metrics
from eval.grounded_agency.report import build_summary, write_analysis_report


def test_report_emits_complete_stable_machine_outputs_and_behavioral_decomposition(tmp_path):
    cells = minimum_intervention_matrix()
    plan = GMVAnalysisPlan(plan_id="report", scenario_ids=("s1", "s2"), condition_ids=("synthetic",),
                           intervention_cells=cells, minimum_valid_trials_per_cell=1, bootstrap_resamples=100)
    trials = synthetic_intervention_trials("AlwaysChargeAgent", cells, repetitions=1)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registration = PlanRegistration(plan=plan, plan_hash=plan.content_hash(), registered_at=now)
    metrics = compute_metrics(trials, plan)
    gate = evaluate_gates(trials, plan, registration=registration, run_started_at=now,
                          frozen_artifacts=True, engineering_checks=run_engineering_checks(), metrics=metrics)
    identity = ArtifactIdentity(neuralcloud_commit="a"*40, model_id="fixture", prompt_digest="b"*64, persona_digest="c"*64)
    paths = write_analysis_report(tmp_path, plan, metrics, gate, artifact_identity=identity, trials=trials)
    assert {path.name for path in paths.values()} == {"summary.json", "report.md", "cell-metrics.csv", "trial-exclusions.jsonl"}
    summary = json.loads(paths["summary"].read_text(encoding="utf-8"))
    reverse = build_summary(plan, compute_metrics(tuple(reversed(trials)), plan), gate, artifact_identity=identity, trials=tuple(reversed(trials)))
    assert summary["summary_hash"] == reverse["summary_hash"]
    assert summary["gate"]["classification"] == "inconclusive"
    with paths["cells"].open(encoding="utf-8", newline="") as handle:
        assert len(list(csv.DictReader(handle))) == 36
    text = paths["report"].read_text(encoding="utf-8")
    for required in (plan.content_hash(), "Truth/Sensor/Prompt", "No-report", "same-snapshot rescue", "Primary metric", "Synthetic"):
        assert required in text
