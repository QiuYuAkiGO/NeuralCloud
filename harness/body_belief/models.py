"""Evidence-bound, revisable hypotheses over observations that the model saw."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from harness.contracts.experimental_decisions import ExperimentalDecision
from harness.contracts.research_profiles import ConsolidationProfile
from harness.research.grounded_agency.hashes import content_hash
from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.research.interoception.contracts import Channel, OpaqueInteroceptiveObservation

Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]
Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Refs = Annotated[tuple[Identifier, ...], Field(max_length=256)]
Transition = Annotated[str, Field(pattern=r"^M[0-8]->M[0-8]$")]


class FrozenModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", revalidate_instances="always", allow_inf_nan=False)


def context_signature(context_key: str, observation: OpaqueInteroceptiveObservation) -> str:
    return content_hash(("body-context-v1", context_key, observation.transform_ref))


def action_signature(decision: ExperimentalDecision) -> str:
    if decision.kind == "act":
        return content_hash(decision.intent)
    return content_hash(decision.model_dump(mode="json", exclude={"confidence"}))


class BodyExperienceEpisode(FrozenModel):
    experience_id: Identifier
    episode_id: Identifier
    decision_id: Identifier
    context_hash: Digest
    context_signature: Digest
    observation_before: OpaqueInteroceptiveObservation
    decision: ExperimentalDecision
    action_outcome_refs: Refs = ()
    observation_after: OpaqueInteroceptiveObservation | None = None
    world_change_refs: Refs = ()
    outcome_verified: bool = Field(default=True, strict=True)
    outcome_success: bool = Field(default=True, strict=True)
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _ordered(self):
        if len(set(self.action_outcome_refs)) != len(self.action_outcome_refs):
            raise ValueError("outcome references must be unique")
        if self.observation_after is not None:
            if self.observation_after.sample_sequence <= self.observation_before.sample_sequence:
                raise ValueError("after observation must follow the before observation")
            if self.observation_after.observed_at < self.completed_at:
                raise ValueError("after observation must not precede the committed outcome")
        return self

    @property
    def action_signature(self) -> str:
        return action_signature(self.decision)

    @property
    def eligible(self) -> bool:
        after = self.observation_after
        return bool(self.outcome_verified and self.action_outcome_refs and after is not None
                    and after.transform_ref == self.observation_before.transform_ref
                    and after.channel_id == self.observation_before.channel_id)

    @property
    def transition(self) -> str | None:
        return f"{self.observation_before.reading}->{self.observation_after.reading}" if self.eligible else None


class _Proposition(FrozenModel):
    proposition: str = Field(min_length=1, max_length=1_000, pattern=r"\S")
    context_signature: Digest
    action_signature: Digest
    predicted_channel: Channel
    predicted_transition: Transition
    confidence: float = Field(ge=0, le=1, strict=True)
    supporting_experience_refs: Refs = Field(min_length=1)
    contradicting_experience_refs: Refs = ()

    @field_validator("proposition")
    @classmethod
    def _neutral(cls, value):
        NeutralityScanner().assert_neutral([{"role": "system", "content": value}])
        return value

    @model_validator(mode="after")
    def _disjoint(self):
        support, contrary = self.supporting_experience_refs, self.contradicting_experience_refs
        if len(set(support)) != len(support) or len(set(contrary)) != len(contrary) or set(support) & set(contrary):
            raise ValueError("support and counterevidence must be unique and disjoint")
        return self


class BodyCausalBeliefCandidate(_Proposition):
    candidate_id: Identifier
    status: Literal["candidate", "consolidated", "dropped"] = "candidate"
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _time(self):
        if self.updated_at < self.created_at:
            raise ValueError("candidate update precedes creation")
        return self


class BodyCausalBelief(_Proposition):
    belief_id: Identifier
    stability: float = Field(ge=0, le=1, strict=True)
    formed_at: AwareDatetime
    last_revised_at: AwareDatetime
    revision: int = Field(ge=1, strict=True)
    status: Literal["consolidated", "weakened", "revised", "dropped"] = "consolidated"

    @model_validator(mode="after")
    def _time(self):
        if self.last_revised_at < self.formed_at:
            raise ValueError("belief revision precedes formation")
        if self.status == "dropped" and self.confidence != 0:
            raise ValueError("dropped beliefs must have zero confidence")
        return self


class SensorReliabilityBelief(FrozenModel):
    channel_id: Channel
    context_signature: Digest
    reliability: float = Field(ge=0, le=1, strict=True)
    calibration_count: int = Field(ge=1, strict=True)
    supporting_refs: Refs = ()
    contradicting_refs: Refs = ()
    revision: int = Field(ge=1, strict=True)

    @model_validator(mode="after")
    def _refs(self):
        support, contrary = set(self.supporting_refs), set(self.contradicting_refs)
        if support & contrary or len(support) != len(self.supporting_refs) or len(contrary) != len(self.contradicting_refs):
            raise ValueError("reliability evidence must be disjoint and unique")
        if self.calibration_count != len(support | contrary):
            raise ValueError("calibration count must equal distinct evidence count")
        if self.reliability != len(support) / self.calibration_count:
            raise ValueError("reliability must equal the observed supporting fraction")
        return self


class FrozenBeliefSnapshot(FrozenModel):
    schema_version: Literal["body-belief-snapshot-v1"] = "body-belief-snapshot-v1"
    consolidation_profile: ConsolidationProfile = Field(default_factory=ConsolidationProfile)
    experiences: tuple[BodyExperienceEpisode, ...] = ()
    candidates: tuple[BodyCausalBeliefCandidate, ...] = ()
    beliefs: tuple[BodyCausalBelief, ...] = ()
    reliabilities: tuple[SensorReliabilityBelief, ...] = ()

    def content_hash(self) -> str:
        return content_hash(self)


def validate_evidence(item: _Proposition, experiences: dict[str, BodyExperienceEpisode]) -> None:
    for contrary, refs in ((False, item.supporting_experience_refs), (True, item.contradicting_experience_refs)):
        for ref in refs:
            episode = experiences.get(ref)
            if episode is None or not episode.eligible:
                raise ValueError("belief evidence requires a verified outcome and later matching observation")
            if (episode.context_signature != item.context_signature or episode.action_signature != item.action_signature
                    or episode.observation_before.channel_id != item.predicted_channel):
                raise ValueError("belief evidence context/action/channel mismatch")
            matches = episode.transition == item.predicted_transition and episode.outcome_success
            if matches == contrary:
                raise ValueError("belief evidence does not support its declared relationship")
