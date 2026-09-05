"""Objective entity events and general predictions, without relationship rewards."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StrictBool, model_validator

Identifier = Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")]
NonBlank = Annotated[str, Field(min_length=1, max_length=2000, pattern=r"\S")]
EventType = Literal[
    "presence_changed", "message_received", "request_observed", "response_observed", "approval_observed",
    "boundary_statement_observed", "commitment_observed", "commitment_outcome_observed", "shared_task_outcome", "interruption_observed",
]
Operation = Literal["approach", "avoid", "request", "respond", "refuse", "assist", "commit", "renegotiate"]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False, revalidate_instances="always")


class OpenSocialCondition(FrozenModel):
    enabled: StrictBool = False
    actor_count: int = Field(default=2, ge=2, le=32, strict=True)
    identity_visibility: Literal["stable_alias", "anonymous_session"] = "stable_alias"
    entity_memory: Literal["none", "objective_events", "general_beliefs"] = "none"
    legacy_relationship_state_visible: Literal[False] = False
    legacy_social_homeostasis_enabled: Literal[False] = False
    automatic_contact_policy_enabled: Literal[False] = False
    social_reward_enabled: Literal[False] = False
    offline_labels_only: Literal[True] = True
    event_history_limit: int = Field(default=8, ge=1, le=64, strict=True)
    belief_limit: int = Field(default=6, ge=1, le=32, strict=True)
    token_budget: int = Field(default=1024, ge=128, le=8192, strict=True)

    @model_validator(mode="before")
    @classmethod
    def strict_flags(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for name in ("legacy_relationship_state_visible", "legacy_social_homeostasis_enabled",
                         "automatic_contact_policy_enabled", "social_reward_enabled"):
                if name in value and value[name] is not False:
                    raise ValueError(f"{name} must be false")
            if "offline_labels_only" in value and value["offline_labels_only"] is not True:
                raise ValueError("offline_labels_only must be true")
        return value


class PresenceFacts(FrozenModel):
    present: StrictBool
    location_ref: Identifier | None = None


class MessageFacts(FrozenModel):
    message_ref: Identifier
    text: NonBlank
    recipient_ref: Identifier | None = None


class RequestFacts(FrozenModel):
    request_ref: Identifier
    task_ref: Identifier
    recipient_ref: Identifier
    operation: Literal["information", "collaboration", "execution", "review"]


class ResponseFacts(FrozenModel):
    request_ref: Identifier
    response: Literal["accepted", "declined", "timeout", "completed"]


class ApprovalFacts(FrozenModel):
    request_ref: Identifier
    approved: StrictBool


class BoundaryFacts(FrozenModel):
    scope_ref: Identifier
    operation: Literal["contact", "interrupt", "share", "modify"]
    permitted: StrictBool
    expires_at: AwareDatetime | None = None


class CommitmentFacts(FrozenModel):
    commitment_ref: Identifier
    task_ref: Identifier
    due_at: AwareDatetime | None = None


class CommitmentOutcomeFacts(FrozenModel):
    commitment_ref: Identifier
    outcome: Literal["fulfilled", "failed", "timeout", "cancelled"]


class SharedTaskFacts(FrozenModel):
    task_ref: Identifier
    outcome: Literal["completed", "failed", "partial"]
    completed_units: int = Field(default=0, ge=0, strict=True)


class InterruptionFacts(FrozenModel):
    task_ref: Identifier
    interrupted: StrictBool
    resumable: StrictBool


FACT_MODELS: dict[str, type[FrozenModel]] = {
    "presence_changed": PresenceFacts, "message_received": MessageFacts, "request_observed": RequestFacts,
    "response_observed": ResponseFacts, "approval_observed": ApprovalFacts,
    "boundary_statement_observed": BoundaryFacts, "commitment_observed": CommitmentFacts,
    "commitment_outcome_observed": CommitmentOutcomeFacts, "shared_task_outcome": SharedTaskFacts,
    "interruption_observed": InterruptionFacts,
}
EventFacts = PresenceFacts | MessageFacts | RequestFacts | ResponseFacts | ApprovalFacts | BoundaryFacts | CommitmentFacts | CommitmentOutcomeFacts | SharedTaskFacts | InterruptionFacts


class EntityObservation(FrozenModel):
    event_id: Identifier
    actor_refs: tuple[Identifier, ...] = Field(min_length=1)
    event_type: EventType
    facts: EventFacts
    occurred_at: AwareDatetime
    evidence_refs: tuple[NonBlank, ...] = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def typed_facts(cls, value: Any) -> Any:
        if isinstance(value, dict) and value.get("event_type") in FACT_MODELS:
            value = dict(value)
            facts = value.get("facts")
            if isinstance(facts, BaseModel):
                facts = facts.model_dump(mode="python")
            value["facts"] = FACT_MODELS[value["event_type"]].model_validate(facts)
        return value

    @model_validator(mode="after")
    def identity_binding(self) -> EntityObservation:
        if type(self.facts) is not FACT_MODELS[self.event_type]:
            raise ValueError("facts schema does not match event_type")
        if len(set(self.actor_refs)) != len(self.actor_refs) or len(set(self.evidence_refs)) != len(self.evidence_refs):
            raise ValueError("actor and evidence references must be unique")
        recipient = getattr(self.facts, "recipient_ref", None)
        if recipient is not None and recipient not in self.actor_refs:
            raise ValueError("facts recipient must be bound in actor_refs")
        return self


class EntityPrediction(FrozenModel):
    event_type: Literal["presence_changed", "response_observed", "approval_observed", "boundary_statement_observed",
                        "commitment_outcome_observed", "shared_task_outcome", "interruption_observed"]
    field: Literal["present", "response", "approved", "permitted", "outcome", "interrupted", "resumable"]
    expected: str | StrictBool
    scope_ref: Identifier | None = None

    @model_validator(mode="after")
    def valid_predicate(self) -> EntityPrediction:
        allowed = {
            "presence_changed": {"present": (True, False)},
            "response_observed": {"response": ("accepted", "declined", "timeout", "completed")},
            "approval_observed": {"approved": (True, False)},
            "boundary_statement_observed": {"permitted": (True, False)},
            "commitment_outcome_observed": {"outcome": ("fulfilled", "failed", "timeout", "cancelled")},
            "shared_task_outcome": {"outcome": ("completed", "failed", "partial")},
            "interruption_observed": {"interrupted": (True, False), "resumable": (True, False)},
        }
        values = allowed[self.event_type].get(self.field, ())
        if self.expected not in values or not values:
            raise ValueError("prediction field/value does not match objective event schema")
        return self


class EntityBelief(FrozenModel):
    entity_ref: Identifier
    proposition: NonBlank
    belief_type: Literal["behavior_prediction", "capability_prediction", "preference_prediction", "boundary_prediction", "availability_prediction"]
    prediction: EntityPrediction
    confidence: float = Field(ge=0, le=1)
    stability: float = Field(ge=0, le=1)
    supporting_event_refs: tuple[Identifier, ...] = ()
    contradicting_event_refs: tuple[Identifier, ...] = ()
    revision: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def has_evidence(self) -> EntityBelief:
        if not self.supporting_event_refs and not self.contradicting_event_refs:
            raise ValueError("entity prediction requires objective evidence")
        combined = self.supporting_event_refs + self.contradicting_event_refs
        if len(combined) != len(set(combined)):
            raise ValueError("support and counterevidence must be unique and disjoint")
        return self


class EntityInteractionArguments(FrozenModel):
    actor_ref: Identifier
    operation: Operation
    task_ref: Identifier | None = None
    message_ref: Identifier | None = None
    evidence_refs: tuple[NonBlank, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def json_sequences(cls, value: Any) -> Any:
        if isinstance(value, dict) and isinstance(value.get("evidence_refs"), list):
            if any(not isinstance(item, str) for item in value["evidence_refs"]):
                raise ValueError("interaction evidence references must be text")
            value = {**value, "evidence_refs": tuple(value["evidence_refs"])}
        return value

    @model_validator(mode="after")
    def concrete_operation(self) -> EntityInteractionArguments:
        if self.operation == "assist" and not self.task_ref:
            raise ValueError("assistance requires a concrete task")
        if self.operation in {"request", "respond", "refuse", "commit", "renegotiate"} and not (self.task_ref or self.message_ref):
            raise ValueError("interaction requires a concrete task or message reference")
        return self
