"""Stopped-writer snapshots and strictly offline verification/restoration.

This module never starts services, opens application stores, repairs journals, or
stops source processes. The caller must stop every writer before acknowledging
the export. SQLite's backup API includes committed WAL data; a before/after
inventory additionally rejects sources that changed during export.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import sys
import uuid
from collections.abc import Iterable, Mapping
from contextlib import closing
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

SCHEMA = "neuralcloud-snapshot-v1"
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ROLE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}\Z")
_SQLITE_HEADER = b"SQLite format 3\x00"
_PATH_KEYS = {
    "memory_db", "state_db", "token_file", "manifest_path", "profile_path", "source_path",
    "state_root", "config_root", "log_root", "evidence_dir", "image_cache_dir",
    "allowed_local_image_roots", "session_config_path", "model_config_path", "preset_path",
    "watcher_status_file", "direct_status_file", "homeostasis_profile_path", "homeostasis_state_dir",
    "body_token_file", "agent_token_file", "shared_control_token_file", "mistyai_token_file",
    "status_file", "control_file", "desktop_status_file", "trace_file", "initial_baseline_observation",
    "api_key_file", "queue_path",
}
_BINDING_PATHS = {
    "NC_GUI_CONFIG", "NC_GUI_PRESETS", "NC_COGNITIVE_STATE_PATH", "NC_COGNITIVE_AUTONOMOUS_LOG_DIR",
    "NC_RUNTIME_STATE_DIR", "NC_HOMEOSTASIS_STATE_DIR", "NC_SHARED_MEMORY_DB", "NC_QQ_SESSION_CONFIG",
    "NC_QQ_WATCHER_CONFIG", "NC_QQ_DIRECT_CONFIG", "NC_CONTROL_TOKEN_FILE", "NC_SERVICE_TOKEN_FILE",
    "NC_BOARD_CONFIG", "NC_VOICE_CONFIG", "NC_WEB_CONFIG", "NC_CALL_LOG_DIR", "NC_RUNTIME_MANIFEST_PATH",
    "NC_COGNITIVE_MANIFEST_PATH", "NC_HOMEOSTASIS_PROFILE_PATH", "NC_EXPERIMENT_MANIFEST",
    "NC_PROFILE_REVISION_DIR", "NC_MANIFEST_REVISION_DIR",
    "NC_DAILY_REVISION_CONFIG",
}
_TRANSIENT_NAMES = {
    ".neuralcloud.pids.json", ".neuralcloud.pids.json.launch.json", ".gui.pid",
    "active-start.json", "pending-start.json", "watcher.status.json", "direct.status.json",
}


class SnapshotError(ValueError):
    """A snapshot is unsafe, incomplete, corrupt, or not quiescent."""


class SnapshotSourceChanged(SnapshotError):
    """Source metadata drift; diagnostic records contain paths/roles, never payloads."""

    def __init__(self, changes: list[dict[str, Any]]) -> None:
        self.changes = changes
        super().__init__("source changed during export; stop all writers and retry; source_changes="
                         + json.dumps(changes, ensure_ascii=False, sort_keys=True))


@dataclass(frozen=True)
class SourceSpec:
    role: str
    path: Path | str
    target: str
    required: bool = True
    rewrite_config: bool = False
    reference_only: bool = False


def _safe_relative(value: str) -> PurePosixPath:
    if not isinstance(value, str) or not value or "\\" in value or ":" in value or "\x00" in value:
        raise SnapshotError("snapshot path must be a portable relative path")
    parts = value.split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) for part in parts):
        raise SnapshotError("snapshot path contains an unsafe component")
    path = PurePosixPath(value)
    if path.is_absolute() or PureWindowsPath(value).drive:
        raise SnapshotError("absolute snapshot paths are forbidden")
    if any(PureWindowsPath(part).is_reserved() for part in parts):
        raise SnapshotError("snapshot path contains a reserved filename")
    return path


def _no_links(path: Path) -> None:
    """Reject symlinks, Windows junctions, and other reparse points, including parents."""
    for candidate in (path, *path.parents):
        try:
            info = candidate.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise SnapshotError(f"symlink or reparse point is forbidden: {candidate}")


def _absolute(path: Path | str) -> Path:
    result = Path(path).absolute()
    _no_links(result)
    return result


def _transient(path: Path) -> bool:
    name = path.name.lower()
    return (name in _TRANSIENT_NAMES or name.endswith((".lock", ".pid", ".tmp", "-wal", "-shm", "-journal"))
            or name.endswith(".status.json") or name.startswith(".neuralcloud.pids."))


def _walk(path: Path) -> list[Path]:
    _no_links(path)
    if path.is_file():
        return [path]
    if not path.is_dir():
        raise SnapshotError(f"source is not a regular file or directory: {path}")
    result: list[Path] = []
    for directory, dirs, files in os.walk(path, followlinks=False):
        for name in [*dirs, *files]:
            child = Path(directory) / name
            _no_links(child)
            if name in files:
                if not child.is_file():
                    raise SnapshotError(f"non-regular source file: {child}")
                result.append(child)
    return sorted(result)


def _hash(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sqlite(path: Path) -> bool:
    with path.open("rb") as handle:
        return handle.read(16) == _SQLITE_HEADER or path.suffix.lower() in {".sqlite3", ".sqlite", ".db"}


def _canonical(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SnapshotError(f"invalid JSON: {path}") from exc


def rewrite_config_paths(value: Any, replacements: Mapping[str, str]) -> Any:
    """Rewrite only recognized path fields in parsed configuration copies.

    No replacement is ever applied to journal, receipt, preset, database, or
    arbitrary free-text values. Prefix matches require a path boundary.
    """
    prefixes = sorted(replacements.items(), key=lambda pair: len(pair[0]), reverse=True)

    def rewrite(text: str) -> str:
        for source, destination in prefixes:
            if not source or not isinstance(destination, str) or not destination:
                raise SnapshotError("path replacements must have nonempty source and destination")
            windows = bool(PureWindowsPath(source).drive) or "\\" in source
            normalized = text.replace("\\", "/") if windows else text
            old = source.replace("\\", "/").rstrip("/") if windows else source.rstrip("/")
            left, right = (normalized.casefold(), old.casefold()) if windows else (normalized, old)
            if left == right or left.startswith(right + "/"):
                return destination.rstrip("/") + normalized[len(old):]
        return text

    def visit(item: Any, key: str = "") -> Any:
        path_field = key in _PATH_KEYS or key in _BINDING_PATHS
        if isinstance(item, dict):
            return {name: visit(child, name) for name, child in item.items()}
        if isinstance(item, list):
            return [rewrite(child) if path_field and isinstance(child, str) else visit(child) for child in item]
        return rewrite(item) if path_field and isinstance(item, str) else item

    return visit(value)


def _inventory(sources: Iterable[SourceSpec]) -> tuple[list[tuple[SourceSpec, Path, str]], dict[str, tuple[int, int]], list[str]]:
    rows: list[tuple[SourceSpec, Path, str]] = []
    stamps: dict[str, tuple[int, int]] = {}
    absent: list[str] = []
    targets: dict[str, str] = {}
    for spec in sources:
        if not isinstance(spec.role, str) or not _ROLE.fullmatch(spec.role):
            raise SnapshotError("invalid source role")
        if not all(isinstance(flag, bool) for flag in (spec.required, spec.rewrite_config, spec.reference_only)):
            raise SnapshotError("source flags must be booleans")
        target = _safe_relative(spec.target)
        source = _absolute(spec.path)
        if spec.reference_only:
            continue
        if not source.exists():
            if spec.required:
                raise SnapshotError(f"required source missing: {spec.role}: {source}")
            absent.append(spec.role)
            continue
        single = source.is_file()
        if spec.rewrite_config and not single:
            raise SnapshotError("configuration rewriting requires an explicitly listed JSON file")
        for file in _walk(source):
            if _transient(file):
                # WAL is excluded from payload because backup() merges it, but
                # participates in drift detection. SHM is merely a reader index.
                if file.name.endswith("-wal"):
                    info = file.stat()
                    # A read-only SQLite connection can create a zero-byte WAL
                    # beside a clean, closed WAL-mode DB. It carries no frames
                    # and must not make our own backup look like a live writer.
                    if info.st_size:
                        stamps[str(file)] = (info.st_size, info.st_mtime_ns)
                continue
            info = file.stat()
            stamps[str(file)] = (info.st_size, info.st_mtime_ns)
            relative = str(target if single else target / file.relative_to(source).as_posix())
            _safe_relative(relative)
            folded = relative.casefold()
            if folded in targets:
                raise SnapshotError(f"overlapping snapshot target: {relative}")
            targets[folded] = str(file)
            rows.append((spec, file, relative))
            if _sqlite(file):
                wal = Path(str(file) + "-wal")
                _no_links(wal)
                if wal.exists():
                    info = wal.stat()
                    if info.st_size:
                        stamps[str(wal)] = (info.st_size, info.st_mtime_ns)
    return rows, stamps, sorted(absent)


def source_change_diagnostics(
    sources: Iterable[SourceSpec], before: Mapping[str, tuple[int, int]], after: Mapping[str, tuple[int, int]],
) -> list[dict[str, Any]]:
    """Return only role/path/change diagnostics, never file contents or credentials."""
    specs = tuple(sources)
    result = []
    for name in sorted(before.keys() | after.keys()):
        if before.get(name) == after.get(name):
            continue
        path = Path(name)
        roles = sorted({spec.role for spec in specs if not spec.reference_only
                        and (path == Path(spec.path).absolute() or path.is_relative_to(Path(spec.path).absolute())
                             or name == str(Path(spec.path).absolute()) + "-wal")})
        result.append({"roles": roles, "path": name,
                       "change": "added" if name not in before else "removed" if name not in after else "modified"})
    return result


def _new_stage(destination: Path) -> Path:
    _no_links(destination)
    if destination.exists():
        raise SnapshotError(f"destination already exists; restore into a new directory: {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}.incomplete")
    stage.mkdir(mode=0o700)
    return stage


def _make_private_dirs(path: Path, root: Path) -> None:
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in (path, *path.parents):
        if directory == root:
            break
        directory.chmod(0o700)


def _private_file(path: Path) -> None:
    path.chmod(0o600)


def _backup_sqlite(source: Path, target: Path) -> None:
    try:
        with closing(sqlite3.connect(source.as_uri() + "?mode=ro", uri=True, timeout=10)) as original:
            with closing(sqlite3.connect(target)) as backup:
                original.backup(backup)
                backup.execute("PRAGMA journal_mode=DELETE")
    except sqlite3.Error as exc:
        raise SnapshotError(f"SQLite backup failed: {source}") from exc


def export_snapshot(
    sources: Iterable[SourceSpec], destination: Path | str, *, stopped_ack: bool,
    desktop_status: str, config_rewrites: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Export one consistent stopped-writer snapshot to a new directory.

    ``stopped_ack`` asserts that core, QQDirect/watcher, session/body, Board,
    NapCat configuration writers and maintenance jobs using these sources have
    stopped. ``desktop_status`` must separately declare stopped/not-configured.
    Neither assertion is a replacement for the operator's service status check.
    """
    if stopped_ack is not True or desktop_status not in {"stopped", "not-configured"}:
        raise SnapshotError("all writers must be stopped and desktop status explicitly declared")
    specs = tuple(sources)
    if not specs:
        raise SnapshotError("at least one source is required")
    destination = _absolute(destination)
    for spec in specs:
        source = _absolute(spec.path)
        if destination == source or destination.is_relative_to(source) or source.is_relative_to(destination):
            raise SnapshotError("snapshot destination overlaps a source")
    rows, stamps, absent = _inventory(specs)
    stage = _new_stage(destination)
    try:
        records: list[dict[str, Any]] = []
        for spec, source, relative in rows:
            target = stage / "data" / relative
            _make_private_dirs(target.parent, stage)
            kind = "sqlite" if _sqlite(source) else "file"
            if kind == "sqlite":
                if spec.rewrite_config:
                    raise SnapshotError("SQLite is not a configuration document")
                _backup_sqlite(source, target)
            elif spec.rewrite_config:
                target.write_bytes(_canonical(rewrite_config_paths(_json(source), config_rewrites or {})))
                kind = "config"
            else:
                shutil.copyfile(source, target)
            _private_file(target)
            records.append({"path": relative, "role": spec.role, "kind": kind,
                            "size": target.stat().st_size, "sha256": _hash(target)})
        later_rows, later_stamps, later_absent = _inventory(specs)
        if stamps != later_stamps or absent != later_absent or len(rows) != len(later_rows):
            raise SnapshotSourceChanged(source_change_diagnostics(specs, stamps, later_stamps))
        manifest = {
            "schema_version": SCHEMA, "created_at": datetime.now(UTC).isoformat(),
            "writers_stopped": True, "desktop_status": desktop_status,
            "sources": [{"role": spec.role, "path": str(_absolute(spec.path)), "target": spec.target,
                         "required": spec.required, "rewrite_config": spec.rewrite_config,
                         "reference_only": spec.reference_only} for spec in specs],
            "missing_optional_roles": absent, "files": sorted(records, key=lambda item: item["path"]),
        }
        encoded = _canonical(manifest)
        (stage / "manifest.json").write_bytes(encoded)
        (stage / "manifest.sha256").write_text(hashlib.sha256(encoded).hexdigest() + "\n", encoding="ascii")
        _private_file(stage / "manifest.json")
        _private_file(stage / "manifest.sha256")
        verify_snapshot(stage)
        os.replace(stage, destination)
        return manifest
    except BaseException:
        shutil.rmtree(stage)
        raise


def _verify_sqlite(path: Path) -> None:
    # Exported databases are standalone. immutable avoids WAL/SHM creation and
    # must never be used to read a live source that may have committed WAL data.
    try:
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro&immutable=1", uri=True, timeout=5)) as connection:
            result = connection.execute("PRAGMA integrity_check").fetchall()
            if result != [("ok",)]:
                raise SnapshotError(f"SQLite integrity check failed: {path}")
    except sqlite3.Error as exc:
        raise SnapshotError(f"invalid SQLite database: {path}") from exc


def _verify_journals(root: Path, records: list[dict[str, Any]]) -> dict[str, Any]:
    # Import the immutable record schema only. RuntimeJournal would truncate
    # malformed trailing data on construction, which offline verification must
    # never do.
    from harness.runtime.journal import RuntimeJournalRecord

    groups: dict[Path, list[Path]] = {}
    for record in records:
        relative = PurePosixPath(record["path"])
        if re.fullmatch(r"runtime-\d{6}\.jsonl", relative.name):
            path = root / relative
            groups.setdefault(path.parent, []).append(path)
    summaries: dict[str, Any] = {}
    for directory, paths in groups.items():
        seq, previous = 0, ""
        checkpoint = directory.parent / "checkpoint.json"
        checkpoint_data = _json(checkpoint) if checkpoint.exists() else None
        checkpoint_seq = checkpoint_data.get("journal_record_seq") if checkpoint_data else None
        checkpoint_hash = "" if checkpoint_seq == 0 else None
        expected_segment = 1
        for path in sorted(paths):
            if path.name != f"runtime-{expected_segment:06d}.jsonl":
                raise SnapshotError(f"missing journal segment: {directory}")
            expected_segment += 1
            with path.open("rb") as handle:
                for line in handle:
                    if not line.endswith(b"\n"):
                        raise SnapshotError(f"incomplete journal tail: {path}")
                    try:
                        item = RuntimeJournalRecord.model_validate_json(line)
                    except ValueError as exc:
                        raise SnapshotError(f"invalid journal record: {path}") from exc
                    if item.record_seq != seq + 1 or item.prev_hash != previous or not item.verify_checksum():
                        raise SnapshotError(f"journal hash chain is invalid at sequence {seq + 1}: {path}")
                    seq, previous = item.record_seq, item.checksum
                    if seq == checkpoint_seq:
                        checkpoint_hash = previous
        if checkpoint_data and (checkpoint_hash is None or checkpoint_data.get("journal_hash") != checkpoint_hash):
            raise SnapshotError(f"checkpoint does not match its journal: {checkpoint}")
        summaries[directory.relative_to(root).as_posix()] = {"records": seq, "last_hash": previous}
    return summaries


def verify_snapshot(snapshot: Path | str) -> dict[str, Any]:
    """Read-only hash, inventory, SQLite and journal-chain checks; never repair."""
    snapshot = _absolute(snapshot)
    actual_paths = _walk(snapshot)
    if not (snapshot / "manifest.json").is_file() or not (snapshot / "manifest.sha256").is_file():
        raise SnapshotError("snapshot manifest and digest are required")
    try:
        expected_digest = (snapshot / "manifest.sha256").read_text(encoding="ascii").strip()
    except UnicodeError as exc:
        raise SnapshotError("invalid snapshot manifest digest") from exc
    if not _DIGEST.fullmatch(expected_digest) or _hash(snapshot / "manifest.json") != expected_digest:
        raise SnapshotError("snapshot manifest hash mismatch")
    manifest = _json(snapshot / "manifest.json")
    if (not isinstance(manifest, dict) or manifest.get("schema_version") != SCHEMA
            or manifest.get("writers_stopped") is not True
            or manifest.get("desktop_status") not in {"stopped", "not-configured"}
            or not isinstance(manifest.get("files"), list)):
        raise SnapshotError("invalid snapshot manifest")
    expected = {"manifest.json", "manifest.sha256"}
    folded: set[str] = set()
    sqlite_count = 0
    total = 0
    records = manifest["files"]
    for record in records:
        if not isinstance(record, dict):
            raise SnapshotError("invalid snapshot file record")
        relative = _safe_relative(record.get("path"))
        relative_key = str(relative).casefold()
        if relative_key in folded:
            raise SnapshotError("duplicate snapshot file")
        folded.add(relative_key)
        if (not isinstance(record.get("role"), str) or not _ROLE.fullmatch(record["role"])
                or record.get("kind") not in {"file", "config", "sqlite"}
                or not isinstance(record.get("size"), int) or isinstance(record["size"], bool) or record["size"] < 0
                or not isinstance(record.get("sha256"), str) or not _DIGEST.fullmatch(record["sha256"])):
            raise SnapshotError("invalid snapshot file metadata")
        path = snapshot / "data" / relative
        if not path.is_file() or path.stat().st_size != record["size"] or _hash(path) != record["sha256"]:
            raise SnapshotError(f"snapshot file hash or size mismatch: {relative}")
        expected.add("data/" + str(relative))
        total += record["size"]
        if record["kind"] == "sqlite":
            _verify_sqlite(path)
            sqlite_count += 1
        elif record["kind"] == "config":
            _json(path)
    actual = {path.relative_to(snapshot).as_posix() for path in actual_paths}
    if actual != expected:
        raise SnapshotError("snapshot contains missing or unlisted files")
    journals = _verify_journals(snapshot / "data", records)
    return {"ok": True, "files": len(records), "bytes": total, "sqlite_databases": sqlite_count,
            "journals": journals, "manifest_sha256": expected_digest}


def restore_snapshot(snapshot: Path | str, destination: Path | str) -> dict[str, Any]:
    """Restore verified bytes to a NEW staging/state root, never over live data."""
    snapshot = _absolute(snapshot)
    destination = _absolute(destination)
    if destination == snapshot or destination.is_relative_to(snapshot) or snapshot.is_relative_to(destination):
        raise SnapshotError("restore destination overlaps snapshot")
    verification = verify_snapshot(snapshot)
    manifest = _json(snapshot / "manifest.json")
    stage = _new_stage(destination)
    try:
        for record in manifest["files"]:
            relative = _safe_relative(record["path"])
            source = snapshot / "data" / relative
            _no_links(source)
            target = stage / relative
            _make_private_dirs(target.parent, stage)
            shutil.copyfile(source, target)
            _private_file(target)
            if target.stat().st_size != record["size"] or _hash(target) != record["sha256"]:
                raise SnapshotError("snapshot changed while restoring")
        os.replace(stage, destination)
        return {**verification, "destination": str(destination)}
    except BaseException:
        shutil.rmtree(stage)
        raise


def discover_shared_sources(
    repo_root: Path | str, bindings: Mapping[str, Any], *, extras: Iterable[SourceSpec] = (),
) -> list[SourceSpec]:
    """Inventory explicit shared bindings, plus paths referenced by private configs.

    Binding values must already be expanded absolute paths. No user name,
    account identifier, key, or deployment-specific location is embedded here.
    Targets form a portable staging layout; config rewriting is explicit later.
    """
    repo = _absolute(repo_root)
    sources: list[SourceSpec] = []
    seen: set[str] = set()

    def add(role: str, path: Any, target: str, *, required: bool = True, config: bool = False,
            reference_only: bool = False, copy_separately: bool = False) -> None:
        if path is None or path == "":
            return
        source = Path(path)
        if not source.is_absolute():
            raise SnapshotError(f"binding requires an expanded absolute path: {role}")
        if not config and not reference_only and not copy_separately and any(
            not item.reference_only and Path(item.path).is_dir()
            and source.is_relative_to(Path(item.path)) for item in sources
        ):
            return
        key = str(source.absolute()).casefold() if os.name == "nt" else str(source.absolute())
        if key not in seen:
            seen.add(key)
            sources.append(SourceSpec(role, source, target, required, config, reference_only))

    add("gui-config", bindings.get("NC_GUI_CONFIG", repo / "data/gui_settings.json"), "config/gui-settings.json", config=True)
    add("presets", bindings.get("NC_GUI_PRESETS", repo / "data/presets"), "state/presets")
    cognitive = Path(bindings.get("NC_COGNITIVE_STATE_PATH", repo / "data/cognitive/state.json"))
    add("cognitive", cognitive.parent, "state/cognitive")
    add("runtime", bindings.get("NC_RUNTIME_STATE_DIR"), "state/runtime")
    homeostasis = bindings.get("NC_HOMEOSTASIS_STATE_DIR")
    add("homeostasis", homeostasis, "state/homeostasis/domain")
    if homeostasis and Path(homeostasis).name == "domain":
        add("homeostasis-parent", Path(homeostasis).parent, "state/homeostasis", required=False, reference_only=True)
        add("qq-feedback", Path(homeostasis).parent / "qq-feedback.sqlite3", "state/homeostasis/qq-feedback.sqlite3", required=False)
    add("shared-memory", bindings.get("NC_SHARED_MEMORY_DB"), "state/shared-memory.sqlite3")
    add("homeostasis-revisions", bindings.get("NC_PROFILE_REVISION_DIR", repo / "data/homeostasis/profiles"),
        "state/homeostasis/profiles", required=False)
    add("manifest-revisions", bindings.get("NC_MANIFEST_REVISION_DIR", repo / "data/runtime/manifests"),
        "state/runtime/manifests", required=False)
    for key, role, name in (
        ("NC_CONTROL_TOKEN_FILE", "control-token", "control.token"),
        ("NC_SERVICE_TOKEN_FILE", "service-token", "service.token"),
        ("NC_RUNTIME_MANIFEST_PATH", "manifest", "manifest.json"),
        ("NC_HOMEOSTASIS_PROFILE_PATH", "homeostasis-profile", "homeostasis-profile.json"),
    ):
        add(role, bindings.get(key), "config/" + name, config=name.endswith(".json"))
    for binding, role, filename in (
        ("NC_QQ_SESSION_CONFIG", "qq-session", "session.json"),
        ("NC_QQ_WATCHER_CONFIG", "qq-watcher", "watcher.json"),
        ("NC_QQ_DIRECT_CONFIG", "qq-direct", "direct.json"),
        ("NC_BOARD_CONFIG", "board-client", "board-client.json"),
        ("NC_VOICE_CONFIG", "voice-client", "voice.json"),
        ("NC_WEB_CONFIG", "web-client", "web/config.json"),
        ("NC_DAILY_REVISION_CONFIG", "daily-revision", "daily-revision.json"),
    ):
        path = bindings.get(binding)
        if not path:
            continue
        add(role + "-config", path, "config/" + filename, config=True)
        payload = _json(Path(path))
        if not isinstance(payload, dict):
            raise SnapshotError(f"configuration must be an object: {role}")
        if role == "web-client":
            if payload.get("schema_version") == "web-config-index-v1":
                parent = Path(path).parent
                add("web-revisions", parent / "revisions", "config/web/revisions", copy_separately=True)
                add("web-keys", parent / "keys", "config/web/keys", required=False, copy_separately=True)
        for key in ("token_file", "body_token_file", "agent_token_file", "shared_control_token_file", "api_key_file"):
            add(role + "-" + key.replace("_", "-"), payload.get(key), f"config/secrets/{role}-{key}")
        for key in ("memory_db", "state_db", "evidence_dir", "image_cache_dir", "queue_path"):
            value = payload.get(key)
            if value:
                add(role + "-" + key.replace("_", "-"), value, f"state/{role}/{Path(value).name}")
        for key, status_role, basename in (
            ("watcher_status_file", "qq-watcher", "watcher.status.json"),
            ("direct_status_file", "qq-direct", "direct.status.json"),
        ):
            add(status_role + "-status", payload.get(key), f"state/{status_role}/{basename}",
                required=False, reference_only=True)
        if role in {"qq-direct", "qq-watcher"}:
            stem = "direct" if role == "qq-direct" else "watcher"
            add(role + "-status", payload.get("status_file"), f"state/{role}/{stem}.status.json",
                required=False, reference_only=True)
            add(role + "-control", payload.get("control_file"), f"state/{role}/{stem}.control.json", required=False)
        if role == "qq-direct":
            add("desktop-status", payload.get("desktop_status_file"), "state/qq-desktop/desktop.status.json",
                required=False, reference_only=True)
        state_db = payload.get("state_db")
        if state_db:
            state_path = Path(state_db)
            if role == "qq-watcher":
                roster = state_path.with_suffix(".roster.sqlite3")
                add("qq-roster", roster, f"state/qq-watcher/{roster.name}", required=False)
                status_path = Path(payload.get("status_file") or state_path.parent / "watcher.status.json")
                trace = Path(payload.get("trace_file") or status_path.with_name("watcher-trace.jsonl"))
                add("qq-watcher-trace", trace, f"state/qq-watcher/{trace.name}", required=False)
                for kind in ("observation", "gaps"):
                    sibling = trace.with_suffix(f".{kind}.jsonl")
                    add(f"qq-watcher-trace-{kind}", sibling, f"state/qq-watcher/{sibling.name}", required=False)
                baseline = payload.get("initial_baseline_observation")
                if baseline:
                    add("qq-watcher-baseline", baseline, f"state/qq-watcher/{Path(baseline).name}")
            elif role == "qq-direct":
                trace = state_path.with_suffix(".trace.jsonl")
                add("qq-direct-trace", trace, f"state/qq-direct/{trace.name}", required=False)
            if role in {"qq-direct", "qq-watcher"} and not payload.get("control_file"):
                stem = "direct" if role == "qq-direct" else "watcher"
                add(role + "-control", state_path.parent / f"{stem}.control.json",
                    f"state/{role}/{stem}.control.json", required=False)
            if role in {"qq-direct", "qq-watcher"}:
                if payload.get("status_file"):
                    continue
                basename = "direct.status.json" if role == "qq-direct" else "watcher.status.json"
                add(role + "-status", state_path.parent / basename, f"state/{role}/{basename}",
                    required=False, reference_only=True)
    sources.extend(extras)
    return sources


def build_sources_manifest(
    sources: Iterable[SourceSpec], *, config_root: str = "/etc/neuralcloud",
    state_root: str = "/var/lib/neuralcloud", extra_path_rewrites: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Build a private portable source list, including reference-only path maps."""
    for root in (config_root, state_root):
        if not PurePosixPath(root).is_absolute() or ".." in PurePosixPath(root).parts or "\\" in root:
            raise SnapshotError("destination roots must be absolute Linux paths")
    rows = []
    rewrites: dict[str, str] = {}
    for spec in sources:
        relative = _safe_relative(spec.target)
        parts = relative.parts
        if parts[0] not in {"config", "state"} or len(parts) < 2:
            raise SnapshotError("mapped source targets must begin with config/ or state/")
        root = config_root if parts[0] == "config" else state_root
        destination = str(PurePosixPath(root).joinpath(*parts[1:]))
        source = str(_absolute(spec.path))
        if source in rewrites and rewrites[source] != destination:
            raise SnapshotError("source has conflicting deployment destinations")
        rewrites[source] = destination
        rows.append({"role": spec.role, "path": source, "target": spec.target, "required": spec.required,
                     "rewrite_config": spec.rewrite_config, "reference_only": spec.reference_only})
    rewrites.update(extra_path_rewrites or {})
    return {"sources": rows, "path_rewrites": rewrites}


def _load_source_manifest(path: Path) -> tuple[list[SourceSpec], dict[str, str]]:
    payload = _json(path)
    if not isinstance(payload, dict) or not isinstance(payload.get("sources"), list):
        raise SnapshotError("sources manifest requires a sources array")
    try:
        sources = [SourceSpec(**row) for row in payload["sources"]]
        replacements = payload.get("path_rewrites", {})
        if not isinstance(replacements, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in replacements.items()):
            raise SnapshotError("path_rewrites must be a string mapping")
        return sources, replacements
    except TypeError as exc:
        raise SnapshotError("invalid source specification") from exc


def _deployment_sources(config: Any) -> list[SourceSpec]:
    """Keep Linux state/config roots intact, and include external bindings."""
    release = config.path("current").resolve(strict=True)
    bindings = config.effective_bindings()
    bindings["NC_RUNTIME_MANIFEST_PATH"] = str(release / config.manifest_path)
    bindings["NC_HOMEOSTASIS_PROFILE_PATH"] = str(release / config.profile_path)
    roots = [config.path("state_root"), config.path("config_root")]
    sources = [SourceSpec("state-root", roots[0], "state"), SourceSpec("config-root", roots[1], "config")]
    candidates = discover_shared_sources(release, bindings)
    candidates.append(SourceSpec("deployment-config", config.config_file, "config/deployment.json"))
    if "board" in config.enabled_components:
        candidates.extend([
            SourceSpec("board-state", bindings.get("NC_BOARD_STATE_DIR", roots[0] / "board"), "state/board"),
            SourceSpec("board-token", bindings.get("NC_BOARD_TOKEN_FILE", roots[1] / "board.token"), "config/board.token"),
        ])
    seen = set()
    for spec in candidates:
        path = Path(spec.path).absolute()
        if spec.required and not spec.reference_only and not path.exists():
            raise SnapshotError(f"required source missing: {spec.role}: {path}")
        if path in seen or any(path.is_relative_to(root.absolute()) for root in roots):
            continue
        seen.add(path)
        sources.append(spec)
    return sources


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("backup", "verify", "restore"))
    parser.add_argument("--config", type=Path, help="Deployment config (backup discovery only)")
    parser.add_argument("--sources-manifest", type=Path, help="Explicit sources and optional path_rewrites JSON")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--snapshot", type=Path)
    parser.add_argument("--destination", type=Path)
    parser.add_argument("--writers-stopped", action="store_true", help="Assert every writer is stopped")
    parser.add_argument("--desktop-status", choices=("stopped", "not-configured"))
    args = parser.parse_args(argv)
    try:
        if args.command == "backup":
            if not args.output:
                raise SnapshotError("backup requires --output")
            if args.sources_manifest:
                sources, replacements = _load_source_manifest(args.sources_manifest)
            elif args.config:
                from harness.deploy.config import load_config

                config = load_config(args.config)
                sources = _deployment_sources(config)
                replacements = {}
            else:
                raise SnapshotError("backup requires --sources-manifest or --config")
            manifest = export_snapshot(sources, args.output, stopped_ack=args.writers_stopped,
                                       desktop_status=args.desktop_status, config_rewrites=replacements)
            result = {"ok": True, "snapshot": str(args.output.absolute()), "files": len(manifest["files"])}
        elif args.command == "verify":
            if not args.snapshot:
                raise SnapshotError("offline verify requires --snapshot")
            result = verify_snapshot(args.snapshot)
        else:
            if not args.snapshot or not args.destination:
                raise SnapshotError("restore requires --snapshot and --destination")
            result = restore_snapshot(args.snapshot, args.destination)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (SnapshotError, OSError) as exc:
        print(f"snapshot error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
