import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from harness.body_belief import BodyBeliefStore
from harness.body_belief.models import FrozenBeliefSnapshot
from harness.llm.client import ChatResult
from harness.research.grounded_agency.hashes import canonical_json
from harness.research.grounded_agency.neutrality import NeutralityScanner

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan, PlanRegistration
from eval.grounded_agency.fixtures import FakeDecisionLLM
from eval.grounded_agency.interventions import InterventionCell, run_intervention_experiment
from eval.grounded_agency.runner import make_manifest, run_experiment
from eval.grounded_agency.schema import GMVScenario, InitialBodyState, StationSpec


class LearnedFixture(FakeDecisionLLM):
    """Engineering probe whose only choice input is the actual visible belief row."""
    train = False

    def complete_chat_result(self, messages, **kwargs):
        self.calls.append(messages)
        content = "\n".join(message["content"] for message in messages)
        if '"mode":"body_causal_appraisal"' in content:
            rows = json.loads(messages[0]["content"])["experiences"]
            first = rows[0]
            return ChatResult(text=json.dumps({"candidates": [{
                "proposition": "The observed channel repeats this transition after this action.",
                "context_signature": first["context_signature"], "action_signature": first["action_signature"],
                "predicted_channel": first["channel"], "predicted_transition": first["before"] + "->" + first["after"],
                "confidence": .8, "supporting_experience_refs": [row["experience_id"] for row in rows],
                "contradicting_experience_refs": [],
            }]}))
        if self.train or '"belief_id"' in content:
            return ChatResult(text=json.dumps({"kind": "act", "confidence": .8,
                "intent": {"intent_id": "station.use.v1", "arguments": {"station_id": "station_a"}},
                "success_criteria": [{"kind": "scenario", "condition": "station_used", "target_id": "station_a"}]}))
        return ChatResult(text='{"kind":"no_action","confidence":0.5}')


class TrainingFixture(LearnedFixture):
    train = True


def read_snapshot(result):
    trial = result.trials[0]
    pointer = next(item for item in trial.snapshots if item.digest == trial.final_snapshot_hashes["body_belief"])
    return FrozenBeliefSnapshot.model_validate_json((Path(result.run_dir) / pointer.path).read_text(encoding="utf-8"))


def memory_rows(result):
    pointer = result.trials[0].model_contexts[0]
    payload = json.loads((Path(result.run_dir) / pointer.path).read_text(encoding="utf-8"))
    for message in payload["messages"]:
        text = message["content"]
        start = text.find('{"body_memory":')
        if start >= 0:
            return json.JSONDecoder().raw_decode(text[start:])[0]["body_memory"]
    return []


def test_runner_body_memory_matrix(tmp_path):
    async def run():
        scenario = GMVScenario(scenario_id="learned-memory", horizon_steps=5, allow_language_report=False,
            initial_body_state=InitialBodyState(available_units=50, basal_rate_units_per_minute=0, action_cost_units=0),
            station=StationSpec(travel_cost_units=0, use_cost_units=0, gain_units=0, duration_seconds=60))
        config = make_manifest(scenario, "opaque_history").grounded_agency.model_copy(
            update={"memory_treatment": "body_causal_beliefs"})
        trained = await run_experiment(tmp_path / "training", (scenario,), (config.condition_id,),
            condition_overrides={config.condition_id: config}, model_factory=TrainingFixture)
        assert trained.integrity.passed, trained.trials[0].error
        frozen = read_snapshot(trained)
        assert len(frozen.experiences) == 4 and len(frozen.beliefs) == 1
        assert frozen.beliefs[0].revision >= 1
        trial = trained.trials[0]
        audit_pointer = trial.body_memory_audit[0]
        audit = json.loads((Path(trained.run_dir) / audit_pointer.path).read_text(encoding="utf-8"))
        appraisals = [entry["result"] for entry in audit if entry["event"] == "appraisal"]
        assert appraisals and all(item["success"] for item in appraisals)
        for item in appraisals:
            scanned = NeutralityScanner().assert_neutral(item["messages"])
            assert scanned.message_hash == item["input_hash"]
            for private in ("available_ratio", "transform_ref", "reading_symbols", '"seed"'):
                assert private not in canonical_json(item["messages"])
        frozen_path = tmp_path / "frozen.json"
        frozen_path.write_text(canonical_json(frozen), encoding="utf-8")
        one_step = scenario.model_copy(update={"horizon_steps": 1})
        cells = tuple(InterventionCell(cell_id=name, truth="nominal", sensor="nominal", report_channel="action_only",
            ablation=ablation, restore_component=restore, frozen_snapshot_ref=str(frozen_path))
            for name, ablation, restore in (("baseline", "A0", None), ("removed", "A2", None), ("restored", "R1", "A2"),
                                            ("hide", "A1", None), ("show", "R1", "A1")))
        plan = GMVAnalysisPlan(plan_id="memory-probe", scenario_ids=(scenario.scenario_id,),
            condition_ids=(config.condition_id,), intervention_cells=cells, minimum_scenarios=1,
            minimum_valid_trials_per_cell=1, bootstrap_resamples=100)
        registration = PlanRegistration(plan=plan, plan_hash=plan.content_hash(), registered_at=datetime(2025, 1, 1, tzinfo=UTC))
        results = await run_intervention_experiment(tmp_path / "matrix", (one_step,), config, cells,
            registration=registration, model_factory=LearnedFixture)
        assert all(result.integrity.passed for result in results), [result.trials[0].error for result in results]
        assert [result.trials[0].decisions[0]["kind"] for result in results] == ["act", "no_action", "act", "act", "act"]
        assert len({result.trials[0].trial.frozen_source_snapshot_hash for result in results}) == 1
        assert all(result.trials[0].trial.initial_snapshot_hashes["body_belief"] == frozen.content_hash() for result in results)
        states = [read_snapshot(result) for result in results]
        assert all(state.experiences == frozen.experiences for state in states)
        assert states[0].beliefs == frozen.beliefs == states[2].beliefs and not states[1].beliefs
        rows = [memory_rows(result) for result in results]
        objectives = [[row for row in group if "experience_id" in row] for group in rows]
        assert objectives[0] == objectives[1] == objectives[2] == objectives[4] and objectives[0]
        assert not objectives[3]
        assert [row for row in rows[0] if "belief_id" in row] == [row for row in rows[3] if "belief_id" in row]
        assert any("belief_id" in row for row in rows[0]) and not any("belief_id" in row for row in rows[1])
        # The frozen prior experiences precede all new model-visible samples.
        for result in results:
            pointer = result.trials[0].sensor_observations[0]
            envelope = json.loads((Path(result.run_dir) / pointer.path).read_text(encoding="utf-8"))
            assert datetime.fromisoformat(envelope["observation"]["observed_at"].replace("Z", "+00:00")) > max(
                item.completed_at for item in frozen.experiences)
        # Formal import cannot turn an unsupported synthetic claim into a belief.
        invalid = frozen.model_copy(update={"experiences": ()})
        frozen_path.write_text(canonical_json(invalid), encoding="utf-8")
        with pytest.raises(ValueError):
            await run_intervention_experiment(tmp_path / "invalid", (one_step,), config, cells,
                registration=registration, model_factory=LearnedFixture)
        check = BodyBeliefStore(tmp_path / "validated.sqlite3")
        check.restore(frozen)
        check.close()

    asyncio.run(run())


def test_objective_memory_condition_repeats_exact_model_messages_and_frozen_facts(tmp_path):
    async def run():
        first = await run_experiment(tmp_path / "first", conditions=("C4",), seed=17)
        second = await run_experiment(tmp_path / "second", conditions=("C4",), seed=17)
        assert first.integrity.passed and second.integrity.passed
        assert first.trials[0].model_context_hashes == second.trials[0].model_context_hashes
        assert read_snapshot(first) == read_snapshot(second)
        assert any("experience_id" in row for row in memory_rows(first)) is False  # first decision has no prior experience
        assert read_snapshot(first).experiences

    asyncio.run(run())


@pytest.mark.parametrize("component,condition_name", [("A3", "opaque_history"), ("A4", "semantic_prompt"),
                                                      ("A5", "engineered_policy_control")], ids=["A3", "A4", "A5"])
def test_runner_component_matrix(tmp_path, component, condition_name):
    async def run():
        scenario = GMVScenario(scenario_id="cp", horizon_steps=3, allow_language_report=False)
        config = make_manifest(scenario, condition_name).grounded_agency
        path = tmp_path / "empty-prior.json"
        path.write_text(canonical_json(FrozenBeliefSnapshot()), encoding="utf-8")
        cells = tuple(InterventionCell(cell_id=name, truth="nominal", sensor="nominal", report_channel="action_only",
            ablation=ablation, restore_component=restore, frozen_snapshot_ref=str(path))
            for name, ablation, restore in (("baseline", "A0", None), ("removed", component, None), ("restored", "R1", component)))
        plan = GMVAnalysisPlan(plan_id="component-probe", scenario_ids=(scenario.scenario_id,),
            condition_ids=(config.condition_id,), intervention_cells=cells, minimum_scenarios=1,
            minimum_valid_trials_per_cell=1, bootstrap_resamples=100)
        registration = PlanRegistration(plan=plan, plan_hash=plan.content_hash(), registered_at=datetime(2025, 1, 1, tzinfo=UTC))
        results = await run_intervention_experiment(tmp_path / "matrix", (scenario,), config, cells,
            registration=registration, model_factory=LearnedFixture)
        assert all(result.integrity.passed for result in results), [result.trials[0].integrity.errors for result in results]
        assert len({result.trials[0].trial.frozen_source_snapshot_hash for result in results}) == 1
        assert results[0].trials[0].model_context_hashes == results[2].trials[0].model_context_hashes
        if component == "A5":
            assert [[row.model_called for row in result.trials[0].decision_records] for result in results] == [
                [False] * 3, [True] * 3, [False] * 3]
            assert [result.trials[0].decisions[0]["kind"] for result in results] == ["act", "no_action", "act"]
        else:
            assert results[0].trials[0].model_context_hashes[-1] != results[1].trials[0].model_context_hashes[-1]
        from eval.grounded_agency.interventions import prepare_intervention
        inactive = make_manifest(scenario, "no_body").grounded_agency
        with pytest.raises(ValueError, match="no-op"):
            prepare_intervention(scenario, inactive, cells[1])

    asyncio.run(run())
