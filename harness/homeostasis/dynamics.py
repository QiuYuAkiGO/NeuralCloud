"""Attention-grounded, replayable cognitive dynamics.

The units and time constants are engineering defaults, not biological measures.
All functions operate on a detached state; the domain ledger owns the commit.
"""
from __future__ import annotations

import hashlib
import math
from datetime import datetime, timedelta
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


PSYCHOLOGICAL = ("safety_predictability", "competence", "autonomy", "social_connection")
RESPONSE_TAU = {"safety_predictability": 1800, "competence": 3600, "autonomy": 1800, "social_connection": 7200}


class DynamicsConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    mode: Literal["off", "shadow", "active"] = "off"
    work_capacity: float = Field(default=4096, gt=0)
    work_window_seconds: int = Field(default=60, ge=1)
    load_rise_seconds: float = Field(default=30, gt=0)
    load_recovery_seconds: float = Field(default=180, gt=0)
    fatigue_work_seconds: float = Field(default=2700, gt=0)
    fatigue_recovery_seconds: float = Field(default=1200, gt=0)
    novelty_seconds: float = Field(default=600, gt=0)
    boredom_seconds: float = Field(default=600, gt=0)
    habituation_seconds: float = Field(default=21600, gt=0)
    appraisal_hourly_budget: float = Field(default=0.15, gt=0, le=1)
    baseline_daily_budget: float = Field(default=0.02, gt=0, le=0.1)
    sensor_stale_seconds: int = Field(default=300, ge=1)


class AttentionAssessment(BaseModel):
    """Optional model interpretation, never an assertion of observed work."""
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    topic: str = Field(min_length=1, max_length=128)
    unexpectedness: float = Field(ge=0, le=1)
    meaning: float = Field(ge=0, le=1)
    wants_engagement: bool = True
    confidence: float = Field(default=0.5, ge=0, le=1)
    impacts: dict[str, float] = Field(default_factory=dict, max_length=4)

    @model_validator(mode="after")
    def valid_impacts(self):
        if set(self.impacts) - set(PSYCHOLOGICAL) or any(not -1 <= v <= 1 for v in self.impacts.values()):
            raise ValueError("invalid psychological interpretation")
        return self


class CognitiveExposure(BaseModel):
    """Runtime-bound processing receipt. No dialogue body is persisted here."""
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)
    schema_version: Literal["cognitive-exposure-v1"] = "cognitive-exposure-v1"
    exposure_id: str = Field(min_length=1, max_length=256)
    context_id: str = Field(min_length=1, max_length=256)
    attended_at: AwareDatetime
    input_tokens: int = Field(default=0, ge=0, le=2_000_000)
    output_tokens: int = Field(default=0, ge=0, le=2_000_000)
    input_count_basis: Literal["exact", "text_units_estimate"] = "exact"
    output_count_basis: Literal["api", "estimate", "unavailable"] = "api"
    images: int = Field(default=0, ge=0, le=1000)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    evidence_refs: list[str] = Field(default_factory=list, max_length=128)
    assessment: AttentionAssessment | None = None
    attention_revision: int | None = Field(default=None, ge=0)

    @property
    def receipt_hash(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


class WorkSample(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    at: AwareDatetime
    units: float = Field(ge=0)
    novelty: float | None = Field(default=None, ge=0, le=1)
    meaning: float | None = Field(default=None, ge=0, le=1)
    wants_engagement: bool = False
    cause: str | None = None


class Habit(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    value: float = Field(ge=0, le=1)
    at: AwareDatetime


class AffectChange(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    at: AwareDatetime
    dimension: str
    short: float = Field(ge=0)
    baseline: float = Field(ge=0)


class DynamicsState(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    schema_version: Literal["cognitive-dynamics-v1"] = "cognitive-dynamics-v1"
    started_at: AwareDatetime
    updated_at: AwareDatetime
    load: float = Field(ge=0, le=1)
    novelty: float = Field(ge=0, le=1)
    fatigue: float = Field(default=0, ge=0, le=1)
    boredom: float = Field(default=0, ge=0, le=1)
    wants_engagement: bool = False
    intent_expiry_enabled: bool = False
    engagement_until: AwareDatetime | None = None
    engagement_at: AwareDatetime | None = None
    engagement_cause: str | None = None
    native_resting: bool = False
    native_appraisals: int = 0
    native_effects: dict[str, dict] = Field(default_factory=dict)
    meaning_target: float = Field(default=1, ge=0, le=1)
    latent_confidence: float = Field(default=0, ge=0, le=1)
    baselines: dict[str, float]
    responses: dict[str, float]
    work: list[WorkSample] = Field(default_factory=list)
    habits: dict[str, Habit] = Field(default_factory=dict)
    changes: list[AffectChange] = Field(default_factory=list)
    last_context: str | None = None
    high_load_since: AwareDatetime | None = None
    recovered_since: AwareDatetime | None = None
    exposures: int = 0
    assessed_exposures: int = 0
    missing_assessments: int = 0
    last_work_at: AwareDatetime | None = None
    work_units: int = Field(default=0, ge=0)
    habituation_fragments: dict[str, str] = Field(default_factory=dict)
    shadow_last_sample_at: AwareDatetime | None = None
    shadow_observed_seconds: float = Field(default=0, ge=0)
    shadow_samples: int = Field(default=0, ge=0)
    shadow_max_gap_seconds: float = Field(default=0, ge=0)

    @model_validator(mode="after")
    def valid_affect(self):
        # Nested dates must also round-trip through the durable engine JSON.
        for effect in self.native_effects.values():
            if isinstance(effect.get('at'), str):
                effect['at'] = datetime.fromisoformat(effect['at'])
        if set(self.baselines) != set(PSYCHOLOGICAL) or set(self.responses) != set(PSYCHOLOGICAL):
            raise ValueError("cognitive dynamics requires four psychological baselines")
        if any(not math.isfinite(v) or not 0 <= v <= 1 for v in self.baselines.values()):
            raise ValueError("invalid psychological baseline")
        if any(not math.isfinite(v) or not -1 <= v <= 1 for v in self.responses.values()):
            raise ValueError("invalid psychological response")
        return self

    @classmethod
    def migrate(cls, values, preferences, at):
        # The old seven values do not identify a baseline/transient split.
        # Preserve them as low-confidence baselines instead of inventing a
        # conflict or a future relationship reward from persona preferences.
        baselines = {k: values[k] for k in PSYCHOLOGICAL}
        return cls(started_at=at, updated_at=at, load=values["cognitive_load"], novelty=values["novelty"],
                   baselines=baselines, responses={k: values[k]-baselines[k] for k in PSYCHOLOGICAL},
                   high_load_since=at if values["cognitive_load"] > .75 else None,
                   recovered_since=at if values["cognitive_load"] < .45 else None)

    def values(self):
        return {"cognitive_load": self.load, "novelty": self.novelty,
                **{k: clamp(self.baselines[k]+self.responses[k]) for k in PSYCHOLOGICAL}}

    def pressure(self):
        sustained = self.high_load_since is not None and (self.updated_at-self.high_load_since).total_seconds() >= 120
        overloaded = sustained or self.fatigue > .70
        return {"level": "overloaded" if overloaded else "elevated" if self.load > .60 or self.fatigue > .50 else "normal",
                "suggest_reduce_input": overloaded,
                "suggest_reopen": self.recovered_since is not None and (self.updated_at-self.recovered_since).total_seconds() >= 300}

    def view(self, mode, *, habituation_seconds=21600):
        return {"mode": mode, "fatigue": self.fatigue, "boredom": self.boredom,
                "wants_engagement": self.wants_engagement,
                "latent_confidence": self.latent_confidence, "baselines": dict(self.baselines),
                "short_term_responses": dict(self.responses), "regulation_pressure": self.pressure(),
                "habituation": {k: h.value * math.exp(-max(0, (self.updated_at-h.at).total_seconds())/habituation_seconds)
                                for k, h in self.habits.items()},
                "assessment_coverage": {"exposures": self.exposures, "assessed": self.assessed_exposures,
                                        "native_event_appraisals": self.native_appraisals,
                                        "unassessed": self.missing_assessments,
                                        "work_units": self.work_units,
                                        "last_work_at": self.last_work_at.isoformat() if self.last_work_at else None},
                "shadow_observation": {"continuous_seconds": self.shadow_observed_seconds,
                    "samples": self.shadow_samples, "maximum_gap_seconds": self.shadow_max_gap_seconds,
                    "last_sample_at": self.shadow_last_sample_at.isoformat() if self.shadow_last_sample_at else None},
                "shadow_values": self.values() if mode == "shadow" else None,
                "started_at": self.started_at.isoformat(), "updated_at": self.updated_at.isoformat()}


def clamp(value, low=0.0, high=1.0):
    return max(low, min(high, value))


def relax(value, target, seconds, tau):
    return target+(value-target)*math.exp(-seconds/tau)


def _idle_crossing(value, threshold, target, tau):
    return 0.0 if value < threshold else max(0, tau*math.log((value-target)/(threshold-target)))


def advance(state: DynamicsState, at: datetime, config: DynamicsConfig) -> DynamicsState:
    """Integrate real elapsed time; input expires even across long downtime.

    Active intervals use UTC-aligned <=1 second steps; the unforced tail has an
    analytic solution, so hours offline are neither truncated nor iterated.
    """
    state = state.model_copy(deep=True)
    if at <= state.updated_at:
        return state
    start = state.updated_at
    def expiry(sample):
        return sample.at+timedelta(seconds=config.work_window_seconds)
    while state.updated_at < at:
        now = state.updated_at
        if state.intent_expiry_enabled and (state.native_resting or state.engagement_until is None or now >= state.engagement_until):
            state.wants_engagement = False
        samples = [w for w in state.work if w.at <= now < expiry(w)]
        future = [point for w in state.work for point in (w.at, expiry(w)) if now < point < at]
        if state.intent_expiry_enabled and state.engagement_until is not None and now < state.engagement_until < at:
            future.append(state.engagement_until)
        end = min([at, *future])
        if not samples:
            seconds = (end-now).total_seconds()
            low_after = max(_idle_crossing(state.load, .45, .1, config.load_recovery_seconds),
                            _idle_crossing(state.fatigue, .5, 0, config.fatigue_recovery_seconds))
            if state.recovered_since is None and low_after < seconds:
                state.recovered_since = now+timedelta(seconds=low_after)
            state.load = relax(state.load, .1, seconds,
                               config.load_recovery_seconds if state.load >= .1 else config.load_rise_seconds)
            state.fatigue = relax(state.fatigue, 0, seconds, config.fatigue_recovery_seconds)
            state.novelty = relax(state.novelty, .5, seconds, config.novelty_seconds)
            # Quiet only becomes boring when an explicit desire to engage is
            # still present. Voluntary rest and unknown intent keep target zero.
            state.boredom = relax(state.boredom, 1 if state.wants_engagement else 0, seconds, config.boredom_seconds)
            state.updated_at = end
            if state.load <= .75:
                state.high_load_since = None
            continue
        # Align to a fixed clock grid rather than the caller's heartbeat cadence.
        grid = datetime.fromtimestamp(math.floor(now.timestamp())+1, tz=now.tzinfo)
        end = min(end, grid)
        seconds = (end-now).total_seconds()
        r = clamp(sum(w.units for w in samples)/config.work_capacity)
        a, b = r/config.fatigue_work_seconds, (1-r)/config.fatigue_recovery_seconds
        midpoint_fatigue = relax(state.fatigue, a/(a+b), seconds/2, 1/(a+b))
        target_load = .1+.85*clamp(r/(1-.4*midpoint_fatigue))
        previous_load, previous_fatigue = state.load, state.fatigue
        state.fatigue = relax(state.fatigue, a/(a+b), seconds, 1/(a+b))
        state.load = relax(state.load, target_load, seconds,
                           config.load_rise_seconds if target_load > state.load else config.load_recovery_seconds)
        assessed = [w for w in samples if w.novelty is not None]
        weight = sum(max(1, w.units) for w in assessed)
        novelty = sum(w.novelty*max(1, w.units) for w in assessed)/weight if weight else .5
        mismatch = clamp(max((.25-state.load)/.25, (state.load-.65)/.35))
        state.novelty = relax(state.novelty, novelty, seconds, config.novelty_seconds)
        state.boredom = relax(state.boredom, max(1-state.meaning_target, mismatch) if state.wants_engagement else 0, seconds, config.boredom_seconds)
        if state.load > .75:
            if state.high_load_since is None:
                state.high_load_since = now if previous_load > .75 else end
        else:
            state.high_load_since = None
        if state.load < .45 and state.fatigue < .50:
            if state.recovered_since is None:
                state.recovered_since = now if previous_load < .45 and previous_fatigue < .50 else end
        else:
            state.recovered_since = None
        state.updated_at = end
    elapsed = (at-start).total_seconds()
    if state.intent_expiry_enabled and (state.native_resting or state.engagement_until is None or at >= state.engagement_until):
        state.wants_engagement = False
    state.responses = {k: relax(v, 0, elapsed, RESPONSE_TAU[k]) for k, v in state.responses.items()}
    state.work = [w for w in state.work if expiry(w) > at]
    state.changes = [c for c in state.changes if (at-c.at).total_seconds() < 86400]
    return state


def expose(state: DynamicsState, receipt: CognitiveExposure, config: DynamicsConfig, *, repeated_content=False):
    state = advance(state, max(receipt.attended_at, state.updated_at), config)
    at = state.updated_at
    units = receipt.input_tokens+receipt.output_tokens+256*receipt.images
    units += 128 if state.last_context is not None and state.last_context != receipt.context_id else 0
    if units > 0:
        state.work_units += units
        state.last_work_at = max(receipt.attended_at, state.last_work_at) if state.last_work_at else receipt.attended_at
    state.last_context = receipt.context_id
    state.exposures += 1
    state.latent_confidence = min(.8, state.latent_confidence+.02)
    sample = WorkSample(at=receipt.attended_at, units=units)
    assessment = receipt.assessment
    if assessment is not None:
        state.assessed_exposures += 1
        key = hashlib.sha256(assessment.topic.strip().casefold().encode()).hexdigest()
        previous = state.habits.get(key)
        h = previous.value*math.exp(-max(0, (at-previous.at).total_seconds())/config.habituation_seconds) if previous else 0
        sample.novelty = 0 if repeated_content else .5+assessment.confidence*(assessment.unexpectedness*(.1+.9*(1-h))-.5)
        sample.meaning = 1-assessment.confidence*(1-assessment.meaning)
        sample.wants_engagement = assessment.wants_engagement
        if not state.intent_expiry_enabled:
            state.wants_engagement = assessment.wants_engagement
        elif state.engagement_at is None or receipt.attended_at >= state.engagement_at:
            state.engagement_until = receipt.attended_at + timedelta(seconds=1800)
            state.engagement_at = receipt.attended_at
            state.engagement_cause = receipt.exposure_id
            state.wants_engagement = assessment.wants_engagement and not state.native_resting and state.engagement_until > state.updated_at
        state.meaning_target = sample.meaning
        fragment = hashlib.sha256((key+"|"+"|".join(sorted(receipt.evidence_refs or [receipt.exposure_id]))).encode()).hexdigest()
        if fragment not in state.habituation_fragments:
            state.habits[key] = Habit(value=h+.25*(1-h), at=at)
            state.habituation_fragments[fragment] = at.isoformat()
            if len(state.habituation_fragments) > 4096:
                del state.habituation_fragments[next(iter(state.habituation_fragments))]
        if len(state.habits) > 2048:
            del state.habits[min(state.habits, key=lambda k: state.habits[k].at)]
    else:
        state.missing_assessments += 1
    state.work.append(sample)
    return state


def appraise(state: DynamicsState, at, impacts, confidence, config, *, eligible_dimensions, verified_outcome=False):
    """Per-cause eligibility is checked by the durable domain ledger, not IDs."""
    state = advance(state, max(at, state.updated_at), config)
    p = .35*clamp(confidence)
    for key in PSYCHOLOGICAL:
        if key not in impacts or key not in eligible_dimensions:
            continue
        v = clamp(impacts[key], -1, 1)
        hourly = sum(c.short for c in state.changes if c.dimension == key and (state.updated_at-c.at).total_seconds() < 3600)
        daily = sum(c.baseline for c in state.changes if c.dimension == key)
        short_limit = max(0, min(.05, config.appraisal_hourly_budget-hourly))
        short = clamp(.2*p*v, -short_limit, short_limit)
        base_limit = max(0, config.baseline_daily_budget-daily)
        base = clamp(.02*p*v, -base_limit, base_limit) if verified_outcome else 0
        previous = state.baselines[key]
        state.baselines[key] = clamp(previous+base)
        base = state.baselines[key]-previous
        old = state.responses[key]
        state.responses[key] = clamp(old+short, -state.baselines[key], 1-state.baselines[key])
        state.changes.append(AffectChange(at=state.updated_at, dimension=key,
                                         short=abs(state.responses[key]-old), baseline=abs(base)))
    return state
