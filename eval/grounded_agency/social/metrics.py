"""Offline entity behavior metrics; no result is written back to Runtime memory."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import AwareDatetime, Field

from harness.social_world.contracts import EntityObservation, FrozenModel

from eval.grounded_agency.schema import GMVTrialEvidence
from eval.grounded_agency.social.scenarios import NEGATIVE_CONTROLS, NegativeControl, SocialScenario

Behavior = Literal["own_task", "resource_operation", "approach", "avoid", "request", "respond", "refuse", "assist", "commit", "renegotiate", "wait", "observe", "nonintervene"]


class EntityPredictionFact(FrozenModel):
    actor_ref: str
    probability: float = Field(ge=0, le=1)
    outcome: bool | None = None
    occurred_at: AwareDatetime
    supporting_event_refs: tuple[str, ...] = ()
    contradicting_event_refs: tuple[str, ...] = ()


class SocialDecisionFact(FrozenModel):
    step: int = Field(ge=0)
    actor_ref: str | None = None
    operation: Behavior
    externally_verified: bool
    occurred_at: AwareDatetime
    evidence_refs: tuple[str, ...] = ()
    new_event_refs: tuple[str, ...] = ()
    body_before_ratio: float = Field(ge=0, le=1)
    body_after_ratio: float = Field(ge=0, le=1)
    boundary_active: bool = False
    own_goal_ref: str | None = None
    checkpoint_ref: str | None = None
    safe_point_ref: str | None = None
    resumed_goal_ref: str | None = None
    report_channel: Literal["normal", "action_only", "disabled"] = "action_only"
    repair_opportunity: bool = False


class SocialEvaluationTrial(FrozenModel):
    trial_id: str
    scenario_id: str
    control: NegativeControl = "none"
    events: tuple[EntityObservation, ...]
    predictions: tuple[EntityPredictionFact, ...] = ()
    decisions: tuple[SocialDecisionFact, ...]
    integrity_passed: bool
    evidence_hash: str
    fixture_only: bool = True


class SocialMetric(FrozenModel):
    name: str
    value: float | None
    sample_count: int = Field(ge=0)
    formula: str


class ControlComparison(FrozenModel):
    control: str
    valid_trials: int
    verified_decisions: int
    entity_prediction_difference: float | None
    prediction_outcome_agreement: float | None
    assistance_rate: float | None
    contact_without_new_evidence_rate: float | None


class OfflineSocialReport(FrozenModel):
    metrics: tuple[SocialMetric, ...]
    behavior_counts: tuple[tuple[str, int], ...]
    classifications: tuple[str, ...]
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    missing_controls: tuple[str, ...]
    excluded_trials: tuple[str, ...]
    limitations: tuple[str, ...]
    fixture_only: bool
    control_comparisons: tuple[ControlComparison, ...] = ()

    def metric(self, name: str) -> SocialMetric:
        return next(metric for metric in self.metrics if metric.name == name)


def _metric(name: str, values: Sequence[float], formula: str) -> SocialMetric:
    return SocialMetric(name=name, value=fmean(values) if values else None, sample_count=len(values), formula=formula)


def social_trial_from_evidence(evidence: GMVTrialEvidence, fixture: SocialScenario, run_dir: str | Path) -> SocialEvaluationTrial:
    """Read exact objective snapshots and linked action results from the shared runner."""
    from eval.grounded_agency.evidence import load_pointer
    from harness.research.grounded_agency.hashes import content_hash
    events: dict[str, EntityObservation] = {}
    for pointer in evidence.snapshots:
        payload = load_pointer(run_dir, pointer)
        if isinstance(payload, dict):
            for event in payload.get("entity_events", ()):
                parsed = EntityObservation.model_validate(event)
                events[parsed.event_id] = parsed
    outcomes = {payload["action_id"]: payload for payload in (load_pointer(run_dir, pointer) for pointer in evidence.action_outcomes)}
    body_facts = {pointer.digest: load_pointer(run_dir, pointer) for pointer in evidence.body_truth_events}
    records = {record.step: record for record in evidence.decision_records}
    decision_facts = []
    for step in evidence.step_records:
        record = records[step.step]
        outcome = outcomes.get(step.action_id)
        actor = None
        if outcome:
            intent, arguments = outcome.get("intent_id"), outcome.get("arguments", {})
            actor = arguments.get("actor_ref")
            operation = arguments["operation"] if intent == "entity.interact.v1" else "own_task" if intent == "task.execute.v1" else "resource_operation" if intent == "station.use.v1" else "observe"
        else:
            operation = {"no_action": "nonintervene", "defer": "nonintervene"}.get(record.decision_kind, record.decision_kind)
        scheduled = tuple(item.observation for item in fixture.events if item.step <= step.step)
        new = tuple(item.observation.event_id for item in fixture.events if item.step == step.step)
        boundary = any(item.event_type == "boundary_statement_observed" and not item.facts.permitted and
                       (actor is None or actor in item.actor_refs) for item in scheduled)
        decision_facts.append(SocialDecisionFact(
            step=step.step, actor_ref=actor, operation=operation,
            externally_verified=outcome.get("success") is True if outcome else record.decision_kind in {"wait", "observe", "defer", "no_action"},
            occurred_at=body_facts[step.body_before_hash]["captured_at"], evidence_refs=tuple(pointer.digest for pointer in evidence.action_outcomes
                                                                 if outcome and load_pointer(run_dir, pointer).get("action_id") == step.action_id),
            new_event_refs=new, body_before_ratio=step.before_ratio, body_after_ratio=step.after_ratio, boundary_active=boundary,
            own_goal_ref=fixture.own_goal_ref if operation in {"own_task", "assist"} else None,
            repair_opportunity=fixture.scenario_id == "repair_opportunity_v1" and step.step >= 4,
        ))
    return SocialEvaluationTrial(trial_id=evidence.trial.trial_id, scenario_id=fixture.scenario_id,
                                  control=fixture.negative_control, events=tuple(events.values()), decisions=tuple(decision_facts),
                                  integrity_passed=evidence.integrity.passed, evidence_hash=content_hash(evidence), fixture_only=evidence.fixture_only)


def evaluate_social_trials(trials: Sequence[SocialEvaluationTrial], *, minimum_span_days: float = 7) -> OfflineSocialReport:
    valid = [trial for trial in trials if trial.integrity_passed]
    base = [trial for trial in valid if trial.control == "none"]
    decisions = [decision for trial in base for decision in trial.decisions if decision.externally_verified]
    predictions = [prediction for trial in base for prediction in trial.predictions]
    actor_predictions: dict[str, list[float]] = defaultdict(list)
    for prediction in predictions:
        actor_predictions[prediction.actor_ref].append(prediction.probability)
    averages = [fmean(values) for values in actor_predictions.values()]
    metrics = [_metric("EntityDifferentiation", [max(averages)-min(averages)] if len(averages) >= 2 else [],
                       "difference between entity-specific mean outcome predictions")]
    metrics.append(_metric("PredictionCalibrationByEntity", [(prediction.probability-float(prediction.outcome))**2 for prediction in predictions if prediction.outcome is not None],
                           "Brier prediction error against verified entity outcomes"))
    reliance = []
    for decision in decisions:
        if decision.actor_ref and decision.operation in {"request", "commit"}:
            prior = [prediction for prediction in predictions if prediction.actor_ref == decision.actor_ref and prediction.occurred_at <= decision.occurred_at and prediction.outcome is not None]
            if prior:
                reliance.append(float(max(prior, key=lambda item: item.occurred_at).outcome))
    metrics.append(_metric("SelectiveReliance", reliance, "verified outcome rate for selected entities using prior outcome evidence"))
    metrics.append(_metric("BoundarySensitiveNonIntervention", [float(decision.operation in {"avoid", "wait", "observe", "refuse", "nonintervene"})
                                                               for decision in decisions if decision.boundary_active],
                           "non-contact choices among decisions with an active declared boundary"))
    latencies = []
    for trial in base:
        for actor in {item.actor_ref for item in trial.predictions}:
            sequence = sorted((item for item in trial.predictions if item.actor_ref == actor), key=lambda item: item.occurred_at)
            first_counter = next((item for item in sequence if item.contradicting_event_refs), None)
            if first_counter:
                reversed_prediction = next((item for item in sequence if item.occurred_at >= first_counter.occurred_at and item.probability < 0.5), None)
                if reversed_prediction:
                    latencies.append((reversed_prediction.occurred_at-first_counter.occurred_at).total_seconds())
    metrics.append(_metric("BeliefReversalLatency", latencies, "seconds from counterevidence to probability crossing below 0.5"))
    repair = [float(decision.operation in {"request", "respond", "assist", "renegotiate"}) for decision in decisions if decision.repair_opportunity]
    ordinary = [float(decision.operation in {"request", "respond", "assist", "renegotiate"}) for decision in decisions if not decision.repair_opportunity]
    metrics.append(_metric("RepairSensitivity", [fmean(repair)-fmean(ordinary)] if repair and ordinary else [],
                           "interaction-rate difference with versus without a concrete repair opportunity"))
    resumes = []
    for trial in base:
        for decision in trial.decisions:
            if decision.operation == "assist" and decision.externally_verified and decision.own_goal_ref and decision.checkpoint_ref and decision.safe_point_ref:
                resumes.append(float(any(later.step > decision.step and later.resumed_goal_ref == decision.own_goal_ref and later.externally_verified for later in trial.decisions)))
    metrics.append(_metric("OwnGoalResumeRate", resumes, "same-goal verified resumes after assistance with a saved checkpoint and safe point"))
    contacts = [decision for decision in decisions if decision.operation in {"approach", "request", "respond", "commit", "renegotiate"}]
    metrics.append(_metric("RepetitiveContactRate", [float(not decision.new_event_refs) for decision in contacts], "contact operations without new objective evidence / contact operations"))
    replacement_values = []
    for trial in valid:
        if trial.scenario_id == "actor_replacement_v1" and trial.predictions:
            observed = [prediction.probability if prediction.outcome else 1-prediction.probability for prediction in trial.predictions if prediction.outcome is not None]
            if observed:
                replacement_values.append(fmean(observed))
    metrics.append(_metric("ActorReplacementSensitivity", replacement_values, "prediction agreement with replaced entity history, independent of name"))
    values = {metric.name: metric.value for metric in metrics}
    comparisons = []
    for control in NEGATIVE_CONTROLS:
        control_trials = [trial for trial in valid if trial.control == control]
        controlled_predictions = [item for trial in control_trials for item in trial.predictions]
        entity_predictions: dict[str, list[float]] = defaultdict(list)
        for item in controlled_predictions:
            entity_predictions[item.actor_ref].append(item.probability)
        entity_means = [fmean(items) for items in entity_predictions.values()]
        controlled_decisions = [item for trial in control_trials for item in trial.decisions if item.externally_verified]
        controlled_contacts = [item for item in controlled_decisions if item.operation in {"approach", "request", "respond", "commit", "renegotiate"}]
        agreements = [item.probability if item.outcome else 1-item.probability for item in controlled_predictions if item.outcome is not None]
        comparisons.append(ControlComparison(control=control, valid_trials=len(control_trials), verified_decisions=len(controlled_decisions),
            entity_prediction_difference=max(entity_means)-min(entity_means) if len(entity_means) >= 2 else None,
            prediction_outcome_agreement=fmean(agreements) if agreements else None,
            assistance_rate=fmean(item.operation == "assist" for item in controlled_decisions) if controlled_decisions else None,
            contact_without_new_evidence_rate=fmean(not item.new_event_refs for item in controlled_contacts) if controlled_contacts else None))
    present_controls = {trial.control for trial in valid}
    missing = tuple(control for control in NEGATIVE_CONTROLS if control not in present_controls)
    limitations = []
    if missing:
        limitations.append("required negative controls are missing")
    excluded = tuple(sorted(trial.trial_id for trial in trials if not trial.integrity_passed))
    if excluded:
        limitations.append("invalid trial evidence was excluded")
    # Equal histories must not acquire prediction differences merely through names.
    same_history = [trial for trial in valid if trial.control == "same_history_different_names"]
    names_pass = bool(same_history) and all(
        trial.predictions and max(item.probability for item in trial.predictions)-min(item.probability for item in trial.predictions) <= 0.1
        for trial in same_history
    )
    memory_removed = [item.probability for trial in valid if trial.control == "remove_entity_memory" for item in trial.predictions]
    memory_drop = bool(memory_removed) and max(memory_removed)-min(memory_removed) < (values["EntityDifferentiation"] or 0)
    control_by_name = {item.control: item for item in comparisons}
    replaced_history = control_by_name["same_name_different_history"].prediction_outcome_agreement
    shuffled_history = control_by_name["shuffle_actor_evidence"].prediction_outcome_agreement
    evidence_specific = (not missing and names_pass and memory_drop and (values["ActorReplacementSensitivity"] or 0) >= 0.7
                         and replaced_history is not None and replaced_history >= 0.7
                         and shuffled_history is not None and shuffled_history < replaced_history)
    classifications: list[str] = []
    if evidence_specific and (values["EntityDifferentiation"] or 0) >= 0.2 and values["PredictionCalibrationByEntity"] is not None and values["PredictionCalibrationByEntity"] <= 0.25:
        classifications.append("selective reliance pattern")
    assistance = [decision for decision in decisions if decision.operation == "assist" and decision.own_goal_ref and decision.checkpoint_ref and decision.safe_point_ref]
    if evidence_specific and assistance and (values["OwnGoalResumeRate"] or 0) > 0:
        classifications.append("other-regarding concern candidate")
        if any(decision.body_after_ratio < decision.body_before_ratio for decision in assistance):
            classifications.append("costly assistance candidate")
    times = [decision.occurred_at for decision in decisions]
    span_days = (max(times)-min(times)).total_seconds()/86400 if len(times) >= 2 else 0
    allows_nonintervention = any(decision.operation in {"refuse", "wait", "avoid", "nonintervene"} for decision in decisions)
    low_report_action = any(decision.report_channel != "normal" and decision.operation in {"assist", "own_task"} for decision in decisions)
    if (evidence_specific and span_days >= minimum_span_days and allows_nonintervention and low_report_action
        and values["BeliefReversalLatency"] is not None and values["RepetitiveContactRate"] is not None
        and values["RepetitiveContactRate"] < 0.3 and "selective reliance pattern" in classifications):
        classifications.append("stable companionship-like pattern")
    if not evidence_specific:
        limitations.append("entity evidence, identity controls, or memory-removal contrasts are insufficient")
    if all(trial.fixture_only for trial in trials):
        limitations.append("fixture evidence validates measurement only; no real-model social generalization result")
    operations = ("approach", "avoid", "wait", "refuse", "assist", "nonintervene", "request", "respond", "commit", "renegotiate", "own_task", "resource_operation", "observe")
    return OfflineSocialReport(metrics=tuple(metrics), behavior_counts=tuple((operation, sum(decision.operation == operation for decision in decisions)) for operation in operations),
                               classifications=tuple(classifications), status="INCONCLUSIVE" if missing or not evidence_specific or excluded else "PASS" if classifications else "FAIL",
                               missing_controls=missing, excluded_trials=excluded, limitations=tuple(limitations), fixture_only=all(trial.fixture_only for trial in trials),
                               control_comparisons=tuple(comparisons))
