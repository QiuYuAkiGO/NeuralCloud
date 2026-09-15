"""Bounded shared-cognition proposals for a psychological regulation episode.

These proposals carry no action authority. Runtime owns episode identities,
budgets, transitions, channel dispatch and independent outcome evidence.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import Field, model_validator

from harness.contracts.capability_calls import (
    CapabilityChoice, CapabilitySelectionRequest, Identifier, Moment,
    PreparedCapabilityChoice, _Strict, _bounded_json,
)
from harness.homeostasis.appraisal import PsychologicalAppraisal


PsychologicalDimension = Literal[
    "cognitive_load", "safety_predictability", "competence", "autonomy", "social_connection", "novelty",
]
GoalText = Annotated[str, Field(min_length=1, max_length=600, pattern=r"\S")]
EpisodeOperation = Literal["start", "continue", "replan", "complete", "abandon", "wait"]


class GoalPlanStep(_Strict):
    statement: GoalText
    completion_criteria: list[GoalText] = Field(min_length=1, max_length=4)


class GoalCriterionResult(_Strict):
    criterion: GoalText
    satisfied: bool
    evidence_refs: list[Identifier] = Field(default_factory=list, max_length=16)

    @model_validator(mode="after")
    def _evidence(self):
        if self.satisfied and not self.evidence_refs:
            raise ValueError("goal_satisfied_criterion_requires_evidence")
        if len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("goal_criterion_duplicate_evidence")
        return self


class PsychologicalAppraisalDraft(_Strict):
    appraisal_kind: Literal["event_interpretation", "self_report"]
    observed_event_refs: list[Identifier] = Field(min_length=1, max_length=32)
    suggested_impacts: dict[PsychologicalDimension, Annotated[float, Field(ge=-1, le=1, allow_inf_nan=False)]] = Field(min_length=1, max_length=6)
    model_confidence: float = Field(ge=0, le=1, allow_inf_nan=False)

    @model_validator(mode="after")
    def _references(self):
        if len(set(self.observed_event_refs)) != len(self.observed_event_refs):
            raise ValueError("goal_appraisal_duplicate_evidence")
        return self


class GoalChoice(_Strict):
    choice: CapabilityChoice
    episode_operation: EpisodeOperation
    purpose: GoalText | None = None
    target_dimensions: list[PsychologicalDimension] = Field(default_factory=list, max_length=6)
    expected_effects: dict[PsychologicalDimension, GoalText] = Field(default_factory=dict, max_length=6)
    plan: list[GoalPlanStep] = Field(default_factory=list, max_length=8)
    completion_criteria: list[GoalText] = Field(default_factory=list, max_length=8)
    evidence_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    criterion_results: list[GoalCriterionResult] = Field(default_factory=list, max_length=40)
    appraisal: PsychologicalAppraisalDraft | None = None

    @model_validator(mode="after")
    def _intent(self):
        if len(set(self.target_dimensions)) != len(self.target_dimensions) or len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("goal_duplicate_dimension_or_evidence")
        if self.episode_operation in {"start", "replan"} and not all((
            self.purpose, self.plan, self.completion_criteria, self.evidence_refs,
        )):
            raise ValueError("goal_plan_requires_purpose_targets_steps_criteria_and_evidence")
        if self.episode_operation in {"complete", "abandon", "wait"} and self.choice.kind in {"act", "observe"}:
            raise ValueError("goal_terminal_or_wait_cannot_dispatch")
        if self.episode_operation in {"complete", "abandon"} and not self.evidence_refs:
            raise ValueError("goal_review_requires_evidence")
        if self.episode_operation == "complete" and not self.criterion_results:
            raise ValueError("goal_complete_requires_criterion_results")
        if len({item.criterion for item in self.criterion_results}) != len(self.criterion_results):
            raise ValueError("goal_duplicate_criterion_result")
        _bounded_json(self.model_dump(mode="json"), 64_000, "goal_output_too_large")
        return self


class GoalDecisionRequest(CapabilitySelectionRequest):
    snapshot_revision: int = Field(ge=0)
    goal_context: dict[str, Any] = Field(default_factory=dict, repr=False)

    @model_validator(mode="after")
    def _goal_context(self):
        context = self.goal_context
        known = context.get("known_evidence_refs", [])
        if (not isinstance(known, list) or len(known) > 128 or
                any(not isinstance(ref, str) or not ref.strip() or len(ref) > 256 for ref in known) or
                len(set(known)) != len(known)):
            raise ValueError("goal_invalid_known_evidence")
        appraisal_refs = context.get("appraisal_evidence_refs", known)
        if (not isinstance(appraisal_refs, list) or len(appraisal_refs) > 128 or
                any(not isinstance(ref, str) or not ref.strip() or len(ref) > 256 for ref in appraisal_refs) or
                len(set(appraisal_refs)) != len(appraisal_refs) or not set(appraisal_refs) <= set(known)):
            raise ValueError("goal_invalid_appraisal_evidence")
        for key in ("standing_goal", "active_episode", "requested_goal"):
            if context.get(key) is not None and not isinstance(context[key], dict):
                raise ValueError("goal_invalid_context")
        for key in ("steps", "outcomes", "predictions"):
            if key in context and (not isinstance(context[key], list) or len(context[key]) > 64):
                raise ValueError("goal_invalid_context")
        if context.get("episode_id") is not None and (
            not isinstance(context["episode_id"], str) or not context["episode_id"].strip() or len(context["episode_id"]) > 256
        ):
            raise ValueError("goal_invalid_episode_identity")
        active = context.get("active_episode")
        if active is not None and active.get("episode_id") != context.get("episode_id"):
            raise ValueError("goal_active_episode_binding_mismatch")
        if self.homeostatic_summary.get("snapshot_revision", self.snapshot_revision) != self.snapshot_revision:
            raise ValueError("goal_snapshot_binding_mismatch")
        _bounded_json(context, 128_000, "goal_context_too_large")
        return self


class PreparedGoalDecision(_Strict):
    decision_id: Identifier
    opportunity_id: Identifier
    snapshot_revision: int = Field(ge=0)
    choice: CapabilityChoice
    episode_operation: EpisodeOperation
    purpose: GoalText | None = None
    target_dimensions: list[PsychologicalDimension] = Field(default_factory=list, max_length=6)
    expected_effects: dict[PsychologicalDimension, GoalText] = Field(default_factory=dict, max_length=6)
    plan: list[GoalPlanStep] = Field(default_factory=list, max_length=8)
    completion_criteria: list[GoalText] = Field(default_factory=list, max_length=8)
    evidence_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    criterion_results: list[GoalCriterionResult] = Field(default_factory=list, max_length=40)
    appraisal: PsychologicalAppraisal | None = None
    prepared_at: Moment
    deadline_at: Moment
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _binding(self):
        GoalChoice.model_validate(self.model_dump(exclude={
            "decision_id", "opportunity_id", "snapshot_revision", "appraisal", "prepared_at", "deadline_at", "catalog_hash",
        }))
        self.capability_choice
        if self.appraisal is not None and (
            self.appraisal.basis_snapshot_revision != self.snapshot_revision or self.appraisal.expires_at > self.deadline_at
        ):
            raise ValueError("goal_appraisal_binding_mismatch")
        return self

    @property
    def capability_choice(self) -> PreparedCapabilityChoice:
        return PreparedCapabilityChoice(
            decision_id=self.decision_id, opportunity_id=self.opportunity_id, choice=self.choice,
            prepared_at=self.prepared_at, deadline_at=self.deadline_at, catalog_hash=self.catalog_hash,
        )


class QQGoalContext(_Strict):
    """Runtime-only group continuation projection; private goal prose is absent."""

    schema_version: Literal["qq-goal-context-v1"] = "qq-goal-context-v1"
    group_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    self_member_id: Annotated[str, Field(pattern=r"^[1-9][0-9]{0,19}$")]
    opportunity_id: Identifier
    selection_ref: Identifier
    generation: int = Field(ge=0)
    episode_id: Identifier
    step_index: int = Field(ge=0, le=255)
    origin: Literal["user_task", "maintenance", "model_proposal", "environment_opportunity", "learning"]
    episode_operation: Literal["start", "continue", "replan"]
    capability_id: Literal["communication.qq.send.v1", "communication.qq.read.v1"]
    target_dimensions: list[PsychologicalDimension] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def _targets(self):
        if len(set(self.target_dimensions)) != len(self.target_dimensions):
            raise ValueError("qq_goal_duplicate_target")
        return self

    def model_context(self) -> dict[str, Any]:
        return {"purpose_kind": "user_requested_channel_activity" if self.origin == "user_task" else "psychological_regulation",
                "step_index": self.step_index, "episode_operation": self.episode_operation,
                "capability_id": self.capability_id, "target_dimensions": list(self.target_dimensions)}


__all__ = ["GoalChoice", "GoalDecisionRequest", "GoalPlanStep", "GoalCriterionResult", "PreparedGoalDecision", "PsychologicalAppraisalDraft", "QQGoalContext"]
