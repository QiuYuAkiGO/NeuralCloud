"""Read-only snapshot inventory and restartable offline production migration.

Usage: python -m harness.memory.production_migration inspect SNAPSHOT.json
       python -m harness.memory.production_migration migrate SNAPSHOT.json --destination NEW.sqlite3
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3

from gui.presets import CharacterPreset
from harness.memory.production import ProductionMemoryStore
from harness.memory.shared import SharedMemoryStore, SharedSourcePage


class _SnapshotSharedMemoryStore(SharedMemoryStore):
    """Read sealed pre-index archives without initializing or changing them.

    A historical snapshot has stable SQLite rowids. They provide an exact,
    bounded insertion cursor when its newer source index does not exist. This
    fallback is deliberately confined to checksum-verified offline migration;
    a live archive still requires the normal explicit index migration.
    """

    def read_after(self, *, cursor=0, limit=256):
        if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 1000:
            raise ValueError("invalid incremental source cursor or limit")
        with closing(self._connect(write=False)) as db:
            indexed = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='shared_memory_source_index'").fetchone()
            if not indexed:
                rows = db.execute("""SELECT *,rowid AS source_sequence FROM shared_memory_entries
                    WHERE owner_id=? AND rowid>? ORDER BY rowid LIMIT ?""", (self.owner_id, cursor, limit + 1)).fetchall()
        if indexed:
            return super().read_after(cursor=cursor, limit=limit)
        selected = rows[:limit]
        return SharedSourcePage(records=tuple(self._decode(row) for row in selected),
            next_cursor=selected[-1]["source_sequence"] if selected else cursor, exhausted=len(rows) <= limit)


def _sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _resolve(parent, value):
    path = Path(value)
    return (parent / path).resolve() if not path.is_absolute() else path.resolve()


def _load(source):
    source = Path(source).resolve(strict=True)
    raw = json.loads(source.read_text(encoding="utf-8"))
    if raw.get("schema_version") == "production-memory-snapshot-v1":
        manifest = raw
        preset_file = _resolve(source.parent, manifest["preset_file"])
        preset = CharacterPreset.model_validate_json(preset_file.read_text(encoding="utf-8"))
        if manifest.get("preset_sha256") and _sha(preset_file) != manifest["preset_sha256"]:
            raise ValueError("snapshot_preset_checksum_mismatch")
        owner = manifest.get("owner_id", "misty")
        if manifest.get("preset_id", preset.preset_id) != preset.preset_id:
            raise ValueError("snapshot_preset_identity_mismatch")
        shared = _resolve(source.parent, manifest["shared_database"]) if manifest.get("shared_database") else None
    else:
        manifest, preset_file, shared = {}, source, None
        preset, owner = CharacterPreset.model_validate(raw), "misty"
    scopes = [item.scope for field in ("episodic", "semantic", "candidates", "relationships")
              for item in getattr(preset, field) if item.scope is not None]
    if any(scope.owner_id != owner for scope in scopes):
        raise ValueError("snapshot_mixed_owner")
    for raw_impression in preset.qq_memory_state.get("impressions", {}).values():
        if raw_impression.get("scope", {}).get("owner_id") != owner:
            raise ValueError("snapshot_impression_owner_mismatch")
    paths = tuple(dict.fromkeys([source, preset_file, *([shared] if shared else [])]))
    if shared:
        # A migration input is an offline, internally consistent source snapshot.
        # Do not silently ignore a WAL that may contain committed source rows.
        wal = Path(str(shared) + "-wal")
        if wal.exists() and wal.stat().st_size:
            raise ValueError("snapshot_has_active_wal_use_consistent_offline_backup")
        if manifest.get("shared_sha256") and _sha(shared) != manifest["shared_sha256"]:
            raise ValueError("snapshot_shared_checksum_mismatch")
        with closing(sqlite3.connect(shared.as_uri() + "?mode=ro", uri=True)) as db:
            db.execute("PRAGMA query_only=ON")
            if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
                raise ValueError("snapshot_shared_integrity_failure")
    return source, manifest, preset, owner, shared, paths


def inspect_snapshot(source):
    path, manifest, preset, owner, shared, paths = _load(source)
    events = {item.event_id for item in preset.episodic}
    references = {ref for fact in preset.semantic for ref in fact.supporting_events}
    references.update(entry["episode_id"] for impression in preset.qq_memory_state.get("impressions", {}).values()
                      for entry in impression.get("evidence", []))
    unknown_scopes = sum(item.scope is None for field in ("episodic", "semantic", "candidates", "relationships")
                         for item in getattr(preset, field))
    return {"schema_version": "production-memory-inventory-v1", "source": str(path),
        "owner_id": owner, "preset_id": preset.preset_id,
        "files": [{"path": str(file), "sha256": _sha(file), "bytes": file.stat().st_size} for file in paths],
        "counts": {**{field: len(getattr(preset, field)) for field in ("episodic", "semantic", "candidates", "relationships")},
                   "impressions": len(preset.qq_memory_state.get("impressions", {})),
                   "tombstones": len(manifest.get("tombstones", [])), "unknown_scopes": unknown_scopes,
                   "missing_episode_references": len(references - events)},
        "shared_database": str(shared) if shared else None,
        "unknown_data_policy": "archive_without_inventing_evidence_or_cross_audience_grants"}


def _snapshot_binding(source, manifest, preset, owner, shared, checksums):
    preset_file = _resolve(source.parent, manifest["preset_file"]) if manifest else source
    tombstones = []
    for item in manifest.get("tombstones", []):
        if (not isinstance(item, dict) or not isinstance(item.get("source_id"), str)
                or not item["source_id"] or not isinstance(item.get("reason", "snapshot_tombstone"), str)):
            raise ValueError("snapshot_tombstone_invalid")
        tombstones.append({"source_id": item["source_id"], "reason": item.get("reason", "snapshot_tombstone")})
    tombstones.sort(key=lambda item: (item["source_id"], item["reason"]))
    payload = json.dumps({"schema_version": "production-memory-migration-input-v1", "owner_id": owner,
        "preset_id": preset.preset_id, "preset_sha256": checksums[preset_file],
        "shared_sha256": checksums[shared] if shared else None, "tombstones": tombstones},
        ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest(), payload


def _bind_destination(destination, *, fingerprint, payload, owner, preset_id):
    """Fence input before schema upgrades, history publication or cursor writes.

    A committed binding without the production schema is a recoverable first
    step. Existing unbound data is not evidence that it came from this input.
    """
    existed = destination.exists()
    destination.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(destination)) as db, db:
        db.execute("BEGIN IMMEDIATE")
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        bound = db.execute("SELECT fingerprint,payload FROM pm_migration_input WHERE singleton=1").fetchone() if "pm_migration_input" in tables else None
        if bound and tuple(bound) != (fingerprint, payload):
            raise ValueError("migration_snapshot_fingerprint_mismatch_use_new_destination")
        if "pm_identity" in tables:
            identity = db.execute("SELECT owner,preset FROM pm_identity WHERE singleton=1").fetchone()
            if identity is None or tuple(identity) != (owner, preset_id):
                raise ValueError("migration_destination_identity_mismatch")
        elif existed and bound is None:
            raise ValueError("existing_destination_is_not_a_production_memory_store")
        if bound:
            return
        # Empty initialized production stores can be bound. Any retained data,
        # cursor or history in an older unbound target requires a fresh copy.
        for table in tables - {"pm_identity", "pm_migration_input", "sqlite_sequence"}:
            quoted = '"' + table.replace('"', '""') + '"'
            if db.execute("SELECT 1 FROM " + quoted + " LIMIT 1").fetchone():
                raise ValueError("migration_unbound_existing_data_use_new_destination")
        db.execute("""CREATE TABLE IF NOT EXISTS pm_migration_input (
            singleton INTEGER PRIMARY KEY CHECK(singleton=1), fingerprint TEXT NOT NULL, payload TEXT NOT NULL)""")
        db.execute("INSERT INTO pm_migration_input VALUES(1,?,?)", (fingerprint, payload))


def migrate_snapshot(source, destination, *, batch_size=256, max_source_batches=None):
    if type(batch_size) is not int or not 1 <= batch_size <= 1000:
        raise ValueError("invalid_migration_batch_size")
    if max_source_batches is not None and (type(max_source_batches) is not int or max_source_batches < 0):
        raise ValueError("invalid_migration_batch_limit")
    source, manifest, preset, owner, shared, paths = _load(source)
    destination = Path(destination).resolve()
    if any(destination == path or (destination.exists() and destination.samefile(path)) for path in paths):
        raise ValueError("migration_destination_cannot_be_a_source")
    if destination.suffix.lower() not in {".db", ".sqlite", ".sqlite3"}:
        raise ValueError("migration_destination_must_be_a_sqlite_file")
    checksums = {path: _sha(path) for path in paths}
    fingerprint, binding = _snapshot_binding(source, manifest, preset, owner, shared, checksums)
    _bind_destination(destination, fingerprint=fingerprint, payload=binding, owner=owner, preset_id=preset.preset_id)
    store = ProductionMemoryStore(destination, owner_id=owner, preset_id=preset.preset_id)
    store.import_legacy(preset)
    batches, complete = 0, True
    if shared:
        originals = _SnapshotSharedMemoryStore(shared, owner_id=owner, preset_id=preset.preset_id)
        stream = "offline-snapshot:" + fingerprint
        while max_source_batches is None or batches < max_source_batches:
            page = originals.read_after(cursor=store.cursor(stream), limit=batch_size)
            store.ingest([store.from_shared(record) for record in page.records], stream=stream, cursor=page.next_cursor)
            batches += 1
            complete = page.exhausted
            if complete:
                break
        if max_source_batches == 0:
            complete = False
    if complete:
        # Newly imported originals may resolve archived legacy references.
        # Revisit deterministic projections, without regenerating model output.
        store.import_legacy(preset)
        for item in manifest.get("tombstones", []):
            store.retract_source(item["source_id"], event_id="migration-retract:" + item["source_id"],
                                 reason=item.get("reason", "snapshot_tombstone"))
    if any(_sha(path) != digest for path, digest in checksums.items()):
        raise ValueError("snapshot_changed_during_migration")
    expected = {item.event_id: item.model_dump(mode="json") for item in preset.episodic}
    expected.update({item.fact_id: item.model_dump(mode="json") for item in preset.semantic})
    expected.update({item.candidate_id: item.model_dump(mode="json") for item in preset.candidates})
    expected.update({item.relation_id: item.model_dump(mode="json") for item in preset.relationships})
    expected.update({key: raw for key, raw in preset.qq_memory_state.get("impressions", {}).items()})
    prefix = "legacy-state:" + preset.preset_id + ":"
    expected[prefix + "identity"] = preset.model_dump(mode="json", exclude={
        "episodic", "semantic", "candidates", "relationships", "qq_memory_state", "daily_revision_state"})
    expected[prefix + "qq_memory"] = preset.qq_memory_state
    expected[prefix + "daily_revision"] = preset.daily_revision_state
    for identifier, original in expected.items():
        if original not in store.legacy_history(identifier):
            raise ValueError("legacy_identity_or_history_mapping_incomplete")
    with closing(store._connect()) as db:
        if db.execute("PRAGMA quick_check").fetchone()[0] != "ok":
            raise ValueError("destination_integrity_failure")
        missing = db.execute("SELECT count(*) FROM pm_evidence e LEFT JOIN pm_sources s ON s.id=e.source WHERE s.id IS NULL").fetchone()[0]
        if missing:
            raise ValueError("destination_evidence_reference_missing")
        counts = {name: db.execute("SELECT count(*) FROM " + table).fetchone()[0] for name, table in (
            ("sources", "pm_sources"), ("records", "pm_records"), ("revisions", "pm_revisions"),
            ("legacy_versions", "pm_legacy_versions"), ("receipts", "pm_receipts"))}
        counts["legacy_lineage_unresolved"] = db.execute("SELECT count(*) FROM pm_legacy_valid WHERE complete=0").fetchone()[0]
        counts["legacy_lineage_resolved"] = db.execute("SELECT count(*) FROM pm_legacy_valid WHERE complete=1").fetchone()[0]
    return {"schema_version": "production-memory-migration-v1", "source": str(source),
        "destination": str(destination), "owner_id": owner, "preset_id": preset.preset_id,
        "snapshot_fingerprint": fingerprint,
        "complete": complete, "source_batches": batches, "source_unchanged": True,
        "legacy_ids_verified": len(expected), "counts": counts,
        "unresolved_evidence_is_archived": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    inspect = commands.add_parser("inspect")
    inspect.add_argument("source")
    migrate = commands.add_parser("migrate")
    migrate.add_argument("source")
    migrate.add_argument("--destination", required=True)
    migrate.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args(argv)
    result = inspect_snapshot(args.source) if args.command == "inspect" else migrate_snapshot(args.source, args.destination, batch_size=args.batch_size)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
