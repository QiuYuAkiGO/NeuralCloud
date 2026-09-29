"""Synthetic originals through the real production QQ and daily adapters."""
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory, ScopedQQMemoryView, SharedQQBinding
from harness.config import Settings
from harness.core.models import SemanticFact
from harness.memory.production import ProductionMemoryStore
from harness.memory.revision.ledger import install_revision
from harness.memory.revision.models import MemoryRevision
from harness.memory.revision.reducer import build_revision, rollback_revision
from harness.memory.revision.sources import collect_source_window
from harness.memory.service import build_inmemory_service
from harness.memory.shared import SharedMemoryStore
from harness.qq import decision
from harness.qq.prepared_evidence import capture_used_sources, validate_used_sources
from harness.qq.telemetry import QQDeadline
from harness.qq.turn_checkpoints import QQTurnStateError
from tests.test_daily_revision_reducer import NOW, preset, proposal, run
from tests.test_qq_decision import setup as setup
from tests.test_qq_prepared_turn import _binding
from tests.test_shared_memory import local


@pytest.fixture
def production_qq(setup, tmp_path, request):
    raw, client, _, kwargs, _ = setup
    settings, character = decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    settings = settings.model_copy(update={"validator_enabled": False, "production_memory_enabled": True})
    memory = build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(character.persona)
    if getattr(request, "param", None) == "old_fields":
        with sqlite3.connect(raw.path) as db:
            for identifier, encoded in db.execute("SELECT record_id,record_json FROM qq_visual_observations").fetchall():
                value = json.loads(encoded)
                for field in ("image_analysis", "message_id", "message_id_source"):
                    value.pop(field, None)
                db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?", (json.dumps(value), identifier))
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    assert shared.import_qq_database(raw.path, allowed_group_ids=["10001"], self_member_id="29999").success
    store = ProductionMemoryStore(tmp_path / "production.sqlite3")
    store.sync_shared(shared)
    host = SimpleNamespace(shared=SimpleNamespace(store=shared, production_enabled=True, production_store=store,
        qq_settings=SimpleNamespace(self_member_id="29999", allowed_group_ids=["10001"], allowed_private_peer_ids=[])),
        chat=SimpleNamespace(memory=memory, active_preset_id="misty"))
    owner = SharedQQBinding(settings, character, client,
        ScopedQQMemoryView(memory, group_id="10001", member_id="20002"), {}, (), {}, store)
    yield host, owner
    memory.close()


def _prepare(setup, host, owner):
    binding = _binding()
    prepared = decision.prepare_qq_turn(**setup[3], trigger_member_id="20002", shared_owner=owner,
        turn_binding=binding, deadline=QQDeadline.after(60),
        used_sources_provider=lambda selected: capture_used_sources(host, selected))
    assert prepared["kind"] == "prepared"
    return binding, prepared


def _resume(setup, host, owner, binding, prepared):
    kwargs = setup[3]
    return decision.resume_qq_response(memory_db=kwargs["memory_db"], config_path=kwargs["config_path"],
        preset_path=kwargs["preset_path"], prepare_id=prepared["prepare_id"], snapshot_hash=prepared["snapshot_hash"],
        turn_binding=binding, shared_owner=owner, deadline=QQDeadline.after(60),
        used_sources_validator=lambda manifest: validate_used_sources(host, manifest))


def test_real_qq_raw_history_filters_withdrawn_text_before_gate(setup, production_qq):
    host, owner = production_qq
    source = setup[4][0]
    host.shared.production_store.retract_source(source.record_id, event_id="withdraw-before-gate", reason="撤回")
    _, prepared = _prepare(setup, host, owner)
    gate_input = str(setup[1].gate_messages)
    assert "UNTRUSTED_A" not in gate_input and "UNTRUSTED_B" in gate_input
    checkpoint = decision.get_qq_turn_checkpoint(memory_db=setup[0].path, prepare_id=prepared["prepare_id"])
    assert checkpoint["snapshot"]["used_sources"]["version"] == "qq-used-sources-v2"
    assert checkpoint["snapshot"]["used_sources"]["production"]
    # Original evidence remains immutable, and only its reachability changed.
    assert source.text in str(setup[0].group_context(group_id="10001").model_dump())


@pytest.mark.parametrize("production_qq", ["old_fields"], indirect=True)
def test_old_qq_optional_field_defaults_preserve_original_hash_through_live_adapter(setup, production_qq):
    host, owner = production_qq
    store = host.shared.production_store
    source = setup[4][1]
    canonical = store.ingest_qq(source, memory_db=setup[0].path)
    _prepare(setup, host, owner)
    with sqlite3.connect(store.path) as db:
        payload = json.loads(db.execute("SELECT payload FROM pm_sources WHERE id=?", (canonical,)).fetchone()[0])
    assert "image_analysis" not in payload["original"]["source"]
    assert "message_id" not in payload["original"]["source"]


@pytest.mark.parametrize("completed", [False, True])
def test_real_qq_raw_withdrawal_blocks_prepared_body_and_completed_replay(setup, production_qq, completed):
    host, owner = production_qq
    binding, prepared = _prepare(setup, host, owner)
    if completed:
        assert _resume(setup, host, owner, binding, prepared).outcome == "ready"
    previous_calls = len(setup[1].chat_calls)
    host.shared.production_store.retract_source(setup[4][0].record_id, event_id="withdraw-after-gate", reason="撤回")
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        _resume(setup, host, owner, binding, prepared)
    assert len(setup[1].chat_calls) == previous_calls


def test_daily_install_publishes_exact_evidence_keys_beyond_source_cursor(tmp_path):
    memory = build_inmemory_service()
    host = SimpleNamespace(chat=SimpleNamespace(settings=Settings(production_memory_enabled=True), memory=memory,
        active_preset_id="misty"), adapter=SimpleNamespace(_component_revisions={"memory": 0, "maintenance": 0,
        "relationship": 0}, _revision=0, _persist_state=lambda: None))
    host.shared = CognitiveSharedMemory(host, tmp_path / "shared.sqlite3")
    archive = host.shared.store
    older = [local(event_id=f"old-{index}", occurred_at=NOW - timedelta(days=3)) for index in range(260)]
    archive._write([archive._prepare("local_incoming", item.event_id, item, item.visibility) for item in older])
    target = archive.record_local(local(event_id="daily-target", occurred_at=NOW - timedelta(hours=12), text="我最近喜欢摄影")).record
    window = collect_source_window(archive, owner_id="misty", preset_id="misty", start=NOW - timedelta(days=1), end=NOW)
    source = next(item for item in window.records if item.source_id == target.record_id)
    changed = build_revision(preset(), run(sources=[source]), proposal([source], memories=[MemoryRevision(
        kind="episodic", subject="local:main", statement="记下对方近期的摄影兴趣", evidence_ids=[source.source_id])]),
        host.chat.settings, now=NOW)
    episode = changed.episodic[0]
    assert episode.source_refs == list(source.evidence_keys)
    try:
        install_revision(host, changed)
        store = host.shared.production_store
        assert store.validate_references([target.record_id, episode.event_id], audience="local:main", now=NOW)
        assert not host.adapter._daily_revision_publication_pending
        rolled_back = rollback_revision(changed, "r1", now=NOW)
        install_revision(host, rolled_back)
        assert not store.validate_references([episode.event_id], audience="local:main", now=NOW)
        assert store.validate_references([target.record_id], audience="local:main", now=NOW)
        # Explicit restored history becomes a new current interpretation;
        # physical withdrawal of its underlying source still overrides it.
        install_revision(host, changed)
        assert store.validate_references([episode.event_id], audience="local:main", now=NOW)
        store.retract_source(target.record_id, event_id="daily-source-withdrawn", reason="撤回")
        assert not store.validate_references([episode.event_id], audience="local:main", now=NOW)
        assert store.legacy_history(episode.event_id)[0] == episode.model_dump(mode="json")
    finally:
        memory.close()


def test_frozen_daily_alias_needs_original_hash_and_late_import_can_recover(tmp_path):
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    source_record = shared.record_local(local(event_id="source", occurred_at=NOW - timedelta(hours=12))).record
    source = collect_source_window(shared, owner_id="misty", preset_id="misty", start=NOW - timedelta(days=1), end=NOW).records[0]
    changed = build_revision(preset(), run(sources=[source]), proposal([source], memories=[MemoryRevision(
        kind="episodic", subject="local:main", statement="保留经历", evidence_ids=[source.source_id])]), Settings(), now=NOW)
    store = ProductionMemoryStore(tmp_path / "production.sqlite3")
    store.import_legacy(changed)
    identifier = changed.episodic[0].event_id
    assert not store.validate_references([identifier], audience="local:main", now=NOW)
    store.sync_shared(shared)
    invalid = changed.model_copy(deep=True)
    invalid.daily_revision_state["sources"][source.source_id]["source_hash"] = "f" * 64
    store.import_legacy(invalid)
    assert not store.validate_references([identifier], audience="local:main", now=NOW)
    store.import_legacy(changed)
    assert store.validate_references([identifier], audience="local:main", now=NOW)
    store.retract_source(source_record.source.original_record_id, event_id="original-deleted", reason="撤回")
    assert not store.validate_references([identifier], audience="local:main", now=NOW)


def test_legacy_same_id_edit_revises_current_text_fences_dependents_and_preserves_history(tmp_path):
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    shared.record_local(local(event_id="source", occurred_at=NOW - timedelta(hours=12)))
    source = collect_source_window(shared, owner_id="misty", preset_id="misty", start=NOW - timedelta(days=1), end=NOW).records[0]
    changed = build_revision(preset(), run(sources=[source]), proposal([source], memories=[MemoryRevision(
        kind="episodic", subject="local:main", statement="保留经历", evidence_ids=[source.source_id])]), Settings(), now=NOW)
    episode = changed.episodic[0]
    changed.semantic = [SemanticFact(fact_id="legacy-understanding", subject="local:main", predicate="兴趣",
        value="对方曾表达爱好", confidence=.5, supporting_events=[episode.event_id], formed_at=NOW)]
    store = ProductionMemoryStore(tmp_path / "production.sqlite3")
    store.sync_shared(shared)
    store.import_legacy(changed)
    before = store.recall(audience="local:main", now=NOW)
    frozen = [entry for entry in before["entries"] if entry["record_id"] in {episode.event_id, "legacy-understanding"}]
    assert len(frozen) == 2
    with sqlite3.connect(store.path) as db:
        immutable_before = db.execute("SELECT payload FROM pm_sources WHERE id=?", (episode.event_id,)).fetchone()[0]
    changed.episodic = [episode.model_copy(update={"objective_fact": "修订后明确记录的摄影兴趣", "summary": "修订解释"})]
    store.import_legacy(changed)
    after = store.recall(audience="local:main", now=NOW)
    revised = next(entry for entry in after["entries"] if entry["record_id"] == episode.event_id)
    assert revised["summary"] == "修订后明确记录的摄影兴趣"
    assert not store.validate_references(frozen, audience="local:main", now=NOW)
    assert len(store.history("legacy-understanding")) == 2
    assert store.legacy_history(episode.event_id)[0] == episode.model_dump(mode="json")
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT payload FROM pm_sources WHERE id=?", (episode.event_id,)).fetchone()[0] == immutable_before
    deleted = changed.model_copy(update={"episodic": [], "semantic": []})
    store.import_legacy(deleted)
    assert not store.validate_references([episode.event_id], audience="local:main", now=NOW)
    assert not store.validate_references(["legacy-understanding"], audience="local:main", now=NOW)
    assert store.validate_references([source.source_id], audience="local:main", now=NOW)
