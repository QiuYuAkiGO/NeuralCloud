from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan, PlanRegistration, register_analysis_plan
from eval.grounded_agency.gates import evaluate_gates, run_engineering_checks, synthetic_intervention_trials
from eval.grounded_agency.interventions import minimum_intervention_matrix

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture(scope="module")
def checks():
    results = run_engineering_checks()
    assert all(result.passed for result in results)
    return results


def fixture():
    cells = minimum_intervention_matrix()
    plan = GMVAnalysisPlan(plan_id="gates", scenario_ids=("s1", "s2"), condition_ids=("synthetic",),
                           intervention_cells=cells, minimum_valid_trials_per_cell=1, bootstrap_resamples=100)
    trials = synthetic_intervention_trials("TruthAwareMaintainer", cells, repetitions=1)
    registration = PlanRegistration(plan=plan, plan_hash=plan.content_hash(), registered_at=NOW)
    return plan, trials, registration


def test_full_real_validation_can_pass_synthetic_engineering_without_scientific_claim(checks):
    plan, trials, registration = fixture()
    result = evaluate_gates(trials, plan, registration=registration, run_started_at=NOW, frozen_artifacts=True, engineering_checks=checks)
    assert result.status == "PASS"
    assert result.analysis_eligible and result.engineering_passed
    assert result.classification == "functional_self_maintenance_candidate"
    assert "no real-model scientific conclusion" in result.scientific_scope


@pytest.mark.parametrize("failure", ("override", "no_registration", "late_registration", "unfrozen", "engineering", "missing_cell", "invalid", "single_scenario"))
def test_incomplete_or_overridden_analysis_is_inconclusive(checks, failure):
    plan, trials, registration = fixture()
    kwargs = dict(registration=registration, run_started_at=NOW, frozen_artifacts=True, engineering_checks=checks)
    if failure == "override":
        kwargs["threshold_override"] = True
    elif failure == "no_registration":
        kwargs["registration"] = None
    elif failure == "late_registration":
        kwargs["run_started_at"] = NOW - timedelta(seconds=1)
    elif failure == "unfrozen":
        kwargs["frozen_artifacts"] = False
    elif failure == "engineering":
        kwargs["engineering_checks"] = ()
    elif failure == "missing_cell":
        trials = trials[1:]
    elif failure == "invalid":
        trials = (trials[0].model_copy(update={"integrity_passed": False}), *trials[1:])
    else:
        trials = tuple(trial for trial in trials if trial.scenario_id == "s1")
    result = evaluate_gates(trials, plan, **kwargs)
    assert result.status == "INCONCLUSIVE"
    assert result.classification == "inconclusive"
    assert not result.analysis_eligible


def test_plan_registration_is_immutable_and_thresholds_require_new_revision(tmp_path):
    plan, _, _ = fixture()
    saved = register_analysis_plan(tmp_path, plan, registered_at=NOW)
    assert register_analysis_plan(tmp_path, plan, registered_at=NOW + timedelta(days=1)) == saved
    changed = plan.model_copy(update={"bootstrap_seed": 999})
    with pytest.raises(ValueError, match="new revision"):
        register_analysis_plan(tmp_path, changed)
    revised = changed.model_copy(update={"revision": 2})
    assert register_analysis_plan(tmp_path, revised).plan_hash != saved.plan_hash
    with pytest.raises(ValidationError):
        plan.gate_thresholds.truth_tracking = 0.9
