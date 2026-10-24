"""Allowlisted, hashed source releases with link-free tar extraction."""
from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path, PurePosixPath

from .config import DeployError, validate_release_id

MANIFEST_NAME = "RELEASE-MANIFEST.json"
SOURCE_ROOTS = ("harness", "infra", "gui", "presets", "contracts", "deploy", "tools/deploy", "tools/qqbot")
SOURCE_FILES = ("pyproject.toml", "uv.lock", "README.md")
OPTIONAL_SOURCE_FILES = ("docs/LINUX_DEPLOYMENT.md", "docs/LINUX_NAPCAT.md", "docs/QQ_OFFICIAL_BOT.md")
EXCLUDED = {"__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".git", ".venv", "node_modules"}
MAX_EXPANDED = 512 * 1024 * 1024


def _linklike(path: Path) -> bool:
    try:
        return path.is_symlink() or bool(getattr(path.lstat(), "st_file_attributes", 0) & 0x400)
    except FileNotFoundError:
        return False


def _no_link_ancestors(path: Path) -> None:
    for item in (path, *path.parents):
        if _linklike(item):
            raise DeployError("release paths must not traverse links or reparse points")


def _safe_name(name: str) -> str:
    path = PurePosixPath(name)
    if not name or "\\" in name or path.is_absolute() or ".." in path.parts or str(path) != name or ":" in name:
        raise DeployError("unsafe archive member path")
    return name


def source_files(root: Path) -> list[Path]:
    files = []
    for name in SOURCE_FILES:
        item = root / name
        if not item.is_file() or _linklike(item):
            raise DeployError(f"release requires regular source file: {name}")
        files.append(item)
    for name in OPTIONAL_SOURCE_FILES:
        item = root / name
        if _linklike(item):
            raise DeployError("source documentation cannot be a symlink")
        if item.is_file():
            files.append(item)
    for name in SOURCE_ROOTS:
        folder = root / name
        if not folder.exists():
            continue
        if _linklike(folder):
            raise DeployError("source roots cannot be links")
        for item in sorted(folder.rglob("*")):
            relative = item.relative_to(root)
            if set(relative.parts) & EXCLUDED or item.suffix in {".pyc", ".pyo"}:
                continue
            if _linklike(item):
                raise DeployError(f"source release refuses symlinks: {relative.as_posix()}")
            if item.is_file():
                # No private env/token/database payload can enter source artifacts.
                if item.name.startswith(".env") or item.suffix.lower() in {".token", ".pem", ".key", ".sqlite", ".sqlite3", ".db"}:
                    raise DeployError(f"private file in source allowlist: {relative.as_posix()}")
                files.append(item)
    return sorted(files)


def create_package(source: Path, output: Path, release_id: str) -> dict:
    release_id = validate_release_id(release_id)
    source = source.absolute()
    _no_link_ancestors(source)
    source = source.resolve(strict=True)
    if output.exists():
        raise DeployError("package output already exists")
    files = source_files(source)
    blobs = {item.relative_to(source).as_posix(): item.read_bytes() for item in files}
    if sum(map(len, blobs.values())) > MAX_EXPANDED:
        raise DeployError("source release is too large")
    manifest = {"schema_version": 1, "release_id": release_id, "files": [
        {"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        for name, data in sorted(blobs.items())]}
    encoded = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode() + b"\n"
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("xb") as handle, tarfile.open(fileobj=handle, mode="w:gz") as archive:
        for name, data in [(MANIFEST_NAME, encoded), *sorted(blobs.items())]:
            member = tarfile.TarInfo(name)
            member.size, member.mode, member.mtime = len(data), 0o644, 0
            archive.addfile(member, io.BytesIO(data))
    return {"release_id": release_id, "files": len(blobs), "archive": str(output.absolute()),
            "sha256": hashlib.sha256(output.read_bytes()).hexdigest()}


def _read_archive(archive_path: Path) -> tuple[dict, dict[str, bytes]]:
    blobs, total = {}, 0
    with tarfile.open(archive_path, "r:*") as archive:
        for member in archive:
            name = _safe_name(member.name)
            if not member.isfile() or name in blobs or member.size < 0:
                raise DeployError("archive must contain unique regular files only")
            total += member.size
            if total > MAX_EXPANDED or len(blobs) >= 20000:
                raise DeployError("archive exceeds release limits")
            stream = archive.extractfile(member)
            if stream is None:
                raise DeployError("unreadable archive member")
            blobs[name] = stream.read()
    if MANIFEST_NAME not in blobs:
        raise DeployError("release manifest is missing")
    manifest = json.loads(blobs[MANIFEST_NAME])
    _validate_manifest(manifest)
    expected = {entry["path"]: entry for entry in manifest["files"]}
    if set(blobs) != set(expected) | {MANIFEST_NAME}:
        raise DeployError("archive contents differ from manifest")
    for name, entry in expected.items():
        data = blobs[name]
        if len(data) != entry["size"] or hashlib.sha256(data).hexdigest() != entry["sha256"]:
            raise DeployError(f"release hash mismatch: {name}")
    return manifest, blobs


def _validate_manifest(manifest: dict) -> None:
    if not isinstance(manifest, dict) or manifest.get("schema_version") != 1:
        raise DeployError("invalid release manifest")
    validate_release_id(manifest.get("release_id"))
    entries = manifest.get("files")
    if not isinstance(entries, list) or not entries:
        raise DeployError("empty release manifest")
    names = []
    for entry in entries:
        if not isinstance(entry, dict) or set(entry) != {"path", "size", "sha256"}:
            raise DeployError("invalid release manifest entry")
        name = _safe_name(entry["path"])
        if name not in SOURCE_FILES + OPTIONAL_SOURCE_FILES and not any(
            PurePosixPath(name).is_relative_to(PurePosixPath(root)) for root in SOURCE_ROOTS
        ):
            raise DeployError("release contains a file outside the source allowlist")
        if type(entry["size"]) is not int or entry["size"] < 0 or not isinstance(entry["sha256"], str) or len(entry["sha256"]) != 64:
            raise DeployError("invalid release size or hash")
        names.append(name)
    if len(names) != len(set(names)) or not set(SOURCE_FILES).issubset(names):
        raise DeployError("duplicate or missing release entries")


def extract_package(archive_path: Path, releases: Path) -> Path:
    manifest, blobs = _read_archive(archive_path)
    _no_link_ancestors(releases.absolute())
    releases.mkdir(parents=True, exist_ok=True)
    destination = releases / manifest["release_id"]
    if destination.exists() or destination.is_symlink():
        raise DeployError("release already exists; release ids are immutable")
    destination.mkdir(mode=0o755)
    for name, data in blobs.items():
        item = destination / name
        item.parent.mkdir(parents=True, exist_ok=True)
        with item.open("xb") as handle:
            handle.write(data)
        item.chmod(0o644)
    return destination


def verify_release(release: Path) -> dict:
    root = release.resolve(strict=True)
    manifest = json.loads((root / MANIFEST_NAME).read_text(encoding="utf-8"))
    _validate_manifest(manifest)
    for entry in manifest["files"]:
        item = root / entry["path"]
        if _linklike(item) or not item.is_file() or not item.resolve().is_relative_to(root):
            raise DeployError("release file missing or replaced with a link")
        if item.stat().st_size != entry["size"] or hashlib.sha256(item.read_bytes()).hexdigest() != entry["sha256"]:
            raise DeployError(f"release hash mismatch: {entry['path']}")
    actual = {item.relative_to(root).as_posix() for item in source_files(root)}
    if actual != {entry["path"] for entry in manifest["files"]}:
        raise DeployError("unexpected source files outside the release manifest")
    return {"release_id": manifest["release_id"], "verified_files": len(manifest["files"])}
