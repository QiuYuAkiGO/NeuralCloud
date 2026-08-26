"""runtime and cognitive migration prerequisites

Revision ID: 002_runtime_prerequisites
Revises: 001_initial
Create Date: 2026-08-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "002_runtime_prerequisites"
down_revision: str | None = "001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("episodic_events", sa.Column("uniqueness", sa.Float(), nullable=True))
    op.add_column(
        "episodic_events",
        sa.Column("tier", sa.String(length=16), server_default="short", nullable=False),
    )
    op.add_column("episodic_events", sa.Column("expire_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "episodic_events",
        sa.Column("hit_count", sa.Integer(), server_default="0", nullable=False),
    )
    op.add_column("episodic_events", sa.Column("last_hit_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("episodic_events", sa.Column("tier_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column(
        "episodic_events",
        sa.Column(
            "tier_reasons",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
    )
    op.create_index("ix_episodic_events_tier", "episodic_events", ["tier"], unique=False)
    op.create_index("ix_episodic_events_expire_at", "episodic_events", ["expire_at"], unique=False)

    op.create_table(
        "cognitive_session_turns",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("character_id", sa.String(length=64), nullable=False),
        sa.Column("session_id", sa.String(length=64), nullable=False),
        sa.Column("turn_seq", sa.BigInteger(), nullable=False),
        sa.Column("role", sa.String(length=16), nullable=False),
        sa.Column("speaker", sa.String(length=128), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=True),
        sa.Column("timestamp", sa.DateTime(timezone=True), nullable=False),
        sa.Column("cognitive_revision", sa.BigInteger(), nullable=False),
        sa.Column(
            "evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'[]'::jsonb"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "character_id",
            "session_id",
            "turn_seq",
            name="uq_cognitive_session_turn_seq",
        ),
    )
    op.create_index(
        "ix_cognitive_session_turns_character_id",
        "cognitive_session_turns",
        ["character_id"],
        unique=False,
    )
    op.create_index(
        "ix_cognitive_session_turns_session_id",
        "cognitive_session_turns",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        "ix_cognitive_session_turns_event_id",
        "cognitive_session_turns",
        ["event_id"],
        unique=False,
    )

    op.create_table(
        "cognitive_state",
        sa.Column("character_id", sa.String(length=64), nullable=False),
        sa.Column("revision", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("acknowledged_receive_seq", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("acknowledged_event_id", sa.String(length=128), nullable=True),
        sa.Column(
            "component_revisions",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("character_id"),
    )

    op.create_table(
        "cognitive_command_ledger",
        sa.Column("command_id", sa.String(length=64), nullable=False),
        sa.Column("character_id", sa.String(length=64), nullable=False),
        sa.Column("command_type", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("request_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("event_id", sa.String(length=128), nullable=False),
        sa.Column("receive_seq", sa.BigInteger(), nullable=False),
        sa.Column("correlation_id", sa.String(length=128), nullable=False),
        sa.Column("expected_revision", sa.BigInteger(), nullable=True),
        sa.Column("committed_revision", sa.BigInteger(), nullable=True),
        sa.Column("acknowledged_receive_seq", sa.BigInteger(), nullable=True),
        sa.Column("response_payload", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("command_id"),
        sa.UniqueConstraint(
            "character_id",
            "command_type",
            "idempotency_key",
            name="uq_cognitive_command_idempotency",
        ),
    )
    op.create_index(
        "ix_cognitive_command_ledger_character_id",
        "cognitive_command_ledger",
        ["character_id"],
        unique=False,
    )
    op.create_index(
        "ix_cognitive_command_ledger_status",
        "cognitive_command_ledger",
        ["status"],
        unique=False,
    )
    op.create_index(
        "ix_cognitive_command_ledger_event_id",
        "cognitive_command_ledger",
        ["event_id"],
        unique=False,
    )
    op.create_index(
        "ix_cognitive_command_ledger_correlation_id",
        "cognitive_command_ledger",
        ["correlation_id"],
        unique=False,
    )

    op.create_table(
        "cognitive_maintenance_ledger",
        sa.Column("job_id", sa.String(length=64), nullable=False),
        sa.Column("character_id", sa.String(length=64), nullable=False),
        sa.Column("job_type", sa.String(length=96), nullable=False),
        sa.Column("idempotency_key", sa.String(length=256), nullable=False),
        sa.Column("target_ref", sa.String(length=128), nullable=True),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column(
            "payload",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("base_revision", sa.BigInteger(), nullable=False),
        sa.Column("committed_revision", sa.BigInteger(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("NOW()"), nullable=False),
        sa.PrimaryKeyConstraint("job_id"),
        sa.UniqueConstraint(
            "character_id",
            "job_type",
            "idempotency_key",
            name="uq_cognitive_maintenance_idempotency",
        ),
    )
    op.create_index(
        "ix_cognitive_maintenance_ledger_character_id",
        "cognitive_maintenance_ledger",
        ["character_id"],
        unique=False,
    )
    op.create_index(
        "ix_cognitive_maintenance_ledger_status",
        "cognitive_maintenance_ledger",
        ["status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_cognitive_maintenance_ledger_status", table_name="cognitive_maintenance_ledger")
    op.drop_index("ix_cognitive_maintenance_ledger_character_id", table_name="cognitive_maintenance_ledger")
    op.drop_table("cognitive_maintenance_ledger")

    op.drop_index("ix_cognitive_command_ledger_correlation_id", table_name="cognitive_command_ledger")
    op.drop_index("ix_cognitive_command_ledger_event_id", table_name="cognitive_command_ledger")
    op.drop_index("ix_cognitive_command_ledger_status", table_name="cognitive_command_ledger")
    op.drop_index("ix_cognitive_command_ledger_character_id", table_name="cognitive_command_ledger")
    op.drop_table("cognitive_command_ledger")

    op.drop_table("cognitive_state")

    op.drop_index("ix_cognitive_session_turns_event_id", table_name="cognitive_session_turns")
    op.drop_index("ix_cognitive_session_turns_session_id", table_name="cognitive_session_turns")
    op.drop_index("ix_cognitive_session_turns_character_id", table_name="cognitive_session_turns")
    op.drop_table("cognitive_session_turns")

    op.drop_index("ix_episodic_events_expire_at", table_name="episodic_events")
    op.drop_index("ix_episodic_events_tier", table_name="episodic_events")
    op.drop_column("episodic_events", "tier_reasons")
    op.drop_column("episodic_events", "tier_updated_at")
    op.drop_column("episodic_events", "last_hit_at")
    op.drop_column("episodic_events", "hit_count")
    op.drop_column("episodic_events", "expire_at")
    op.drop_column("episodic_events", "tier")
    op.drop_column("episodic_events", "uniqueness")
