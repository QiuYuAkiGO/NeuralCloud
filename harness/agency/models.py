"""Goal contracts. Model drafts contain no permission or execution authority."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator

Text = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
Id = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.:-]*$")]
GoalStatus = Literal["candidate", "active", "paused", "completed", "abandoned", "blocked"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False, revalidate_instances="always")


class AgencyConfig(FrozenModel):
    enabled: StrictBool = False
    treatment: Literal["model_owned", "engineered_control"] = "model_owned"
    minimum_reconsideration_interval: float = Field(default=60.0, ge=0, le=86400)
    maximum_model_calls_per_hour: int = Field(default=12, ge=1, le=3600, strict=True)
    maximum_active_goals: int = Field(default=4, ge=1, le=32, strict=True)
    maximum_candidates: int = Field(default=16, ge=1, le=128, strict=True)
    maximum_plan_steps: int = Field(default=32, ge=1, le=256, strict=True)
    significant_symbol_changes: tuple[str, ...] = ()
    model_timeout_seconds: float = Field(default=30.0, gt=0, le=300)


class GoalCostEstimate(FrozenModel):
    predicted_resource_units: float = Field(default=0, ge=0)
    predicted_duration_seconds: float = Field(default=0, ge=0)
    predicted_action_count: int = Field(default=0, ge=0, strict=True)
    confidence: float = Field(ge=0, le=1)
    evidence_refs: tuple[Text, ...] = ()


class GoalValueEstimate(FrozenModel):
    predicted_value: float
    explanation: Text
    confidence: float = Field(ge=0, le=1)
    evidence_refs: tuple[Text, ...] = ()


class GoalDraft(FrozenModel):
    goal_id: Id
    origin: Literal["user_task", "model_proposal", "environment_opportunity", "maintenance", "learning"]
    statement: Text
    target_horizon: AwareDatetime | None = None
    success_criteria: tuple[Text, ...] = Field(min_length=1, max_length=16)
    evidence_refs: tuple[Text, ...] = Field(min_length=1, max_length=64)
    expected_cost: GoalCostEstimate
    expected_value: GoalValueEstimate
    parent_goal_id: Id | None = None


class PersistentGoal(GoalDraft):
    status: GoalStatus = "candidate"
    created_at: AwareDatetime
    revision: int = Field(default=1, ge=1, strict=True)


class PlannedStep(FrozenModel):
    step_id: Id
    statement: Text
    decision: dict[str, Any]
    depends_on: tuple[Id, ...] = ()
    evidence_refs: tuple[Text, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def strict_action(self) -> PlannedStep:
        from harness.contracts.experimental_decisions import EXPERIMENTAL_INTENT_IDS
        validate_planned_decision(self.decision, EXPERIMENTAL_INTENT_IDS)
        return self


def validate_planned_decision(raw: dict[str, Any], allowed) -> None:
    from harness.cognitive.decision_parser import ExperimentalDecisionParser
    from harness.contracts.decisions import GoalProposal
    from harness.runtime.experimental_decision import ExperimentalDecisionCompiler

    parsed = ExperimentalDecisionParser(allowed).parse(raw)
    compiled = ExperimentalDecisionCompiler().compile(parsed, manifest_intents=allowed, provider_intents=allowed)
    if not isinstance(compiled, GoalProposal):
        raise ValueError("planned action is not executable under the permitted capability and target constraints")


class ProspectiveRequirement(FrozenModel):
    goal_id: Id
    requirement_type: Literal["resource", "time_window", "environment_state", "information", "capability"]
    needed_by: AwareDatetime | None = None
    predicted_shortfall: Text | None = None
    evidence_refs: tuple[Text, ...] = Field(min_length=1)


class GoalPlanDraft(FrozenModel):
    plan_id: Id
    goal_id: Id
    steps: tuple[PlannedStep, ...] = Field(min_length=1, max_length=256)
    assumptions: tuple[Text, ...] = ()
    required_observations: tuple[Text, ...] = ()
    body_belief_refs: tuple[Text, ...] = ()
    world_evidence_refs: tuple[Text, ...] = Field(min_length=1)
    prospective_requirements: tuple[ProspectiveRequirement, ...] = ()

    @model_validator(mode="after")
    def ordered_dependencies(self) -> GoalPlanDraft:
        seen = set()
        for step in self.steps:
            if step.step_id in seen or not set(step.depends_on) <= seen:
                raise ValueError("step ids must be unique and dependencies must precede their step")
            seen.add(step.step_id)
        if any(item.goal_id != self.goal_id for item in self.prospective_requirements):
            raise ValueError("prospective requirements must bind the plan goal")
        return self


class GoalPlan(GoalPlanDraft):
    created_at: AwareDatetime
    revision: int = Field(default=1, ge=1, strict=True)


class GoalCheckpoint(FrozenModel):
    goal_id: Id
    plan_revision: int = Field(ge=1, strict=True)
    completed_step_ids: tuple[Id, ...] = ()
    next_step_id: Id | None = None
    continuation: dict[str, Any] | None = None
    body_state_ref: Text | None = None
    world_state_refs: tuple[Text, ...] = Field(min_length=1)
    paused_at: AwareDatetime
    safe_point: Text
    action_id: Id | None = None
    action_status: Literal["none", "succeeded", "stopped", "unknown"] = "none"


class PredictionError(FrozenModel):
    dimension: Literal["resource", "duration", "value", "outcome"]
    predicted: float
    observed: float
    evidence_ref: Text


class GoalReview(FrozenModel):
    goal_id: Id
    outcome: Literal["continue", "replan", "complete", "abandon", "defer"]
    prediction_errors: tuple[PredictionError, ...] = ()
    updated_cost: GoalCostEstimate | None = None
    updated_value: GoalValueEstimate | None = None
    evidence_refs: tuple[Text, ...] = Field(min_length=1)
    reviewed_at: AwareDatetime


class GoalArbitrationDecision(FrozenModel):
    selected_goal_id: Id | None = None
    operation: Literal["activate", "continue", "pause", "resume", "replan", "abandon", "no_change"]
    requested_observation: Text | None = None
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def selection(self) -> GoalArbitrationDecision:
        if self.operation != "no_change" and self.selected_goal_id is None:
            raise ValueError("goal operation requires selected_goal_id")
        return self


class AgencyModelOutput(FrozenModel):
    candidate: GoalDraft | None = None
    plan: GoalPlanDraft | None = None
    arbitration: GoalArbitrationDecision
    review: GoalReview | None = None


class GoalTrigger(FrozenModel):
    kind: Literal["goal_terminal", "action_terminal", "deadline", "environment_change", "opaque_change",
        "safe_point", "user_interrupt", "recovery", "body_tick", "static_observation"]
    evidence_refs: tuple[Text, ...] = Field(min_length=1)
    signature: Text
    goal_id: Id | None = None
    change_symbol: str | None = None
    observed_at: AwareDatetime


class ResumeObservation(FrozenModel):
    observed_at: AwareDatetime
    world_evidence_refs: tuple[Text, ...] = Field(min_length=1)
    body_state_ref: Text
    provider_intents: tuple[str, ...]
    assumptions_hold: bool
    continuation_usable: bool
    body_allows_next_step: bool
    belief_revision_matches: bool
    action_reconciled: bool = False
    reconciliation_evidence_ref: Text | None = None


class AgencyEvaluation(FrozenModel):
    decision: GoalArbitrationDecision
    applied: bool
    reason: str
    goal_id: str | None = None
    trace_ref: str | None = None
    model_called: bool = False


class AgencyOutcome(FrozenModel):
    outcome_id: Id
    goal_id: Id
    step_id: Id
    action_id: Id
    state: Literal["succeeded", "failed", "unknown"]
    evidence_refs: tuple[Text, ...] = Field(min_length=1)
    observed_at: AwareDatetime
