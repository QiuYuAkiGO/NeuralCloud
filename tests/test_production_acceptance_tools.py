"""Portable CLI and sealed-input guarantees for the published acceptance tools."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys

from tools.production_acceptance.memory_drill import synthetic_manifest


REPO = Path(__file__).resolve().parents[1]
BRIDGE = REPO / "tools/production_acceptance/bridge_production_snapshot.py"


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _sealed_inputs(tmp_path):
    fixture_manifest = synthetic_manifest(tmp_path / "synthetic-fixture")
    fixture = json.loads(fixture_manifest.read_text(encoding="utf-8"))
    snapshot = tmp_path / "sealed"
    snapshot.mkdir()
    rows = []
    for field, relative, role in (
        ("preset_file", "memory/selected.json", "presets"),
        ("shared_database", "memory/originals.sqlite3", "shared-memory"),
    ):
        target = snapshot / "data" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(fixture_manifest.parent / fixture[field], target)
        rows.append({"path": relative, "role": role, "size": target.stat().st_size, "sha256": _sha(target)})
    manifest = snapshot / "manifest.json"
    manifest.write_text(json.dumps({
        "schema_version": "neuralcloud-snapshot-v1", "writers_stopped": True,
        "desktop_status": "not-configured", "created_at": datetime(2026, 9, 15, tzinfo=timezone.utc).isoformat(),
        "files": rows,
    }), encoding="utf-8")
    (snapshot / "manifest.sha256").write_text(_sha(manifest), encoding="ascii")
    return snapshot, fixture


def _run(snapshot, fixture, destination, cwd, *, select_preset=True):
    command = [sys.executable, "-I", "-B", str(BRIDGE), "--snapshot", str(snapshot),
        "--destination", str(destination), "--owner-id", fixture["owner_id"],
        "--preset-id", fixture["preset_id"], "--shared-relative", "memory/originals.sqlite3"]
    if select_preset:
        command.extend(["--preset-relative", "memory/selected.json"])
    return subprocess.run(command, cwd=cwd, capture_output=True, text=True, timeout=30)


def test_bridge_external_cwd_preserves_inputs_and_rejects_replacement(tmp_path):
    snapshot, fixture = _sealed_inputs(tmp_path)
    before = {path: _sha(path) for path in snapshot.rglob("*") if path.is_file()}
    destination = tmp_path / "bridge"
    result = _run(snapshot, fixture, destination, tmp_path)
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["source_unchanged"] and report["source_files_copied"] == 2
    assert all(_sha(path) == expected for path, expected in before.items())
    published = destination / "manifest.json"
    published_hash = _sha(published)
    assert _run(snapshot, fixture, destination, tmp_path).returncode == 2
    assert _sha(published) == published_hash
    source = snapshot / "data/memory/selected.json"
    source.write_bytes(source.read_bytes() + b"\n")
    refused = tmp_path / "refused"
    result = _run(snapshot, fixture, refused, tmp_path)
    assert result.returncode == 2
    assert json.loads(result.stderr)["error_type"] == "ValueError"
    assert not refused.exists()


def test_bridge_requires_explicit_preset_selection(tmp_path):
    snapshot, fixture = _sealed_inputs(tmp_path)
    destination = tmp_path / "not-created"
    result = _run(snapshot, fixture, destination, tmp_path, select_preset=False)
    assert result.returncode == 2
    assert "--preset-relative" in result.stderr
    assert not destination.exists()
