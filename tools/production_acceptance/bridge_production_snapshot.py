"""Copy only selected memory inputs from a sealed offline deployment snapshot.

No service clients, credential/config payloads, live state, or model calls.
This verifies the manifest and selected inputs, not the entire snapshot.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path, PurePosixPath
import shutil
import sys


def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def no_links(path):
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ValueError("linked_input_or_destination")


def selected(snapshot, manifest, relative, role):
    path = PurePosixPath(relative)
    if path.is_absolute() or ".." in path.parts or str(path) != relative or "\\" in relative:
        raise ValueError("invalid_snapshot_relative_path")
    matches = [row for row in manifest["files"] if row["path"] == relative and row["role"] == role]
    if len(matches) != 1:
        raise ValueError("selected_role_not_unique")
    source = snapshot / "data" / relative
    no_links(source)
    row = matches[0]
    if source.stat().st_size != row["size"] or sha(source) != row["sha256"]:
        raise ValueError("selected_snapshot_input_changed")
    wal = Path(str(source) + "-wal")
    if wal.exists() and wal.stat().st_size:
        raise ValueError("selected_snapshot_has_nonempty_wal")
    return source, row


def bridge(args):
    snapshot = args.snapshot.absolute()
    destination = args.destination.absolute()
    no_links(snapshot)
    no_links(destination)
    if destination == snapshot or destination.is_relative_to(snapshot) or snapshot.is_relative_to(destination):
        raise ValueError("source_destination_overlap")
    manifest_file = snapshot / "manifest.json"
    digest_file = snapshot / "manifest.sha256"
    no_links(manifest_file)
    no_links(digest_file)
    expected = digest_file.read_text(encoding="ascii").strip()
    if len(expected) != 64 or sha(manifest_file) != expected:
        raise ValueError("snapshot_manifest_checksum_mismatch")
    manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
    if (manifest.get("schema_version") != "neuralcloud-snapshot-v1"
            or manifest.get("writers_stopped") is not True
            or manifest.get("desktop_status") not in {"stopped", "not-configured"}):
        raise ValueError("not_a_quiesced_deployment_snapshot")
    preset, preset_row = selected(snapshot, manifest, args.preset_relative, "presets")
    shared, shared_row = selected(snapshot, manifest, args.shared_relative, "shared-memory")
    destination.mkdir(parents=True, exist_ok=False, mode=0o700)
    for source, name in ((preset, "preset.json"), (shared, "shared-memory.sqlite3")):
        target = destination / name
        with source.open("rb") as origin, target.open("xb") as copied:
            shutil.copyfileobj(origin, copied)
        target.chmod(0o600)
    result_manifest = {"schema_version": "production-memory-snapshot-v1",
        "owner_id": args.owner_id, "preset_id": args.preset_id,
        "preset_file": "preset.json", "preset_sha256": preset_row["sha256"],
        "shared_database": "shared-memory.sqlite3", "shared_sha256": shared_row["sha256"],
        "tombstones": [], "provenance": {
            "schema_version": manifest["schema_version"], "created_at": manifest["created_at"],
            "manifest_sha256": expected, "verification_scope": "manifest_and_selected_memory_inputs",
            "preset_relative": args.preset_relative, "shared_relative": args.shared_relative}}
    pending = destination / "manifest.pending.json"
    pending.write_text(json.dumps(result_manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    pending.chmod(0o600)
    from harness.memory.production_migration import inspect_snapshot
    inventory = inspect_snapshot(pending)  # Parses only copied memory; never prints its text.
    if inventory["owner_id"] != args.owner_id or inventory["preset_id"] != args.preset_id:
        raise ValueError("memory_identity_mismatch")
    selected(snapshot, manifest, args.preset_relative, "presets")
    selected(snapshot, manifest, args.shared_relative, "shared-memory")
    if sha(manifest_file) != expected:
        raise ValueError("snapshot_manifest_changed_during_bridge")
    final = destination / "manifest.json"
    pending.rename(final)
    return {"ok": True, "verification_scope": "manifest_and_selected_memory_inputs",
        "source_created_at": manifest["created_at"], "source_manifest_sha256": expected,
        "source_unchanged": True, "owner_id": args.owner_id, "preset_id": args.preset_id,
        "counts": inventory["counts"], "source_files_copied": 2,
        "destination_manifest": str(final), "services_started": False,
        "credential_files_read": False, "historical_snapshot_only": True}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--destination", type=Path, required=True)
    parser.add_argument("--owner-id", required=True)
    parser.add_argument("--preset-id", required=True)
    parser.add_argument("--preset-relative", required=True)
    parser.add_argument("--shared-relative", default="state/shared-memory.sqlite3")
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args(argv)
    try:
        if not (args.repo / "harness/memory/production_migration.py").is_file():
            raise ValueError("repository_path_invalid")
        sys.path.insert(0, str(args.repo.resolve()))
        print(json.dumps(bridge(args), ensure_ascii=False, indent=2))
        return 0
    except Exception as exc:
        # Pydantic input errors can contain private originals; suppress values.
        print(json.dumps({"ok": False, "error_type": type(exc).__name__,
            "detail": "bridge failed; inspect the isolated inputs without publishing their contents"}), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
