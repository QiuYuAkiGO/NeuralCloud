"""SQLAlchemy schema for the six memory/state layers plus writer audit tables."""

from __future__ import annotations

from datetime import datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    Float,
    Integer,
    String,
    Text,
    func,
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
    embedding = mapped_column(Vector(DEFAULT_EMBEDDING_DIM), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SemanticFactRow(Base):
    __tablename__ = "semantic_facts"

    fact_id: Mapped[str] = mapped_column(String(64), primary_key=True)
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

