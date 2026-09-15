"""Persist scoped memory provenance and decay state.

Revision ID: 003_scoped_memory
Revises: 002_runtime_prerequisites
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "003_scoped_memory"
down_revision = "002_runtime_prerequisites"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("episodic_events", "semantic_facts", "memory_candidates"):
        op.add_column(table, sa.Column("scope", postgresql.JSONB(none_as_null=True), nullable=True))
        op.add_column(table, sa.Column("source_refs", postgresql.JSONB(), nullable=False,
                                      server_default=sa.text("'[]'::jsonb")))
        op.add_column(table, sa.Column("processing_version", sa.String(128), nullable=True))
    op.add_column("episodic_events", sa.Column("strength", sa.Float(), nullable=False, server_default="1"))
    op.add_column("episodic_events", sa.Column("strength_updated_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("episodic_events", sa.Column("last_decay_review_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("episodic_events", sa.Column("decay_demoted", sa.Boolean(), nullable=False,
                                             server_default=sa.text("false")))


def downgrade() -> None:
    for name in ("decay_demoted", "last_decay_review_at", "strength_updated_at", "strength"):
        op.drop_column("episodic_events", name)
    for table in ("memory_candidates", "semantic_facts", "episodic_events"):
        for name in ("processing_version", "source_refs", "scope"):
            op.drop_column(table, name)
