"""Memory-side canonical objects. Modules pass these, never free-form prose."""

from __future__ import annotations

from datetime import datetime
import re
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from harness.core.ids import new_id


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def default_event_participants(speaker: str) -> list[str]:
    name = speaker.strip() or "user"
    if name == "character":
        return ["character", "user"]
    if name == "user":
        return ["user", "character"]
    return [name, "user", "character"]


class ProvenanceRecord(BaseModel):
    model_version: str = "mock"
    prompt_version: str = "memory-v1"
    schema_version: str = "state-v1"
    why_context_selected: str | None = None
    timestamp: datetime | None = None


class InteractionEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    timestamp: datetime
    channel: str = "chat"
    speaker: str
    modality: str = "text"
    raw_text: str
    session_id: str
    participants: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    uniqueness: float | None = Field(default=None, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def _default_participants(self) -> InteractionEvent:
        if not self.participants:
            self.participants = default_event_participants(self.speaker)
        return self


class PersonaProfile(BaseModel):
    """Structured, source-linked facts the character may know about itself."""

    aliases: list[str] = Field(default_factory=list)
    age: int | None = Field(default=None, ge=0, le=200)
    birthday: str | None = None
    height_cm: float | None = Field(default=None, gt=0, le=300)
    hobbies: list[str] = Field(default_factory=list)
    signature_line: str | None = None
    appearance: list[str] = Field(default_factory=list)
    personal_items: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)


class PersonaIdentity(BaseModel):
    name: str
    core_traits: list[str] = Field(default_factory=list)
    values: list[str] = Field(default_factory=list)
    speech_style: list[str] = Field(default_factory=list)
    knowledge_boundary: list[str] = Field(default_factory=list)
    profile: PersonaProfile | None = None
    locked: bool = True


class PersonaCore(BaseModel):
    persona_id: str = Field(default_factory=lambda: new_id("persona"))
    version: str = "v1"
    identity: PersonaIdentity
    created_at: datetime


MemoryTier = Literal["short", "mid", "long", "forgotten"]


class MemoryScope(BaseModel):
    """Application-assigned provenance and visibility, never a retrieval-plan guess."""

    owner_id: str = "misty"
    channel: str
    conversation_id: str | None = None
    visibility: list[str] = Field(default_factory=lambda: ["local:main"])

    @field_validator("visibility")
    @classmethod
    def _canonical_visibility(cls, value: list[str]) -> list[str]:
        if any(not audience.strip() for audience in value):
            raise ValueError("memory visibility cannot contain blank audiences")
        return sorted(set(value))

    @property
    def qq_private_audience(self) -> str | None:
        """Return a bound QQ private audience, excluding unbound legacy scopes."""
        if self.channel != "qq_private" or not self.conversation_id:
            return None
        match = re.fullmatch(r"qq:private:([1-9][0-9]{0,19}):([1-9][0-9]{0,19})", self.conversation_id)
        if match is None or match[1] == match[2]:
            return None
        return self.conversation_id

    @model_validator(mode="after")
    def _private_visibility(self) -> MemoryScope:
        if self.channel != "qq_private":
            return self
        audience = self.qq_private_audience
        if self.conversation_id is not None and audience is None:
            raise ValueError("QQ private memory requires an exact self and peer audience")
        allowed = {"local:main", audience} if audience else {"local:main"}
        if not set(self.visibility).issubset(allowed):
            raise ValueError("QQ private memory cannot be visible outside its self and peer scope")
        return self


class DualMemoryRecord(BaseModel):
    event_id: str
    objective_fact: str
    persona_conditioned_insight: str | None = None
    insight_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)


class EpisodicEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: new_id("event"))
    time: datetime
    participants: list[str]
    summary: str
    character_action: str | None = None
    outcome: str | None = None
    importance: float = Field(ge=0.0, le=1.0)
    emotional_valence: float = Field(ge=-1.0, le=1.0)
    evidence: list[str]
    objective_fact: str
    persona_conditioned_insight: str | None = None
    insight_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    supporting_evidence: list[str] = Field(default_factory=list)
    channel: str = "chat"
    event_type: str | None = None
    session_id: str | None = None
    embedding: list[float] | None = None
    tier: MemoryTier = "short"
    uniqueness: float | None = Field(default=None, ge=0.0, le=1.0)
    expire_at: datetime | None = None
    hit_count: int = 0
    last_hit_at: datetime | None = None
    tier_updated_at: datetime | None = None
    tier_reasons: list[str] = Field(default_factory=list)
    scope: MemoryScope | None = None
    source_refs: list[str] = Field(default_factory=list)
    processing_version: str | None = None
    strength: float = Field(default=1.0, ge=0.0)
    strength_updated_at: datetime | None = None
    last_decay_review_at: datetime | None = None
    decay_demoted: bool = False

    @field_validator("evidence")
    @classmethod
    def _evidence_required(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("episodic memory requires a non-empty evidence chain")
        return value

    def dual(self) -> DualMemoryRecord:
        return DualMemoryRecord(
            event_id=self.event_id,
            objective_fact=self.objective_fact,
            persona_conditioned_insight=self.persona_conditioned_insight,
            insight_confidence=self.insight_confidence,
            supporting_evidence=self.supporting_evidence or list(self.evidence),
        )


class SemanticFact(BaseModel):
    fact_id: str = Field(default_factory=lambda: new_id("fact"))
    subject: str
    predicate: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)
    supporting_events: list[str]
    formed_at: datetime
    embedding: list[float] | None = None
    scope: MemoryScope | None = None
    source_refs: list[str] = Field(default_factory=list)
    processing_version: str | None = None

    @field_validator("supporting_events")
    @classmethod
    def _events_required(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("semantic facts require supporting_events")
        return value


class RelationshipState(BaseModel):
    relation_id: str = Field(default_factory=lambda: new_id("relation"))
    target: str
    closeness: float = Field(default=0.5, ge=0.0, le=1.0)
    trust: float = Field(default=0.5, ge=0.0, le=1.0)
    attachment: float = Field(default=0.5, ge=0.0, le=1.0)
    recent_tension: float = Field(default=0.0, ge=0.0, le=1.0)
    shared_history_summary: str = ""
    last_major_event: str | None = None
    updated_at: datetime

    def apply_delta(
        self,
        *,
        closeness: float | None = None,
        trust: float | None = None,
        attachment: float | None = None,
        recent_tension: float | None = None,
        last_major_event: str | None = None,
        updated_at: datetime,
        history_note: str | None = None,
    ) -> RelationshipState:
        data = self.model_dump()
        if closeness is not None:
            data["closeness"] = _clamp01(self.closeness + closeness)
        if trust is not None:
            data["trust"] = _clamp01(self.trust + trust)
        if attachment is not None:
            data["attachment"] = _clamp01(self.attachment + attachment)
        if recent_tension is not None:
            data["recent_tension"] = _clamp01(self.recent_tension + recent_tension)
        if last_major_event is not None:
            data["last_major_event"] = last_major_event
        if history_note:
            existing = self.shared_history_summary
            data["shared_history_summary"] = (
                f"{existing}; {history_note}" if existing else history_note
            )
        data["updated_at"] = updated_at
        return RelationshipState.model_validate(data)


class PsychState(BaseModel):
    character_id: str = "character"
    long: dict[str, Any] = Field(default_factory=dict)
    mid: dict[str, Any] = Field(default_factory=dict)
    short: dict[str, Any] = Field(default_factory=dict)
    updated_at: datetime


class WorldState(BaseModel):
    time: datetime
    active_application: str | None = None
    active_window_title: str | None = None
    open_windows: list[str] = Field(default_factory=list)
    recent_files_modified: list[str] = Field(default_factory=list)
    user_presence: str = "unknown"
    user_activity: str = "unknown"
    channel: str = "chat"


class CandidateMemory(BaseModel):
    candidate_id: str = Field(default_factory=lambda: new_id("candidate"))
    claim: str
    source: str
    confidence: float = Field(ge=0.0, le=1.0)
    status: Literal["candidate", "consolidated", "dropped"] = "candidate"
    evidence: list[str] = Field(min_length=1)
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None
    created_at: datetime
    updated_at: datetime
    consolidated_fact_id: str | None = None
    scope: MemoryScope | None = None
    source_refs: list[str] = Field(default_factory=list)
    processing_version: str | None = None


class RetrievalPlan(BaseModel):
    time_scope: str | None = None
    entities: list[str] = Field(default_factory=list)
    event_type: str | None = None
    emotional_relevance: str | None = None
    # Channel filter is a hard constraint in pool selection; default empty so
    # memories from non-chat channels (lore, combat, daily) stay reachable.
    channels: list[str] = Field(default_factory=list)


class ScoreBreakdown(BaseModel):
    semantic: float = 0.0
    entity: float = 0.0
    recency: float = 0.0
    importance: float = 0.0
    relationship: float = 0.0
    persona: float = 0.0
    total: float = 0.0


class ScoredMemory(BaseModel):
    memory_type: Literal["episodic", "semantic"]
    ref: str
    relevance: float
    why_retrieved: list[str] = Field(default_factory=list)
    score_breakdown: ScoreBreakdown
    episodic: EpisodicEvent | None = None
    semantic: SemanticFact | None = None


class CuratorOp(BaseModel):
    op: Literal["ADD", "UPDATE", "DELETE", "NO_OP"]
    type: Literal["episodic", "semantic", "candidate", "relationship"] | None = None
    ref: str | None = None
    importance: float | None = None
    confidence_delta: float | None = None
    reason: str | None = None
    claim: str | None = None
    source: str | None = None
    confidence: float | None = None
    evidence: list[str] | None = None
    subject: str | None = None
    predicate: str | None = None
    value: str | None = None


class CuratorOutput(BaseModel):
    ops: list[CuratorOp]


class CurrentInference(BaseModel):
    about: str
    value: str
    polarity: Literal["positive", "negative"] = "positive"
    entities: list[str] = Field(default_factory=list)
    expected_valence: float | None = None


class RetrievalQuery(BaseModel):
    text: str
    now: datetime
    session_id: str | None = None
    entities: list[str] = Field(default_factory=list)
    relationship: RelationshipState | None = None
    persona: PersonaCore | None = None
    current_inference: CurrentInference | None = None
    include_conflicts: bool = False
    top_k: int | None = None
    trusted_audience: str = Field(default="local:main", min_length=1)


class CharacterSnapshot(BaseModel):
    persona: PersonaCore | None = None
    relationship: RelationshipState | None = None
    psych: PsychState | None = None
    world: WorldState | None = None
    short_psych_expired: bool = False
