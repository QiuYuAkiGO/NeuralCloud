"""Pure, evidence-bound QQ impressions; memorability is not belief confidence.

The caller resolves source attribution and stores these serializable values in
the same transaction as its processing receipt. No model or store is called here.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from harness.config import Settings
from harness.core.models import MemoryScope

ImpressionKind = Literal["person", "relationship", "topic", "group"]
ImpressionStatus = Literal["provisional", "stable", "disputed", "dormant"]
_SHANGHAI = ZoneInfo("Asia/Shanghai")


class ImpressionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    episode_id: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    time: AwareDatetime
    stance: Literal["support", "contradict"]
    rationale: str = Field(min_length=1)
    statement: str = Field(min_length=1)
    independent: bool = True
    quoted: bool = False
    self_generated: bool = False

    @field_validator("source_ids")
    @classmethod
    def _sources(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("source IDs must not be blank")
        return sorted(set(value))

    @field_validator("episode_id", "rationale", "statement")
    @classmethod
    def _nonblank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("impression evidence must not be blank")
        return value.strip()


class ImpressionProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: ImpressionKind
    subject_refs: list[str] = Field(default_factory=list)
    statement: str = Field(min_length=1)
    evidence: ImpressionEvidence
    topic: str = ""
    context: str = ""
    uncertain: bool = False
    contrast: bool = False
    continues_episode_id: str | None = None

    @field_validator("subject_refs")
    @classmethod
    def _subjects(cls, value: list[str]) -> list[str]:
        if any(not item.strip() for item in value):
            raise ValueError("subject references must not be blank")
        return sorted(set(value))

    @field_validator("statement")
    @classmethod
    def _statement(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("impression statement must not be blank")
        return value.strip()

    @model_validator(mode="after")
    def _bound_subjects(self) -> ImpressionProposal:
        if self.kind == "person" and len(self.subject_refs) != 1:
            raise ValueError("person impressions require exactly one subject")
        if self.kind == "relationship" and len(self.subject_refs) < 2:
            raise ValueError("relationship impressions require both subjects")
        if self.evidence.statement != self.statement:
            raise ValueError("proposal and evidence statements must match")
        return self


class ImpressionRevision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    revision: int = Field(ge=1)
    statement: str
    confidence: float = Field(ge=0, le=1)
    status: ImpressionStatus
    evidence_episode_id: str
    evidence_stance: Literal["support", "contradict"]
    revised_at: AwareDatetime


class ImpressionMemory(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    impression_id: str
    scope: MemoryScope
    kind: ImpressionKind
    subject_refs: list[str]
    topic: str = ""
    context: str = ""
    first_impression: str
    current_impression: str
    first_episode_id: str
    first_observed_at: AwareDatetime
    confidence: float = Field(ge=0, le=1)
    status: ImpressionStatus = "provisional"
    strength: float = Field(default=1.0, ge=0)
    strength_updated_at: AwareDatetime
    evidence: list[ImpressionEvidence] = Field(min_length=1)
    continuation_evidence: list[ImpressionEvidence] = Field(default_factory=list)
    supporting_episode_ids: list[str] = Field(default_factory=list)
    contradicting_episode_ids: list[str] = Field(default_factory=list)
    revision: int = Field(ge=1)
    history: list[ImpressionRevision] = Field(min_length=1)
    updated_at: AwareDatetime


class ImpressionUpdate(BaseModel):
    memory: ImpressionMemory | None = None
    changed: bool = False
    reinforce_episode_ids: list[str] = Field(default_factory=list)
    reason: str = ""


def impression_key(
    scope: MemoryScope,
    kind: ImpressionKind,
    subject_refs: list[str],
    topic: str = "",
    context: str = "",
) -> str:
    """Identity includes a trusted scope, never a model's display name alone."""
    payload = [scope.model_dump(mode="json"), kind, sorted(set(subject_refs)), topic.strip(), context.strip()]
    canonical = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return "impression_" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _confidence(evidence: list[ImpressionEvidence], settings: Settings) -> float:
    confidence = 0.0
    seen_support = False
    for item in sorted(evidence, key=lambda item: (item.time, item.episode_id)):
        if item.stance == "contradict":
            confidence *= settings.qq_impression_counter_multiplier
        elif not seen_support:
            confidence = settings.qq_impression_initial_confidence
            seen_support = True
        else:
            confidence += settings.qq_impression_support_increment * (1.0 - confidence)
    return min(1.0, max(0.0, confidence))


def apply_impression(
    existing: ImpressionMemory | None,
    proposal: ImpressionProposal,
    *,
    scope: MemoryScope,
    now: datetime,
    settings: Settings | None = None,
) -> ImpressionUpdate:
    """Apply one independently observed episode exactly once to an impression.

    A quote or the character's own synthesis may provide retrieval context but
    cannot create or reinforce beliefs. Callers should backfill chronologically.
    """
    if now.tzinfo is None or now.utcoffset() is None:
        raise ValueError("impression update time must be timezone aware")
    settings = settings or Settings()
    key = impression_key(scope, proposal.kind, proposal.subject_refs, proposal.topic, proposal.context)
    if existing is not None and existing.impression_id != key:
        raise ValueError("impression scope or subject identity does not match")
    item = proposal.evidence
    if proposal.uncertain or not item.independent or item.quoted or item.self_generated:
        return ImpressionUpdate(memory=existing, reason="uncertain_or_dependent_evidence")
    if item.time > now:
        raise ValueError("impression evidence cannot come from the future")
    previous = list(existing.evidence) if existing else []
    continuations = list(existing.continuation_evidence) if existing else []
    observed = [*previous, *continuations]
    for old in observed:
        if old.episode_id == item.episode_id or set(old.source_ids).intersection(item.source_ids):
            return ImpressionUpdate(memory=existing, reason="duplicate_evidence")
    if proposal.continues_episode_id:
        anchor = next((entry for entry in observed if entry.episode_id == proposal.continues_episode_id), None)
        if anchor is None or anchor.stance != item.stance:
            raise ValueError("continuation requires an existing episode with the same stance")
    if not existing and item.stance == "contradict":
        return ImpressionUpdate(reason="no_prior_impression_to_contradict")
    if existing and item.time < max(old.time for old in observed):
        raise ValueError("impression evidence must be replayed in chronological order")
    latest = max(observed, key=lambda entry: (entry.time, entry.episode_id)) if observed else None
    nearby = (latest is not None and latest.stance == item.stance
              and settings.qq_impression_continuation_gap_minutes > 0
              and item.time - latest.time < timedelta(minutes=settings.qq_impression_continuation_gap_minutes))
    if existing is not None and (proposal.continues_episode_id or nearby):
        # Window boundaries are scheduling details, not independent experiences.
        # Record every source in the same conversational round without adding
        # confidence, independent support, revisions, or retrieval strength.
        continued = item.model_copy(update={"independent": False})
        memory = existing.model_copy(update={"continuation_evidence": [*continuations, continued]})
        return ImpressionUpdate(memory=memory, changed=True, reason="continued_experience")
    evidence = [*previous, item]
    confidence = _confidence(evidence, settings)
    supports = [entry for entry in evidence if entry.stance == "support"]
    dates = {entry.time.astimezone(_SHANGHAI).date() for entry in supports}
    stable = (len(supports) >= settings.qq_impression_min_support_episodes
              and len(dates) >= settings.qq_impression_min_distinct_days
              and confidence >= settings.qq_impression_stable_confidence)
    last_counter = max((index for index, entry in enumerate(evidence) if entry.stance == "contradict"), default=-1)
    unresolved = last_counter >= 0 and (
        len([entry for entry in evidence[last_counter + 1:] if entry.stance == "support"])
        < settings.qq_impression_min_support_episodes
        or not stable
    )
    status: ImpressionStatus = "disputed" if unresolved else "stable" if stable else "provisional"
    revision = existing.revision + 1 if existing else 1
    history = list(existing.history) if existing else []
    history.append(ImpressionRevision(
        revision=revision, statement=proposal.statement, confidence=confidence,
        status=status, evidence_episode_id=item.episode_id,
        evidence_stance=item.stance, revised_at=now,
    ))
    memory = ImpressionMemory(
        impression_id=key, scope=scope, kind=proposal.kind,
        subject_refs=proposal.subject_refs, topic=proposal.topic.strip(), context=proposal.context.strip(),
        first_impression=existing.first_impression if existing else proposal.statement,
        current_impression=proposal.statement,
        first_episode_id=existing.first_episode_id if existing else item.episode_id,
        first_observed_at=existing.first_observed_at if existing else item.time,
        confidence=confidence, status=status, evidence=evidence, continuation_evidence=continuations,
        strength=existing.strength if existing else 1.0, strength_updated_at=now,
        supporting_episode_ids=[entry.episode_id for entry in supports],
        contradicting_episode_ids=[entry.episode_id for entry in evidence if entry.stance == "contradict"],
        revision=revision, history=history, updated_at=now,
    )
    # Salience increases for both sides of the contrast, even as belief falls.
    reinforce = []
    if existing and (proposal.contrast or item.stance == "contradict"):
        reinforce = list(dict.fromkeys([existing.first_episode_id, previous[-1].episode_id, item.episode_id]))
    return ImpressionUpdate(memory=memory, changed=True, reinforce_episode_ids=reinforce, reason="applied")
