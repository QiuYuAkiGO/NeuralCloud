"""Read-only snapshot inputs, resumable targets, and synthetic benchmark smoke."""
import json
import hashlib
from pathlib import Path
import sqlite3
import shutil

import pytest

from harness.memory.production import ProductionMemoryStore
from harness.memory.production_migration import inspect_snapshot, migrate_snapshot
from harness.memory.shared import SharedMemoryStore
from tests.test_daily_revision_reducer import preset, run, proposal, NOW
from harness.memory.revision.models import MemoryRevision
from harness.memory.revision.reducer import build_revision
from harness.config import Settings
from tests.test_shared_memory import local
from tools.benchmark_production_memory import benchmark


def snapshot(tmp_path):
    changed = build_revision(preset(), run(), proposal(memories=[MemoryRevision(kind="episodic", subject="local:main",
        statement="摄影经历", evidence_ids=["s1"])]), Settings(), now=NOW)
    preset_file = tmp_path / "preset.json"
    preset_file.write_text(changed.model_dump_json(), encoding="utf-8")
    shared = SharedMemoryStore(tmp_path / "shared.sqlite3")
    records = [shared.record_local(local(event_id=f"local-{i}", text=f"合成来源{i}")).record for i in range(3)]
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"schema_version": "production-memory-snapshot-v1", "owner_id": "misty", "preset_id": "misty",
        "preset_file": preset_file.name, "shared_database": shared.path.name,
        "tombstones": [{"source_id": records[0].record_id, "reason": "撤销测试"}]}, ensure_ascii=False), encoding="utf-8")
    return manifest, preset_file, shared.path, changed, records


def test_inventory_and_roundtrip_preserve_source_originals_and_tombstones(tmp_path):
    source, preset_file, shared, original, records = snapshot(tmp_path)
    before = {path: path.read_bytes() for path in (source, preset_file, shared)}
    inventory = inspect_snapshot(source)
    assert inventory["counts"]["episodic"] == 1
    assert inventory["owner_id"] == inventory["preset_id"] == "misty"
    assert "合成来源" not in str(inventory)
    destination = tmp_path / "production.sqlite3"
    report = migrate_snapshot(source, destination, batch_size=1)
    assert report["complete"] and report["source_unchanged"]
    store = ProductionMemoryStore(destination)
    assert store.legacy_history(original.episodic[0].event_id) == (original.episodic[0].model_dump(mode="json"),)
    assert store.legacy_history("legacy-state:misty:daily_revision") == (original.daily_revision_state,)
    assert report["counts"]["legacy_lineage_unresolved"] == 1
    assert not store.validate_references([records[0].record_id], audience="local:main")
    assert store.validate_references([records[1].record_id], audience="local:main")
    assert {path: path.read_bytes() for path in before} == before
    replay = migrate_snapshot(source, destination, batch_size=1)
    assert replay["counts"] == report["counts"]


def test_interrupted_migration_continues_without_duplicate_sources_or_receipts(tmp_path):
    source, _, _, _, _ = snapshot(tmp_path)
    destination = tmp_path / "resume.sqlite3"
    interrupted = migrate_snapshot(source, destination, batch_size=1, max_source_batches=1)
    assert not interrupted["complete"]
    complete = migrate_snapshot(source, destination, batch_size=1)
    assert complete["complete"] and complete["source_batches"] == 2
    fresh = migrate_snapshot(source, tmp_path / "fresh.sqlite3", batch_size=2)
    assert complete["counts"] == fresh["counts"]


@pytest.mark.parametrize("legacy_source", [False, True])
def test_same_path_replaced_snapshot_rejected_before_any_target_mutation(tmp_path, legacy_source):
    source, _, shared, _, _ = snapshot(tmp_path)
    if legacy_source:
        with sqlite3.connect(shared) as db:
            db.execute("DROP TABLE shared_memory_source_index")
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["tombstones"] = []
    manifest["shared_sha256"] = hashlib.sha256(shared.read_bytes()).hexdigest()
    source.write_text(json.dumps(manifest), encoding="utf-8")
    original_source, original_shared = source.read_bytes(), shared.read_bytes()
    target = tmp_path / "target.sqlite3"
    partial = migrate_snapshot(source, target, batch_size=1, max_source_batches=1)
    assert partial["complete"] is False
    before = target.read_bytes()
    replacement = SharedMemoryStore(tmp_path / "replacement.sqlite3")
    for index in range(3):
        replacement.record_local(local(event_id="new-snapshot-" + str(index), text="replacement synthetic source"))
    if legacy_source:
        with sqlite3.connect(replacement.path) as db:
            db.execute("DROP TABLE shared_memory_source_index")
    shared.write_bytes(replacement.path.read_bytes())
    manifest["shared_sha256"] = hashlib.sha256(shared.read_bytes()).hexdigest()
    source.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot_fingerprint_mismatch"):
        migrate_snapshot(source, target, batch_size=1)
    assert target.read_bytes() == before
    # Restoring the exact original frozen input resumes the original cursor.
    source.write_bytes(original_source)
    shared.write_bytes(original_shared)
    resumed = migrate_snapshot(source, target, batch_size=1)
    assert resumed["complete"] and resumed["source_batches"] == 2
    assert resumed["snapshot_fingerprint"] == partial["snapshot_fingerprint"]


def test_identical_snapshot_moved_to_other_path_keeps_content_cursor(tmp_path):
    source, preset_file, shared, _, _ = snapshot(tmp_path)
    target = tmp_path / "target.sqlite3"
    partial = migrate_snapshot(source, target, batch_size=1, max_source_batches=1)
    moved = tmp_path / "same-snapshot-new-location"
    moved.mkdir()
    for path in (source, preset_file, shared):
        shutil.copyfile(path, moved / path.name)
    manifest = json.loads((moved / source.name).read_text(encoding="utf-8"))
    manifest["provenance"] = {"copy_note": "same immutable payloads at another location"}
    (moved / source.name).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    resumed = migrate_snapshot(moved / source.name, target, batch_size=1)
    assert resumed["complete"] and resumed["source_batches"] == 2
    assert resumed["snapshot_fingerprint"] == partial["snapshot_fingerprint"]


@pytest.mark.parametrize("change", ["preset", "tombstones"])
def test_binding_rejects_changed_preset_or_tombstones_without_target_writes(tmp_path, change):
    source, preset_file, _, _, _ = snapshot(tmp_path)
    target = tmp_path / "target.sqlite3"
    migrate_snapshot(source, target, max_source_batches=0)
    before = target.read_bytes()
    if change == "preset":
        raw = json.loads(preset_file.read_text(encoding="utf-8"))
        raw["persona"]["identity"]["name"] = "Changed synthetic identity"
        preset_file.write_text(json.dumps(raw), encoding="utf-8")
    else:
        raw = json.loads(source.read_text(encoding="utf-8"))
        raw["tombstones"] = []
        source.write_text(json.dumps(raw), encoding="utf-8")
    with pytest.raises(ValueError, match="snapshot_fingerprint_mismatch"):
        migrate_snapshot(source, target)
    assert target.read_bytes() == before


def test_unbound_target_with_history_is_not_assumed_to_match(tmp_path):
    source, _, _, original, _ = snapshot(tmp_path)
    target = tmp_path / "pre-binding.sqlite3"
    ProductionMemoryStore(target).import_legacy(original)
    before = target.read_bytes()
    with pytest.raises(ValueError, match="unbound_existing_data"):
        migrate_snapshot(source, target)
    assert target.read_bytes() == before


def test_binding_commit_survives_interruption_before_production_schema(tmp_path, monkeypatch):
    from harness.memory import production_migration
    source, _, _, _, _ = snapshot(tmp_path)
    target = tmp_path / "bound-only.sqlite3"

    def interrupted(*args, **kwargs):
        raise RuntimeError("synthetic_interruption_after_binding")

    monkeypatch.setattr(production_migration, "ProductionMemoryStore", interrupted)
    with pytest.raises(RuntimeError, match="synthetic_interruption_after_binding"):
        migrate_snapshot(source, target)
    with sqlite3.connect(target) as db:
        assert db.execute("SELECT count(*) FROM pm_migration_input").fetchone()[0] == 1
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='pm_identity'").fetchone()
    monkeypatch.setattr(production_migration, "ProductionMemoryStore", ProductionMemoryStore)
    assert migrate_snapshot(source, target)["complete"]


def test_migration_rejects_source_as_destination_and_wrong_identity(tmp_path):
    source, preset_file, _, _, _ = snapshot(tmp_path)
    before = preset_file.read_bytes()
    with pytest.raises(ValueError, match="cannot_be_a_source"):
        migrate_snapshot(source, preset_file)
    assert preset_file.read_bytes() == before
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["preset_id"] = "someone-else"
    source.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="identity_mismatch"):
        migrate_snapshot(source, tmp_path / "target.sqlite3")


def test_direct_preset_input_and_checksum_mismatch(tmp_path):
    source, preset_file, _, _, _ = snapshot(tmp_path)
    assert migrate_snapshot(preset_file, tmp_path / "direct.sqlite3")["complete"]
    manifest = json.loads(source.read_text(encoding="utf-8"))
    manifest["preset_sha256"] = "0" * 64
    source.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="checksum_mismatch"):
        inspect_snapshot(source)


def test_synthetic_benchmark_batches_and_resume(tmp_path):
    path = tmp_path / "benchmark.sqlite3"
    report = benchmark(path, rows=100, batch_size=20, queries=5)
    assert report["rows_inserted"] == 100 and report["synthetic_only"]
    assert report["candidates"]["max"] <= 192
    assert report["recall_ms"]["p95"] > 0
    preparation = report["production_context_preparation"]
    assert preparation["warm_prepare_ms"]["p95"] > 0
    assert preparation["memory_entries"]["min"] > 0
    assert preparation["external_model_calls"] == 0
    assert "no_drop_caches" in preparation["cache_condition"]
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM pm_sources").fetchone()[0] == 100
    with pytest.raises(ValueError, match="destination_exists"):
        benchmark(path, rows=100, queries=1)
    assert benchmark(path, rows=100, queries=1, resume=True)["rows_inserted"] == 0


def test_benchmark_uses_actual_production_messages_and_receipts_on_copy(tmp_path, monkeypatch):
    from tools.benchmark_production_memory import _PromptBoundary
    original = _PromptBoundary.complete_chat_result
    seen = []

    def observe(self, messages, **kwargs):
        payload = json.loads(messages[-1]["content"])
        entries = payload["memories"][0]
        assert entries["why"] == "production_memory"
        assert entries["memory_evidence"]["entries"]
        assert set(entries["source_ids"]).issubset(payload["source_refs"])
        assert "ProductionChoice" in messages[0]["content"]
        seen.append(len(entries["source_ids"]))
        return original(self, messages, **kwargs)

    monkeypatch.setattr(_PromptBoundary, "complete_chat_result", observe)
    source = tmp_path / "benchmark.sqlite3"
    report = benchmark(source, rows=10, queries=2)
    assert len(seen) == 7
    copied = Path(report["production_context_preparation"]["retained_working_copy"]) / "production.sqlite3"
    with sqlite3.connect(source) as db:
        assert db.execute("SELECT count(*) FROM pm_sources").fetchone()[0] == 10
        assert db.execute("SELECT count(*) FROM pm_receipts").fetchone()[0] == 0
    with sqlite3.connect(copied) as db:
        assert db.execute("SELECT count(*) FROM pm_sources").fetchone()[0] == 17
        kinds = dict(db.execute("SELECT kind,count(*) FROM pm_receipts GROUP BY kind"))
        assert kinds == {"retrieval_hit": 7, "context_exposure": 7}


def test_sql_only_benchmark_explicitly_skips_preparation(tmp_path):
    report = benchmark(tmp_path / "benchmark.sqlite3", rows=10, queries=1, context_prepare=False)
    assert report["production_context_preparation"] is None


def test_pre_index_historical_archive_migrates_read_only_and_resumes(tmp_path):
    from harness.memory.shared import SharedMemoryStorageError

    shared = SharedMemoryStore(tmp_path / "historical.sqlite3")
    records = [shared.record_local(local(event_id="historical-" + str(index))).record for index in range(3)]
    with sqlite3.connect(shared.path) as db:
        db.execute("DROP TABLE shared_memory_source_index")
        db.execute("DROP TABLE shared_memory_identity")
        db.execute("DROP TABLE shared_memory_archive_coverage")
    before = hashlib.sha256(shared.path.read_bytes()).hexdigest()
    preset_file = tmp_path / "preset.json"
    preset_file.write_text(preset().model_dump_json(), encoding="utf-8")
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps({"schema_version": "production-memory-snapshot-v1", "preset_file": preset_file.name,
        "shared_database": shared.path.name, "shared_sha256": before}), encoding="utf-8")
    target = tmp_path / "migrated.sqlite3"
    partial = migrate_snapshot(source, target, batch_size=1, max_source_batches=1)
    assert partial["complete"] is False
    complete = migrate_snapshot(source, target, batch_size=1)
    repeated = migrate_snapshot(source, target, batch_size=1)
    assert complete["complete"] and complete["counts"] == repeated["counts"]
    memory = ProductionMemoryStore(target, owner_id="misty", preset_id="misty")
    assert memory.validate_references([row.record_id for row in records], audience="local:main")
    assert hashlib.sha256(shared.path.read_bytes()).hexdigest() == before
    with sqlite3.connect(shared.path) as db:
        assert not db.execute("SELECT 1 FROM sqlite_master WHERE name='shared_memory_source_index'").fetchone()
    # The live incremental reader still requires its explicit schema upgrade.
    with pytest.raises(SharedMemoryStorageError, match="requires migration"):
        shared.read_after()


def test_migration_manifest_original_id_retraction_reaches_legacy_layers(tmp_path):
    from harness.core.models import EpisodicEvent, MemoryScope, SemanticFact
    shared = SharedMemoryStore(tmp_path / "originals.sqlite3")
    record = shared.record_local(local(event_id="raw-authored-event")).record
    episode = EpisodicEvent(event_id="old-summary", time=NOW, participants=["local:main"], summary="旧摘要",
        objective_fact="原有摘要", importance=.5, emotional_valence=0, evidence=["raw-authored-event"],
        source_refs=["raw-authored-event"], scope=MemoryScope(channel="local_chat", visibility=["local:main"]))
    fact = SemanticFact(fact_id="old-understanding", subject="local:main", predicate="兴趣", value="围棋",
        confidence=.6, supporting_events=[episode.event_id], formed_at=NOW)
    frozen = preset(episodic=[episode], semantic=[fact])
    preset_file = tmp_path / "preset.json"
    preset_file.write_text(frozen.model_dump_json(), encoding="utf-8")
    source = tmp_path / "manifest.json"
    source.write_text(json.dumps({"schema_version": "production-memory-snapshot-v1", "preset_file": preset_file.name,
        "shared_database": shared.path.name, "tombstones": [{"source_id": "raw-authored-event", "reason": "withdrawn"}]}), encoding="utf-8")
    destination = tmp_path / "migration.sqlite3"
    report = migrate_snapshot(source, destination, batch_size=1)
    assert report["complete"] and report["counts"]["legacy_lineage_resolved"] == 1
    store = ProductionMemoryStore(destination)
    assert store.recall(audience="local:main", now=NOW) == {}
    for identifier in (record.record_id, episode.event_id, fact.fact_id):
        assert not store.validate_references([identifier], audience="local:main", now=NOW)
    assert store.legacy_history(fact.fact_id)
    assert migrate_snapshot(source, destination, batch_size=1)["counts"] == report["counts"]
