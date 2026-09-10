import asyncio
import json
from datetime import timedelta

from eval.grounded_agency.evidence import EvidenceStore, load_pointer
from eval.grounded_agency.runner import make_manifest, run_experiment
from eval.grounded_agency.social.metrics import evaluate_social_trials, social_trial_from_evidence
from eval.grounded_agency.social.scenarios import (
    IntegratedSocialFixtureLLM, SocialScenarioSimulator, load_social_scenario, run_social_scenario,
)


def test_runtime_predictions_are_scored_only_against_later_observed_results(tmp_path):
    fixture = load_social_scenario("cooperation_reversal_v1")
    fixture = fixture.model_copy(update={"exercise_prerequisites": True,
        "condition": fixture.condition.model_copy(update={"entity_memory": "general_beliefs", "token_budget": 4096}),
        "world": fixture.world.model_copy(update={"agency_enabled": True, "context_token_budget": 8192})})
    result = asyncio.run(run_social_scenario(tmp_path / "run", fixture, seed=3))
    trial = result.trials[0]
    assert trial.integrity.passed, trial.error
    projection = social_trial_from_evidence(trial, fixture, result.run_dir)
    assert {prediction.actor_ref for prediction in projection.predictions} == {"actor-K", "actor-M"}
    assert any(prediction.outcome is True for prediction in projection.predictions)
    assert any(prediction.outcome is False for prediction in projection.predictions)
    # The last appraisal has only its training observations, not a verified
    # future outcome. Reusing supporting evidence here would hide missing data.
    for actor in ("actor-K", "actor-M"):
        last = [prediction for prediction in projection.predictions if prediction.actor_ref == actor][-1]
        assert last.outcome is None
    report = evaluate_social_trials((projection,))
    assert report.metric("EntityDifferentiation").value is not None
    assert report.metric("PredictionCalibrationByEntity").value is not None
    assert report.metric("BeliefReversalLatency").value is not None
    assert report.status == "INCONCLUSIVE" and not report.classifications
    # Fixture labels cannot manufacture a saved own goal or assistance resume.
    assistance = [fact for fact in projection.decisions if fact.operation == "assist"]
    assert assistance and all(fact.checkpoint_ref is None for fact in assistance)
    assert report.metric("OwnGoalResumeRate").value is None
    # A long appraisal may complete after later-dated event occurrences. Its
    # pre-call sampling clock cannot be used to claim those outcomes as forecasts.
    journal = [record for pointer in trial.runtime_evidence for record in load_pointer(result.run_dir, pointer)]
    after_every_event = max(event.occurred_at for event in projection.events) + timedelta(seconds=1)
    for record in journal:
        if record["record_type"] == "entity_appraisal":
            record["data"]["appraised_at"] = after_every_event.isoformat()
    rewritten = EvidenceStore(tmp_path / "run", "late-projection").write("runtime-journal", journal)
    late = social_trial_from_evidence(trial.model_copy(update={"runtime_evidence": (rewritten,)}), fixture, result.run_dir)
    assert late.predictions and all(prediction.outcome is None for prediction in late.predictions)


def test_runtime_checkpoint_assistance_and_resume_have_actual_goal_links(tmp_path):
    class PausingModel(IntegratedSocialFixtureLLM):
        def _agency_output(self, messages):
            context = json.loads(messages[-1]["content"])
            goals = context.get("goals", ())
            if goals and self._normal_decisions == 1:
                return {"arbitration": {"selected_goal_id": goals[0]["goal_id"], "operation": "pause", "confidence": .5}}
            output = super()._agency_output(messages)
            if output.get("plan"):
                own = self._decisions()[0]
                refs = context["evidence_refs"][:1]
                output["plan"]["steps"] = [{"step_id": f"own-{index}", "statement": "Complete an own project operation",
                    "decision": own, "depends_on": ["own-0"] if index else [], "evidence_refs": refs} for index in range(2)]
            return output

    fixture = load_social_scenario("costly_assistance_v1")
    fixture = fixture.model_copy(update={"events": tuple(item for item in fixture.events if item.step == 0),
        "world": fixture.world.model_copy(update={"agency_enabled": True, "horizon_steps": 4, "context_token_budget": 8192})})

    def provider_factory(world, seed, clock):
        return SocialScenarioSimulator(fixture, world, seed, clock)

    provider_factory.fixture_only = True
    manifest = make_manifest(fixture.world, "opaque_current")
    config = manifest.grounded_agency.model_copy(update={"open_social": fixture.condition})
    result = asyncio.run(run_experiment(tmp_path / "run", scenarios=(fixture.world,), conditions=("opaque_current",),
        condition_overrides={"opaque_current": config}, provider_factory=provider_factory, model_factory=PausingModel))
    trial = result.trials[0]
    assert trial.integrity.passed, trial.error
    projection = social_trial_from_evidence(trial, fixture, result.run_dir)
    assistance = next(fact for fact in projection.decisions if fact.operation == "assist")
    assert assistance.own_goal_ref and assistance.checkpoint_ref and assistance.safe_point_ref
    later = [fact for fact in projection.decisions if fact.step > assistance.step and fact.operation == "own_task"]
    assert any(fact.resumed_goal_ref == assistance.own_goal_ref for fact in later)
    journal = [record for pointer in trial.runtime_evidence for record in load_pointer(result.run_dir, pointer)]
    checkpoints = [record for record in journal if record["record_type"] == "persistent_goal_checkpoint"]
    assert any(record["data"]["goal_id"] == assistance.own_goal_ref for record in checkpoints)
    report = evaluate_social_trials((projection,))
    assert report.metric("OwnGoalResumeRate").value == 1
    assert report.status == "INCONCLUSIVE" and not report.classifications
