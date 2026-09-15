"""Typed internal QQ opportunities, never fabricated incoming messages."""

from __future__ import annotations

from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.qq.memory import QQID


class QQIdleIntent(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["qq-idle-intent-v1"] = "qq-idle-intent-v1"
    intent_id: str = Field(min_length=1, max_length=160, strict=True)
    group_id: QQID
    self_member_id: QQID
    run_id: str = Field(min_length=1, max_length=160, strict=True)
    created_at: AwareDatetime
    deadline_at: AwareDatetime
    episode_signature: str = Field(min_length=1, max_length=96, strict=True)
    homeostasis_revision: int = Field(ge=0, strict=True)
    homeostatic_interoception: HomeostaticInteroceptiveSummary
    context_signature: str = Field(min_length=1, max_length=2048, strict=True)
    body_instance_id: str = Field(min_length=1, max_length=128, strict=True)
    environment_revision: int = Field(ge=1, strict=True)
    window_id: str = Field(min_length=1, max_length=128, strict=True)
    opportunity_ref: str | None = Field(default=None, min_length=1, max_length=256)
    selection_ref: str | None = Field(default=None, min_length=1, max_length=256)
    scheduler_generation: int | None = Field(default=None, ge=0, strict=True)
    owner_generation: int | None = Field(default=None, ge=0, strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def nonblank(cls, value):
        if isinstance(value, str) and not value.strip():
            raise ValueError("idle identifiers must not be blank")
        return value

    @field_validator("homeostatic_interoception", mode="before")
    @classmethod
    def validate_full_summary(cls, value):
        if isinstance(value, HomeostaticInteroceptiveSummary):
            value = value.model_dump(mode="python")
        return HomeostaticInteroceptiveSummary.model_validate(value)

    @model_validator(mode="after")
    def bind_summary_and_deadline(self):
        summary = self.homeostatic_interoception
        if any(value is not None for value in (self.opportunity_ref, self.selection_ref, self.scheduler_generation)) and any(
                value is None for value in (self.opportunity_ref, self.selection_ref, self.scheduler_generation)):
            raise ValueError("shared opportunity binding must be complete")
        if not 0 < (self.deadline_at - self.created_at).total_seconds() <= 60:
            raise ValueError("idle lifetime must be positive and at most sixty seconds")
        if (self.homeostasis_revision != summary.snapshot_revision
                or self.episode_signature != summary.episode_signature):
            raise ValueError("idle summary revision and episode must match")
        return self


def validated_idle_intent(value: QQIdleIntent | dict) -> QQIdleIntent:
    """Revalidate even model_copy/model_construct objects at trust boundaries."""
    if isinstance(value, QQIdleIntent):
        value = value.model_dump(mode="python")
    return QQIdleIntent.model_validate(value)
