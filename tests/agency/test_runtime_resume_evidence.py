"""Resume uses newly captured evidence and verified reconciliation in Runtime."""

import asyncio

from eval.grounded_agency.fixtures import FixtureClock, ScenarioSimulator
from eval.grounded_agency.runner import RuntimeTrialDriver, make_manifest
from eval.grounded_agency.scenarios import load_scenario
from harness.runtime.models import ActionBackendResult, ActionExecutionState, RuntimeLifecycle
from tests.agency.test_runtime_wiring import PlannedWaitModel


def _driver(tmp_path, *, unknown=False):
    scenario = load_scenario("action_only_v1").model_copy(update={"agency_enabled": True})
    clock = FixtureClock()
    provider = ScenarioSimulator(scenario, 0, clock)
    if unknown:
        provider.config = provider.config.model_copy(update={"scripted_outcomes": {"control.wait.v1": "unknown"}})
    return RuntimeTrialDriver(scenario, make_manifest(scenario, "C2"), provider, PlannedWaitModel(0), tmp_path), clock


def test_resume_requires_new_physical_sample_not_just_later_decision_time(tmp_path):
    async def run():
        driver, clock = _driver(tmp_path)
        await driver.start()
        try:
            await driver.observe_body(await driver.provider.snapshot(), step=0)
            await driver.decide("Inspect the current view.", step=0, moment=clock())
            await driver.runtime.pause(reason="checkpoint")
            clock.advance(2)
            await driver.runtime.resume(reason="request reassessment")
            trace, action = await driver.decide("Inspect the current view.", step=1, moment=clock(), agency_trigger="recovery")
            assert action is None
            assert trace["compiled_decision"]["kind"] == "defer"
            service = driver.runtime._agency_service
            assert service.store.goal("fixture-project").status == "paused"
            assert any("fresh observation" in item.get("error", "") for item in service.traces.values())
            clock.advance(2)
            await driver.observe_body(await driver.provider.snapshot(), step=2)
            _, action = await driver.decide("Inspect the current view again.", step=2, moment=clock(), agency_trigger="recovery")
            assert action is not None and action.state == ActionExecutionState.SUCCEEDED
            assert service.store.goal("fixture-project").status == "active"
        finally:
            await driver.close()
    asyncio.run(run())


def test_verified_unknown_reconciliation_allows_next_step_without_replaying_first(tmp_path):
    async def run():
        driver, clock = _driver(tmp_path, unknown=True)
        await driver.start()
        try:
            await driver.observe_body(await driver.provider.snapshot(), step=0)
            _, uncertain = await driver.decide("Inspect the current view.", step=0, moment=clock())
            assert uncertain.state == ActionExecutionState.UNKNOWN
            assert driver.runtime.lifecycle == RuntimeLifecycle.PAUSED
            service = driver.runtime._agency_service
            assert service.store.checkpoint("fixture-project").action_status == "unknown"
            await driver.runtime.resolve_action(uncertain.action_id, result=ActionBackendResult(
                success=True, summary="Fixture independently confirmed the first operation", safe_point="verified-complete",
            ))
            clock.advance(2)
            await driver.observe_body(await driver.provider.snapshot(), step=1)
            await driver.runtime.resume(reason="verified outcome and new physical sample")
            driver.provider.config = driver.provider.config.model_copy(update={"scripted_outcomes": {}})
            _, action = await driver.decide("Inspect the current view.", step=1, moment=clock(), agency_trigger="recovery")
            assert action is not None and action.state == ActionExecutionState.SUCCEEDED
            outcomes = service.store.all("action_outcome")
            assert sorted(item["step_id"] for item in outcomes) == ["fixture-inspect", "fixture-second"]
            assert all(item["state"] == "succeeded" for item in outcomes)
        finally:
            await driver.close()
    asyncio.run(run())
