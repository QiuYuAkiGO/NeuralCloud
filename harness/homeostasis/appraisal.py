"""Evidence-bound psychological suggestions, separate from measured body state."""

from __future__ import annotations

import hashlib
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

PsychologicalDimension = Literal[
    "cognitive_load",
    "safety_predictability",
    "competence",
    "autonomy",
    "social_connection",
    "novelty",
]
AppraisalKind = Literal["event_interpretation", "self_report"]
Identity = Annotated[str, Field(strict=True, min_length=1, max_length=256)]
EvidenceRef = Annotated[str, Field(strict=True, min_length=1, max_length=2048)]
SuggestedImpact = Annotated[float, Field(strict=True, ge=-1.0, le=1.0)]


class PsychologicalAppraisal(BaseModel):
    """A model suggestion; it cannot choose its source, precision or energy value.

    Runtime binds the episode, snapshot revision and times after inference. The
    model's confidence only attenuates the server-owned appraisal precision.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    schema_version: Literal["psychological-appraisal-v1"] = "psychological-appraisal-v1"
    episode_id: Identity
    appraisal_id: Identity
    basis_snapshot_revision: int = Field(strict=True, ge=0)
    observed_event_refs: list[EvidenceRef] = Field(min_length=1, max_length=32, strict=True)
    appraisal_kind: AppraisalKind
    suggested_impacts: dict[PsychologicalDimension, SuggestedImpact] = Field(min_length=1, max_length=6)
    model_confidence: float = Field(strict=True, ge=0.0, le=1.0)
    created_at: AwareDatetime
    expires_at: AwareDatetime

    @field_validator("episode_id", "appraisal_id")
    @classmethod
    def validate_identity(cls, value: str) -> str:
        if not value.strip() or value != value.strip():
            raise ValueError("appraisal identities must be nonblank without surrounding whitespace")
        return value

    @field_validator("observed_event_refs")
    @classmethod
    def validate_refs(cls, values: list[str]) -> list[str]:
        if any(not value.strip() or value != value.strip() for value in values):
            raise ValueError("evidence references must be nonblank without surrounding whitespace")
        if len(set(values)) != len(values):
            raise ValueError("evidence references must be unique")
        return values

    @model_validator(mode="after")
    def validate_times(self) -> PsychologicalAppraisal:
        if self.expires_at <= self.created_at:
            raise ValueError("appraisal expires_at must be after created_at")
        return self

    @property
    def evidence_set_hash(self) -> str:
        """Ignore appraisal ID, interpretation kind and reference order."""
        canonical = json.dumps(sorted(self.observed_event_refs), ensure_ascii=False, separators=(",", ":"))
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class ApplyPsychologicalAppraisal(BaseModel):
    """Trusted Runtime command payload; never accepted as model output.

    The enclosing authenticated Runtime channel owns episode_id and the verified
    reference allowlist. Keeping these outside the model schema prevents its
    output from self-certifying evidence or selecting another active episode.
    """

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    episode_id: Identity
    appraisal: PsychologicalAppraisal
    verified_evidence_refs: list[EvidenceRef] = Field(min_length=1, max_length=128, strict=True)

    @model_validator(mode="after")
    def validate_runtime_binding(self) -> ApplyPsychologicalAppraisal:
        if self.episode_id != self.appraisal.episode_id:
            raise ValueError("appraisal episode does not match the Runtime episode")
        if not set(self.appraisal.observed_event_refs) <= set(self.verified_evidence_refs):
            raise ValueError("appraisal references evidence not verified by Runtime")
        return self
