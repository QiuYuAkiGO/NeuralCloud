"""Owner-defined v1 contracts; exported as JSON Schema by the maintenance CLI."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class SourceEvent(Contract):
    schema_version: Literal[1] = 1
    character_id: str = Field(min_length=1, max_length=128)
    source_instance: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=256)
    session_id: str = Field(min_length=1, max_length=256)
    conversation_id: str | None = Field(default=None, max_length=256)
    activity_id: str | None = Field(default=None, max_length=256)
    occurred_at: float = Field(ge=0, allow_inf_nan=False)
    content: str = Field(min_length=1, max_length=32768)
    kind: Literal["observation", "summary", "relationship", "persona_history"] = "observation"
    privacy: Literal["public", "private", "sensitive"] = "private"
    participants: list[str] = Field(default_factory=list, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=64)
    source_ids: list[str] = Field(default_factory=list, max_length=64)
    pinned: bool = False


class Correction(Contract):
    schema_version: Literal[1] = 1
    character_id: str = Field(min_length=1, max_length=128)
    source_instance: str = Field(min_length=1, max_length=128)
    event_id: str = Field(min_length=1, max_length=256)
    record_id: str = Field(min_length=1, max_length=256)
    expected_revision: int = Field(ge=1)
    content: str | None = Field(default=None, max_length=32768)
    reason: str = Field(min_length=1, max_length=1024)
    retract: bool = False


class Recall(Contract):
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=50)


def canonical(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
