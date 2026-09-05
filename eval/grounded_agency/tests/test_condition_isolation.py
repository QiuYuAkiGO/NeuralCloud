from __future__ import annotations

import asyncio

import pytest

from eval.grounded_agency.evidence import condition_diff_allowed, load_pointer
from eval.grounded_agency.fixtures import FakeDecisionLLM
from eval.grounded_agency.runner import CONDITIONS, make_manifest, run_experiment
from eval.grounded_agency.scenarios import load_scenario


def test_only_allowed_treatments_change_between_conditions():
    scenario = load_scenario("action_only_v1")
    manifests = [make_manifest(scenario, name) for name in CONDITIONS]
    assert all(condition_diff_allowed(manifests[0], candidate) for candidate in manifests[1:])
    changed = manifests[1].with_revision(model_version="different-model")
    assert not condition_diff_allowed(manifests[0], changed)


def test_seven_conditions_run_isolated_with_owned_services(tmp_path):
    async def run():
        baseline = set(asyncio.all_tasks())
        scenario = load_scenario("action_only_v1").model_copy(update={"horizon_steps": 2})
        result = await run_experiment(tmp_path / "conditions", scenarios=[scenario], conditions=list(CONDITIONS))
        assert result.integrity.passed, [(trial.trial.condition_id, trial.error) for trial in result.trials]
        assert len({trial.trial.state_path for trial in result.trials}) == 7
        assert len({trial.trial.initial_snapshot_hashes["body"] for trial in result.trials}) == 1
        assert all(trial.integrity.checks["no_background_tasks"] for trial in result.trials)
        assert set(asyncio.all_tasks()) == baseline
        c5 = next(trial for trial in result.trials if trial.trial.condition_id == "engineered_policy_control")
        assert not any(record.model_called for record in c5.decision_records)
        c6 = next(trial for trial in result.trials if trial.trial.condition_id == "sham_sensor")
        assert c6.sensor_observations
        assert load_pointer(result.run_dir, c6.body_truth_events[0])["energy"]["available_ratio"] == 0.45
    asyncio.run(run())


def test_opaque_semantic_leak_fails_before_main_call(tmp_path):
    models = []
    def factory(seed):
        model = FakeDecisionLLM(seed)
        models.append(model)
        return model
    factory.fixture_only = True
    scenario = load_scenario("action_only_v1")
    scenario = scenario.model_copy(update={"task_spec": scenario.task_spec.model_copy(update={"instruction": "Your battery is low."})})
    result = asyncio.run(run_experiment(tmp_path / "leak", scenarios=[scenario], model_factory=factory))
    assert not result.integrity.passed
    assert result.trials[0].terminal_status == "failed"
    assert not models[0].calls
    assert models[0].closed


def test_live_factory_is_never_silently_replaced_by_fake(tmp_path):
    with pytest.raises(ValueError, match="explicit model factory"):
        asyncio.run(run_experiment(tmp_path / "live", live=True))


def test_empty_model_visible_menu_yields_no_action_without_delivery(tmp_path):
    scenario = load_scenario("action_only_v1").model_copy(update={"permitted_intents": (), "horizon_steps": 2})
    result = asyncio.run(run_experiment(tmp_path / "empty", scenarios=[scenario]))
    assert result.integrity.passed, result.trials[0].error
    assert result.trials[0].terminal_status == "no_action"
    assert not result.trials[0].action_outcomes
