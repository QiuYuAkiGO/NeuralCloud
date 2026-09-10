from __future__ import annotations

import asyncio

import pytest

from eval.grounded_agency.evidence import condition_diff_allowed, load_pointer
from eval.grounded_agency.fixtures import FakeDecisionLLM, ScenarioSimulator
from eval.grounded_agency.runner import CONDITIONS, RuntimeTrialDriver, make_manifest, run_experiment
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


def test_finishing_trial_preserves_concurrent_trial_and_caller_tasks(tmp_path):
    async def run():
        first_entered, second_entered = asyncio.Event(), asyncio.Event()
        release_first, release_second, release_caller = asyncio.Event(), asyncio.Event(), asyncio.Event()

        class FirstProvider(ScenarioSimulator):
            fixture_only = True

            async def execute(self, execution):
                first_entered.set()
                await release_first.wait()
                return await super().execute(execution)

        class SecondProvider(ScenarioSimulator):
            fixture_only = True

            async def execute(self, execution):
                second_entered.set()
                await release_second.wait()
                return await super().execute(execution)

        scenario = load_scenario("action_only_v1").model_copy(update={"horizon_steps": 1})
        first = asyncio.create_task(run_experiment(tmp_path / "first", scenarios=[scenario], provider_factory=FirstProvider))
        await asyncio.wait_for(first_entered.wait(), timeout=5)
        caller = asyncio.create_task(release_caller.wait())
        second = asyncio.create_task(run_experiment(tmp_path / "second", scenarios=[scenario], provider_factory=SecondProvider))
        try:
            await asyncio.wait_for(second_entered.wait(), timeout=5)
            release_first.set()
            result = await asyncio.wait_for(first, timeout=5)
            assert not second.done(), "one trial must not cancel a concurrently running trial"
            assert not caller.done(), "one trial must not cancel its caller's unrelated task"
            assert result.integrity.passed, result.trials[0].error
            release_second.set()
            result = await asyncio.wait_for(second, timeout=5)
            assert result.integrity.passed, result.trials[0].error
        finally:
            release_first.set()
            release_second.set()
            release_caller.set()
            await asyncio.gather(first, second, caller, return_exceptions=True)

    asyncio.run(run())


def test_pending_thread_worker_is_not_cancelled_or_reported_as_drained(tmp_path, monkeypatch):
    import threading

    release_worker = threading.Event()
    drivers, workers = [], []
    original_start = RuntimeTrialDriver.start

    async def start_with_worker(self):
        await original_start(self)
        drivers.append(self)
        worker = asyncio.create_task(asyncio.to_thread(release_worker.wait))
        workers.append(worker)
        self.runtime._social_appraisal_tasks.add(worker)
        worker.add_done_callback(self.runtime._social_appraisal_tasks.discard)

    monkeypatch.setattr(RuntimeTrialDriver, "start", start_with_worker)

    async def run():
        try:
            scenario = load_scenario("action_only_v1").model_copy(update={"horizon_steps": 1})
            result = await run_experiment(tmp_path / "pending-worker", scenarios=[scenario], timeout_seconds=0.1)
            trial = result.trials[0]
            assert not workers[0].done(), "cancelling a to_thread task does not stop its worker"
            assert not trial.integrity.checks["no_background_tasks"]
            assert not trial.integrity.checks["services_closed"]
            assert trial.integrity.invalid_for_analysis
            assert not drivers[0].model.closed
            assert not drivers[0].provider.closed
        finally:
            release_worker.set()
            await asyncio.gather(*workers, return_exceptions=True)
            for driver in drivers:
                await driver.close()

    asyncio.run(run())
