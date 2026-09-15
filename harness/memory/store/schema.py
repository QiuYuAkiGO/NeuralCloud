"""SQLAlchemy schema for the six memory/state layers plus writer audit tables."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text as sql_text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from harness.config import DEFAULT_EMBEDDING_DIM


class Base(DeclarativeBase):
    pass


class PersonaCoreRow(Base):
    __tablename__ = "persona_core"

    persona_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    identity: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    locked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class EpisodicEventRow(Base):
    __tablename__ = "episodic_events"

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb"))
    scope: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb"))
    processing_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    strength: Mapped[float] = mapped_column(Float, nullable=False, server_default="1")
    strength_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_decay_review_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decay_demoted: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=sql_text("false"))
    time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    participants: Mapped[list] = mapped_column(JSONB, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    character_action: Mapped[str | None] = mapped_column(Text, nullable=True)
    outcome: Mapped[str | None] = mapped_column(Text, nullable=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    emotional_valence: Mapped[float] = mapped_column(Float, nullable=False)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False)
    objective_fact: Mapped[str] = mapped_column(Text, nullable=False)
    persona_conditioned_insight: Mapped[str | None] = mapped_column(Text, nullable=True)
    insight_confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    supporting_evidence: Mapped[list] = mapped_column(JSONB, nullable=False, default=list)
    channel: Mapped[str] = mapped_column(String(32), nullable=False, default="chat")
    event_type: Mapped[str | None] = mapped_column(String(64), nullable=True)
    session_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    uniqueness: Mapped[float | None] = mapped_column(Float, nullable=True)
    tier: Mapped[str] = mapped_column(String(16), nullable=False, server_default="short", index=True)
    expire_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    hit_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    last_hit_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tier_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tier_reasons: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sql_text("'[]'::jsonb"),
    )
    embedding = mapped_column(Vector(DEFAULT_EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SemanticFactRow(Base):
    __tablename__ = "semantic_facts"

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb"))
    scope: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb"))
    processing_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    subject: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    predicate: Mapped[str] = mapped_column(String(128), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    supporting_events: Mapped[list] = mapped_column(JSONB, nullable=False)
    formed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    embedding = mapped_column(Vector(DEFAULT_EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class RelationshipStateRow(Base):
    __tablename__ = "relationship_states"

    relation_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    scope: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb"))
    revision_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb"))
    target: Mapped[str] = mapped_column(String(128), nullable=False, unique=True)
    closeness: Mapped[float] = mapped_column(Float, nullable=False)
    trust: Mapped[float] = mapped_column(Float, nullable=False)
    attachment: Mapped[float] = mapped_column(Float, nullable=False)
    recent_tension: Mapped[float] = mapped_column(Float, nullable=False)
    shared_history_summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    last_major_event: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class PsychStateRow(Base):
    __tablename__ = "psych_states"

    character_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    long_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    mid_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    short_state: Mapped[dict] = mapped_column(JSONB, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class WorldStateRow(Base):
    __tablename__ = "world_states"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    captured_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)


class MemoryCandidateRow(Base):
    __tablename__ = "memory_candidates"

    candidate_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision_metadata: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict, server_default=sql_text("'{}'::jsonb"))
    scope: Mapped[dict | None] = mapped_column(JSONB(none_as_null=True), nullable=True)
    source_refs: Mapped[list] = mapped_column(JSONB, nullable=False, default=list, server_default=sql_text("'[]'::jsonb"))
    processing_version: Mapped[str | None] = mapped_column(String(128), nullable=True)
    claim: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    evidence: Mapped[list] = mapped_column(JSONB, nullable=False)
    subject: Mapped[str | None] = mapped_column(String(128), nullable=True)
    predicate: Mapped[str | None] = mapped_column(String(128), nullable=True)
    value: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consolidated_fact_id: Mapped[str | None] = mapped_column(String(64), nullable=True)


class EvidenceLinkRow(Base):
    __tablename__ = "evidence_links"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    candidate_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(64), nullable=False)
    added_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryOpsLogRow(Base):
    __tablename__ = "memory_ops_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    op: Mapped[str] = mapped_column(String(32), nullable=False)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False)
    target_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    detail: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SessionTurnRow(Base):
    """Committed Cognitive session projection; prepared output is not stored here."""

    __tablename__ = "cognitive_session_turns"
    __table_args__ = (
        UniqueConstraint("character_id", "session_id", "turn_seq", name="uq_cognitive_session_turn_seq"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    character_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    session_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    turn_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    role: Mapped[str] = mapped_column(String(16), nullable=False)
    speaker: Mapped[str | None] = mapped_column(String(128), nullable=True)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    event_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    cognitive_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    evidence: Mapped[list] = mapped_column(
        JSONB,
        nullable=False,
        default=list,
        server_default=sql_text("'[]'::jsonb"),
    )


class CognitiveStateRow(Base):
    """Monotonic Cognitive revision and acknowledged Runtime event cursor."""

    __tablename__ = "cognitive_state"

    character_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    acknowledged_receive_seq: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default="0")
    acknowledged_event_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    component_revisions: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'::jsonb"),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CognitiveCommandLedgerRow(Base):
    """Stable result for a Cognitive command idempotency key."""

    __tablename__ = "cognitive_command_ledger"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "command_type",
            "idempotency_key",
            name="uq_cognitive_command_idempotency",
        ),
    )

    command_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    command_type: Mapped[str] = mapped_column(String(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    event_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    receive_seq: Mapped[int] = mapped_column(BigInteger, nullable=False)
    correlation_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    expected_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    committed_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    acknowledged_receive_seq: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    response_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class CognitiveMaintenanceLedgerRow(Base):
    """Recoverable ownership record for memory tier and other background work."""

    __tablename__ = "cognitive_maintenance_ledger"
    __table_args__ = (
        UniqueConstraint(
            "character_id",
            "job_type",
            "idempotency_key",
            name="uq_cognitive_maintenance_idempotency",
        ),
    )

    job_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    character_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    job_type: Mapped[str] = mapped_column(String(96), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(256), nullable=False)
    target_ref: Mapped[str | None] = mapped_column(String(128), nullable=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    payload: Mapped[dict] = mapped_column(
        JSONB,
        nullable=False,
        default=dict,
        server_default=sql_text("'{}'::jsonb"),
    )
    base_revision: Mapped[int] = mapped_column(BigInteger, nullable=False)
    committed_revision: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


PERSONA_LOCK_FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION prevent_persona_mutation()
RETURNS TRIGGER AS $$
BEGIN
    RAISE EXCEPTION 'Persona Core is locked and cannot be modified';
END;
$$ LANGUAGE plpgsql;
"""

PERSONA_LOCK_TRIGGER_SQL = """
DROP TRIGGER IF EXISTS persona_core_no_update ON persona_core;
CREATE TRIGGER persona_core_no_update
BEFORE UPDATE OR DELETE ON persona_core
FOR EACH ROW EXECUTE FUNCTION prevent_persona_mutation();
"""

PERSONA_LOCK_SQL = PERSONA_LOCK_FUNCTION_SQL + PERSONA_LOCK_TRIGGER_SQL

