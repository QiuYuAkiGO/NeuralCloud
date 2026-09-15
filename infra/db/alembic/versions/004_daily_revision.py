"""Persist daily revision metadata and scoped relationship provenance.

Revision ID: 004_daily_revision
Revises: 003_scoped_memory
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "004_daily_revision"
down_revision = "003_scoped_memory"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table in ("episodic_events", "semantic_facts", "memory_candidates", "relationship_states"):
        op.add_column(table, sa.Column("revision_metadata", postgresql.JSONB(), nullable=False,
                                      server_default=sa.text("'{}'::jsonb")))
    op.add_column("relationship_states", sa.Column("scope", postgresql.JSONB(none_as_null=True), nullable=True))
    op.add_column("relationship_states", sa.Column("source_refs", postgresql.JSONB(), nullable=False,
                                                  server_default=sa.text("'[]'::jsonb")))


def downgrade() -> None:
    for name in ("source_refs", "scope"):
        op.drop_column("relationship_states", name)
    for table in ("relationship_states", "memory_candidates", "semantic_facts", "episodic_events"):
        op.drop_column(table, "revision_metadata")
