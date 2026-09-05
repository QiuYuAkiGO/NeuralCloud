from __future__ import annotations

import asyncio
import json

from eval.grounded_agency.evidence import load_pointer, validate_run
from eval.grounded_agency.fixtures import ScenarioSimulator
from eval.grounded_agency.runner import run_experiment
from eval.grounded_agency.scenarios import load_scenario


def test_tampered_body_evidence_is_rejected(tmp_path):
    result = asyncio.run(run_experiment(tmp_path / "run"))
    pointer = result.trials[0].body_truth_events[0]
    (tmp_path / "run" / pointer.path).write_text("{}", encoding="utf-8")
    assert not validate_run(result.run_dir).passed


def test_tampered_manifest_binding_is_rejected(tmp_path):
    result = asyncio.run(run_experiment(tmp_path / "run"))
    metadata = json.loads((tmp_path / "run" / "metadata.json").read_text(encoding="utf-8"))
    path = tmp_path / "run" / next(iter(metadata["manifests"].values()))
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["model_version"] = "tampered"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert not validate_run(result.run_dir).passed


def test_actions_link_to_experimental_decisions(tmp_path):
    result = asyncio.run(run_experiment(tmp_path / "run"))
    trial = result.trials[0]
    decisions = {item.decision_id: item for item in trial.decision_records}
    for pointer in trial.action_outcomes:
        payload = load_pointer(result.run_dir, pointer)
        assert decisions[payload["decision_id"]].action_id == payload["action_id"]


def test_terminal_trial_stops_and_records_final_state(tmp_path):
    scenario = load_scenario("action_only_v1")
    scenario = scenario.model_copy(update={"initial_body_state": scenario.initial_body_state.model_copy(update={"available_units": 0.0})})
    result = asyncio.run(run_experiment(tmp_path / "terminal", scenarios=[scenario]))
    assert result.integrity.passed, result.trials[0].error
    assert result.trials[0].terminal_status == "terminal"
    assert not result.trials[0].decisions


def test_provider_failure_is_saved_and_resources_closed(tmp_path):
    class FailedProvider(ScenarioSimulator):
        fixture_only = True
        async def execute(self, execution):
            raise ConnectionError("fixture transport failed")
    result = asyncio.run(run_experiment(tmp_path / "failed", provider_factory=FailedProvider))
    assert result.trials[0].integrity.checks["services_closed"]
    assert result.trials[0].integrity.checks["no_background_tasks"]
    assert (tmp_path / "failed" / "trials.jsonl").is_file()
    assert result.trials[0].terminal_status in {"failed", "completed"}
    assert result.trials[0].action_outcomes or result.trials[0].error


def test_provider_timeout_cancels_and_closes_tasks(tmp_path):
    instances = []
    class SlowProvider(ScenarioSimulator):
        fixture_only = True
        def __init__(self, *args):
            super().__init__(*args)
            instances.append(self)
        async def execute(self, execution):
            await asyncio.sleep(30)
            return await super().execute(execution)
    result = asyncio.run(run_experiment(tmp_path / "timeout", provider_factory=SlowProvider, timeout_seconds=0.2))
    assert result.trials[0].terminal_status in {"timeout", "failed"}
    assert result.trials[0].integrity.checks["services_closed"]
    assert instances[0].closed
