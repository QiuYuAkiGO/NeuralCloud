"""A new decision ID cannot replay a planned action with uncertain effects."""

import asyncio

from eval.grounded_agency.fixtures import FixtureClock, ScenarioSimulator
from eval.grounded_agency.runner import RuntimeTrialDriver, make_manifest
from eval.grounded_agency.scenarios import load_scenario
from harness.agency.models import AgencyOutcome
from tests.agency.test_runtime_wiring import PlannedWaitModel


def test_unknown_plan_action_is_not_reexecuted_as_an_unbound_action(tmp_path):
    executed = []

    class CountingProvider(ScenarioSimulator):
        async def execute(self, execution):
            executed.append(execution.action_id)
            return await super().execute(execution)

    scenario = load_scenario("action_only_v1").model_copy(update={"agency_enabled": True, "horizon_steps": 2})
    clock = FixtureClock()
    provider = CountingProvider(scenario, 0, clock)
    driver = RuntimeTrialDriver(scenario, make_manifest(scenario, "C2"), provider, PlannedWaitModel(0), tmp_path / "state")

    async def run():
        await driver.start()
        try:
            await driver.observe_body(await provider.snapshot(), step=0)
            _, action = await driver.decide("Inspect the current view.", step=0, moment=clock())
            assert action.terminal
            # Persistent plan evidence can outlive the corresponding action
            # coordinator entry. A fresh decision must respect that evidence.
            service = driver.runtime._agency_service
            service.record_outcome(AgencyOutcome(
                outcome_id="recovered-uncertain-outcome", goal_id="fixture-project",
                step_id="fixture-second", action_id="prior-process-action",
                state="unknown", evidence_refs=("durable-prior-process-record",), observed_at=clock(),
            ))
            await driver.runtime.pause(reason="restore persistent plan evidence")
            clock.advance(2)
            await driver.observe_body(await provider.snapshot(), step=1)
            await driver.runtime.resume(reason="fresh physical state available")
            trace, action = await driver.decide("Inspect the current view.", step=1, moment=clock(), agency_trigger="recovery")
            assert action is None
            assert trace["compiled_decision"]["kind"] == "defer"
            assert "unresolved outcome" in trace["runtime_guard"]
            assert len(executed) == 1
        finally:
            await driver.close()

    asyncio.run(run())
