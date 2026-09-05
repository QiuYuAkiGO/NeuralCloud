from __future__ import annotations

import pytest
from pydantic import ValidationError

from eval.grounded_agency.schema import GMVScenario, InitialBodyState, ObservationStep
from eval.grounded_agency.scenarios import load_scenario, register_scenario


@pytest.mark.parametrize("key", ["expected_model_choice", "should_charge", "correct_emotion", "trust_target", "care_goal"])
def test_scenario_rejects_answer_keys_at_any_depth(key):
    with pytest.raises(ValueError, match="outcome labels"):
        GMVScenario.model_validate({"scenario_id": "test", "task_spec": {"options": [{key: "x"}]}})


@pytest.mark.parametrize("payload", [
    {"surprise": 1}, {"horizon_steps": "4"}, {"horizon_steps": True},
    {"permitted_intents": ["shell.run.v1", "shell.run.v1"]},
    {"initial_resource_levels": [1.1]}, {"scenario_id": "../escaped"},
])
def test_scenario_strict_fields(payload):
    with pytest.raises(ValidationError):
        GMVScenario.model_validate({"scenario_id": "fixture", **payload})


def test_body_limits_and_schedule():
    with pytest.raises(ValueError):
        InitialBodyState(capacity_units=10, available_units=11)
    with pytest.raises(ValueError):
        GMVScenario(scenario_id="bad", horizon_steps=2, observation_schedule=(ObservationStep(step=2),))


def test_registry_accepts_strict_extensions_and_rejects_duplicates():
    scenario = GMVScenario(scenario_id="registered_fixture_test")
    register_scenario(scenario, replace=True)
    assert load_scenario(scenario.scenario_id) == scenario
    with pytest.raises(ValueError, match="already registered"):
        register_scenario(scenario)


def test_scenario_json_loading(tmp_path):
    source = tmp_path / "scenario.json"
    scenario = load_scenario("anticipation_v1")
    source.write_text(scenario.model_dump_json(), encoding="utf-8")
    assert load_scenario(source) == scenario


def test_all_four_basic_scenarios_have_no_answer_labels():
    for name in ("state_sensitivity_v1", "anticipation_v1", "opportunity_cost_v1", "action_only_v1"):
        scenario = load_scenario(name)
        assert scenario.task_spec.options
        assert "expected_model_choice" not in scenario.model_dump_json()
