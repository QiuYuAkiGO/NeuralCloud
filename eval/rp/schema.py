"""Unified RP episode / sample objects. Runners and judges pass these, not raw JSON."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

BenchName = Literal["charactereval", "roleeval", "timechara"]
RunMode = Literal["raw", "harness"]


class Turn(BaseModel):
    speaker: str
    text: str
    role: Literal["user", "character", "other"] | None = None


class RPEpisode(BaseModel):
    bench: BenchName
    episode_id: str
    role_id: str
    profile: dict[str, Any] = Field(default_factory=dict)
    history: list[Turn] = Field(default_factory=list)
    query: str
    choices: dict[str, str] | None = None
    answer: str | None = None
    story_time: datetime | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class RPSample(BaseModel):
    episode_id: str
    bench: BenchName
    mode: RunMode
    role_id: str
    response: str
    latency_ms: float
    scores: dict[str, float] = Field(default_factory=dict)
    skipped_scores: list[str] = Field(default_factory=list)
    trace_ref: str | None = None
    error: str | None = None
    meta: dict[str, Any] = Field(default_factory=dict)


class DatasetMissingError(FileNotFoundError):
    def __init__(self, bench: str, hint: str) -> None:
        super().__init__(hint)
        self.bench = bench
        self.hint = hint
