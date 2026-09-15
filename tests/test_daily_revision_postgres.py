"""Postgres DTO round-trips and migration SQL without a live production database."""
from datetime import datetime, timezone
import importlib
from io import StringIO
from types import SimpleNamespace

from alembic.migration import MigrationContext
from alembic.operations import Operations
import pytest
from sqlalchemy.dialects import postgresql

from harness.core.models import CandidateMemory, EpisodicEvent, MemoryScope, RelationshipState, SemanticFact
from harness.memory.store.postgres import (
    PostgresCandidateStore, PostgresEpisodicStore, PostgresRelationshipStore, PostgresSemanticStore,
)
from harness.memory.store.schema import EpisodicEventRow, MemoryCandidateRow, RelationshipStateRow, SemanticFactRow


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
SCOPE = MemoryScope(channel="qq_group", conversation_id="qq:group:100", visibility=["local:main", "qq:group:100"])
METADATA = {"batch_id": "revision-one", "status": "current", "evidence_keys": ["qq-message:999:100:200:1"]}


class RowSession:
    """Exercise the actual repository methods against detached SQLAlchemy rows."""
    def __init__(self):
        self.rows = []

    def add(self, row):
        self.rows.append(row)

    def get(self, cls, identifier):
        name = {EpisodicEventRow: "event_id", SemanticFactRow: "fact_id",
                MemoryCandidateRow: "candidate_id", RelationshipStateRow: "relation_id"}[cls]
        return next((row for row in self.rows if type(row) is cls and getattr(row, name) == identifier), None)

    def execute(self, statement):
        cls = statement.column_descriptions[0]["entity"]
        rows = [row for row in self.rows if type(row) is cls]
        for criterion in statement._where_criteria:
            field = criterion.left.name
            value = criterion.right.value
            rows = [row for row in rows if getattr(row, field) == value]
        return SimpleNamespace(scalar_one_or_none=lambda: rows[0] if rows else None,
                               scalars=lambda: SimpleNamespace(all=lambda: rows))

    def delete(self, row):
        self.rows.remove(row)

    def flush(self):
        pass


@pytest.mark.parametrize("kind", ["episodic", "semantic", "candidate", "relationship"])
def test_revision_metadata_scope_and_sources_roundtrip_on_insert_update_and_delete(kind):
    common = {"scope": SCOPE, "source_refs": ["source-one"], "revision_metadata": METADATA}
    session = RowSession()
    if kind == "episodic":
        store = PostgresEpisodicStore(session)
        value = EpisodicEvent(event_id="row", time=NOW, participants=["qq:member:200"],
            summary="原始行为", objective_fact="原始行为", importance=.5, emotional_valence=0,
            evidence=["original-one"], **common)
    elif kind == "semantic":
        store = PostgresSemanticStore(session)
        value = SemanticFact(fact_id="row", subject="qq:member:200", predicate="preference", value="阅读",
            confidence=.8, supporting_events=["original-one"], formed_at=NOW, **common)
    elif kind == "candidate":
        store = PostgresCandidateStore(session)
        value = CandidateMemory(candidate_id="row", claim="喜欢阅读", source="original-one", confidence=.3,
            evidence=["original-one"], created_at=NOW, updated_at=NOW, **common)
    else:
        store = PostgresRelationshipStore(session)
        value = RelationshipState(relation_id="relation-one", target="row", updated_at=NOW, **common)
    insert = store.upsert if kind == "relationship" else store.insert
    update = store.upsert if kind == "relationship" else store.update
    insert(value)
    restored = store.get("row")
    assert restored == value
    restored.revision_metadata["evidence_keys"].append("mutated-copy")
    assert store.get("row").revision_metadata["evidence_keys"] == METADATA["evidence_keys"]
    changed = value.model_copy(update={"source_refs": ["source-two"],
        "revision_metadata": {"batch_id": "revision-two", "status": "disputed"}})
    update(changed)
    assert store.get("row") == changed
    assert store.list_all() == [changed]
    store.delete("row")
    store.delete("row")
    assert store.get("row") is None
    assert session.rows == []  # Candidate evidence links are removed too.


@pytest.mark.parametrize("query_embedding", [None, [.1, .2]])
def test_semantic_conflict_status_is_filtered_in_postgres_sql_before_order_and_limit(query_embedding):
    statements = []

    def execute(statement):
        statements.append(statement)
        return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))

    PostgresSemanticStore(SimpleNamespace(execute=execute)).search_pool(
        query_embedding=query_embedding, subject=None, limit=1, trusted_audience="qq:group:100")
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    where = sql[sql.index("WHERE"):sql.index("ORDER BY")]
    assert "revision_metadata" in where and "IS NULL" in where and "NOT IN" in where
    assert ["superseded", "disputed"] in compiled.params.values()
    assert ["qq:group:100"] in compiled.params.values()
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")


def test_daily_revision_migration_compiles_offline_and_preserves_legacy_defaults():
    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    migration = importlib.import_module("infra.db.alembic.versions.004_daily_revision")
    assert migration.down_revision == "003_scoped_memory"
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    for table in ("episodic_events", "semantic_facts", "memory_candidates", "relationship_states"):
        assert f"ALTER TABLE {table} ADD COLUMN revision_metadata JSONB DEFAULT '{{}}'::jsonb NOT NULL" in sql
        assert f"ALTER TABLE {table} DROP COLUMN revision_metadata" in sql
    assert "ALTER TABLE relationship_states ADD COLUMN scope JSONB" in sql
    assert "ALTER TABLE relationship_states ADD COLUMN source_refs JSONB DEFAULT '[]'::jsonb NOT NULL" in sql
