"""Typed state and configuration for the deterministic homeostasis engine."""

from __future__ import annotations

import hashlib
import json
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

HomeostaticSalience = Literal["nominal", "noticeable", "marked", "critical"]
HomeostaticDeviationDirection = Literal["low", "high"]
HomeostaticTrend = Literal["worsening", "steady", "recovering"]

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


class QQSocialDriveProfile(BaseModel):
    """Opt-in functional needs, never a claim of subjective boredom."""

    model_config = ConfigDict(frozen=True, extra="forbid", allow_inf_nan=False)

    enabled: bool = False
    social_depletion_per_hour: float = Field(default=0.14, gt=0, le=0.3)
    novelty_depletion_per_hour: float = Field(default=0.12, gt=0, le=0.3)
    social_floor: float = Field(default=0.22, ge=0, le=1)
    novelty_floor: float = Field(default=0.16, ge=0, le=1)
    idle_grace_seconds: int = Field(default=600, ge=60, le=86400)
    interaction_social_recovery: float = Field(default=0.10, ge=0, le=0.12)
    interaction_novelty_recovery: float = Field(default=0.02, ge=0, le=0.1)
    message_novelty_recovery: float = Field(default=0.015, ge=0, le=0.03)
    experience_novelty_recovery: float = Field(default=0.08, ge=0, le=0.1)


class QQHomeostaticFact(BaseModel):
    """A trusted collector's evidence-bound fact, with no dialogue body.

    fact_id must be the stable observation/receipt identity, not a new delivery
    command ID. An interaction must link an actual earlier own-send receipt.
    Verification is an upstream assertion; the engine grants no action rights.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["qq-homeostatic-fact-v1"] = "qq-homeostatic-fact-v1"
    fact_id: str = Field(min_length=1, max_length=256)
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    kind: Literal["verified_message", "verified_interaction", "verified_novelty", "send_attempt", "no_response"]
    occurred_at: AwareDatetime
    verification: Literal["qq_ui_verified"]
    evidence_refs: list[str] = Field(min_length=1, max_length=16)
    related_attempt_id: str | None = Field(default=None, min_length=1, max_length=256)

    @model_validator(mode="after")
    def _validate_evidence(self) -> QQHomeostaticFact:
        if (not self.fact_id.strip()
                or (self.related_attempt_id is not None and not self.related_attempt_id.strip())
                or any(not item.strip() or len(item) > 2048 for item in self.evidence_refs)):
            raise ValueError("QQ fact identity and evidence must be nonblank and bounded")
        if self.kind == "verified_interaction" and self.related_attempt_id is None:
            raise ValueError("a verified interaction requires an actual related send receipt")
        return self

    @property
    def fact_key(self) -> str:
        return f"qq:{self.group_id}:{self.fact_id}"

    @property
    def fact_hash(self) -> str:
        body = self.model_dump(mode="json")
        body["evidence_refs"] = sorted(set(self.evidence_refs))
        return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


class QQDriveContext(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    resting: bool = False


class QQFactLookup(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)
    group_id: str = Field(pattern=r"^[1-9][0-9]{0,19}$")
    fact_id: str = Field(min_length=1, max_length=256)

    @property
    def fact_key(self) -> str:
        return f"qq:{self.group_id}:{self.fact_id}"


class QQFactStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal["qq-homeostatic-fact-status-v1"] = "qq-homeostatic-fact-status-v1"
    service: Literal["homeostasis"] = "homeostasis"
    fact_key: str
    status: Literal["unknown", "applied"]
    fact_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    committed_revision: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def complete_commit(self):
        if (self.fact_hash is not None or self.committed_revision is not None) != (self.status == "applied"):
            raise ValueError("Unknown facts cannot claim a commit")
        if self.status == "applied" and (self.fact_hash is None or self.committed_revision is None):
            raise ValueError("Applied facts require their original hash and revision")
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
    coupling_semantics: Literal["requested_delta_v1", "realized_delta_v2"] = (
        "requested_delta_v1"
    )
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
    watchdog_boundary_heartbeats: int | None = Field(default=None, ge=2, le=60)
    interoception_profile_version: str = "interoception-v1"
    salience_noticeable_enter: float = Field(default=0.25, ge=0.0, le=1.0)
    salience_marked_enter: float = Field(default=0.50, ge=0.0, le=1.0)
    salience_noticeable_exit: float = Field(default=0.15, ge=0.0, le=1.0)
    salience_marked_exit: float = Field(default=0.35, ge=0.0, le=1.0)
    qq_social_drive: QQSocialDriveProfile | None = None

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
        known_semantics = {
            "coupling-v1": "requested_delta_v1",
            "coupling-v2-realized-delta": "realized_delta_v2",
        }
        expected_semantics = known_semantics.get(self.coupling_matrix_version)
        if expected_semantics is not None and self.coupling_semantics != expected_semantics:
            raise ValueError(
                f"{self.coupling_matrix_version} requires coupling_semantics={expected_semantics}"
            )
        if not (
            self.salience_noticeable_exit
            < self.salience_noticeable_enter
            < self.salience_marked_enter
        ):
            raise ValueError(
                "salience thresholds must satisfy noticeable_exit < "
                "noticeable_enter < marked_enter"
            )
        if not (
            self.salience_noticeable_exit
            < self.salience_marked_exit
            < self.salience_marked_enter
        ):
            raise ValueError(
                "salience thresholds must satisfy noticeable_exit < "
                "marked_exit < marked_enter"
            )
        if self.qq_social_drive is not None and self.qq_social_drive.enabled:
            drive = self.qq_social_drive
            for dimension, floor, rate in (
                ("social_connection", drive.social_floor, drive.social_depletion_per_hour),
                ("novelty", drive.novelty_floor, drive.novelty_depletion_per_hour),
            ):
                spec = self.dimensions[dimension]
                if not spec.hard_bounds[0] < floor < spec.baseline_band[0]:
                    raise ValueError("QQ idle floors must stay above hard bounds and below baseline")
                if rate <= spec.drift_per_hour:
                    raise ValueError("QQ idle depletion must exceed baseline recovery")
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


class HomeostaticObservation(BaseModel):
    """Absolute body observation; it is intentionally not an event impact."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: Literal["system_sensor"] = "system_sensor"
    observed_values: dict[DimensionName, float]
    body_instance_id: str
    episode_id: str
    body_revision: int = Field(ge=1)
    captured_at: AwareDatetime
    evidence_refs: list[str] = Field(min_length=1)
    experiment_mode: Literal["grounded"] = "grounded"

    @field_validator("observed_values")
    @classmethod
    def _validate_observed_values(
        cls, value: dict[DimensionName, float]
    ) -> dict[DimensionName, float]:
        if set(value) != {"energy_reserve"}:
            raise ValueError("body-state-v1 initially observes only energy_reserve")
        if any(not math.isfinite(item) or not 0 <= item <= 1 for item in value.values()):
            raise ValueError("observed values must be finite within [0, 1]")
        return value


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


class HomeostaticDominantNeed(BaseModel):
    """One model-safe semantic need derived from a homeostatic factor."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    dimension: DimensionName
    direction: HomeostaticDeviationDirection
    normalized_deviation: float = Field(ge=0.0, le=1.0)
    trend: HomeostaticTrend
    confidence: float = Field(ge=0.0, le=1.0)
    semantic_need: str = Field(min_length=1, max_length=160)

    @model_validator(mode="after")
    def _validate_canonical_semantics(self) -> HomeostaticDominantNeed:
        expected = _SEMANTIC_NEEDS.get((self.dimension, self.direction))
        if expected is None or self.semantic_need != expected:
            raise ValueError(
                "semantic_need must be the canonical text for dimension and direction"
            )
        return self


class HomeostaticEmotionHint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    label: str = Field(min_length=1, max_length=64)
    intensity: float = Field(ge=0.0, le=1.0)


class HomeostaticInteroceptiveSummary(BaseModel):
    """Versioned, model-safe projection of the authoritative factor state."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["homeostatic-interoception-v1"] = (
        "homeostatic-interoception-v1"
    )
    profile_ref: str = Field(min_length=1, max_length=160)
    snapshot_revision: int = Field(ge=0)
    salience: HomeostaticSalience
    dominant_needs: list[HomeostaticDominantNeed] = Field(
        default_factory=list,
        max_length=3,
    )
    emotion_hint: HomeostaticEmotionHint | None = None
    policy_refs: list[str] = Field(default_factory=list, max_length=3)
    episode_signature: str = Field(min_length=1, max_length=96)


class HomeostaticSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["homeostasis-state-v2"] = "homeostasis-state-v2"
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
    interoceptive_summary: HomeostaticInteroceptiveSummary | None = None

    @model_validator(mode="before")
    @classmethod
    def _migrate_v1_payload(cls, value: object) -> object:
        """Accept persisted v1 snapshots; the engine supplies their derived summary."""

        if isinstance(value, dict) and value.get("schema_version") == "homeostasis-state-v1":
            migrated = dict(value)
            migrated["schema_version"] = "homeostasis-state-v2"
            migrated.setdefault("interoceptive_summary", None)
            return migrated
        return value

    @model_validator(mode="after")
    def _validate_dimensions(self) -> HomeostaticSnapshot:
        if set(self.dimensions) != set(DIMENSIONS):
            raise ValueError("snapshot must contain all seven dimensions")
        if self.interoceptive_summary is not None:
            if self.interoceptive_summary.snapshot_revision != self.revision:
                raise ValueError("interoceptive summary revision must match snapshot revision")
            expected_ref = f"{self.profile_id}@{self.profile_revision}"
            if self.interoceptive_summary.profile_ref != expected_ref:
                raise ValueError("interoceptive summary profile_ref must match snapshot")
            if bool(self.watchdog) != (
                self.interoceptive_summary.salience == "critical"
            ):
                raise ValueError(
                    "snapshot watchdog presence must match critical summary salience"
                )
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
    observed_values: dict[DimensionName, float] = Field(default_factory=dict)
    assimilation_gain: float | None = Field(default=None, ge=0.0, le=1.0)
    values_before: dict[DimensionName, float] = Field(default_factory=dict)
    values_after: dict[DimensionName, float] = Field(default_factory=dict)
    body_instance_id: str | None = None
    episode_id: str | None = None
    body_revision: int | None = Field(default=None, ge=1)
    evidence_refs: list[str] = Field(default_factory=list)
    experiment_mode: str | None = None
    salience: HomeostaticSalience | None = None
    dominant_needs: list[DimensionName] = Field(default_factory=list)
    affect_valence: float | None = Field(default=None, ge=-1.0, le=1.0)
    affect_arousal: float | None = Field(default=None, ge=0.0, le=1.0)
    affect_dominance: float | None = Field(default=None, ge=0.0, le=1.0)


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
    snapshot = HomeostaticSnapshot(
        profile_id=profile.profile_id,
        profile_revision=profile.revision,
        dimensions=factors,
        core_affect=affect,
        emotion=emotion,
        updated_at=now,
    )
    return snapshot.model_copy(
        update={
            "interoceptive_summary": build_interoceptive_summary(
                profile,
                revision=snapshot.revision,
                factors=factors,
                affect=affect,
                emotion=emotion,
                candidates=[],
                watchdog=[],
            )
        }
    )


_SEMANTIC_NEEDS: dict[
    tuple[DimensionName, HomeostaticDeviationDirection], str
] = {
    ("energy_reserve", "low"): "需要降低消耗并恢复可用精力",
    ("energy_reserve", "high"): "有余力承担更充分的投入",
    ("cognitive_load", "low"): "需要适量参与以保持专注",
    ("cognitive_load", "high"): "需要降低负荷并简化当前任务",
    ("safety_predictability", "low"): "需要更多确定性、澄清与安全边界",
    ("safety_predictability", "high"): "可以容纳少量不确定性和变化",
    ("competence", "low"): "需要一个可验证的小进展来恢复把握",
    ("competence", "high"): "需要更有挑战、能发挥能力的目标",
    ("autonomy", "low"): "需要保留选择权并重新协商边界",
    ("autonomy", "high"): "可以主动承担更多选择和责任",
    ("social_connection", "low"): "需要低压力的连接与被理解",
    ("social_connection", "high"): "需要留出独处空间并降低社交密度",
    ("novelty", "low"): "需要一点轻量的新鲜感或探索",
    ("novelty", "high"): "需要减少新刺激并回到熟悉节奏",
}


def normalized_homeostatic_deviation(
    profile: HomeostaticProfile,
    factor: FactorBelief,
) -> tuple[HomeostaticDeviationDirection | None, float]:
    """Scale target-band departure to the relevant hard bound and clamp to one."""

    low, high = factor.target_band
    hard_low, hard_high = profile.dimensions[factor.dimension].hard_bounds
    if factor.value < low:
        span = low - hard_low
        return "low", _bounded_ratio(low - factor.value, span)
    if factor.value > high:
        span = hard_high - high
        return "high", _bounded_ratio(factor.value - high, span)
    return None, 0.0


def derive_homeostatic_salience(
    profile: HomeostaticProfile,
    factors: dict[DimensionName, FactorBelief],
    *,
    previous: HomeostaticSalience | None = None,
    watchdog: list[str] | None = None,
) -> HomeostaticSalience:
    """Derive a hysteretic salience tier independently of emotion labeling."""

    if watchdog:
        return "critical"
    deviations = [normalized_homeostatic_deviation(profile, item)[1] for item in factors.values()]
    noticeable_enter_count = sum(
        _meets_threshold(value, profile.salience_noticeable_enter) for value in deviations
    )
    if (
        any(_meets_threshold(value, profile.salience_marked_enter) for value in deviations)
        or noticeable_enter_count >= 2
    ):
        return "marked"
    if previous in {"marked", "critical"}:
        if (
            any(_meets_threshold(value, profile.salience_marked_exit) for value in deviations)
            or sum(
                _meets_threshold(value, profile.salience_noticeable_exit)
                for value in deviations
            )
            >= 2
        ):
            return "marked"
        if any(
            _meets_threshold(value, profile.salience_noticeable_exit)
            for value in deviations
        ):
            return "noticeable"
        return "nominal"
    if previous == "noticeable":
        if any(
            _meets_threshold(value, profile.salience_noticeable_exit)
            for value in deviations
        ):
            return "noticeable"
        return "nominal"
    if noticeable_enter_count:
        return "noticeable"
    return "nominal"


def build_interoceptive_summary(
    profile: HomeostaticProfile,
    *,
    revision: int,
    factors: dict[DimensionName, FactorBelief],
    affect: CoreAffect,
    emotion: DerivedEmotion,
    candidates: list[PolicyCandidate],
    watchdog: list[str],
    previous_salience: HomeostaticSalience | None = None,
    salience: HomeostaticSalience | None = None,
) -> HomeostaticInteroceptiveSummary:
    """Build the deterministic semantic projection consumed by response systems."""

    resolved_salience = salience or derive_homeostatic_salience(
        profile,
        factors,
        previous=previous_salience,
        watchdog=watchdog,
    )
    if bool(watchdog) != (resolved_salience == "critical"):
        raise ValueError(
            "watchdog presence must match critical interoceptive salience"
        )
    ranked: list[
        tuple[float, int, DimensionName, HomeostaticDeviationDirection, FactorBelief]
    ] = []
    for order, dimension in enumerate(DIMENSIONS):
        factor = factors[dimension]
        direction, deviation = normalized_homeostatic_deviation(profile, factor)
        if direction is not None and _meets_threshold(
            deviation,
            profile.salience_noticeable_exit,
        ):
            ranked.append((deviation, order, dimension, direction, factor))
    ranked.sort(key=lambda item: (-item[0], item[1]))
    needs: list[HomeostaticDominantNeed] = []
    for deviation, _order, dimension, direction, factor in ranked[:3]:
        if abs(factor.velocity) <= 1e-9:
            trend: HomeostaticTrend = "steady"
        else:
            moving_lower = factor.velocity < 0.0
            worsening = moving_lower if direction == "low" else not moving_lower
            trend = "worsening" if worsening else "recovering"
        needs.append(
            HomeostaticDominantNeed(
                dimension=dimension,
                direction=direction,
                normalized_deviation=round(deviation, 6),
                trend=trend,
                confidence=factor.precision,
                semantic_need=_SEMANTIC_NEEDS[(dimension, direction)],
            )
        )
    need_dimensions = {item.dimension for item in needs}
    policy_refs = [
        item.policy_id for item in candidates if item.source_drive in need_dimensions
    ][:3]
    signature_payload = {
        "profile_ref": profile.profile_ref,
        "salience": resolved_salience,
        "needs": sorted([item.dimension, item.direction] for item in needs),
        "watchdog": sorted(watchdog),
    }
    digest = hashlib.sha256(
        json.dumps(signature_payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()[:20]
    emotion_label = emotion.label
    if resolved_salience in {"marked", "critical"} and emotion_label == "settled":
        emotion_label = "strained" if affect.arousal > 0.55 else "attentive"
    return HomeostaticInteroceptiveSummary(
        profile_ref=profile.profile_ref,
        snapshot_revision=revision,
        salience=resolved_salience,
        dominant_needs=needs,
        emotion_hint=HomeostaticEmotionHint(
            label=emotion_label,
            intensity=emotion.intensity,
        ),
        policy_refs=policy_refs,
        episode_signature=f"homeostasis:{digest}",
    )


def _bounded_ratio(numerator: float, denominator: float) -> float:
    if denominator <= 1e-12:
        return 1.0 if numerator > 0.0 else 0.0
    return max(0.0, min(1.0, numerator / denominator))


def _meets_threshold(value: float, threshold: float) -> bool:
    """Treat decimal profile thresholds as inclusive despite float round-off."""

    return value >= threshold - 1e-9
