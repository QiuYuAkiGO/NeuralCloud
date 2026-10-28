"""Versioned, source-neutral JSON contracts; permissions are server-owned."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Literal, Protocol

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Provider = Literal["neuralcloud", "hy_memory"]


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class ReadContext(Contract):
    audience: str = Field(min_length=1)
    principal: str = Field(min_length=1)
    now: AwareDatetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    hy_user_ids: tuple[str, ...] = ()
    hy_agent_ids: tuple[str, ...] = ()
    hy_session_ids: tuple[str, ...] = ()


class MemoryRecord(Contract):
    record_id: str = Field(min_length=1, max_length=512)
    revision: str = Field(min_length=1, max_length=256)
    subject_ids: tuple[str, ...] = ()
    subject_mention: str | None = None
    kind: str = "unknown"
    content: str = Field(min_length=1, max_length=100000)
    valid_from: AwareDatetime | None = None
    valid_until: AwareDatetime | None = None
    evidence_refs: tuple[str, ...] = ()
    # Source semantics remain explicit instead of promoting an interpretation to fact.
    details: dict = Field(default_factory=dict)

    @model_validator(mode="after")
    def check_time(self):
        if self.valid_from and self.valid_until and self.valid_until <= self.valid_from:
            raise ValueError("empty validity interval")
        return self

    def valid_at(self, now: datetime) -> bool:
        return (self.valid_from is None or self.valid_from <= now) and (
            self.valid_until is None or now < self.valid_until)


class MemoryBundle(Contract):
    schema_version: Literal["memory-bundle-v1"] = "memory-bundle-v1"
    provider: Provider
    audience: str = Field(min_length=1)
    read_at: AwareDatetime
    records: tuple[MemoryRecord, ...] = Field(default=(), max_length=100)

    @model_validator(mode="after")
    def unique_records(self):
        ids = [r.record_id for r in self.records]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate memory record")
        return self


class MemoryQuery(Contract):
    provider: Provider
    query: str = Field(min_length=1, max_length=2048)
    limit: int = Field(default=8, ge=1, le=16)


class Message(Contract):
    role: Literal["system", "user", "assistant"]
    content: str = Field(min_length=1, max_length=20000)


class ChatRequest(Contract):
    messages: tuple[Message, ...] = Field(min_length=1, max_length=64)
    memory: MemoryQuery
    max_new_tokens: int = Field(default=256, ge=1, le=512)


class ManifestChunk(Contract):
    record_id: str
    revision: str
    chunk_index: int
    text: str
    prefix: str
    start: int
    end: int
    token_ids_hash: str
    token_count: int
    slot_start: int
    slot_end: int


class EncodingManifest(Contract):
    schema_version: Literal["encoded-memory-v1"] = "encoded-memory-v1"
    provider: Provider
    audience: str
    bundle_hash: str
    chunks: tuple[ManifestChunk, ...] = ()
    omitted: tuple[dict, ...] = ()


class MemorySource(Protocol):
    def read(self, query: MemoryQuery, context: ReadContext) -> MemoryBundle: ...
    def validate(self, bundle: MemoryBundle, context: ReadContext) -> bool: ...
