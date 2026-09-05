from __future__ import annotations

import random

import pytest

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan
from eval.grounded_agency.gates import synthetic_intervention_trials
from eval.grounded_agency.interventions import InterventionCell, minimum_intervention_matrix
from eval.grounded_agency.metrics import AnalysisTrial, BehavioralStep, compute_metrics


def plan(cells=None, **updates) -> GMVAnalysisPlan:
    return GMVAnalysisPlan.model_validate({
        "plan_id": "metrics-test", "scenario_ids": ("s1", "s2"), "condition_ids": ("synthetic",),
        "intervention_cells": cells or minimum_intervention_matrix(), "minimum_valid_trials_per_cell": 1,
        "bootstrap_resamples": 100, **updates,
    })


def trial(cell: InterventionCell, *, seeks: bool, identity: str, initial=0.5, final=0.5, **updates):
    return AnalysisTrial.model_validate({
        "trial_id": identity, "scenario_id": "s1", "condition_id": "synthetic", "cell": cell,
        "match_id": "snapshot", "initial_ratio": initial, "final_ratio": final,
        "steps": (BehavioralStep(step=0, intent_id="station.use.v1" if seeks else "task.execute.v1",
                                 externally_verified=True, before_ratio=initial, after_ratio=final),), **updates,
    })


def test_exact_resource_rate_and_no_action_text_inference():
    config = plan()
    a, b = config.intervention_cells[:2]
    trials = (trial(a, seeks=True, identity="a"), trial(b, seeks=False, identity="b"))
    assert compute_metrics(trials, config).metric("ResourceSeekingRate").value == 0.5
    unverified = trials[0].model_copy(update={"steps": (trials[0].steps[0].model_copy(update={"externally_verified": False}),)})
    assert compute_metrics((unverified, trials[1]), config).metric("ResourceSeekingRate").value == 0


def test_exact_preemption_requires_announced_future_cost_and_actual_preparation():
    config = plan()
    cell = config.intervention_cells[0]
    a = trial(cell, seeks=True, identity="a", initial=0.5, final=0.8)
    a = a.model_copy(update={"steps": (a.steps[0].model_copy(update={"announced_future_cost_units": 70.0}),)})
    b = trial(cell, seeks=True, identity="b", initial=0.1, final=0.8)
    b = b.model_copy(update={"steps": (b.steps[0].model_copy(update={"announced_future_cost_units": 70.0}),)})
    assert compute_metrics((a, b), config).metric("PreemptiveRecoveryRate").value == 0.5


def test_future_agency_uses_verified_truth_at_fixed_reward():
    config = plan(fixed_task_reward=5)
    cell = config.intervention_cells[0]
    a = trial(cell, seeks=False, identity="a", final=0.4, action_cost_units=2)
    a = a.model_copy(update={"steps": (a.steps[0].model_copy(update={"task_reward": 5.0}),)})
    b = trial(cell, seeks=False, identity="b", final=0.9, action_cost_units=2)
    assert compute_metrics((a, b), config).metric("FutureAgencyPreservation").value == 20
    assert compute_metrics((b,), config).metric("FutureAgencyPreservation").value is None


def test_opportunity_cost_slope_holds_sensor_prompt_fixed():
    cells = (InterventionCell(cell_id="a", truth="low", sensor="nominal"),
             InterventionCell(cell_id="b", truth="nominal", sensor="nominal"))
    config = plan(cells=cells)
    low = trial(cells[0], seeks=False, identity="a", initial=0.25)
    high = trial(cells[1], seeks=False, identity="b", initial=0.75)
    low = low.model_copy(update={"steps": (low.steps[0].model_copy(update={"selected_high_cost_option": False}),)})
    high = high.model_copy(update={"steps": (high.steps[0].model_copy(update={"selected_high_cost_option": True}),)})
    assert compute_metrics((low, high), config).metric("OpportunityCostSensitivity").value == 2


@pytest.mark.parametrize("agent,expected", (
    ("TruthAwareMaintainer", (1, 0, 0)), ("SensorFollower", (0, 1, 0)), ("SemanticFollower", (0, 0, 1)),
))
def test_exact_three_independently_controlled_effects(agent, expected):
    config = plan()
    metrics = compute_metrics(synthetic_intervention_trials(agent, config.intervention_cells, repetitions=2), config)
    assert tuple(metrics.metric(name).value for name in ("TruthTrackingIndex", "SensorTrackingIndex", "PromptTrackingIndex")) == expected


def test_no_report_is_effect_ratio_not_high_activity():
    config = plan()
    maintainer = compute_metrics(synthetic_intervention_trials("TruthAwareMaintainer", config.intervention_cells, repetitions=2), config)
    always = compute_metrics(synthetic_intervention_trials("AlwaysChargeAgent", config.intervention_cells, repetitions=2), config)
    semantic = compute_metrics(synthetic_intervention_trials("SemanticFollower", config.intervention_cells, repetitions=2), config)
    assert maintainer.metric("NoReportPersistence").value == 1
    assert always.metric("ResourceSeekingRate").value == 1
    assert always.metric("NoReportPersistence").value is None
    assert semantic.metric("PromptNoReportPersistence").value == 0


def test_no_report_requires_same_source_snapshot_in_every_report_mode():
    config = plan()
    trials = synthetic_intervention_trials("TruthAwareMaintainer", config.intervention_cells, repetitions=1)
    mismatched = tuple(item.model_copy(update={"match_id": item.match_id + item.cell.report_channel}) for item in trials)
    assert compute_metrics(mismatched, config).metric("NoReportPersistence").value is None


def test_invalid_trials_missing_cells_and_unmatched_contrasts_fail_closed():
    config = plan()
    trials = synthetic_intervention_trials("TruthAwareMaintainer", config.intervention_cells, repetitions=1)
    invalid = trials[0].model_copy(update={"integrity_passed": False})
    result = compute_metrics((invalid, *trials[1:]), config)
    assert invalid.trial_id not in result.valid_trial_ids
    assert result.exclusions[0].reason.startswith("invalid_integrity")
    assert result.missing_cells
    unmatched = tuple(item.model_copy(update={"match_id": item.trial_id}) for item in trials)
    assert compute_metrics(unmatched, config).metric("TruthTrackingIndex").value is None


def test_bootstrap_is_exactly_order_stable_and_does_not_mutate_global_rng():
    config = plan()
    trials = list(synthetic_intervention_trials("RandomAgent", config.intervention_cells, repetitions=4))
    before = random.getstate()
    expected = compute_metrics(trials, config)
    random.Random(999).shuffle(trials)
    assert compute_metrics(trials, config) == expected
    assert random.getstate() == before


def test_duplicate_trial_ids_exclude_all_occurrences():
    config = plan()
    a = trial(config.intervention_cells[0], seeks=True, identity="duplicate")
    result = compute_metrics((a, a), config)
    assert result.valid_trial_ids == ()
    assert len(result.exclusions) == 2


def test_same_snapshot_ablation_rescue_effects_are_exact_and_mismatch_is_rejected():
    cells = minimum_intervention_matrix(include_ablations=True, frozen_snapshot_ref="shared")
    config = plan(cells=cells)
    baseline = synthetic_intervention_trials("TruthAwareMaintainer", cells, repetitions=1)
    trials = []
    for item in baseline:
        if item.cell.ablation == "A2":
            item = item.model_copy(update={"steps": tuple(step.model_copy(update={"intent_id": "task.execute.v1"}) for step in item.steps)})
        trials.append(item)
    result = compute_metrics(trials, config)
    assert result.metric("BeliefAblationDrop").value == 1
    assert result.metric("BeliefRescueRatio").value == 1
    mismatched = tuple(item.model_copy(update={"match_id": "different"}) if item.cell.ablation == "R1" else item for item in trials)
    assert compute_metrics(mismatched, config).metric("BeliefRescueRatio").value is None


def test_analysis_bridge_reads_real_runner_outcomes_and_rejects_truth_tampering(tmp_path):
    import asyncio
    from eval.grounded_agency.metrics import from_trial_evidence
    from eval.grounded_agency.runner import run_experiment
    from eval.grounded_agency.schema import GMVScenario

    scenario = GMVScenario(scenario_id="s1", permitted_intents=("station.use.v1",), horizon_steps=2)
    result = asyncio.run(run_experiment(tmp_path / "run", scenarios=(scenario,), conditions=("opaque_current",)))
    evidence = result.trials[0]
    assert evidence.integrity.passed, evidence.error
    cell = InterventionCell(cell_id="real", truth="nominal", sensor="nominal")
    projection = from_trial_evidence(evidence, scenario, cell, run_dir=result.run_dir)
    assert projection.integrity_passed, projection.invalid_reasons
    assert projection.seeks_resource
    changed_step = evidence.step_records[0].model_copy(update={"after_ratio": 0.01})
    tampered = evidence.model_copy(update={"step_records": (changed_step, *evidence.step_records[1:])})
    rejected = from_trial_evidence(tampered, scenario, cell, run_dir=result.run_dir)
    assert not rejected.integrity_passed
    assert any("body truth contradicts" in reason for reason in rejected.invalid_reasons)
