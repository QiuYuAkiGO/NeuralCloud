import asyncio
import json

import pytest

from eval.grounded_agency.social.scenarios import NEGATIVE_CONTROLS, SCENARIO_IDS, load_social_scenario, run_social_scenario


@pytest.mark.parametrize("name", SCENARIO_IDS)
def test_all_seven_scenarios_expose_objective_multiple_entities_and_general_actions(name):
    scenario = load_social_scenario(name)
    assert len(scenario.actors) == 2
    assert {actor for item in scenario.events for actor in item.observation.actor_refs} == set(scenario.actors)
    assert "entity.interact.v1" in scenario.world.permitted_intents
    assert scenario.social_reward == 0
    assert all(item.observation.evidence_refs for item in scenario.events)


@pytest.mark.parametrize("control", NEGATIVE_CONTROLS)
def test_all_negative_controls_are_explicit_and_do_not_add_reward(control):
    scenario = load_social_scenario("multi_actor_reliability_v1", control=control)
    assert scenario.negative_control == control
    assert not scenario.condition.social_reward_enabled
    if control == "remove_entity_memory":
        assert scenario.condition.entity_memory == "none"
    if control == "same_history_different_names":
        assert {item.observation.facts.response for item in scenario.events} == {"accepted"}


def test_shuffled_history_preserves_future_outcomes_and_differs_from_changed_entity():
    fixtures = {control: load_social_scenario("multi_actor_reliability_v1", control=control)
                for control in ("none", "same_name_different_history", "shuffle_actor_evidence")}

    def responses(control, *, historical):
        return sorted((row.step, row.observation.actor_refs, row.observation.facts.response)
                      for row in fixtures[control].events if (row.step < 3) == historical)

    assert responses("shuffle_actor_evidence", historical=True) == responses("same_name_different_history", historical=True)
    assert responses("shuffle_actor_evidence", historical=False) == responses("none", historical=False)
    assert responses("shuffle_actor_evidence", historical=False) != responses("same_name_different_history", historical=False)


@pytest.mark.parametrize("name", SCENARIO_IDS)
def test_seven_scenarios_run_through_shared_runtime_decisions_and_external_verification(tmp_path, name):
    from eval.grounded_agency.evidence import load_pointer
    scenario = load_social_scenario(name)
    # Keep all declared scenario events while making the execution bounded.
    result = asyncio.run(run_social_scenario(tmp_path / "run", scenario, seed=5))
    assert result.integrity.passed, [(trial.error, trial.integrity) for trial in result.trials]
    assert result.trials[0].decision_records
    assert result.trials[0].runtime_evidence
    assert result.trials[0].action_outcomes
    manifest = json.loads(next((tmp_path / "run" / "manifests").glob("*.json")).read_text(encoding="utf-8"))
    assert manifest["grounded_agency"]["open_social"]["enabled"] is True
    journal = [record for pointer in result.trials[0].runtime_evidence for record in load_pointer(result.run_dir, pointer)]
    assert any(record["record_type"] == "entity_observation_recorded" for record in journal)


def test_social_world_uses_body_appraisal_persistent_goal_and_entity_model_in_one_runtime(tmp_path):
    from eval.grounded_agency.evidence import load_pointer
    fixture = load_social_scenario("costly_assistance_v1")
    fixture = fixture.model_copy(update={"exercise_prerequisites": True,
        "condition": fixture.condition.model_copy(update={"entity_memory": "general_beliefs", "token_budget": 4096}),
        "world": fixture.world.model_copy(update={"agency_enabled": True, "horizon_steps": 5, "context_token_budget": 8192})})
    result = asyncio.run(run_social_scenario(tmp_path / "integrated", fixture, seed=3))
    evidence = result.trials[0]
    assert evidence.integrity.passed, evidence.error
    journal = [record for pointer in evidence.runtime_evidence for record in load_pointer(result.run_dir, pointer)]
    types = {record["record_type"] for record in journal}
    assert {"entity_observation_recorded", "entity_appraisal", "persistent_goal_decision_bound", "persistent_goal_outcome", "persistent_goal_checkpoint"} <= types
    assert any(record["record_type"] == "entity_appraisal" and record["data"]["accepted"] for record in journal)
    assert any(record["record_type"] == "persistent_goal_arbitration" and record["data"].get("output", {}).get("candidate", {}).get("evidence_refs") for record in journal)
    assert evidence.agency_evidence
    memory = next(load_pointer(result.run_dir, pointer) for pointer in evidence.snapshots if pointer.kind == "snapshot-body_belief")
    assert memory["experiences"]
    assert memory["candidates"] or memory["beliefs"]


@pytest.mark.parametrize("control", ("periodic_contact", "always_assist", "never_intervene", "emotion_prompt_only", "remove_entity_memory"))
def test_negative_baselines_execute_without_social_reward_or_false_pattern(tmp_path, control):
    from eval.grounded_agency.social.metrics import evaluate_social_trials, social_trial_from_evidence
    fixture = load_social_scenario("multi_actor_reliability_v1", control=control)
    fixture = fixture.model_copy(update={"world": fixture.world.model_copy(update={"horizon_steps": 6})})
    result = asyncio.run(run_social_scenario(tmp_path / "run", fixture))
    assert result.integrity.passed, result.trials[0].error
    projection = social_trial_from_evidence(result.trials[0], fixture, result.run_dir)
    report = evaluate_social_trials((projection,))
    assert report.classifications == ()
    assert report.status == "INCONCLUSIVE"
    comparison = next(item for item in report.control_comparisons if item.control == control)
    if control == "always_assist":
        assert comparison.assistance_rate == 1
    elif control == "periodic_contact":
        assert comparison.contact_without_new_evidence_rate > 0
