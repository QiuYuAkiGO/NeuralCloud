"""Typed character decisions and evidence accepted by Runtime."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.contracts.envelope import VersionVector
from harness.core.ids import new_id
from harness.embodiment.contracts import EmbodiedActionSpec


class ReplyDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["reply"] = "reply"
    text: str = Field(min_length=1)
    segments: list[str] = Field(default_factory=list)
    policy_id: str | None = None
    policy_decision_id: str | None = None
    channel: str = "chat"

    @model_validator(mode="after")
    def _validate_segments(self) -> ReplyDecision:
        if any(not segment.strip() for segment in self.segments):
            raise ValueError("reply segments cannot be blank")
        return self


class GoalProposal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["goal"] = "goal"
    goal_id: str = Field(default_factory=lambda: new_id("goal"))
    policy_id: str
    goal: str = Field(min_length=1)
    success_criteria: list[str] = Field(min_length=1)
    permission_class: str = Field(min_length=1)
    reversible: bool
    verification_method: str = Field(min_length=1)
    required_approvals: list[str] = Field(default_factory=list)
    embodied_action: EmbodiedActionSpec | None = None


class NoActionDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["no_action"] = "no_action"
    policy_id: str | None = None
    reason: str = Field(min_length=1)


class DeferDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: Literal["defer"] = "defer"
    policy_id: str | None = None
    reason: str = Field(min_length=1)
    required_evidence: list[str] = Field(default_factory=list)


CharacterDecision = Annotated[
    ReplyDecision | GoalProposal | NoActionDecision | DeferDecision,
    Field(discriminator="kind"),
]


class AutonomousAssessment(BaseModel):
    """Metadata for one autonomous-output assessment.

    The model's raw prose deliberately stays in ``record_ref`` rather than the
    JSON command result/runtime journal.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    assessment_id: str = Field(min_length=1)
    decision: Literal["silent", "speak"]
    record_ref: str = Field(min_length=1)
    evaluator: Literal["aux", "main_fallback", "forced_silent"]
    internal_memory_ref: str | None = None
    session_id: str = Field(min_length=1)
    channel: str = Field(min_length=1)
    evaluated_at: AwareDatetime
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)


class PreparedResponse(BaseModel):
    """Replayable Cognitive output; it has not yet been delivered or committed."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    prepare_id: str = Field(default_factory=lambda: new_id("prepare"))
    event_id: str
    correlation_id: str
    session_id: str
    decision: CharacterDecision
    read_versions: VersionVector
    prepared_at: AwareDatetime
    expires_at: AwareDatetime
    model_version: str
    prompt_version: str
    production_effects: dict | None = None

    @model_validator(mode="after")
    def _validate_expiry(self) -> PreparedResponse:
        if self.expires_at <= self.prepared_at:
            raise ValueError("expires_at must be later than prepared_at")
        return self


class DeliveryEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_kind: Literal["delivery"] = "delivery"
    delivery_id: str = Field(default_factory=lambda: new_id("delivery"))
    prepare_id: str
    delivered_at: AwareDatetime
    channel: str
    success: bool
    delivered_segments: list[str] = Field(default_factory=list)
    external_ref: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_delivery(self) -> DeliveryEvidence:
        if self.success and not self.delivered_segments:
            raise ValueError("successful delivery requires delivered_segments")
        if not self.success and not self.error:
            raise ValueError("failed delivery requires error")
        return self


class ActionOutcomeEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_kind: Literal["action_outcome"] = "action_outcome"
    outcome_id: str = Field(default_factory=lambda: new_id("outcome"))
    prepare_id: str
    goal_id: str
    observed_at: AwareDatetime
    success: bool
    summary: str = Field(min_length=1)
    evidence_refs: list[str] = Field(default_factory=list)
    external_effect_ref: str | None = None


class DecisionEvidence(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_kind: Literal["decision"] = "decision"
    prepare_id: str
    recorded_at: AwareDatetime
    decision_kind: Literal["no_action", "defer"]
    reason: str = Field(min_length=1)


CommitEvidence = Annotated[
    DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence,
    Field(discriminator="evidence_kind"),
]
