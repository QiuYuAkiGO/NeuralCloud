"""initial memory schema

Revision ID: 001_initial
Revises:
Create Date: 2026-08-21
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

VECTOR_DIM = 1024


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "persona_core",
        sa.Column("persona_id", sa.String(64), primary_key=True),
        sa.Column("version", sa.String(32), nullable=False),
        sa.Column("identity", postgresql.JSONB, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("locked", sa.Boolean, nullable=False, server_default=sa.text("true")),
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION prevent_persona_mutation()
        RETURNS TRIGGER AS $$
        BEGIN
            RAISE EXCEPTION 'Persona Core is locked and cannot be modified';
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    op.execute(
        """
        CREATE TRIGGER persona_core_no_update
        BEFORE UPDATE OR DELETE ON persona_core
        FOR EACH ROW EXECUTE FUNCTION prevent_persona_mutation();
        """
    )

    op.create_table(
        "episodic_events",
        sa.Column("event_id", sa.String(64), primary_key=True),
        sa.Column("time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("participants", postgresql.JSONB, nullable=False),
        sa.Column("summary", sa.Text, nullable=False),
        sa.Column("character_action", sa.Text, nullable=True),
        sa.Column("outcome", sa.Text, nullable=True),
        sa.Column("importance", sa.Float, nullable=False),
        sa.Column("emotional_valence", sa.Float, nullable=False),
        sa.Column("evidence", postgresql.JSONB, nullable=False),
        sa.Column("objective_fact", sa.Text, nullable=False),
        sa.Column("persona_conditioned_insight", sa.Text, nullable=True),
        sa.Column("insight_confidence", sa.Float, nullable=True),
        sa.Column(
            "supporting_evidence",
            postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("channel", sa.String(32), nullable=False, server_default="chat"),
        sa.Column("event_type", sa.String(64), nullable=True),
        sa.Column("session_id", sa.String(64), nullable=True),
        sa.Column("embedding", Vector(VECTOR_DIM), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_episodic_events_time", "episodic_events", ["time"])
    op.execute(
        "CREATE INDEX ix_episodic_events_embedding ON episodic_events "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "semantic_facts",
        sa.Column("fact_id", sa.String(64), primary_key=True),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("predicate", sa.String(128), nullable=False),
        sa.Column("value", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("supporting_events", postgresql.JSONB, nullable=False),
        sa.Column("formed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("embedding", Vector(VECTOR_DIM), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )
    op.create_index("ix_semantic_facts_subject", "semantic_facts", ["subject"])
    op.execute(
        "CREATE INDEX ix_semantic_facts_embedding ON semantic_facts "
        "USING hnsw (embedding vector_cosine_ops)"
    )

    op.create_table(
        "relationship_states",
        sa.Column("relation_id", sa.String(64), primary_key=True),
        sa.Column("target", sa.String(128), nullable=False, unique=True),
        sa.Column("closeness", sa.Float, nullable=False),
        sa.Column("trust", sa.Float, nullable=False),
        sa.Column("attachment", sa.Float, nullable=False),
        sa.Column("recent_tension", sa.Float, nullable=False),
        sa.Column("shared_history_summary", sa.Text, nullable=False, server_default=""),
        sa.Column("last_major_event", sa.String(64), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "psych_states",
        sa.Column("character_id", sa.String(64), primary_key=True),
        sa.Column("long_state", postgresql.JSONB, nullable=False),
        sa.Column("mid_state", postgresql.JSONB, nullable=False),
        sa.Column("short_state", postgresql.JSONB, nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )

    op.create_table(
        "world_states",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
    )
    op.create_index("ix_world_states_captured_at", "world_states", ["captured_at"])

    op.create_table(
        "memory_candidates",
        sa.Column("candidate_id", sa.String(64), primary_key=True),
        sa.Column("claim", sa.Text, nullable=False),
        sa.Column("source", sa.Text, nullable=False),
        sa.Column("confidence", sa.Float, nullable=False),
        sa.Column("status", sa.String(32), nullable=False),
        sa.Column("evidence", postgresql.JSONB, nullable=False),
        sa.Column("subject", sa.String(128), nullable=True),
        sa.Column("predicate", sa.String(128), nullable=True),
        sa.Column("value", sa.Text, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consolidated_fact_id", sa.String(64), nullable=True),
    )
    op.create_index("ix_memory_candidates_status", "memory_candidates", ["status"])

    op.create_table(
        "evidence_links",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("candidate_id", sa.String(64), nullable=False),
        sa.Column("event_id", sa.String(64), nullable=False),
        sa.Column("added_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_evidence_links_candidate_id", "evidence_links", ["candidate_id"])

    op.create_table(
        "memory_ops_log",
        sa.Column("id", sa.Integer, primary_key=True, autoincrement=True),
        sa.Column("op", sa.String(32), nullable=False),
        sa.Column("target_type", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), nullable=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("success", sa.Boolean, nullable=False),
        sa.Column("detail", postgresql.JSONB, nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("NOW()"),
        ),
    )


def downgrade() -> None:
    op.drop_table("memory_ops_log")
    op.drop_table("evidence_links")
    op.drop_table("memory_candidates")
    op.drop_table("world_states")
    op.drop_table("psych_states")
    op.drop_table("relationship_states")
    op.drop_table("semantic_facts")
    op.drop_table("episodic_events")
    op.execute("DROP TRIGGER IF EXISTS persona_core_no_update ON persona_core")
    op.execute("DROP FUNCTION IF EXISTS prevent_persona_mutation()")
    op.drop_table("persona_core")
