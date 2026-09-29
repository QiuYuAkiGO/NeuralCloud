"""Model-proposed goal interpretations; counters remain execution-owned."""
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field


class ProductionGoalUpdate(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    goal_id: str | None = None
    status: Literal["candidate", "active", "paused", "completed", "abandoned"]
    description: str = Field(min_length=1, max_length=2000)
    success_criteria: list[str] = Field(min_length=1, max_length=16)
    evidence_refs: list[str] = Field(default_factory=list, max_length=32)
    reason: str = Field(min_length=1, max_length=2000)
    deadline_at: AwareDatetime | None = None
