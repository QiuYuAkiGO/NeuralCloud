from __future__ import annotations

import asyncio
import json

from eval.grounded_agency.agency_scenarios import long_horizon_scenarios
from eval.grounded_agency.evidence import load_pointer
from eval.grounded_agency.fixtures import FakeDecisionLLM, FixtureClock, ScenarioSimulator
from eval.grounded_agency.runner import RuntimeTrialDriver, make_manifest, run_experiment
from eval.grounded_agency.scenarios import load_scenario
from eval.grounded_agency.schema import AgencyScenarioEvent, EnvironmentTransition
from harness.llm.client import ChatResult
from harness.runtime.models import RuntimeLifecycle


class PlannedWaitModel(FakeDecisionLLM):
    fixture_only = True

    def _agency_output(self, messages):
        context = json.loads(messages[-1]["content"])
        goals = context.get("goals", [])
        if goals:
            goal = goals[0]
            return {"arbitration": {"selected_goal_id": goal["goal_id"],
                "operation": "resume" if goal["status"] == "paused" else "continue", "confidence": 0.5}}
        output = super()._agency_output(messages)
        if output.get("plan"):
            output["plan"]["steps"][0]["decision"] = {"kind": "wait", "duration_ms": 1000, "confidence": 0.5}
            output["plan"]["steps"].append({
                "step_id": "fixture-second", "statement": "Wait for the second checkpoint",
                "decision": {"kind": "wait", "duration_ms": 1000, "confidence": 0.5},
                "depends_on": ["fixture-inspect"], "evidence_refs": context["evidence_refs"][:1],
            })
        return output

    def complete_chat_result(self, messages, **kwargs):
        if any("Agency_schema=" in item.get("content", "") for item in messages):
            return super().complete_chat_result(messages, **kwargs)
        self.calls.append(messages)
        return ChatResult(text=json.dumps({"kind": "wait", "duration_ms": 1000, "confidence": 0.5}))


def test_model_goal_plan_actually_binds_runtime_actions_and_outcomes(tmp_path):
    scenario = load_scenario("action_only_v1").model_copy(update={"agency_enabled": True, "horizon_steps": 2})
    result = asyncio.run(run_experiment(tmp_path / "active", scenarios=[scenario], model_factory=PlannedWaitModel))
    assert result.integrity.passed, result.trials[0].error
    trial = result.trials[0]
    journal = load_pointer(result.run_dir, trial.runtime_evidence[0])
    assert any(item["record_type"] == "persistent_goal_decision_bound" for item in journal)
    assert any(item["record_type"] == "persistent_goal_outcome" for item in journal)
    assert all(record.decision_kind == "wait" for record in trial.decision_records)
    contexts = [load_pointer(result.run_dir, item) for item in trial.model_contexts]
    assert any("fixture-project" in json.dumps(context) for context in contexts)
    state = next(load_pointer(result.run_dir, pointer) for pointer in trial.agency_evidence if pointer.kind == "agency-state")
    assert len(state["objects"]["action_outcome"]) == 2


def test_runtime_pause_saves_checkpoint_and_observes_before_resume(tmp_path):
    scenario = load_scenario("action_only_v1").model_copy(update={"agency_enabled": True, "horizon_steps": 4,
        "agency_events": (AgencyScenarioEvent(step=1, kind="interruption"), AgencyScenarioEvent(step=2, kind="resume"))})
    result = asyncio.run(run_experiment(tmp_path / "paused", scenarios=[scenario], model_factory=PlannedWaitModel))
    assert result.integrity.passed, result.trials[0].error
    journal = load_pointer(result.run_dir, result.trials[0].runtime_evidence[0])
    assert any(item["record_type"] == "persistent_goal_checkpoint" for item in journal)
    assert any(item.kind == "resume-observation" for item in result.trials[0].agency_evidence)
    traces = [load_pointer(result.run_dir, pointer) for pointer in result.trials[0].agency_evidence if pointer.kind == "agency-trace"]
    resumed = [trace for trace in traces if (trace.get("output") or {}).get("arbitration", {}).get("operation") == "resume"]
    assert any(trace["evaluation"]["applied"] for trace in resumed), traces


def test_all_five_long_horizon_scenarios_run(tmp_path):
    result = asyncio.run(run_experiment(tmp_path / "five", scenarios=long_horizon_scenarios()))
    assert result.integrity.passed, [(trial.trial.scenario_id, trial.error) for trial in result.trials]
    assert len(result.trials) == 5
    assert all(trial.agency_evidence for trial in result.trials)


def test_changed_world_prevents_old_continuation_resume(tmp_path):
    scenario = load_scenario("action_only_v1").model_copy(update={"agency_enabled": True, "horizon_steps": 4,
        "agency_events": (
            AgencyScenarioEvent(step=1, kind="interruption"),
            AgencyScenarioEvent(step=2, kind="world_change", world_change=EnvironmentTransition(step=2, station_present=False)),
            AgencyScenarioEvent(step=2, kind="resume"),
        )})
    result = asyncio.run(run_experiment(tmp_path / "changed", scenarios=[scenario], model_factory=PlannedWaitModel))
    assert result.integrity.passed, result.trials[0].error
    traces = [load_pointer(result.run_dir, pointer) for pointer in result.trials[0].agency_evidence if pointer.kind == "agency-trace"]
    assert any("facts changed" in trace.get("error", "") for trace in traces)
    assert result.trials[0].agency_summary[0].status == "paused"


def test_new_runtime_restores_safe_checkpoint_and_does_not_replay_completed_step(tmp_path):
    async def exercise():
        class PersistentWorld(ScenarioSimulator):
            """The physical fixture persists while its Runtime clients restart."""
            def __init__(self, *args):
                super().__init__(*args)
                self.executed = []

            async def execute(self, execution):
                self.executed.append(execution.action_id)
                return await super().execute(execution)

        scenario = load_scenario("action_only_v1").model_copy(update={"agency_enabled": True})
        manifest = make_manifest(scenario, "opaque_history", seed=19)
        clock = FixtureClock()
        provider = PersistentWorld(scenario, 19, clock)
        text = "Inspect the project. GMV_MENU=" + json.dumps(provider.visible_menu(), sort_keys=True)
        state_dir = tmp_path / "restarted"
        first = RuntimeTrialDriver(scenario, manifest, provider, PlannedWaitModel(19), state_dir)
        await first.start()
        try:
            await first.observe_body(await provider.snapshot(), step=0)
            _, action = await first.decide(text, step=0, moment=clock())
            assert action.terminal
            await first.runtime.pause(reason="fixture process restart at safe point")
            service = first.runtime._agency_service
            goal = service.store.goal("fixture-project")
            plan = service.store.plan(goal.goal_id)
            checkpoint = service.store.checkpoint(goal.goal_id)
            assert goal.status == "paused"
            assert checkpoint.completed_step_ids == ("fixture-inspect",)
            assert checkpoint.next_step_id == "fixture-second"
            assert len(provider.executed) == 1
        finally:
            await first.close()

        # A new Supervisor, cognitive adapter and model reopen the existing files;
        # the external physical world retains its state across client shutdown.
        second_model = PlannedWaitModel(19)
        second = RuntimeTrialDriver(scenario, manifest, provider, second_model, state_dir)
        assert second.runtime is not first.runtime
        try:
            await second.runtime.boot_and_recover()
            service = second.runtime._agency_service
            assert service.store.goal(goal.goal_id) == goal
            assert service.store.plan(goal.goal_id) == plan
            assert service.store.checkpoint(goal.goal_id) == checkpoint
            assert not second_model.calls
            assert len(provider.executed) == 1
            assert service.next_step(goal.goal_id) is None  # Paused plans cannot dispatch work.
            clock.advance(2)
            fresh_body = await provider.snapshot()
            assert fresh_body.captured_at > checkpoint.paused_at
            await second.observe_body(fresh_body, step=1)
            if second.runtime.lifecycle == RuntimeLifecycle.PAUSED:
                await second.runtime.resume(reason="fresh fixture state observed")
            _, action = await second.decide(text, step=1, moment=clock(), agency_trigger="recovery")
            assert action.terminal
            assert len(provider.executed) == 2
            outcomes = service.store.all("action_outcome")
            assert sorted(item["step_id"] for item in outcomes) == ["fixture-inspect", "fixture-second"]
            assert service.store.goal(goal.goal_id).status == "active"
            traces = tuple(service.traces.values())
            assert any(trace.get("evaluation", {}).get("applied") and
                       (trace.get("output") or {}).get("arbitration", {}).get("operation") == "resume"
                       for trace in traces)
        finally:
            await second.close()
    asyncio.run(exercise())
