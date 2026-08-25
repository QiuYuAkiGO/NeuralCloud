"""Online inference objects (02-inference-flow.md). Modules pass these, never free-form prose."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator

from harness.core.ids import new_id
from harness.core.models import (
    CharacterSnapshot,
    RelationshipState,
    ScoredMemory,
    WorldState,
)


class Reference(BaseModel):
    type: str
    time_hint: str | None = None
    about: str | None = None


class Hypothesis(BaseModel):
    about: str
    value: str
    confidence: float = Field(ge=0.0, le=1.0)


class ParsedInteraction(BaseModel):
    parse_id: str = Field(default_factory=lambda: new_id("parse"))
    speech_act: str
    references: list[Reference] = Field(default_factory=list)
    possible_social_intent: Hypothesis | None = None
    emotion_cues: dict[str, float] = Field(default_factory=dict)
    entities: list[str] = Field(default_factory=lambda: ["user", "character"])
    # When the parser's LLM call is made, it doubles as the gate's second judge;
    # the Complexity Gate reuses this vote instead of paying for a second call.
    gate_vote: str | None = None


class SituationTrigger(BaseModel):
    type: str = "user_message"
    ref: str


class SituationActor(BaseModel):
    persona_core_ref: str | None = None
    psych_state: dict[str, Any] = Field(default_factory=dict)


class ComplexitySignals(BaseModel):
    time_scope: str | None = None
    has_commitment_language: bool = False
    has_memory_probe: bool = False
    has_value_conflict: bool = False
    has_person_name: bool = False
    has_unknown_entity: bool = False
    is_greeting: bool = False
    is_question: bool = False
    text_length: int = 0
    emotion_strength: float = Field(default=0.0, ge=0.0, le=1.0)
    retrieval_commitment_conflict: bool = False


class ComplexityAssessment(BaseModel):
    score: float = Field(default=0.0, ge=0.0, le=1.0)
    requires_deep_path: bool = False
    reasons: list[str] = Field(default_factory=list)
    signals: ComplexitySignals = Field(default_factory=ComplexitySignals)


# Downstream stages (Interpreter / Compiler / GUI) only list this many hits.
MAX_RELEVANT_MEMORIES = 5


class SituationState(BaseModel):
    """Canonical internal state — not a prompt (01 §8.1 / 02 §5)."""

    situation_id: str = Field(default_factory=lambda: new_id("situation"))
    trigger: SituationTrigger
    utterance: str
    parsed: ParsedInteraction | None = None
    actor: SituationActor
    relationship: RelationshipState | None = None
    world: WorldState | None = None
    relevant_memories: list[ScoredMemory] = Field(
        default_factory=list,
        max_length=MAX_RELEVANT_MEMORIES,
    )
    hypotheses: list[Hypothesis] = Field(default_factory=list)
    complexity: ComplexityAssessment = Field(default_factory=ComplexityAssessment)
    snapshot: CharacterSnapshot | None = None

    @field_validator("relevant_memories", mode="before")
    @classmethod
    def _cap_relevant_memories(cls, value: object) -> object:
        if isinstance(value, list):
            return value[:MAX_RELEVANT_MEMORIES]
        return value


class InferenceRequest(BaseModel):
    inference_id: str = Field(default_factory=lambda: new_id("inference"))
    path: Literal["fast", "deep"]
    score: float
    reasons: list[str] = Field(default_factory=list)
    situation_id: str
    trigger_subagents: list[str] = Field(default_factory=list)


class IntentHypothesis(BaseModel):
    intent: str
    confidence: float = Field(ge=0.0, le=1.0)


class StructuredInsight(BaseModel):
    insight_id: str = Field(default_factory=lambda: new_id("insight"))
    kind: str = "social_inference"
    possible_intents: list[IntentHypothesis] = Field(default_factory=list)
    relationship_delta_expected: str | None = None
    recommended_stance: str | None = None
    constraint: str | None = None
    source: str = "social_interpreter"


class ContextSegment(BaseModel):
    kind: str
    text: str
    source_ids: list[str] = Field(default_factory=list)
    why: str | None = None
    token_estimate: int = 0


class ContextBudget(BaseModel):
    total: int
    used: int = 0
    per_kind: dict[str, int] = Field(default_factory=dict)


class ProvenanceEntry(BaseModel):
    segment_kind: str
    source_ids: list[str] = Field(default_factory=list)
    why: str | None = None


class ModelContext(BaseModel):
    context_id: str = Field(default_factory=lambda: new_id("context"))
    segments: list[ContextSegment] = Field(default_factory=list)
    budget: ContextBudget
    provenance: list[ProvenanceEntry] = Field(default_factory=list)
    trigger_text: str = ""

    def render_messages(self) -> list[dict[str, str]]:
        system_parts = [seg.text for seg in self.segments if seg.kind != "user" and seg.text]
        user_parts = [seg.text for seg in self.segments if seg.kind == "user" and seg.text]
        user_content = "\n\n".join(user_parts) if user_parts else self.trigger_text
        return [
            {"role": "system", "content": "\n\n".join(system_parts)},
            {"role": "user", "content": user_content},
        ]


class TokenUsage(BaseModel):
    """Character-model token accounting for one call or a summed turn."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    calls: int = 0
    revision_calls: int = 0
    revision_tokens: int = 0
    source: Literal["api", "estimate", "mixed"] = "estimate"

    @model_validator(mode="after")
    def _fill_total(self) -> TokenUsage:
        if self.total_tokens <= 0:
            self.total_tokens = self.prompt_tokens + self.completion_tokens
        return self

    def for_call(self, *, revision: int = 0) -> TokenUsage:
        total = self.total_tokens or (self.prompt_tokens + self.completion_tokens)
        revised = revision >= 1
        return self.model_copy(
            update={
                "total_tokens": total,
                "calls": 1,
                "revision_calls": 1 if revised else 0,
                "revision_tokens": total if revised else 0,
            }
        )

    def add(self, other: TokenUsage | None) -> TokenUsage:
        if other is None:
            return self
        if self.calls == 0 and self.total_tokens == 0:
            return other
        if other.calls == 0 and other.total_tokens == 0:
            return self
        source: Literal["api", "estimate", "mixed"]
        source = self.source if self.source == other.source else "mixed"
        prompt = self.prompt_tokens + other.prompt_tokens
        completion = self.completion_tokens + other.completion_tokens
        return TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=prompt + completion,
            calls=self.calls + other.calls,
            revision_calls=self.revision_calls + other.revision_calls,
            revision_tokens=self.revision_tokens + other.revision_tokens,
            source=source,
        )


class CandidateResponse(BaseModel):
    text: str
    model_version: str = "mock"
    revision: int = 0
    continuation: Literal["continue", "end"] = "end"
    usage: TokenUsage = Field(default_factory=TokenUsage)


class ValidationResult(BaseModel):
    verdict: Literal["pass", "revise"] = "pass"
    violations: list[str] = Field(default_factory=list)
    severity: float = Field(default=0.0, ge=0.0, le=1.0)
    instruction: str | None = None
    skipped: bool = False


class SessionTurn(BaseModel):
    role: Literal["user", "character"]
    text: str
    speaker: str | None = None
    event_id: str | None = None
    timestamp: datetime | None = None


class FinalResponse(BaseModel):
    response_id: str = Field(default_factory=lambda: new_id("response"))
    text: str
    path: Literal["fast", "deep"]
    revisions: int = 0
    situation_id: str
    context_id: str | None = None
    event_id: str
    insights: list[StructuredInsight] = Field(default_factory=list)
    validation: ValidationResult | None = None
    complexity_score: float = 0.0
    segments: list[str] = Field(default_factory=list)
    stopped_by: Literal["marker", "max_segments", "empty", "disabled", "duplicate"] = "disabled"
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = Field(default=0.0, ge=0.0)
