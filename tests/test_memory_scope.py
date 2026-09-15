from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from sqlalchemy.dialects import postgresql

from gui.presets import CharacterPreset, atomic_write_json, hydrate_memory, snapshot_memory
from harness.core.exceptions import InvalidCuratorOpError
from harness.core.models import (
    CandidateMemory, CuratorOp, CuratorOutput, EpisodicEvent, MemoryScope, RetrievalPlan,
    RetrievalQuery, SemanticFact,
)
from harness.memory.service import build_inmemory_service
from harness.memory.store.postgres import PostgresCandidateStore, PostgresEpisodicStore, PostgresSemanticStore
from harness.memory.store.schema import EpisodicEventRow, MemoryCandidateRow, SemanticFactRow


NOW = datetime(2026, 9, 14, 12, tzinfo=UTC)


def scope(group="100", owner="misty"):
    return MemoryScope(owner_id=owner, channel="qq_group", conversation_id=group,
                       visibility=["local:main", f"qq:group:{group}"])


def event(identifier, *, boundary=None, source=None, embedding=None):
    return EpisodicEvent(event_id=identifier, time=NOW, participants=["qq:member:200"],
                         summary="帮助解释题目", objective_fact="成员帮助解释题目", importance=0.5,
                         emotional_valence=0.2, evidence=[identifier], scope=boundary,
                         source_refs=[source] if source else [], embedding=embedding,
                         channel="qq_group" if boundary else "chat")


def fact(identifier, *, boundary=None, confidence=0.7):
    return SemanticFact(fact_id=identifier, subject="qq:member:200", predicate="helps",
                        value="解释题目", confidence=confidence, supporting_events=[identifier],
                        formed_at=NOW, scope=boundary)


def test_scope_filters_before_pool_limit_and_survives_fallback(service):
    # Many higher scoring private memories must not crowd out the authorized row.
    for i in range(20):
        service.stores.episodic.insert(event(f"private-{i}", embedding=[1.0, 0.0]))
        service.stores.semantic.insert(fact(f"private-fact-{i}", confidence=1.0))
    service.stores.episodic.insert(event("group-event", boundary=scope(), embedding=[0.0, 1.0]))
    service.stores.semantic.insert(fact("group-fact", boundary=scope(), confidence=0.1))
    service.stores.episodic.insert(event("other-group", boundary=scope("101"), embedding=[1.0, 0.0]))
    service.stores.semantic.insert(fact("other-owner", boundary=scope(owner="other")))
    selected = service.stores.episodic.search_pool(query_embedding=[1.0, 0.0], since=None, until=None,
        channels=None, entities=None, limit=1, trusted_audience="qq:group:100")
    assert [row.event_id for row in selected] == ["group-event"]
    assert [row.fact_id for row in service.stores.semantic.search_pool(query_embedding=None, subject=None,
        limit=1, trusted_audience="qq:group:100")] == ["group-fact"]
    query = RetrievalQuery(text="帮助", now=NOW + timedelta(days=4), trusted_audience="qq:group:100", top_k=10)
    found = service.hybrid.search(query, RetrievalPlan(time_scope="yesterday", channels=["missing"]),
                                  query_embedding=[1.0, 0.0])
    assert {row.ref for row in found} == {"group-event", "group-fact"}
    assert "channel:fallback" in next(row.why_retrieved for row in found if row.ref == "group-event")


def test_legacy_scope_defaults_local_and_visibility_does_not_cross_owner(service):
    service.stores.semantic.insert(fact("legacy"))
    service.stores.semantic.insert(fact("other-owner", boundary=scope(owner="other")))
    kwargs = dict(query_embedding=None, subject=None, limit=10)
    assert [row.fact_id for row in service.stores.semantic.search_pool(**kwargs)] == ["legacy"]
    assert service.stores.semantic.search_pool(**kwargs, trusted_audience="qq:group:100") == []


def _add(subject="qq:member:200", predicate="helps", confidence=0.15):
    return CuratorOutput(ops=[CuratorOp(op="ADD", type="candidate", subject=subject, predicate=predicate,
        value="解释题目", claim="愿意解释题目", confidence=confidence)])


def test_equal_claims_do_not_merge_people_predicates_or_groups(service):
    writer = service.pipeline
    writer.apply(_add(), event=event("one", boundary=scope()), now=NOW)
    writer.apply(_add(subject="qq:member:201"), event=event("two", boundary=scope()), now=NOW)
    writer.apply(_add(predicate="dislikes"), event=event("three", boundary=scope()), now=NOW)
    writer.apply(_add(), event=event("four", boundary=scope("101")), now=NOW)
    assert len(service.stores.candidates.list_open()) == 4
    writer.apply(_add(), event=event("five", boundary=scope()), now=NOW)
    assert len(service.stores.candidates.list_open()) == 4
    first = service.stores.candidates.list_open()[0]
    assert first.evidence == ["one", "five"]


def test_source_alias_replay_never_increases_candidate_or_semantic_confidence(service):
    writer = service.pipeline
    original = event("one", boundary=scope(), source="protocol-message:100:200:1")
    writer.apply(_add(confidence=0.2), event=original, now=NOW)
    candidate = service.stores.candidates.list_open()[0]
    update = CuratorOutput(ops=[CuratorOp(op="UPDATE", type="candidate", ref=candidate.candidate_id,
                                         confidence_delta=0.3)])
    replay = event("another-observation", boundary=scope(), source=original.source_refs[0])
    writer.apply(update, event=replay, now=NOW)
    assert service.stores.candidates.get(candidate.candidate_id).confidence == 0.2
    writer.apply(update, event=event("new", boundary=scope(), source="protocol-message:100:200:2"), now=NOW)
    assert service.stores.candidates.get(candidate.candidate_id).confidence == 0.5
    derived = fact("derived", boundary=scope()).model_copy(update={"source_refs": original.source_refs})
    service.stores.semantic.insert(derived)
    writer.apply(CuratorOutput(ops=[CuratorOp(op="UPDATE", type="semantic", ref="derived",
        confidence_delta=0.2)]), event=replay, now=NOW)
    assert service.stores.semantic.get("derived").confidence == 0.7


def test_direct_update_and_delete_cannot_cross_scope(service):
    service.pipeline.apply(_add(), event=event("first", boundary=scope()), now=NOW)
    candidate = service.stores.candidates.list_open()[0]
    for operation in ("UPDATE", "DELETE"):
        with pytest.raises(InvalidCuratorOpError, match="scope"):
            service.pipeline.apply(CuratorOutput(ops=[CuratorOp(op=operation, type="candidate",
                ref=candidate.candidate_id, confidence_delta=0.3)]),
                event=event("other", boundary=scope("101")), now=NOW)
    assert service.stores.candidates.get(candidate.candidate_id) == candidate


def test_consolidation_preserves_scope_and_does_not_merge_other_group(service):
    for group in ("100", "101"):
        service.pipeline.apply(_add(confidence=0.8), event=event(group, boundary=scope(group), source=group), now=NOW)
    facts = service.stores.semantic.list_all()
    assert len(facts) == 2
    assert {row.scope.conversation_id for row in facts} == {"100", "101"}
    assert {tuple(row.source_refs) for row in facts} == {("100",), ("101",)}
    service.pipeline.apply(_add(confidence=1.0),
        event=event("replay", boundary=scope(), source="100"), now=NOW)
    assert service.stores.semantic.find("qq:member:200", "helps", "解释题目", scope=scope()).confidence == 0.8


def test_preset_state_roundtrip_and_no_shared_mutable_receipts(service):
    service.qq_memory_state = {"version": 1, "receipts": [{"source": "message-1"}], "impressions": {"200": "初印象"}}
    row = event("rich", boundary=scope(), source="message-1").model_copy(update={
        "strength": 0.4, "strength_updated_at": NOW, "last_decay_review_at": NOW,
        "decay_demoted": True, "processing_version": "v1"})
    service.stores.episodic.insert(row)
    preset = snapshot_memory(service, preset_id="misty", session_id="test", transcript=[])
    decoded = CharacterPreset.model_validate_json(preset.model_dump_json())
    restored = build_inmemory_service()
    try:
        hydrate_memory(restored, decoded)
        assert restored.qq_memory_state == service.qq_memory_state
        assert restored.stores.episodic.get("rich").model_dump(exclude={"embedding"}) == row.model_dump(exclude={"embedding"})
        restored.qq_memory_state["receipts"].clear()
        assert len(service.qq_memory_state["receipts"]) == 1
        assert len(decoded.qq_memory_state["receipts"]) == 1
        legacy = decoded.model_dump(exclude={"qq_memory_state"})
        assert CharacterPreset.model_validate(legacy).qq_memory_state == {}
    finally:
        restored.close()


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_atomic_preset_failure_preserves_original_and_cleans_temp(tmp_path, monkeypatch, failure):
    import gui.presets as presets
    path = tmp_path / "misty.json"
    original = b'{"old": true}\n'
    path.write_bytes(original)
    def fail(*args):
        raise OSError("injected failure")
    monkeypatch.setattr(presets.os, failure, fail)
    with pytest.raises(OSError, match="injected failure"):
        atomic_write_json(path, {"new": True})
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


@pytest.mark.parametrize("kind", ["episodic", "semantic"])
def test_postgres_scope_is_in_sql_before_limit_without_database(kind):
    statements = []
    class Session:
        def execute(self, statement):
            statements.append(statement)
            return SimpleNamespace(scalars=lambda: SimpleNamespace(all=lambda: []))
    if kind == "episodic":
        PostgresEpisodicStore(Session()).search_pool(query_embedding=None, since=None, until=None,
            channels=None, entities=None, limit=1, trusted_audience="qq:group:100")
    else:
        PostgresSemanticStore(Session()).search_pool(query_embedding=None, subject=None, limit=1,
                                                     trusted_audience="qq:group:100")
    compiled = statements[0].compile(dialect=postgresql.dialect())
    sql = str(compiled)
    assert sql.index("WHERE") < sql.index("ORDER BY") < sql.index("LIMIT")
    assert "owner_id" in compiled.params.values()
    assert ["qq:group:100"] in compiled.params.values()


def test_postgres_provenance_and_decay_roundtrip_without_database():
    class Session:
        def __init__(self):
            self.rows = {}

        def add(self, row):
            primary_key = {EpisodicEventRow: "event_id", SemanticFactRow: "fact_id", MemoryCandidateRow: "candidate_id"}
            if type(row) in primary_key:
                self.rows[type(row), getattr(row, primary_key[type(row)])] = row

        def get(self, cls, identifier):
            return self.rows.get((cls, identifier))

        def flush(self):
            pass

    session = Session()
    episode = event("stored", boundary=scope(), source="message-1").model_copy(update={
        "strength": 0.4, "strength_updated_at": NOW, "last_decay_review_at": NOW,
        "decay_demoted": True, "processing_version": "v1"})
    semantic = fact("fact", boundary=scope()).model_copy(update={"source_refs": ["message-1"], "processing_version": "v1"})
    candidate = CandidateMemory(candidate_id="candidate", claim="helps", source="observation", confidence=0.2,
        evidence=["stored"], created_at=NOW, updated_at=NOW, scope=scope(), source_refs=["message-1"], processing_version="v1")
    for store, item, identifier in [(PostgresEpisodicStore(session), episode, "stored"),
                                    (PostgresSemanticStore(session), semantic, "fact"),
                                    (PostgresCandidateStore(session), candidate, "candidate")]:
        store.insert(item)
        assert store.get(identifier) == item
        updated = item.model_copy(update={"source_refs": ["message-1", "message-2"], "processing_version": "v2"})
        store.update(updated)
        assert store.get(identifier) == updated


def test_scoped_memory_migration_compiles_offline():
    import importlib
    from io import StringIO
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    output = StringIO()
    context = MigrationContext.configure(dialect_name="postgresql", opts={"as_sql": True, "output_buffer": output})
    migration = importlib.import_module("infra.db.alembic.versions.003_scoped_memory")
    with Operations.context(context):
        migration.upgrade()
        migration.downgrade()
    sql = output.getvalue()
    assert "ALTER TABLE episodic_events ADD COLUMN strength FLOAT DEFAULT '1' NOT NULL" in sql
    assert "ALTER TABLE semantic_facts ADD COLUMN scope JSONB" in sql
    assert "ALTER TABLE memory_candidates DROP COLUMN scope" in sql
