"""Offline migration/rollback drill using copies of a sealed memory manifest.

Run from the repository, or execute this file from another working directory:
  python -m tools.production_acceptance.memory_drill --manifest BRIDGE/manifest.json --destination-dir NEW
  python -m tools.production_acceptance.memory_drill --self-test --destination-dir NEW
Imports default to this checkout; --repo explicitly selects another checkout.

No services or models run. The rollback restores the compatibility preset
projection inside an isolated SQL copy while retaining post-cutover facts.
It is not a live release rollback, and an old snapshot cannot certify today's
live state. Standard output and report.json contain hashes/counts only.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import sqlite3
import sys


class DrillFailure(RuntimeError):
    pass


def require(condition, code):
    if not condition:
        raise DrillFailure(code)


def offline_guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind",
                 "socket.sendto", "subprocess.Popen", "os.system", "os.posix_spawn"}:
        raise DrillFailure("external_io_forbidden")


def sha(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def no_links(path):
    require(not any(item.is_symlink() for item in (path, *path.parents)), "linked_path_forbidden")


def private_write(path, value):
    with path.open("x", encoding="utf-8") as stream:
        stream.write(encoded(value) + "\n")
    path.chmod(0o600)


def ro(path):
    database = sqlite3.connect(Path(path).resolve().as_uri() + "?mode=ro", uri=True)
    database.row_factory = sqlite3.Row
    database.execute("PRAGMA query_only=ON")
    return database


def backup(source, destination):
    require(not destination.exists(), "backup_destination_exists")
    with closing(ro(source)) as old, closing(sqlite3.connect(destination)) as new:
        old.backup(new)
    destination.chmod(0o600)


TABLES = {
    "sources": ("pm_sources", "id"), "records": ("pm_records", "id"),
    "revisions": ("pm_revisions", "id,revision"), "evidence": ("pm_evidence", "id,source"),
    "evidence_versions": ("pm_evidence_revision", "id,source"),
    "source_aliases": ("pm_source_alias", "alias,id"),
    "original_aliases": ("pm_source_original", "ref,id"),
    "lineage": ("pm_lineage", "parent,child"), "legacy_validity": ("pm_legacy_valid", "id"),
    "legacy_presence": ("pm_legacy_presence", "id"), "legacy_versions": ("pm_legacy_versions", "id,digest"),
    "receipts": ("pm_receipts", "event_id"), "retention": ("pm_retention", "id"),
}


def database_summary(path):
    counts, hashes = {}, {}
    with closing(ro(path)) as db:
        require(db.execute("PRAGMA quick_check").fetchone()[0] == "ok", "database_integrity_failure")
        for label, (table, ordering) in TABLES.items():
            digest, count = hashlib.sha256(), 0
            for row in db.execute("SELECT * FROM " + table + " ORDER BY " + ordering):
                digest.update(encoded(dict(row)).encode("utf-8") + b"\n")
                count += 1
            counts[label], hashes[label] = count, digest.hexdigest()
        missing = db.execute("""SELECT count(*) FROM pm_evidence e LEFT JOIN pm_sources s
            ON s.id=e.source WHERE s.id IS NULL""").fetchone()[0]
        missing += db.execute("""SELECT count(*) FROM pm_lineage l LEFT JOIN pm_sources p ON p.id=l.parent
            LEFT JOIN pm_sources c ON c.id=l.child WHERE p.id IS NULL OR c.id IS NULL""").fetchone()[0]
        require(missing == 0, "dangling_evidence_or_lineage")
        counts["legacy_lineage_resolved"] = db.execute("SELECT count(*) FROM pm_legacy_valid WHERE complete=1").fetchone()[0]
        counts["legacy_lineage_unresolved"] = db.execute("SELECT count(*) FROM pm_legacy_valid WHERE complete=0").fetchone()[0]
        counts["revoked_sources"] = db.execute("SELECT count(*) FROM pm_sources WHERE revoked=1").fetchone()[0]
    return {"counts": counts, "logical_sha256": hashes}


def immutable_sources(path):
    # This private in-memory map is never serialized or printed.
    with closing(ro(path)) as db:
        return {row["id"]: (row["digest"], hashlib.sha256(row["payload"].encode()).hexdigest())
                for row in db.execute("SELECT id,digest,payload FROM pm_sources")}


def copy_inputs(manifest, destination, *, synthetic_fixture=False):
    from harness.memory.production_migration import _load
    source, original, preset, owner, shared, paths = _load(manifest)
    require(original.get("schema_version") == "production-memory-snapshot-v1", "bridge_manifest_required")
    require(shared is not None, "shared_originals_required_for_drill")
    for path in paths:
        no_links(path)
        require(not path.is_relative_to(destination)
                or (synthetic_fixture and path.is_relative_to(destination / "synthetic-fixture")),
                "source_inside_drill_destination")
    before = {path: sha(path) for path in paths}
    copied = destination / "inputs"
    copied.mkdir(mode=0o700)
    # Only these two memory authorities are copied. Configs, tokens, logs and
    # unrelated snapshots are never enumerated or opened.
    preset_path = source.parent / original["preset_file"]
    preset_path = preset_path.resolve()
    for origin, name in ((preset_path, "preset.json"), (shared, "shared-memory.sqlite3")):
        target = copied / name
        with origin.open("rb") as old, target.open("xb") as new:
            shutil.copyfileobj(old, new)
        target.chmod(0o600)
        require(sha(origin) == sha(target), "copy_checksum_mismatch")
    frozen = {**original, "preset_file": "preset.json", "shared_database": "shared-memory.sqlite3",
              "preset_sha256": sha(copied / "preset.json"), "shared_sha256": sha(copied / "shared-memory.sqlite3")}
    copied_manifest = copied / "manifest.json"
    private_write(copied_manifest, frozen)
    return copied_manifest, preset, owner, before, {
        "manifest_sha256": sha(source), "preset_sha256": frozen["preset_sha256"],
        "shared_sha256": frozen["shared_sha256"], "owner_id": owner, "preset_id": preset.preset_id,
        "historical_snapshot_created_at": original.get("provenance", {}).get("created_at"),
        "source_age_policy": "historical_snapshot_only_not_current_live_state",
    }


def synthetic_manifest(root):
    from gui.presets import load_builtin
    from harness.core.models import EpisodicEvent, MemoryScope, SemanticFact
    from harness.memory.shared import SharedLocalEvent, SharedMemoryStore
    root.mkdir(mode=0o700)
    now = datetime(2026, 9, 15, tzinfo=timezone.utc)
    shared = SharedMemoryStore(root / "shared-memory.sqlite3")
    records = [shared.record_local(SharedLocalEvent(event_id="fixture-original-" + str(index), session_id="fixture",
        author_principal="local:main", role="user", text="SYNTHETIC baseline fact " + str(index),
        occurred_at=now, evidence_refs=("fixture-proof-" + str(index),))).record for index in range(4)]
    episode = EpisodicEvent(event_id="fixture-legacy-episode", time=now, participants=["local:main"],
        summary="SYNTHETIC old interpretation", objective_fact="SYNTHETIC old interpretation",
        importance=.5, emotional_valence=0, evidence=[records[0].record_id], source_refs=[records[0].record_id],
        scope=MemoryScope(channel="local_chat", conversation_id="fixture", visibility=["local:main"]))
    fact = SemanticFact(fact_id="fixture-legacy-belief", subject="local:main", predicate="fixture", value="SYNTHETIC belief",
        confidence=.5, supporting_events=[episode.event_id], formed_at=now)
    preset = load_builtin("misty").model_copy(update={"episodic": [episode], "semantic": [fact], "candidates": [],
        "relationships": [], "qq_memory_state": {}, "daily_revision_state": {}, "transcript": []})
    private_write(root / "preset.json", preset.model_dump(mode="json"))
    private_write(root / "manifest.json", {"schema_version": "production-memory-snapshot-v1", "owner_id": "misty",
        "preset_id": "misty", "preset_file": "preset.json", "shared_database": "shared-memory.sqlite3",
        "preset_sha256": sha(root / "preset.json"), "shared_sha256": sha(shared.path), "tombstones": [],
        "provenance": {"created_at": now.isoformat(), "synthetic_fixture": True}})
    return root / "manifest.json"


def rollback_drill(baseline, directory, preset, owner):
    from harness.core.models import EpisodicEvent, MemoryScope
    from harness.memory.production import BeliefRevision, Experience, ProductionMemoryStore
    directory.mkdir(mode=0o700)
    target = directory / "rollback-working.sqlite3"
    backup(baseline, target)
    baseline_originals = immutable_sources(baseline)
    memory = ProductionMemoryStore(target, owner_id=owner, preset_id=preset.preset_id)
    prefix = "synthetic-drill:" + sha(baseline)[:16] + ":"
    now = datetime.now(timezone.utc)
    scope = MemoryScope(owner_id=owner, channel="local_chat", conversation_id="synthetic-drill", visibility=["local:main"])
    source_ids = [prefix + name for name in ("withdrawn-source", "surviving-source")]
    require(not set(source_ids) & set(baseline_originals), "synthetic_identity_collision")
    memory.ingest([Experience(record_id=identifier, author_id="local:main", text="SYNTHETIC post-cutover fact " + str(index),
        occurred_at=now, scope=scope, source_refs=(identifier + ":original",), independent_key=identifier,
        original={"synthetic_drill_only": True}) for index, identifier in enumerate(source_ids)])
    belief_ids = [prefix + "belief-" + str(index) for index in range(2)]
    for index, (identifier, source) in enumerate(zip(belief_ids, source_ids, strict=True)):
        memory.revise(BeliefRevision(record_id=identifier, subject_id="local:main", topic="synthetic-drill-" + str(index),
            statement="SYNTHETIC revisable understanding", kind="self_report", revision=1,
            supporting_ids=(source,), valid_from=now), event_id=identifier + ":revision")
    memory.retain(source_ids[0], event_id=prefix + "pin", pinned=True)
    episodes = [EpisodicEvent(event_id=prefix + "legacy-" + str(index), time=now, participants=["local:main"],
        summary="SYNTHETIC compatibility interpretation", objective_fact="SYNTHETIC compatibility interpretation",
        importance=.5, emotional_valence=0, evidence=[source], source_refs=[source], scope=scope)
        for index, source in enumerate(source_ids)]
    upgraded = preset.model_copy(update={"episodic": [*preset.episodic, *episodes]})
    memory.import_legacy(upgraded)
    require(memory.validate_references([*source_ids, *belief_ids, *(row.event_id for row in episodes)], audience="local:main"),
            "synthetic_upgrade_projection_missing")
    # Commit a real source tombstone before rollback; retained/pinned entries
    # must never turn withdrawal into mere ranking or disappearance.
    memory.retract_source(source_ids[0], event_id=prefix + "withdraw", reason="synthetic drill withdrawal")
    withdrawn = [source_ids[0], belief_ids[0], episodes[0].event_id]
    require(all(not memory.validate_references([identifier], audience="local:main") for identifier in withdrawn),
            "withdrawal_not_propagated")
    backup(target, directory / "pre-rollback-retained.sqlite3")
    facts_before_rollback = immutable_sources(target)
    count_before = database_summary(target)["counts"]
    memory.import_legacy(preset)
    require(immutable_sources(target) == facts_before_rollback, "rollback_rewrote_or_dropped_original_facts")
    require(all(not memory.validate_references([row.event_id], audience="local:main") for row in episodes),
            "rollback_kept_removed_compatibility_projection")
    require(memory.validate_references([source_ids[1], belief_ids[1]], audience="local:main"),
            "rollback_lost_new_fact_or_understanding")
    require(all(not memory.validate_references([identifier], audience="local:main") for identifier in withdrawn),
            "rollback_resurrected_withdrawn_source")
    facts_after_rollback = immutable_sources(target)
    require(all(facts_after_rollback.get(identifier) == original for identifier, original in baseline_originals.items()),
            "drill_modified_historical_originals")
    count_after = database_summary(target)["counts"]
    require(count_after["sources"] == count_before["sources"] and count_after["revisions"] == count_before["revisions"],
            "rollback_dropped_fact_or_revision_history")
    # A release rollback must carry these SQL originals and histories forward;
    # restoring only the old preset would be an incomplete rollback bundle.
    private_write(directory / "legacy-preset.json", preset.model_dump(mode="json"))
    private_write(directory / "rollback-bundle.json", {
        "schema_version": "production-memory-rollback-bundle-v1", "owner_id": owner, "preset_id": preset.preset_id,
        "legacy_preset": "legacy-preset.json", "legacy_preset_sha256": sha(directory / "legacy-preset.json"),
        "retained_production_database": target.name, "retained_production_sha256": sha(target),
        "restore_rule": "retain_new_sql_originals_and_history_do_not_replace_with_old_preset_only",
        "live_services_modified": False, "contains_synthetic_drill_rows": True,
    })
    # Exercise forward recovery on another new copy, keeping the rollback
    # bundle frozen for independent inspection and later Linux repetition.
    forward = directory / "forward-recovery.sqlite3"
    backup(target, forward)
    restored = ProductionMemoryStore(forward, owner_id=owner, preset_id=preset.preset_id)
    restored.import_legacy(upgraded)
    require(restored.validate_references([source_ids[1], belief_ids[1], episodes[1].event_id], audience="local:main"),
            "forward_recovery_lost_surviving_evidence")
    require(all(not restored.validate_references([identifier], audience="local:main") for identifier in withdrawn),
            "forward_recovery_resurrected_withdrawn_source")
    require(immutable_sources(forward) == facts_before_rollback, "forward_recovery_changed_originals")
    return {"scope": "isolated_compatibility_projection_rollback_and_forward_recovery",
        "synthetic_post_cutover_sources": 2, "synthetic_beliefs": 2, "synthetic_legacy_projections": 2,
        "original_payloads_preserved": True, "new_fact_and_understanding_survive": True,
        "withdrawal_and_pin_precedence_preserved": True, "removed_projection_hidden": True,
        "forward_recovery_verified": True, "historical_source_count_unchanged": len(baseline_originals),
        "counts_before_rollback": count_before, "counts_after_rollback": count_after,
        "rollback_database_sha256": sha(target), "rollback_bundle_sha256": sha(directory / "rollback-bundle.json"),
        "not_proven": ["live_release_rollback", "old_software_reads_new_SQL_schema", "current_live_state_coverage"]}


def run(args, progress):
    from harness.memory.production import ProductionMemoryStore
    from harness.memory.production_migration import migrate_snapshot
    destination = args.destination_dir.absolute()
    no_links(destination)
    require(not destination.exists(), "destination_must_be_new")
    if args.manifest:
        origin = args.manifest.absolute()
        require(not destination.is_relative_to(origin.parent) and not origin.is_relative_to(destination),
                "source_destination_overlap")
    destination.mkdir(parents=True, mode=0o700)
    manifest = synthetic_manifest(destination / "synthetic-fixture") if args.self_test else args.manifest
    progress["stage"] = "copy_inputs"
    copied, preset, owner, source_hashes, source_report = copy_inputs(manifest, destination, synthetic_fixture=args.self_test)
    frozen_hashes = {path: sha(path) for path in (copied, copied.parent / "preset.json", copied.parent / "shared-memory.sqlite3")}
    progress["stage"] = "fresh_migration"
    fresh = destination / "fresh.sqlite3"
    report = migrate_snapshot(copied, fresh, batch_size=args.batch_size)
    require(report["complete"] and report["source_unchanged"], "fresh_migration_incomplete")
    expected = database_summary(fresh)
    progress["stage"] = "interrupted_resume"
    resumed = destination / "resumed.sqlite3"
    partial = migrate_snapshot(copied, resumed, batch_size=1, max_source_batches=1)
    continuation = migrate_snapshot(copied, resumed, batch_size=args.batch_size)
    require(continuation["complete"] and database_summary(resumed) == expected, "resumed_migration_differs")
    progress["stage"] = "idempotent_repeat"
    repeated = migrate_snapshot(copied, resumed, batch_size=args.batch_size)
    require(repeated["complete"] and database_summary(resumed) == expected, "migration_repeat_changed_logical_state")
    memory = ProductionMemoryStore(fresh, owner_id=owner, preset_id=preset.preset_id)
    hidden = 0
    with closing(ro(fresh)) as db:
        unresolved = [row[0] for row in db.execute("SELECT id FROM pm_legacy_valid WHERE complete=0")]
    for identifier in unresolved:
        require(not memory.validate_references([identifier], audience="local:main"), "unresolved_lineage_became_fact")
        hidden += 1
    progress["stage"] = "rollback_drill"
    rollback = rollback_drill(fresh, destination / "rollback", preset, owner)
    progress["stage"] = "final_source_hash_check"
    require(all(sha(path) == digest for path, digest in source_hashes.items()), "input_source_changed")
    require(all(sha(path) == digest for path, digest in frozen_hashes.items()), "frozen_input_copy_changed")
    result = {"schema_version": "production-memory-offline-drill-v1", "ok": True,
        "synthetic_input_fixture": bool(args.self_test), "source": source_report,
        "source_files_unchanged": True, "copied_inputs_unchanged": True,
        "migration": {"complete": True, "legacy_ids_verified": report["legacy_ids_verified"], **expected,
            "interruption_was_incomplete": not partial["complete"], "interrupted_resume_equal": True,
            "idempotent_repeat_equal": True, "unresolved_history_stayed_hidden": hidden},
        "rollback": rollback, "network_and_subprocess_calls_forbidden": True, "model_calls": 0,
        "credential_files_read": False, "live_services_modified": False,
        "limits": ["selected_memory_authorities_only", "historical_snapshot_not_current_live_snapshot",
                   "deterministic_engineering_drill_not_model_behavior_evaluation"]}
    private_write(destination / "report.json", result)
    progress["stage"] = "complete"
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--manifest", type=Path)
    source.add_argument("--self-test", action="store_true")
    parser.add_argument("--destination-dir", type=Path, required=True)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--batch-size", type=int, default=256)
    args = parser.parse_args(argv)
    progress = {"stage": "initialize"}
    try:
        require(1 <= args.batch_size <= 1000, "invalid_batch_size")
        require((args.repo / "harness" / "memory" / "production.py").is_file(), "repository_path_invalid")
        sys.path.insert(0, str(args.repo.resolve()))
        sys.addaudithook(offline_guard)
        result = run(args, progress)
        print(encoded(result))
        return 0
    except Exception as exc:
        # Pydantic/SQLite messages can contain original values. Never render
        # exception text unless it is one of this script's fixed safe codes.
        print(encoded({"schema_version": "production-memory-offline-drill-v1", "ok": False,
            "stage": progress["stage"], "error_type": type(exc).__name__,
            "error_code": str(exc) if type(exc) is DrillFailure else "details_suppressed_to_protect_source_content"}))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
