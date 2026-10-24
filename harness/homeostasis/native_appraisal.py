"""Validated interpretations, separate from physical cognitive work receipts."""
from __future__ import annotations

from datetime import timedelta
import hashlib
import math
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.homeostasis.dynamics import (
    AttentionAssessment, Habit, PSYCHOLOGICAL, RESPONSE_TAU, WorkSample,
    advance, appraise, clamp,
)


class NativeAppraisalReceipt(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)
    receipt_id: str = Field(min_length=1, max_length=256)
    cause_id: str = Field(min_length=1, max_length=256)
    kind: Literal['appraise', 'retract', 'control'] = 'appraise'
    occurred_at: AwareDatetime
    source_refs: list[str] = Field(default_factory=list, max_length=128)
    assessment: AttentionAssessment | None = None
    engagement: Literal['engage', 'rest', 'undecided'] = 'undecided'
    intent_until: AwareDatetime | None = None
    resting: bool | None = None

    @model_validator(mode='after')
    def receipt_kind(self):
        if self.kind == 'appraise' and (self.assessment is None or not self.source_refs):
            raise ValueError('native_appraisal_requires_evidence')
        if self.kind != 'control' and self.resting is not None:
            raise ValueError('native_body_control_not_allowed')
        return self

    @property
    def receipt_hash(self):
        return hashlib.sha256(self.model_dump_json().encode()).hexdigest()


def apply_native(state, receipt, at, config, *, eligible_dimensions):
    """No energy, work units, exposure counts, or recovery rates are writable."""
    state = advance(state, max(at, state.updated_at), config)
    if not state.intent_expiry_enabled:
        state.intent_expiry_enabled = True
        state.wants_engagement = False
        state.engagement_until = None
    if receipt.kind == 'control':
        if receipt.resting is not None:
            state.native_resting = receipt.resting
        if state.native_resting:
            state.wants_engagement = False
            state.engagement_until = None
        return state
    if receipt.kind == 'retract':
        contribution = state.native_effects.pop(receipt.cause_id, None)
        if contribution:
            elapsed = max(0, (state.updated_at - contribution['at']).total_seconds())
            for key, delta in contribution['deltas'].items():
                state.responses[key] = clamp(state.responses[key] - delta * math.exp(-elapsed / RESPONSE_TAU[key]),
                    -state.baselines[key], 1 - state.baselines[key])
            state.work = [w for w in state.work if w.cause != receipt.cause_id]
        if state.engagement_cause == receipt.cause_id:
            state.wants_engagement = False
            state.engagement_until = None
        return state
    assessment = receipt.assessment
    if assessment is None or not receipt.source_refs or receipt.cause_id in state.native_effects:
        return state
    # Old source events can remain meaningful emotional memories, but do not
    # become new current body stimuli merely because a queued worker read them.
    age = (state.updated_at - receipt.occurred_at).total_seconds()
    if age > 300 or age < -5:
        return state
    before = dict(state.responses)
    state = appraise(state, state.updated_at, assessment.impacts, assessment.confidence, config,
        eligible_dimensions=set(eligible_dimensions).intersection(PSYCHOLOGICAL))
    state.native_effects[receipt.cause_id] = {'at': state.updated_at,
        'deltas': {key: state.responses[key] - before[key] for key in PSYCHOLOGICAL}}
    state.native_appraisals += 1
    topic = hashlib.sha256(assessment.topic.strip().casefold().encode()).hexdigest()
    previous = state.habits.get(topic)
    habituation = previous.value * math.exp(-max(0, (state.updated_at-previous.at).total_seconds()) / config.habituation_seconds) if previous else 0
    state.habits[topic] = Habit(value=habituation + .25 * (1-habituation), at=state.updated_at)
    if len(state.habits) > 2048:
        del state.habits[min(state.habits, key=lambda key:state.habits[key].at)]
    # A semantic sample carries zero work. The original HTTP work remains in
    # its own ledger and is never billed a second time by an appraisal.
    state.work.append(WorkSample(at=state.updated_at, units=0, cause=receipt.cause_id,
        novelty=.5 + assessment.confidence * (assessment.unexpectedness * (.1 + .9 * (1-habituation)) - .5),
        meaning=assessment.meaning, wants_engagement=receipt.engagement == 'engage'))
    if state.engagement_at is None or receipt.occurred_at >= state.engagement_at:
        state.engagement_at = receipt.occurred_at
        state.engagement_cause = receipt.cause_id
        state.engagement_until = min(receipt.intent_until or receipt.occurred_at,
            receipt.occurred_at + timedelta(seconds=3600))
        state.wants_engagement = (receipt.engagement == 'engage' and not state.native_resting
            and state.engagement_until > state.updated_at)
        state.meaning_target = 1 - assessment.confidence * (1 - assessment.meaning)
    # After 72h the slowest transient has < 3e-16 of its original effect.
    state.native_effects = {key:value for key,value in state.native_effects.items()
        if (state.updated_at - value['at']).total_seconds() < 259200}
    return state
