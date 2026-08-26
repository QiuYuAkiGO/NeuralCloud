"""Typed state and configuration for the deterministic homeostasis engine."""

from __future__ import annotations

import math
from datetime import datetime
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

DimensionName = Literal[
    "energy_reserve",
    "cognitive_load",
    "safety_predictability",
    "competence",
    "autonomy",
    "social_connection",
    "novelty",
]

DIMENSIONS: tuple[DimensionName, ...] = (
    "energy_reserve",
    "cognitive_load",
    "safety_predictability",
    "competence",
    "autonomy",
    "social_connection",
    "novelty",
)


class DimensionProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    baseline_band: tuple[float, float]
    hard_bounds: tuple[float, float] = (0.0, 1.0)
    drift_per_hour: float = Field(default=0.02, ge=0.0, le=1.0)
    lower_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    upper_weight: float = Field(default=1.0, ge=0.0, le=10.0)
    max_event_delta: float = Field(default=0.12, gt=0.0, le=1.0)

    @model_validator(mode="after")
    def _validate_bands(self) -> DimensionProfile:
        low, high = self.baseline_band
        hard_low, hard_high = self.hard_bounds
        if not (0.0 <= hard_low < hard_high <= 1.0):
            raise ValueError("hard_bounds must be ordered within [0, 1]")
        if not (hard_low <= low < high <= hard_high):
            raise ValueError("baseline_band must be ordered within hard_bounds")
        return self


class HomeostaticProfile(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["homeostasis-profile-v1"] = "homeostasis-profile-v1"
    profile_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(default=1, ge=1)
    mode: Literal["functional_proxy", "virtual_physiology"] = "functional_proxy"
    persona_version: str = Field(default="persona_v1", min_length=1)
    dimensions: dict[DimensionName, DimensionProfile]
    observation_model_version: str = "A-v1"
    transition_model_version: str = "B-v1"
    preference_model_version: str = "C-v1"
    precision_profile_version: str = "precision-v1"
    coupling_matrix_version: str = "coupling-v1"
    observation_model: dict[str, dict[DimensionName, float]] = Field(default_factory=dict)
    transition_model: dict[str, dict[DimensionName, float]] = Field(default_factory=dict)
    preferences: dict[DimensionName, float] = Field(default_factory=dict)
    source_precision: dict[str, float] = Field(default_factory=lambda: {
        "system_sensor": 0.95,
        "verified_outcome": 0.90,
        "user_statement": 0.75,
        "behavioral_cue": 0.55,
        "llm_appraisal": 0.35,
        "runtime": 0.80,
    })
    coupling: dict[DimensionName, dict[DimensionName, float]] = Field(default_factory=dict)
    affect_weights: dict[str, dict[DimensionName, float]] = Field(default_factory=dict)
    emotion_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    emotion_minimum_hold_seconds: int = Field(default=180, ge=0, le=86400)
    max_heartbeat_step_seconds: int = Field(default=90, ge=1, le=3600)

    @field_validator("source_precision")
    @classmethod
    def _validate_precision(cls, value: dict[str, float]) -> dict[str, float]:
        if not value:
            raise ValueError("source_precision cannot be empty")
        if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in value.values()):
            raise ValueError("source precision values must be finite within [0, 1]")
        return value

    @model_validator(mode="after")
    def _validate_dimensions_and_matrices(self) -> HomeostaticProfile:
        if set(self.dimensions) != set(DIMENSIONS):
            raise ValueError(f"dimensions must contain exactly: {', '.join(DIMENSIONS)}")
        for table_name, table in (
            ("observation_model", self.observation_model),
            ("transition_model", self.transition_model),
        ):
            for row, impacts in table.items():
                if any(key not in DIMENSIONS for key in impacts):
                    raise ValueError(f"{table_name}.{row} contains an unknown dimension")
                if any(not math.isfinite(float(item)) for item in impacts.values()):
                    raise ValueError(f"{table_name}.{row} contains a non-finite value")
        for source, impacts in self.coupling.items():
            if source not in DIMENSIONS or any(key not in DIMENSIONS for key in impacts):
                raise ValueError("coupling contains an unknown dimension")
            if any(not math.isfinite(float(item)) or abs(float(item)) > 1.0 for item in impacts.values()):
                raise ValueError("coupling coefficients must be finite within [-1, 1]")
        return self

    @property
    def profile_ref(self) -> str:
        return f"{self.profile_id}@{self.revision}"


class FactorBelief(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: DimensionName
    value: float = Field(ge=0.0, le=1.0)
    velocity: float = Field(default=0.0, ge=-1.0, le=1.0)
    posterior: dict[Literal["low", "in_band", "high"], float]
    target_band: tuple[float, float]
    precision: float = Field(default=0.5, ge=0.0, le=1.0)
    dominant_evidence: list[str] = Field(default_factory=list)
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_posterior(self) -> FactorBelief:
        if set(self.posterior) != {"low", "in_band", "high"}:
            raise ValueError("posterior must contain low/in_band/high")
        if any(not math.isfinite(item) or item < 0.0 or item > 1.0 for item in self.posterior.values()):
            raise ValueError("posterior values must be finite within [0, 1]")
        if abs(sum(self.posterior.values()) - 1.0) > 1e-6:
            raise ValueError("posterior must sum to 1")
        low, high = self.target_band
        if not 0.0 <= low < high <= 1.0:
            raise ValueError("target_band must be ordered within [0, 1]")
        return self


class CoreAffect(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    valence: float = Field(ge=-1.0, le=1.0)
    arousal: float = Field(ge=0.0, le=1.0)
    dominance: float = Field(ge=0.0, le=1.0)
    confidence: float = Field(ge=0.0, le=1.0)


class DerivedEmotion(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str
    intensity: float = Field(ge=0.0, le=1.0)
    entered_at: AwareDatetime
    minimum_hold_seconds: int = Field(ge=0)
    supporting_factors: list[str] = Field(default_factory=list)


class EventAppraisal(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = "runtime"
    precision: float = Field(default=0.8, ge=0.0, le=1.0)
    suggested_impacts: dict[DimensionName, float] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)


class ExpectedFreeEnergyBreakdown(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    pragmatic_value: float
    ambiguity: float
    risk: float
    information_gain: float
    total: float


class PolicyCandidate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_id: str
    label: str
    source_drive: DimensionName
    expected_impacts: dict[DimensionName, float]
    reversible: bool = True
    success_criteria: list[str] = Field(default_factory=list)
    efe: ExpectedFreeEnergyBreakdown
    rank: int = Field(ge=1)


class HomeostaticSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["homeostasis-state-v1"] = "homeostasis-state-v1"
    revision: int = Field(default=0, ge=0)
    profile_id: str
    profile_revision: int = Field(ge=1)
    dimensions: dict[DimensionName, FactorBelief]
    core_affect: CoreAffect
    emotion: DerivedEmotion
    candidates: list[PolicyCandidate] = Field(default_factory=list)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    acknowledged_event_id: str | None = None
    last_heartbeat_at: AwareDatetime | None = None
    updated_at: AwareDatetime
    stable_snapshot_ref: str | None = None
    watchdog: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def _validate_dimensions(self) -> HomeostaticSnapshot:
        if set(self.dimensions) != set(DIMENSIONS):
            raise ValueError("snapshot must contain all seven dimensions")
        return self


class HomeostaticTransitionTrace(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=1)
    command_type: str
    event_id: str
    receive_seq: int = Field(ge=1)
    occurred_at: AwareDatetime
    source: str
    precision: float = Field(ge=0.0, le=1.0)
    suggested_impacts: dict[DimensionName, float]
    applied_impacts: dict[DimensionName, float]
    coupling_impacts: dict[DimensionName, float]
    watchdog: list[str] = Field(default_factory=list)


def initial_snapshot(profile: HomeostaticProfile, *, now: datetime) -> HomeostaticSnapshot:
    factors: dict[DimensionName, FactorBelief] = {}
    for dimension in DIMENSIONS:
        spec = profile.dimensions[dimension]
        value = sum(spec.baseline_band) / 2.0
        factors[dimension] = FactorBelief(
            dimension=dimension,
            value=value,
            posterior={"low": 0.1, "in_band": 0.8, "high": 0.1},
            target_band=spec.baseline_band,
            precision=profile.source_precision.get("runtime", 0.8),
            updated_at=now,
        )
    affect = CoreAffect(valence=0.2, arousal=0.3, dominance=0.6, confidence=0.8)
    emotion = DerivedEmotion(
        label="settled",
        intensity=0.2,
        entered_at=now,
        minimum_hold_seconds=profile.emotion_minimum_hold_seconds,
    )
    return HomeostaticSnapshot(
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        dimensions=factors,
        core_affect=affect,
        emotion=emotion,
        updated_at=now,
    )
