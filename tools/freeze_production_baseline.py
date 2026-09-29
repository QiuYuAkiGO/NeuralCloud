"""Make a non-destructive, checked local migration snapshot; never deploy it.

Pause writers before capture. SQLite backup includes committed WAL pages; all
source fingerprints must remain unchanged across the multi-file capture.
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
import subprocess


def sha(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def fingerprint(paths):
    return {str(path): sha(path) if path.exists() else None for path in paths}


def freeze(*, preset, destination, shared_database=None, repos=(), writers_stopped=False):
    if not writers_stopped:
        raise ValueError("freeze_requires_quiesced_writers")
    source = Path(preset).resolve(strict=True)
    shared = Path(shared_database).resolve(strict=True) if shared_database else None
    target = Path(destination).resolve()
    # No overwrite/resume: an incomplete capture is independently inspectable.
    target.mkdir(parents=True, exist_ok=False)
    from gui.presets import CharacterPreset
    value = CharacterPreset.model_validate_json(source.read_text(encoding="utf-8"))
    watched = [source, *([shared, Path(str(shared) + "-wal")] if shared else [])]
    before = fingerprint(watched)
    preset_copy = target / "preset.json"
    shutil.copyfile(source, preset_copy)
    manifest = {"schema_version": "production-memory-snapshot-v1", "owner_id": "misty",
        "preset_id": value.preset_id, "preset_file": preset_copy.name, "preset_sha256": sha(preset_copy),
        "created_at": datetime.now(timezone.utc).isoformat(), "tombstones": [], "repositories": []}
    if shared:
        copied = target / "shared-memory.sqlite3"
        with closing(sqlite3.connect(shared.as_uri() + "?mode=ro", uri=True)) as origin:
            with closing(sqlite3.connect(copied)) as backup:
                origin.backup(backup)
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise ValueError("snapshot_database_integrity_failure")
        manifest.update(shared_database=copied.name, shared_sha256=sha(copied))
    for repo in repos:
        root = Path(repo).resolve(strict=True)
        def git(*args):
            return subprocess.run(["git", "-C", str(root), *args], check=True,
                capture_output=True, text=True, encoding="utf-8").stdout.strip()
        manifest["repositories"].append({"path": str(root), "head": git("rev-parse", "HEAD"),
            "branch": git("branch", "--show-current"), "working_tree_status": git("status", "--short")})
    if before != fingerprint(watched):
        raise ValueError("source_changed_during_snapshot_no_manifest_published")
    manifest["source_fingerprints"] = before
    manifest_path = target / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"manifest": str(manifest_path), "source_unchanged": True,
            "includes_shared_originals": shared is not None, "repositories": len(repos)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--preset", required=True)
    parser.add_argument("--shared-database")
    parser.add_argument("--destination", required=True)
    parser.add_argument("--repo", action="append", default=[])
    parser.add_argument("--writers-stopped", action="store_true")
    args = parser.parse_args()
    print(json.dumps(freeze(preset=args.preset, shared_database=args.shared_database,
        destination=args.destination, repos=args.repo, writers_stopped=args.writers_stopped), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
