"""Strict, channel-bound contracts for the expression director."""
from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, StringConstraints, model_validator

from harness.core.models import PersonaCore

Identity = Annotated[str, StringConstraints(strict=True, strip_whitespace=False, min_length=1, max_length=256)]
EmotionLabel = Literal["anger", "frustration", "sadness", "fear", "joy", "interest", "loneliness", "shame", "disgust", "calm", "affection", "surprise"]
Unit = Annotated[float, Field(strict=True, ge=0, le=1)]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class EmotionMessage(StrictModel):
    source_id: Identity
    scope: Identity
    role: Literal["user", "character"]
    text: str = Field(min_length=1, max_length=1_000_000)
    timestamp: AwareDatetime
    speaker: str | None = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def valid_identity(self):
        if any(not value.strip() or value != value.strip() for value in (self.source_id, self.scope)):
            raise ValueError("emotion_identity_invalid")
        return self


class EmotionInput(StrictModel):
    context_id: Identity
    scope: Identity
    as_of: AwareDatetime
    persona: PersonaCore | None = None
    homeostasis: dict[str, Any] = Field(default_factory=dict)
    messages: list[EmotionMessage] = Field(default_factory=list, max_length=200)
    new_source_ids: list[Identity] = Field(default_factory=list, max_length=200)
    deadline_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def bound_sources(self):
        if any(not value.strip() or value != value.strip() for value in (self.context_id, self.scope)):
            raise ValueError("emotion_identity_invalid")
        if any(message.scope != self.scope for message in self.messages):
            raise ValueError("emotion_message_scope_mismatch")
        identities = [message.source_id for message in self.messages]
        if len(identities) != len(set(identities)) or len(self.new_source_ids) != len(set(self.new_source_ids)):
            raise ValueError("emotion_duplicate_source_id")
        external = {message.source_id for message in self.messages if message.role == "user"}
        if not set(self.new_source_ids) <= external:
            raise ValueError("emotion_new_sources_must_be_observed_external_messages")
        return self


class EmotionSignal(StrictModel):
    label: EmotionLabel
    intensity: Unit
    confidence: Unit = 1.0


class EmotionRecovery(StrictModel):
    label: EmotionLabel
    strength: Annotated[float, Field(strict=True, ge=0, le=0.5)]
    evidence_quote: str = Field(min_length=1, max_length=300)


class EmotionEventAppraisal(StrictModel):
    source_id: Identity
    signals: list[EmotionSignal] = Field(default_factory=list, max_length=6)
    recovery: list[EmotionRecovery] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def unique_labels(self):
        for items in (self.signals, self.recovery):
            if len({item.label for item in items}) != len(items):
                raise ValueError("emotion_duplicate_label")
        return self


class EmotionAppraisal(StrictModel):
    events: list[EmotionEventAppraisal] = Field(default_factory=list, max_length=200)
    need_interpretation: str = Field(default="", max_length=160)
    personality_response: str = Field(default="", max_length=160)


class EmotionFrame(StrictModel):
    context_id: Identity
    scope: Identity
    revision: int = Field(ge=0)
    as_of: AwareDatetime
    deadline_at: AwareDatetime | None = None
    status: Literal["off", "model", "fallback", "empty"] = "empty"
    labels: dict[EmotionLabel, Unit] = Field(default_factory=dict)
    dialogue_trends: dict[EmotionLabel, Literal["rising", "steady", "recovering"]] = Field(default_factory=dict)
    weights: dict[Literal["homeostasis", "personality", "dialogue"], Unit]
    strengths: dict[Literal["homeostasis", "personality", "dialogue"], Unit]
    intensity: Unit
    dominant_source: Literal["homeostasis", "personality", "dialogue"]
    homeostatic_needs: list[dict[str, Any]] = Field(default_factory=list, max_length=7)
    salience: Literal["nominal", "noticeable", "marked", "critical"] = "nominal"
    persona: dict[str, Any] = Field(default_factory=dict)
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class EmotionGuidance(StrictModel):
    text: str = Field(max_length=300)
    context_id: Identity
    scope: Identity
    revision: int = Field(ge=0)
    status: Literal["off", "model", "fallback"] = "fallback"
    diagnostics: dict[str, Any] = Field(default_factory=dict)


class GuidanceOutput(StrictModel):
    feeling: str = Field(min_length=1, max_length=100, description="角色当前感受和主次状态，不是角色台词")
    delivery: str = Field(min_length=1, max_length=100, description="给表演模型的口吻、措辞与节奏要求")
    needs: str = Field(min_length=1, max_length=80, description="已确认的当前需要，不决定是否发言或采取行动")

    @model_validator(mode="after")
    def nonempty_parts(self):
        if any(not value.strip() for value in (self.feeling, self.delivery, self.needs)):
            raise ValueError("emotion_guidance_empty_part")
        return self

    @property
    def text(self) -> str:
        return self.compose()

    def compose(self, *, needs_first: bool = False) -> str:
        parts = [("当前感受：", self.feeling), ("表达方式：", self.delivery), ("当前需要：", self.needs)]
        if needs_first:
            parts = [("首要需要：", self.needs), *parts[:2]]
        return "".join(title + value.strip().rstrip("。；.!;") + "。" for title, value in parts)
