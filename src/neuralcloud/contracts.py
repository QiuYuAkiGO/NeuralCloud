"""Owner-defined v1 contracts; exported as JSON Schema by the maintenance CLI."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class EvidenceReference(Contract):
    record_id: str = Field(min_length=1, max_length=256)
    original_revision: int = Field(ge=0)
    role: str = Field(min_length=1, max_length=64)
    start: int | None = Field(default=None, ge=0)
    end: int | None = Field(default=None, ge=0)


class SupersessionReference(Contract):
    record_id: str = Field(min_length=1, max_length=256)
    expected_revision: int | None = Field(default=None, ge=0)


class LegacyProvenance(Contract):
    format: Literal["production-memory-v1-v2"] = "production-memory-v1-v2"
    snapshot_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    payload_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_id: str = Field(min_length=1, max_length=256)
    original_revision: int = Field(ge=0)
    semantic_status: Literal["observation", "self_expression", "derived_summary", "historical_interpretation"]
    # A historical interpretation is never promoted to a verified/current fact.
    valid_from: str | None = None
    valid_until: str | None = None
    topic: str | None = None
    context: str | None = None
    source_channel: str | None = None
    visibility: list[str] = Field(default_factory=list, max_length=128)
    external_refs: list[str] = Field(default_factory=list, max_length=128)
    evidence: list[EvidenceReference] = Field(default_factory=list, max_length=128)
    supersedes: list[SupersessionReference] = Field(default_factory=list, max_length=128)
    original_pinned: bool = False
    original_important: bool = False


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
    source_ids: list[str] = Field(default_factory=list, max_length=128)
    pinned: bool = False
    provenance: LegacyProvenance | None = None


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


SourceInfo = create_model(
    "SourceInfo",
    __base__=Contract,
    **{name: (field.annotation, field) for name, field in SourceEvent.model_fields.items() if name != "content"},
)


class DurableReceipt(Contract):
    status: Literal["DURABLE"]
    seq: int
    record_id: str
    revision: int


class MissingReceipt(Contract):
    status: Literal["NOT_FOUND"]


class RecordView(Contract):
    status: Literal["ONLINE", "NOT_FOUND", "ARCHIVED_OFFLINE", "RETRACTED", "SOURCE_CHANGED", "INACTIVE"]
    record_id: str
    revision: int | None = None
    kind: str | None = None
    occurred_at: float | None = None
    generation: int | None = None
    content: str | None = None
    offset: int | None = None
    total_characters: int | None = None
    source: SourceInfo | None = None


class RecallResult(Contract):
    status: Literal["OK"]
    scope: Literal["GLOBAL_ONLINE"]
    generation: int
    items: list[RecordView]
    coverage: str


def canonical(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
