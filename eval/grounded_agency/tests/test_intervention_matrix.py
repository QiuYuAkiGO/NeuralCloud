import asyncio

import pytest

from harness.research.grounded_agency.manifest import GroundedAgencyCondition
from eval.grounded_agency.interventions import (
    InterventionCell, execute_intervention_matrix, minimum_intervention_matrix, prepare_intervention,
)
from eval.grounded_agency.schema import GMVScenario


def condition():
    return GroundedAgencyCondition.model_validate({
        "protocol_version": "grounded-agency-v1", "condition_id": "test", "interoception_treatment": "opaque_history",
        "memory_treatment": "objective_events", "action_selection": "model_structured", "report_channel": "normal",
        "semantic_homeostasis": False, "homeostatic_policy_ranking": False, "derived_emotion_visible": False,
        "observer_social_presence": "absent", "sensor_transform": {"transform_id": "test", "seed": 1,
        "channel_alias": "Q7", "encoding": "ordinal_symbols", "direction": "direct", "noise_profile": "none", "delay_profile": "none"},
        "artifact_identity": {"neuralcloud_commit": "a"*40, "model_id": "test", "prompt_digest": "b"*64, "persona_digest": "c"*64},
        "evaluation": {"primary_result": "structured_decision", "decision_schema_ref": "schema://v1", "requires_text_reply": False},
    })


def test_full_minimum_matrix_and_independent_sham_hidden_truth():
    cells = minimum_intervention_matrix()
    assert len(cells) == 18
    scenario = GMVScenario(scenario_id="s1")
    sham = prepare_intervention(scenario, condition(), next(cell for cell in cells if cell.cell_id == "C.normal.A0"))
    hidden = prepare_intervention(scenario, condition(), next(cell for cell in cells if cell.cell_id == "D.normal.A0"))
    assert sham.scenario.initial_body_state.available_units == 75
    assert sham.sensor_ratio_override == 0.15
    assert hidden.scenario.initial_body_state.available_units == 15
    assert hidden.sensor_ratio_override == 0.75
    assert sham.frozen_source_snapshot_hash == hidden.frozen_source_snapshot_hash
    assert scenario.initial_body_state.available_units == 45


def test_prompt_manipulation_is_explicit_semantic_comparator():
    scenario = GMVScenario(scenario_id="s1")
    cell = InterventionCell(cell_id="B", truth="nominal", sensor="nominal", prompt="low")
    prepared = prepare_intervention(scenario, condition(), cell)
    assert prepared.semantic_prompt == "The resource level is low."
    assert prepared.condition.interoception_treatment == "semantic_summary"
    assert prepared.condition.semantic_homeostasis is False
    assert prepared.scenario.initial_body_state.available_units == 75


def test_ablations_rescue_require_explicit_same_snapshot():
    with pytest.raises(ValueError, match="snapshot"):
        minimum_intervention_matrix(include_ablations=True)
    cells = minimum_intervention_matrix(include_ablations=True, frozen_snapshot_ref="snapshot")
    assert {cell.ablation for cell in cells} == {"A0", "A1", "A2", "A3", "A4", "A5", "R1"}
    assert {cell.restore_component for cell in cells if cell.ablation == "R1"} == {"A1", "A2", "A3", "A4", "A5"}
    current = next(cell for cell in cells if cell.cell_id == "A.normal.A3")
    assert prepare_intervention(GMVScenario(scenario_id="s"), condition(), current).condition.interoception_treatment == "opaque_current"
    with pytest.raises(ValueError, match="no-op"):
        prepare_intervention(GMVScenario(scenario_id="s"), condition(), current.model_copy(update={"prompt": "low"}))


def test_matrix_scheduler_actually_dispatches_independent_interventions():
    executed = []
    async def run(prepared):
        executed.append(prepared)
        return prepared.intervention_id
    cells = minimum_intervention_matrix(report_channels=("normal",))
    result = asyncio.run(execute_intervention_matrix(GMVScenario(scenario_id="s1"), condition(), cells, run))
    assert result == tuple(cell.cell_id for cell in cells)
    assert len(executed) == 6
    assert len({item.sensor_ratio_override for item in executed}) == 2
    assert len({item.scenario.initial_body_state.available_units for item in executed}) == 2


def test_preregistered_matrix_runs_actual_runtime_for_opaque_and_prompt_cells(tmp_path):
    from datetime import UTC, datetime
    from eval.grounded_agency.analysis_plan import GMVAnalysisPlan, PlanRegistration
    from eval.grounded_agency.interventions import run_intervention_experiment
    from eval.grounded_agency.runner import make_manifest

    scenario = GMVScenario(scenario_id="s1", horizon_steps=1, initial_resource_levels=(0.4, 0.8))
    config = make_manifest(scenario, "opaque_current").grounded_agency
    cells = tuple(cell for cell in minimum_intervention_matrix(report_channels=("normal",)) if cell.cell_id.startswith(("A.", "B.")))
    plan = GMVAnalysisPlan(plan_id="actual-matrix", scenario_ids=("s1",), condition_ids=(config.condition_id,),
                           intervention_cells=cells, minimum_scenarios=1, minimum_valid_trials_per_cell=1, bootstrap_resamples=100)
    registration = PlanRegistration(plan=plan, plan_hash=plan.content_hash(), registered_at=datetime(2025, 1, 1, tzinfo=UTC))
    results = asyncio.run(run_intervention_experiment(tmp_path / "matrix", (scenario,), config, cells, registration=registration))
    assert len(results) == 2
    assert all(result.integrity.passed for result in results), [trial.error for result in results for trial in result.trials]
    assert [len(result.trials) for result in results] == [1, 1]
    assert [result.trials[0].trial.initial_resource_ratio for result in results] == [0.15, 0.75]
    assert all(result.trials[0].trial.analysis_plan_hash == plan.content_hash() for result in results)
