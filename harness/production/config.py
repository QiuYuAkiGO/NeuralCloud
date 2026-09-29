"""Versioned production mechanism choices, independent of research treatment."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class ProductionConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["production-config-v1"] = "production-config-v1"
    autonomous_enabled: bool = True
    autonomous_attempts_per_hour: int = Field(default=12, ge=1, le=12)
    maximum_active_goals: int = Field(default=4, ge=1, le=4)
    maximum_plan_steps: int = Field(default=32, ge=1, le=32)
    maximum_no_progress: int = Field(default=3, ge=1, le=3)
    foreground_slots: Literal[2] = 2
    autonomous_review_seconds: float = Field(default=300, ge=60, le=3600)
    body_timeout_seconds: float = Field(default=2, gt=0, le=10)
    body_max_age_seconds: float = Field(default=10, gt=0, le=60)
    permitted_intents: tuple[str, ...] = ()
