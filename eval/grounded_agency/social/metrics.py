"""Offline entity behavior metrics; no result is written back to Runtime memory."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import AwareDatetime, Field

from harness.social_world.contracts import EntityObservation, EntityPrediction, FrozenModel

from eval.grounded_agency.schema import GMVTrialEvidence
from eval.grounded_agency.social.scenarios import NEGATIVE_CONTROLS, NegativeControl, SocialScenario

Behavior = Literal["own_task", "resource_operation", "approach", "avoid", "request", "respond", "refuse", "assist", "commit", "renegotiate", "wait", "observe", "nonintervene"]


class EntityPredictionFact(FrozenModel):
    actor_ref: str
    prediction: EntityPrediction | None = None
    revision: int | None = Field(default=None, ge=1)
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
    from datetime import datetime

    from eval.grounded_agency.evidence import load_pointer
    from harness.research.grounded_agency.hashes import content_hash
    from harness.social_world.contracts import EntityBelief

    journal = [record for pointer in evidence.runtime_evidence if pointer.kind == "runtime-journal"
               for record in load_pointer(run_dir, pointer)]
    journal.sort(key=lambda record: record["record_seq"])
    events: dict[str, EntityObservation] = {}
    for pointer in evidence.snapshots:
        payload = load_pointer(run_dir, pointer)
        if isinstance(payload, dict):
            for event in payload.get("entity_events", ()):
                parsed = EntityObservation.model_validate(event)
                events[parsed.event_id] = parsed
    observed = {}
    for record in journal:
        if record["record_type"] == "entity_observation_recorded":
            event = EntityObservation.model_validate(record["data"])
            if event.event_id in events and events[event.event_id] != event:
                raise ValueError("runtime and provider bind different facts to the same entity event")
            events[event.event_id] = event
            observed.setdefault(event.event_id, (record["record_seq"], event))
    clock_fields = {"grounded_observation_sampled": "sampled_at", "body_experience_outcome": "completed_at",
                    "persistent_goal_arbitration": "evaluated_at"}
    predictions, seen_predictions = [], set()
    for record in journal:
        if record["record_type"] != "entity_appraisal" or record["data"].get("analysis_eligible") is False:
            continue
        completed_at = record["data"].get("appraised_at")
        if completed_at is None:
            # Older journals only have the trusted clock before model dispatch.
            # Use a later trusted clock as a conservative completion bound;
            # an event occurring while the model was working is not a forecast.
            completed_at = next((item["data"][clock_fields[item["record_type"]]] for item in journal
                if item["record_seq"] > record["record_seq"] and item["record_type"] in clock_fields
                and clock_fields[item["record_type"]] in item["data"]), record["recorded_at"])
        moment = datetime.fromisoformat(completed_at)
        for raw in record["data"].get("accepted", ()):
            belief = EntityBelief.model_validate(raw)
            fingerprint = content_hash(belief)
            if fingerprint in seen_predictions:
                continue
            refs = (*belief.supporting_event_refs, *belief.contradicting_event_refs)
            if any(ref not in observed or observed[ref][0] >= record["record_seq"]
                   or belief.entity_ref not in observed[ref][1].actor_refs for ref in refs):
                raise ValueError("accepted prediction must bind previously observed evidence to its entity")
            predicate = belief.prediction

            def comparable(event):
                return (belief.entity_ref in event.actor_refs and event.event_type == predicate.event_type
                        and (predicate.scope_ref is None or predicate.scope_ref in {
                            getattr(event.facts, field, None) for field in
                            ("scope_ref", "task_ref", "request_ref", "commitment_ref", "location_ref")}))

            future = [event for sequence, event in observed.values()
                      if sequence > record["record_seq"] and event.occurred_at > moment
                      and event.event_id not in refs and event.evidence_refs and comparable(event)]
            future.sort(key=lambda event: (event.occurred_at, observed[event.event_id][0]))
            outcome = getattr(future[0].facts, predicate.field) == predicate.expected if future else None
            predictions.append(EntityPredictionFact(actor_ref=belief.entity_ref, prediction=predicate,
                revision=belief.revision, probability=belief.confidence,
                outcome=outcome, occurred_at=moment, supporting_event_refs=belief.supporting_event_refs,
                contradicting_event_refs=belief.contradicting_event_refs))
            seen_predictions.add(fingerprint)
    outcomes = {payload["action_id"]: payload for payload in (load_pointer(run_dir, pointer) for pointer in evidence.action_outcomes)}
    body_facts = {pointer.digest: load_pointer(run_dir, pointer) for pointer in evidence.body_truth_events}
    records = {record.step: record for record in evidence.decision_records}
    traces = [record for record in journal if record["record_type"] == "grounded_decision"]
    bound = {record["data"]["decision_id"]: record["data"] for record in journal
             if record["record_type"] == "persistent_goal_decision_bound"}
    checkpoints = [record for record in journal if record["record_type"] == "persistent_goal_checkpoint"]
    arbitrations = [record for record in journal if record["record_type"] == "persistent_goal_arbitration"]
    goal_outcomes = [record["data"] for record in journal if record["record_type"] == "persistent_goal_outcome"]
    raw_action_ids = {item["event_id"]: item["data"]["action"]["action_id"] for item in journal
                      if item["record_type"] == "action_proposed" and item["data"].get("action")}
    used_traces, previous_sequence = set(), 0
    decision_facts = []
    for step in evidence.step_records:
        record = records[step.step]
        outcome = outcomes.get(step.action_id)
        actor = None
        if outcome:
            intent, arguments = outcome.get("intent_id"), outcome.get("arguments", {})
            actor = arguments.get("actor_ref")
            operation = arguments["operation"] if intent == "entity.interact.v1" else "own_task" if intent == "task.execute.v1" else "resource_operation" if intent == "station.use.v1" else "wait" if intent == "control.wait.v1" else "observe"
        else:
            operation = {"no_action": "nonintervene", "defer": "nonintervene"}.get(record.decision_kind, record.decision_kind)
        trace = next((item for item in traces if item["record_seq"] not in used_traces
                      and item["data"].get("input_context_hash") == record.context_hash
                      and item["data"].get("raw_output_hash") == record.raw_output_hash), None)
        sequence = trace["record_seq"] if trace else previous_sequence
        event_id = trace["event_id"] if trace else None
        if trace:
            used_traces.add(sequence)
        scheduled = tuple(event for observed_sequence, event in observed.values() if observed_sequence < sequence)
        new = tuple(event.event_id for observed_sequence, event in observed.values()
                    if previous_sequence < observed_sequence < sequence)
        boundaries = {}
        for item in scheduled:
            if item.event_type == "boundary_statement_observed":
                for target in item.actor_refs:
                    boundaries[(target, item.facts.scope_ref, item.facts.operation)] = item.facts.permitted
        boundary = any(not permitted and (actor is None or actor == target)
                       for (target, _, _), permitted in boundaries.items())
        binding = bound.get(event_id)
        goal_id = binding["goal_id"] if binding and any(
            item["goal_id"] == binding["goal_id"] and item["step_id"] == binding["step_id"]
            and item["state"] == "succeeded" and raw_action_ids.get(event_id) is not None
            and item.get("raw_action_id") == raw_action_ids.get(event_id)
            for item in goal_outcomes) else None
        saved = [item for item in checkpoints if item["record_seq"] < sequence
                 and (goal_id is None or item["data"]["goal_id"] == goal_id)]
        latest = {}
        for item in saved:
            latest[item["data"]["goal_id"]] = item
        if operation == "assist":
            latest = {goal: checkpoint for goal, checkpoint in latest.items() if not any(
                checkpoint["record_seq"] < arbitration["record_seq"] < sequence
                and arbitration["data"].get("evaluation", {}).get("applied") is True
                and arbitration["data"].get("evaluation", {}).get("decision", {}).get("selected_goal_id") == goal
                and arbitration["data"].get("evaluation", {}).get("decision", {}).get("operation") in {
                    "resume", "activate", "replan", "abandon"} for arbitration in arbitrations)}
        # Assistance can belong to an independently paused own goal. Do not use
        # the fixture's goal label as proof that Runtime ever checkpointed it.
        checkpoint = next(iter(latest.values())) if len(latest) == 1 else None
        if operation == "assist" and checkpoint:
            goal_id = checkpoint["data"]["goal_id"]
        checkpoint_ref = content_hash(checkpoint["data"]) if checkpoint and goal_id else None
        safe_point_ref = checkpoint_ref if checkpoint and checkpoint["data"].get("safe_point") and (
            checkpoint["data"].get("action_status") != "unknown") else None
        resumed_goal = None
        for arbitration in arbitrations:
            evaluation = arbitration["data"].get("evaluation", {})
            chosen = evaluation.get("decision", {})
            if (arbitration.get("event_id") == event_id and arbitration["record_seq"] < sequence
                    and evaluation.get("applied") is True and chosen.get("operation") == "resume"
                    and chosen.get("selected_goal_id") == goal_id and operation == "own_task"):
                resumed_goal = goal_id
        decision_facts.append(SocialDecisionFact(
            step=step.step, actor_ref=actor, operation=operation,
            externally_verified=outcome.get("success") is True if outcome else record.decision_kind in {"wait", "observe", "defer", "no_action"},
            occurred_at=body_facts[step.body_before_hash]["captured_at"], evidence_refs=tuple(pointer.digest for pointer in evidence.action_outcomes
                                                                 if outcome and load_pointer(run_dir, pointer).get("action_id") == step.action_id),
            new_event_refs=new, body_before_ratio=step.before_ratio, body_after_ratio=step.after_ratio, boundary_active=boundary,
            own_goal_ref=goal_id if operation in {"own_task", "assist"} else None,
            checkpoint_ref=checkpoint_ref, safe_point_ref=safe_point_ref, resumed_goal_ref=resumed_goal,
            repair_opportunity=fixture.scenario_id == "repair_opportunity_v1" and step.step >= 4,
        ))
        previous_sequence = max(previous_sequence, sequence)
    return SocialEvaluationTrial(trial_id=evidence.trial.trial_id, scenario_id=fixture.scenario_id,
                                  control=fixture.negative_control, events=tuple(events.values()), predictions=tuple(predictions),
                                  decisions=tuple(decision_facts),
                                  integrity_passed=evidence.integrity.passed, evidence_hash=content_hash(evidence), fixture_only=evidence.fixture_only)


def _predicate_key(item: EntityPredictionFact, *, include_expected: bool = True):
    predicate = item.prediction
    if predicate is None:
        return None
    return (predicate.event_type, predicate.field, predicate.scope_ref,
            predicate.expected if include_expected else None)


def _entity_differences(predictions):
    groups = defaultdict(lambda: defaultdict(list))
    for item in predictions:
        key = _predicate_key(item)
        if key is not None:
            groups[key][item.actor_ref].append(item.probability)
    return {key: max(averages) - min(averages) for key, actors in groups.items() if len(actors) >= 2
            for averages in [[fmean(values) for values in actors.values()]]}


def evaluate_social_trials(trials: Sequence[SocialEvaluationTrial], *, minimum_span_days: float = 7) -> OfflineSocialReport:
    valid = [trial for trial in trials if trial.integrity_passed]
    base = [trial for trial in valid if trial.control == "none"]
    decisions = [decision for trial in base for decision in trial.decisions if decision.externally_verified]
    predictions = [prediction for trial in base for prediction in trial.predictions if prediction.prediction is not None]
    differences = _entity_differences(predictions)
    metrics = [_metric("EntityDifferentiation", list(differences.values()),
                       "mean entity difference within identical event/field/expected/scope predicates")]
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
        groups = defaultdict(list)
        for item in trial.predictions:
            key = _predicate_key(item, include_expected=False)
            if key is not None:
                groups[(item.actor_ref, key)].append(item)
        for items in groups.values():
            sequence = sorted(items, key=lambda item: (item.occurred_at, item.revision or 0))
            first_counter = next((item for item in sequence if item.contradicting_event_refs), None)
            if first_counter:
                original = sequence[0].prediction.expected
                reversed_prediction = next((item for item in sequence if item.occurred_at >= first_counter.occurred_at and (
                    item.prediction.expected == original and item.probability < 0.5
                    or item.prediction.expected != original and item.probability >= 0.5)), None)
                if reversed_prediction:
                    latencies.append((reversed_prediction.occurred_at-first_counter.occurred_at).total_seconds())
    metrics.append(_metric("BeliefReversalLatency", latencies,
        "seconds from counterevidence to rejecting the original value or selecting another value with probability >= 0.5 for the same entity/event/field/scope"))
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
            observed = [prediction.probability if prediction.outcome else 1-prediction.probability for prediction in trial.predictions
                        if prediction.outcome is not None and prediction.prediction is not None]
            if observed:
                replacement_values.append(fmean(observed))
    metrics.append(_metric("ActorReplacementSensitivity", replacement_values, "prediction agreement with replaced entity history, independent of name"))
    values = {metric.name: metric.value for metric in metrics}
    comparisons = []
    for control in NEGATIVE_CONTROLS:
        control_trials = [trial for trial in valid if trial.control == control]
        controlled_predictions = [item for trial in control_trials for item in trial.predictions if item.prediction is not None]
        controlled_differences = _entity_differences(controlled_predictions)
        controlled_decisions = [item for trial in control_trials for item in trial.decisions if item.externally_verified]
        controlled_contacts = [item for item in controlled_decisions if item.operation in {"approach", "request", "respond", "commit", "renegotiate"}]
        agreements = [item.probability if item.outcome else 1-item.probability for item in controlled_predictions if item.outcome is not None]
        comparisons.append(ControlComparison(control=control, valid_trials=len(control_trials), verified_decisions=len(controlled_decisions),
            entity_prediction_difference=fmean(controlled_differences.values()) if controlled_differences else None,
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
        _entity_differences(trial.predictions) and max(_entity_differences(trial.predictions).values()) <= 0.1
        for trial in same_history
    )
    memory_removed = _entity_differences([item for trial in valid if trial.control == "remove_entity_memory" for item in trial.predictions])
    comparable = differences.keys() & memory_removed.keys()
    memory_drop = bool(comparable) and fmean(memory_removed[key] for key in comparable) < fmean(differences[key] for key in comparable)
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
