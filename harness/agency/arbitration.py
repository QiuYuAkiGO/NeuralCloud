"""Feasibility filters for model-owned arbitration, with no utility ranking."""

from __future__ import annotations

from collections.abc import Iterable

from harness.agency.models import AgencyConfig, GoalArbitrationDecision, ResumeObservation, validate_planned_decision
from harness.agency.store import GoalStore


def validate_resume(store: GoalStore, goal_id: str, observation: ResumeObservation | None) -> None:
    checkpoint = store.checkpoint(goal_id)
    plan = store.plan(goal_id)
    if checkpoint is None or plan is None:
        raise ValueError("resume requires a persisted plan and safe-point checkpoint")
    if observation is None or observation.observed_at <= checkpoint.paused_at:
        raise ValueError("resume requires a fresh observation after the checkpoint")
    if checkpoint.plan_revision != plan.revision:
        raise ValueError("plan changed since checkpoint; replan before resuming")
    if not all((observation.assumptions_hold, observation.continuation_usable,
                observation.body_allows_next_step, observation.belief_revision_matches)):
        raise ValueError("resume facts changed; request observation, replan or abandon")
    unknown = [item for item in store.all("action_outcome") if item["goal_id"] == goal_id and item["state"] == "unknown"]
    if unknown:
        raise ValueError("unknown action requires a recorded reconciliation outcome; replay is forbidden")
    if checkpoint.action_status == "unknown":
        reconciled = store.get("action_outcome", checkpoint.action_id or "")
        if (reconciled is None or reconciled["state"] == "unknown" or not observation.action_reconciled
                or observation.reconciliation_evidence_ref not in reconciled["evidence_refs"]):
            raise ValueError("checkpoint action requires verified reconciliation before resume")
    if checkpoint.next_step_id is not None:
        step = next(item for item in plan.steps if item.step_id == checkpoint.next_step_id)
        validate_planned_decision(step.decision, observation.provider_intents)


def apply_arbitration(store: GoalStore, decision: GoalArbitrationDecision, *, config: AgencyConfig,
                      provider_intents: Iterable[str], permitted_intents: Iterable[str],
                      evidence_refs: tuple[str, ...], resume_observation: ResumeObservation | None = None) -> bool:
    if decision.operation == "no_change":
        return False
    goal = store.goal(str(decision.selected_goal_id))
    plan = store.plan(goal.goal_id)
    if decision.operation in {"activate", "continue", "resume", "replan"}:
        if plan is None:
            raise ValueError("selected goal requires an evidence-backed plan")
        for step in plan.steps:
            validate_planned_decision(step.decision, set(provider_intents) & set(permitted_intents))
    if decision.operation == "activate":
        if goal.status not in {"candidate", "blocked"}:
            raise ValueError("activate requires a candidate or blocked goal")
        if sum(item.status == "active" for item in store.goals()) >= config.maximum_active_goals:
            raise ValueError("active goal budget reached; model must choose what to pause")
        store.transition(goal.goal_id, "active", evidence_refs=evidence_refs)
    elif decision.operation == "continue":
        if goal.status != "active":
            raise ValueError("continue requires an active goal")
        if any(item["goal_id"] == goal.goal_id and item["state"] == "unknown" for item in store.all("action_outcome")):
            raise ValueError("unknown action cannot be replayed")
        return False
    elif decision.operation == "pause":
        if goal.status != "active":
            raise ValueError("pause requires an active goal")
        store.transition(goal.goal_id, "paused", evidence_refs=evidence_refs)
    elif decision.operation == "resume":
        if goal.status != "paused":
            raise ValueError("resume requires a paused goal")
        validate_resume(store, goal.goal_id, resume_observation)
        store.transition(goal.goal_id, "active", evidence_refs=evidence_refs)
    elif decision.operation == "replan":
        if goal.status not in {"active", "paused", "blocked", "candidate"}:
            raise ValueError("cannot replan a terminal goal")
        # New plan is supplied by the model and committed before arbitration.
        return True
    elif decision.operation == "abandon":
        store.transition(goal.goal_id, "abandoned", evidence_refs=evidence_refs)
    return True
