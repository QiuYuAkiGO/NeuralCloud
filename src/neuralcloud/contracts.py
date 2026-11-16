"""Owner-defined v1 contracts; exported as JSON Schema by the maintenance CLI."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, create_model, model_validator


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


class ContentPerception(Contract):
    source_uri: str = Field(min_length=1, max_length=2048)
    coverage: Literal["title", "description", "subtitles", "frames", "audio", "complete"]
    observed_fraction: float = Field(ge=0, le=1, allow_inf_nan=False)


class TextRange(Contract):
    """Half-open byte range in the extracted document, including its exact returned digest."""

    unit: Literal["utf8_bytes"]
    start: Literal[0]
    end: int = Field(gt=0, le=30000)
    total: int = Field(gt=0, le=1048576)
    truncated: bool
    sha256: str = Field(pattern="^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def valid_range(self):
        if self.end > self.total or self.truncated != (self.end < self.total):
            raise ValueError("inconsistent document byte range")
        return self


class DocumentPerception(ContentPerception):
    coverage: Literal["document_text"]
    text_range: TextRange

    @model_validator(mode="after")
    def valid_fraction(self):
        if abs(self.observed_fraction - self.text_range.end / self.text_range.total) > 1e-12:
            raise ValueError("document fraction does not match byte range")
        return self


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
    kind: Literal["observation", "self_expression", "summary", "relationship", "persona_history"] = "observation"
    privacy: Literal["public", "private", "sensitive"] = "private"
    participants: list[str] = Field(default_factory=list, max_length=64)
    aliases: list[str] = Field(default_factory=list, max_length=64)
    source_ids: list[str] = Field(default_factory=list, max_length=128)
    pinned: bool = False
    provenance: LegacyProvenance | None = None
    perception: DocumentPerception | ContentPerception | None = None


class ChannelContext(Contract):
    platform: Literal["qq", "qqbot"]
    account_id: str = Field(min_length=1, max_length=128)
    message_id: str = Field(min_length=1, max_length=128)
    sender_id: str = Field(min_length=1, max_length=128)
    conversation_id: str = Field(min_length=1, max_length=256)
    mentions: list[str] = Field(default_factory=list, max_length=64)
    reply_to: str | None = Field(default=None, max_length=128)
    nickname: str | None = Field(default=None, max_length=128)
    group_card: str | None = Field(default=None, max_length=128)


class SourceEventV2(SourceEvent):
    # Separate model preserves the exact canonical bytes and receipts of v1.
    schema_version: Literal[2] = 2
    channel: ChannelContext


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


class RecallSubject(Contract):
    platform: Literal["qq", "qqbot"]
    account_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_:-]+$")


class RecallConstraints(Contract):
    conversation_id: str | None = Field(default=None, max_length=256)
    since: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    until: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    phrases: list[str] = Field(default_factory=list, max_length=8)


class EvidenceAssessment(Contract):
    state: Literal["DIRECT", "INFERRED", "RELATED", "CONFLICT", "NOT_FOUND", "INCOMPLETE"]
    sufficient: bool
    absence_confirmed: Literal[False] = False
    reason: str


class WorkflowEvidence(Contract):
    entity: str | None = None
    predicate: str
    value: str
    basis: Literal["direct", "inferred", "context"]
    confirmation: str
    references: list[dict] = Field(default_factory=list)
    scope: str | None = None
    occurred_at: float
    conflict: bool = False
    eligible: bool = False
    extractor_version: str | None = None
    temporal_status: Literal["observed_at_only"] = "observed_at_only"


class Recall(Contract):
    query: str = Field(min_length=1, max_length=256)
    limit: int = Field(default=10, ge=1, le=50)
    subject: RecallSubject | None = None
    mode: Literal["auto", "legacy", "keyword", "semantic", "hybrid"] = "auto"
    intent: Literal["auto", "identity", "fact", "event", "cause", "topic"] = "auto"
    targets: list[RecallSubject] = Field(default_factory=list, max_length=8)
    constraints: RecallConstraints | None = None
    workflow: Literal["enabled", "compat"] = "enabled"


SourceInfo = create_model(
    "SourceInfo",
    __base__=Contract,
    **{name: (field.annotation, field) for name, field in SourceEvent.model_fields.items() if name != "content"},
)


SourceInfoV2 = create_model(
    "SourceInfoV2",
    __base__=Contract,
    **{name: (field.annotation, field) for name, field in SourceEventV2.model_fields.items() if name != "content"},
)


class DurableReceipt(Contract):
    status: Literal["DURABLE"]
    seq: int
    record_id: str
    revision: int


class MissingReceipt(Contract):
    status: Literal["NOT_FOUND"]


class IdentityEvidence(Contract):
    platform: str
    account_id: str
    confirmation: Literal["operator_confirmed", "self_reported", "third_party"]
    name: str
    relation: str
    conflict: bool


class RetrievalEvidence(Contract):
    score: float
    semantic: bool
    keyword: bool
    start: int | None = None
    end: int | None = None


class RecallDiagnostics(Contract):
    mode: str
    semantic_requested: bool
    degraded: str | None = None
    identity_count: int
    keyword_candidates: int
    semantic_candidates: int
    elapsed_ms: float
    workflow_version: str | None = None
    index_generation: int | None = None
    stages: list[dict] | None = None
    rounds: int | None = None
    stop_reason: str | None = None
    cache_hit: bool = False


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
    source: SourceInfo | SourceInfoV2 | None = None
    identity: IdentityEvidence | None = None
    retrieval: RetrievalEvidence | None = None
    evidence: WorkflowEvidence | None = None


class RecallResult(Contract):
    status: Literal["OK"]
    scope: Literal["GLOBAL_ONLINE"]
    generation: int
    items: list[RecordView]
    coverage: str
    diagnostics: RecallDiagnostics | None = None
    assessment: EvidenceAssessment | None = None


def canonical(value: object) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()
