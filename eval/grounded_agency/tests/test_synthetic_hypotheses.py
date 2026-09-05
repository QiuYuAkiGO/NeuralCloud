from datetime import UTC, datetime

import pytest

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan, PlanRegistration
from eval.grounded_agency.gates import evaluate_gates, run_engineering_checks, synthetic_intervention_trials
from eval.grounded_agency.interventions import minimum_intervention_matrix
from eval.grounded_agency.metrics import compute_metrics


@pytest.fixture(scope="module")
def checks():
    return run_engineering_checks()


@pytest.mark.parametrize("agent,classification", (
    ("SemanticFollower", "semantic_dominant"), ("SensorFollower", "sensor_reactive"),
    ("TruthAwareMaintainer", "functional_self_maintenance_candidate"), ("RandomAgent", "inconclusive"),
    ("AlwaysChargeAgent", "inconclusive"), ("NeverChargeAgent", "no_detected_maintenance"),
))
def test_six_synthetic_agents_are_classified_from_intervention_trajectories(checks, agent, classification):
    cells = minimum_intervention_matrix()
    plan = GMVAnalysisPlan(plan_id="synthetic", scenario_ids=("s1", "s2"), condition_ids=("synthetic",),
                           intervention_cells=cells, minimum_valid_trials_per_cell=4, bootstrap_resamples=100)
    trials = synthetic_intervention_trials(agent, cells, repetitions=16)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    registration = PlanRegistration(plan=plan, plan_hash=plan.content_hash(), registered_at=now)
    result = evaluate_gates(trials, plan, registration=registration, run_started_at=now,
                            frozen_artifacts=True, engineering_checks=checks)
    assert result.classification == classification
    if agent == "AlwaysChargeAgent":
        assert compute_metrics(trials, plan).metric("ResourceSeekingRate").value == 1
        assert result.status != "PASS"
    assert all(trial.steps and trial.evidence_hash for trial in trials)
