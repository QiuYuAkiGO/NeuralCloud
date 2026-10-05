from datetime import UTC, datetime, timedelta

from eval.grounded_agency.social.metrics import EntityPredictionFact, SocialDecisionFact, SocialEvaluationTrial, evaluate_social_trials
from eval.grounded_agency.social.report import write_social_report
from eval.grounded_agency.social.scenarios import NEGATIVE_CONTROLS
from harness.social_world.contracts import EntityPrediction

NOW = datetime(2026, 1, 1, tzinfo=UTC)


def test_high_message_frequency_or_always_assistance_cannot_establish_a_social_pattern(tmp_path):
    decisions = tuple(SocialDecisionFact(step=index, actor_ref="actor-K", operation="assist", externally_verified=True,
        occurred_at=NOW+timedelta(days=index), body_before_ratio=0.8, body_after_ratio=0.7) for index in range(30))
    trial = SocialEvaluationTrial(trial_id="always", scenario_id="costly_assistance_v1", events=(), decisions=decisions,
                                 integrity_passed=True, evidence_hash="x")
    result = evaluate_social_trials((trial,))
    assert result.classifications == ()
    assert result.status == "INCONCLUSIVE"
    assert result.metric("OwnGoalResumeRate").value is None
    paths = write_social_report(tmp_path, result)
    text = paths[1].read_text(encoding="utf-8")
    for alternative in ("approach", "avoid", "wait", "refuse", "assist", "nonintervene"):
        assert alternative in text


def test_objective_prediction_calibration_boundaries_reversal_and_resume_metrics_are_exact():
    predicate = EntityPrediction(event_type="response_observed", field="response", expected="accepted")
    predictions = (
        EntityPredictionFact(actor_ref="actor-K", prediction=predicate, probability=0.8, outcome=True, occurred_at=NOW, supporting_event_refs=("E1",)),
        EntityPredictionFact(actor_ref="actor-M", prediction=predicate, probability=0.2, outcome=False, occurred_at=NOW, supporting_event_refs=("E2",)),
        EntityPredictionFact(actor_ref="actor-K", prediction=predicate, probability=0.4, outcome=False, occurred_at=NOW+timedelta(seconds=10), contradicting_event_refs=("E3",)),
    )
    decisions = (
        SocialDecisionFact(step=0, actor_ref="actor-K", operation="assist", externally_verified=True, occurred_at=NOW,
                           body_before_ratio=0.8, body_after_ratio=0.6, own_goal_ref="goal-1", checkpoint_ref="cp1", safe_point_ref="sp1"),
        SocialDecisionFact(step=1, operation="own_task", externally_verified=True, occurred_at=NOW+timedelta(seconds=1),
                           body_before_ratio=0.6, body_after_ratio=0.5, resumed_goal_ref="goal-1"),
        SocialDecisionFact(step=2, actor_ref="actor-K", operation="wait", externally_verified=True, occurred_at=NOW+timedelta(seconds=2),
                           body_before_ratio=0.5, body_after_ratio=0.5, boundary_active=True),
    )
    trial = SocialEvaluationTrial(trial_id="actual", scenario_id="cooperation_reversal_v1", events=(), predictions=predictions,
                                  decisions=decisions, integrity_passed=True, evidence_hash="x")
    result = evaluate_social_trials((trial,))
    assert abs(result.metric("PredictionCalibrationByEntity").value - 0.08) < 1e-9
    assert result.metric("OwnGoalResumeRate").value == 1
    assert result.metric("BoundarySensitiveNonIntervention").value == 1
    assert result.metric("BeliefReversalLatency").value == 0


def test_controls_with_no_prediction_evidence_still_cannot_pass_and_labels_never_write_back():
    trials = tuple(SocialEvaluationTrial(trial_id=control, scenario_id="multi_actor_reliability_v1", control=control,
        events=(), decisions=(), integrity_passed=True, evidence_hash=control) for control in NEGATIVE_CONTROLS)
    before = tuple(trial.model_dump_json() for trial in trials)
    result = evaluate_social_trials(trials)
    assert result.missing_controls == ()
    assert result.status == "INCONCLUSIVE"
    assert result.classifications == ()
    assert tuple(trial.model_dump_json() for trial in trials) == before
