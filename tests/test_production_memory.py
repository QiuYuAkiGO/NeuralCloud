"""Production memory invariants, with synthetic records and isolated SQLite."""
from datetime import datetime, timedelta, timezone
import sqlite3
from types import SimpleNamespace
import asyncio

import pytest

from harness.core.models import MemoryScope
from harness.memory.production import BeliefRevision, Experience, ProductionMemoryStore
from harness.memory.revision.recall import select_daily_recall
from harness.config import Settings
from tests.test_daily_revision_reducer import preset, run, proposal
from harness.memory.revision.models import MemoryRevision
from harness.memory.revision.reducer import build_revision
from tests.test_shared_memory import local, person, source_database
from harness.memory.shared import SharedMemoryStore


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def experience(identifier="e1", *, audience="qq:group:100", author="qq:member:200", text="分享摄影经验", **kwargs):
    private = audience.startswith("qq:private:")
    return Experience(record_id=identifier, author_id=author, text=text, occurred_at=NOW,
        scope=MemoryScope(channel="qq_private" if private else "qq_group", conversation_id=audience,
                          visibility=["local:main", audience]), source_refs=("raw:" + identifier,),
        independent_key=identifier, **kwargs)


def belief(identifier="b1", *, source="e1", revision=1, statement="愿意分享摄影经验", **kwargs):
    return BeliefRevision(record_id=identifier, subject_id="qq:member:200", topic="摄影", statement=statement,
        revision=revision, supporting_ids=(source,), valid_from=NOW, **kwargs)


@pytest.fixture
def store(tmp_path):
    return ProductionMemoryStore(tmp_path / "production.sqlite3")


def test_immutable_sources_atomic_cursors_and_reopen(store):
    assert store.ingest([experience()], stream="qq", cursor=1) == 1
    assert store.ingest([experience()], stream="qq", cursor=1) == 0
    with pytest.raises(ValueError, match="immutable"):
        store.ingest([experience("e2"), experience(text="changed")], stream="qq", cursor=2)
    assert store.cursor("qq") == 1
    assert not store.validate_references(["e2"], audience="local:main")
    reopened = ProductionMemoryStore(store.path)
    assert reopened.validate_references(["e1"], audience="qq:group:100")
    with pytest.raises(ValueError, match="owner"):
        ProductionMemoryStore(store.path, preset_id="other")


def test_stable_identity_across_scopes_without_private_inference_leak(store):
    store.ingest([experience(), experience("private", audience="qq:private:999:200", text="私密担忧")])
    store.revise(belief(), event_id="public-belief")
    store.revise(belief("combined", counter_ids=("private",), statement="私聊中透露担忧"), event_id="combined-belief")
    public = store.recall(audience="qq:group:100", subject_ids=("qq:member:200",), now=NOW)
    assert "combined" not in {row["record_id"] for row in public["entries"]}
    assert "私密" not in str(public)
    private = store.recall(audience="local:main", subject_ids=("qq:member:200",), now=NOW)
    assert "combined" in {row["record_id"] for row in private["entries"]}
    assert store.recall(audience="qq:group:101", query="摄影", now=NOW) == {}


def test_self_report_correction_immediate_idempotent_with_history(store):
    store.ingest([experience(text="我现在喜欢摄影"), experience("e2", text="我现在不喜欢摄影")])
    args = dict(audience="qq:group:100", subject_id="qq:member:200", now=NOW)
    update = {"kind": "self_report", "topic": "偏好", "statement": "对方说喜欢摄影", "source_ids": ["e1"]}
    assert store.apply_updates([update], event_id="turn1", **args)
    entry = next(row for row in store.recall(audience=args["audience"], now=NOW)["entries"] if row["kind"] == "person")
    corrected = {**update, "record_id": entry["record_id"], "statement": "对方现在说不喜欢摄影", "source_ids": ["raw:e2"]}
    assert store.apply_updates([corrected], event_id="turn2", **args)
    assert not store.apply_updates([corrected], event_id="turn2", **args)
    history = store.history(entry["record_id"])
    assert len(history) == 2
    assert history[-1]["supporting_ids"] == ["e2"]
    assert not store.validate_references([entry], audience=args["audience"], now=NOW)
    assert store.validate_references([entry["record_id"]], audience=args["audience"], now=NOW)


def test_author_mismatch_and_self_expression_cannot_create_external_belief(store):
    store.ingest([experience(), experience("self", author="character:misty", kind="self_expression")])
    with pytest.raises(ValueError, match="self_report_author"):
        store.apply_updates([{"kind": "self_report", "statement": "我喜欢摄影", "source_ids": ["e1"]}],
            event_id="bad-author", audience="qq:group:100", subject_id="qq:member:201", now=NOW)
    with pytest.raises(ValueError, match="self_expression"):
        store.revise(belief(source="self"), event_id="self-proof")


def test_read_and_decision_use_are_idempotent_without_belief_reinforcement(store):
    store.ingest([experience()])
    store.revise(belief(), event_id="belief")
    before = store.recall(audience="qq:group:100", now=NOW)
    assert store.record_use("read1", ["b1"], audience="qq:group:100")
    assert not store.record_use("read1", ["b1"], audience="qq:group:100")
    assert store.record_use("use1", ["b1"], audience="qq:group:100", kind="decision_use")
    assert store.recall(audience="qq:group:100", now=NOW) == before
    with pytest.raises(ValueError, match="outside_audience"):
        store.record_use("bad", ["b1"], audience="qq:group:101")


def test_explicit_retrieval_events_and_qq_actual_use_have_distinct_idempotent_receipts(store):
    from harness.cognitive.qq_memory import QQMemoryControl, QQMemoryWorker
    store.ingest([experience()])
    store.revise(belief(), event_id="initial")
    result = store.recall(audience="qq:group:100", now=NOW, event_id="retrieval-one")
    assert store.recall(audience="qq:group:100", now=NOW, event_id="retrieval-one") == result
    worker = object.__new__(QQMemoryWorker)
    worker.control = QQMemoryControl(recall_enabled=False)
    worker.host = SimpleNamespace(shared=SimpleNamespace(production_enabled=True, production_store=store),
                                  chat=SimpleNamespace(active_preset_id="misty"))
    worker._recall_pending = {}
    decision = SimpleNamespace(decision_id="decision", decision_source="model", outcome="ready", completed_at=NOW,
        group_id="100", recalled_memory_ids=[row["record_id"] for row in result["entries"]], adopted_memory_ids=["b1"])
    worker.note_recall(decision)
    worker.note_recall(decision)
    with sqlite3.connect(store.path) as db:
        rows = db.execute("SELECT kind,payload FROM pm_receipts WHERE kind<>'revise'").fetchall()
        assert sorted(row[0] for row in rows) == ["context_exposure", "decision_use", "retrieval_hit"]
        import json
        assert json.loads(next(row[1] for row in rows if row[0] == "decision_use"))["ids"] == ["b1"]
        assert db.execute("SELECT confidence FROM pm_records WHERE id='b1'").fetchone()[0] == pytest.approx(2 / 3)


def test_retraction_immediately_invalidates_pinned_claim_and_old_snapshot(store):
    store.ingest([experience()])
    store.revise(belief(), event_id="belief")
    store.retain("b1", event_id="pin", pinned=True)
    snapshot = store.recall(audience="qq:group:100", now=NOW)
    assert store.validate_references(snapshot["entries"], audience="qq:group:100", now=NOW)
    assert store.retract_source("e1", event_id="delete", reason="source withdrawn")
    assert not store.retract_source("e1", event_id="delete", reason="source withdrawn")
    assert not store.validate_references(snapshot["entries"], audience="qq:group:100", now=NOW)
    assert store.recall(audience="qq:group:100", now=NOW) == {}
    store.ingest([experience()])
    assert store.recall(audience="qq:group:100", now=NOW) == {}


def test_importance_requires_bound_evidence_and_retains_reason(store):
    store.ingest([experience(), experience("e2")])
    store.revise(belief(), event_id="belief")
    with pytest.raises(ValueError, match="reason_and_evidence"):
        store.retain("b1", event_id="bad")
    with pytest.raises(ValueError, match="not_bound"):
        store.retain("b1", event_id="bad2", important_reason="长期承诺", evidence_ids=["e2"])
    assert store.retain("b1", event_id="important", important_reason="对方明确作出长期承诺", evidence_ids=["e1"])
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT important FROM pm_records WHERE id='b1'").fetchone()[0] == 1


def test_bounded_sql_candidates_whole_records_and_independent_use(store):
    store.ingest([experience(str(i), text="摄影" + str(i)) for i in range(400)])
    result = store.recall(audience="qq:group:100", query="摄影", limit=3, max_characters=1800, now=NOW)
    assert 0 < len(result["entries"]) <= 3
    assert all(row["summary"].startswith("摄影") for row in result["entries"])
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM pm_receipts").fetchone()[0] == 0


def test_incremental_shared_stream_and_provider_convenience(store, tmp_path):
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    shared.record_local(local())
    shared.record_local(local(event_id="two", text="第二件事"))
    first = store.sync_shared(shared, batch_size=1)
    assert not first.exhausted
    second = store.sync_shared(shared, batch_size=1)
    assert second.exhausted and second.next_cursor > first.next_cursor
    assert not store.sync_shared(shared).records
    record_id = store.ingest_qq(person().observations[0])
    assert store.validate_references([record_id], audience="qq:group:100")
    assert store.ingest_local(local()) == first.records[0].record_id
    shared.import_qq_context(person())
    store.sync_shared(shared)
    assert store.validate_references([record_id], audience="qq:group:100")


def test_daily_revision_cannot_resurrect_forgotten_or_expired_episodes():
    from tests.test_daily_revision_reducer import NOW as source_now
    changed = build_revision(preset(), run(), proposal(memories=[MemoryRevision(kind="episodic", subject="local:main",
        statement="摄影事件", evidence_ids=["s1"])]), Settings(), now=source_now)
    for updates in ({"tier": "forgotten"}, {"tier": "short", "expire_at": source_now - timedelta(seconds=1)}):
        changed.episodic = [event.model_copy(update=updates) for event in changed.episodic]
        stores = SimpleNamespace(**{name: SimpleNamespace(list_all=lambda values=getattr(changed, field): values)
            for name, field in (("episodic", "episodic"), ("semantic", "semantic"), ("candidates", "candidates"), ("relationship", "relationships"))})
        memory = SimpleNamespace(stores=stores, daily_revision_state=changed.daily_revision_state, qq_memory_state=changed.qq_memory_state)
        assert select_daily_recall(memory, audience="local:main", now=source_now) == {}


def test_legacy_projection_preserves_original_ids_history_and_never_calls_model(store):
    from tests.test_daily_revision_reducer import NOW as source_now
    changed = build_revision(preset(), run(), proposal(memories=[MemoryRevision(kind="episodic", subject="local:main",
        statement="摄影事件", evidence_ids=["s1"])]), Settings(), now=source_now)
    assert store.import_legacy(changed)
    assert not store.import_legacy(changed)
    identifier = changed.episodic[0].event_id
    original = changed.episodic[0].model_dump(mode="json")
    changed.episodic[0] = changed.episodic[0].model_copy(update={"tier": "forgotten"})
    assert store.import_legacy(changed)
    assert store.legacy_history(identifier)[0] == original
    assert not store.validate_references([identifier], audience="local:main")


def test_live_qq_facade_uses_production_store_and_public_scope(tmp_path, monkeypatch):
    from tests.test_basic_persona_continuity import _host
    async def scenario():
        host, _ = _host(tmp_path, monkeypatch, ["嗯。"])
        try:
            host.chat.settings.production_memory_enabled = True
            host.shared.qq_settings = SimpleNamespace(memory_db=tmp_path / "unused.sqlite3",
                allowed_group_ids=["100"], allowed_private_peer_ids=[], self_member_id="999")
            monkeypatch.setattr(host.shared, "sync_qq", lambda: None)
            host.shared.store.import_qq_context(person())
            production = host.shared.production_store
            production.ingest([experience("private", audience="qq:private:999:200", text="私密事件")])
            binding = host.shared.qq_binding(group_id="100", member_id="200", query="账号")
            assert binding.derived_memory["schema_version"] == "production-memory-v1"
            assert binding.derived_memory["entries"]
            assert "私密事件" not in str(binding.derived_memory)
            assert not host.shared.qq_binding(group_id="101", member_id="200").derived_memory
            assert host.shared.validate_production_context(binding.derived_memory, audience="qq:group:100")
        finally:
            await host.close()
    asyncio.run(scenario())


def test_qq_incremental_import_advances_atomically_and_matches_full_import(tmp_path):
    source = source_database(tmp_path, segmented=True)
    shared = SharedMemoryStore(tmp_path / "incremental.sqlite3")
    before = source.read_bytes()
    reports = []
    for _ in range(10):
        result = shared.import_qq_database(source, allowed_group_ids=["100"], self_member_id="999", incremental=True, batch_size=2)
        assert result.success
        reports.append(result)
        assert result.counts_by_kind["qq_observation"] <= 2
        if not result.has_more:
            break
    assert len(reports) > 1 and sum(report.created_count for report in reports) == 27
    assert source.read_bytes() == before
    replay = shared.import_qq_database(source, allowed_group_ids=["100"], self_member_id="999", incremental=True, batch_size=2)
    assert replay.created_count == replay.selected_count == 0
    full = SharedMemoryStore(tmp_path / "full.sqlite3")
    full.import_qq_database(source, allowed_group_ids=["100"], self_member_id="999")
    assert {r.record_id for r in shared.read_after(limit=100).records} == {r.record_id for r in full.read_after(limit=100).records}


def test_failed_incremental_batch_does_not_advance_cursor(tmp_path):
    source = source_database(tmp_path, receipt_enabled=False)
    shared = SharedMemoryStore(tmp_path / "incremental.sqlite3")
    with sqlite3.connect(source) as db:
        db.execute("UPDATE qq_visual_observations SET record_json='{}' WHERE rowid=1")
    result = shared.import_qq_database(source, allowed_group_ids=["100"], self_member_id="999", incremental=True, batch_size=2)
    assert not result.success
    with sqlite3.connect(shared.path) as db:
        assert db.execute("SELECT count(*) FROM shared_memory_import_cursors").fetchone()[0] == 0
        assert db.execute("SELECT count(*) FROM shared_memory_entries").fetchone()[0] == 0


def test_duplicate_source_identity_does_not_count_twice_or_accept_changed_text(store):
    first = experience()
    duplicate = first.model_copy(update={"record_id": "same-observed-event-again"})
    store.ingest([first, duplicate])
    store.revise(BeliefRevision(
        record_id="dedup-belief", subject_id=first.author_id, topic="摄影", statement="分享经验", revision=1,
        supporting_ids=(first.record_id, duplicate.record_id), valid_from=NOW), event_id="dedup")
    entry = next(row for row in store.recall(audience="qq:group:100", now=NOW)["entries"] if row["kind"] == "person")
    assert entry["legacy_confidence"] == pytest.approx(2 / 3)
    with pytest.raises(ValueError, match="independent_source_identity"):
        store.ingest([first.model_copy(update={"record_id": "conflicting-copy", "text": "changed source"})])
    store.delete_source(first.record_id, event_id="delete-all-copies")
    assert not store.validate_references([duplicate.record_id], audience="local:main")
    assert not store.validate_references(["dedup-belief"], audience="local:main")
    store.ingest([first.model_copy(update={"record_id": "late-copy"})])
    assert not store.validate_references(["late-copy"], audience="local:main")


@pytest.mark.parametrize("sources_first", [False, True])
def test_legacy_exact_source_lineage_retraction_and_late_import(store, tmp_path, sources_first):
    from harness.core.models import EpisodicEvent, SemanticFact
    shared = SharedMemoryStore(tmp_path / "raw.sqlite3")
    record = shared.record_local(local(event_id="exact-original-id", text="我喜欢摄影")).record
    episode = EpisodicEvent(event_id="old-episode", time=NOW, summary="旧模型的摄影摘要",
        objective_fact="旧摘要不是新的原话", source_refs=["exact-original-id"],
        participants=["local:main"], importance=.5, emotional_valence=0, evidence=["legacy-unverified-evidence"],
        scope=MemoryScope(channel="local_chat", conversation_id=record.session_id, visibility=["local:main"]))
    fact = SemanticFact(fact_id="old-fact", subject="local:main", predicate="兴趣", value="摄影",
        confidence=.8, supporting_events=[episode.event_id], formed_at=NOW)
    snapshot = preset(episodic=[episode], semantic=[fact])
    if sources_first:
        store.sync_shared(shared)
    store.import_legacy(snapshot)
    if not sources_first:
        assert not store.validate_references([episode.event_id], audience="local:main", now=NOW)
        assert store.recall(audience="local:main", now=NOW) == {}
        store.sync_shared(shared)
        store.import_legacy(snapshot)
    recalled = store.recall(audience="local:main", now=NOW)
    assert {record.record_id, "old-episode", "old-fact"} <= {row["record_id"] for row in recalled["entries"]}
    assert next(row for row in recalled["entries"] if row["record_id"] == "old-episode")["authority"] == "legacy_derived_episode"
    store.retain("old-fact", event_id="pin-old", pinned=True)
    store.retract_source(record.record_id, event_id="revoke-raw", reason="原作者撤回")
    assert store.recall(audience="local:main", now=NOW) == {}
    assert not store.validate_references(recalled["entries"], audience="local:main", now=NOW)
    store.import_legacy(snapshot)
    assert store.recall(audience="local:main", now=NOW) == {}
    assert store.legacy_history("old-episode")[0] == episode.model_dump(mode="json")


def test_unmapped_or_scope_incompatible_legacy_episode_is_history_only(store, tmp_path):
    from harness.core.models import EpisodicEvent
    shared = SharedMemoryStore(tmp_path / "raw.sqlite3")
    source = shared.record_local(local(event_id="private-raw")).record
    store.sync_shared(shared)
    no_evidence = EpisodicEvent(event_id="unknown", time=NOW, summary="未知来源旧摘要", objective_fact="未知",
        participants=[], importance=.5, emotional_valence=0, evidence=["legacy-unverified-evidence"])
    too_public = EpisodicEvent(event_id="too-public", time=NOW, summary="私下原文的旧摘要", source_refs=["private-raw"],
        objective_fact="摘要", participants=[], importance=.5, emotional_valence=0, evidence=["legacy-unverified-evidence"],
        scope=MemoryScope(channel="qq_group", conversation_id="qq:group:100", visibility=["local:main", "qq:group:100"]))
    guessed = no_evidence.model_copy(update={"event_id": "guessed", "source_refs": ["prefix:" + source.record_id]})
    store.import_legacy(preset(episodic=[no_evidence, too_public, guessed]))
    assert {row["record_id"] for row in store.recall(audience="local:main", now=NOW)["entries"]} == {source.record_id}
    assert store.recall(audience="qq:group:100", now=NOW) == {}
    for identifier in ("unknown", "too-public", "guessed"):
        assert store.legacy_history(identifier)
