"""Private, versioned web-body configuration, activated only at Runtime startup."""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Literal, Mapping
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.files import replace_path


class WebConfigurationError(ValueError):
    """A deliberately sanitized configuration failure."""


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: Literal["tavily", "deepseek"] = "tavily"
    base_url: str = "https://api.tavily.com"
    body_instance_id: str = Field(default="web-01", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    timeout_seconds: float = Field(default=12, gt=0, le=60, allow_inf_nan=False)
    max_results: int = Field(default=5, ge=1, le=10, strict=True)
    daily_requests: int = Field(default=100, ge=1, le=100000, strict=True)
    autonomous_daily_requests: int = Field(default=20, ge=0, le=100000, strict=True)

    @model_validator(mode="after")
    def validate_connection(self):
        try:
            parsed = urlsplit(self.base_url)
            parsed.port
        except ValueError:
            raise ValueError("web_endpoint_invalid") from None
        if (parsed.scheme != "https" or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.path not in {"", "/"}
                or parsed.query or parsed.fragment or any(c.isspace() for c in self.base_url)):
            raise ValueError("web_endpoint_requires_https_origin")
        if self.autonomous_daily_requests > self.daily_requests:
            raise ValueError("web_autonomous_quota_exceeds_total")
        if self.provider == "deepseek" and (parsed.hostname != "api.deepseek.com" or parsed.port not in {None, 443}):
            raise ValueError("web_deepseek_endpoint_invalid")
        return self


class WebAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(strict=True)
    conversation_enabled: bool = Field(strict=True)
    autonomous_enabled: bool = Field(strict=True)


class _Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["web-config-v1"] = "web-config-v1"
    revision: int = Field(ge=1, strict=True)
    settings: WebSettings
    api_key_file: str | None = None
    authorization: WebAuthorization | None = None


def _check_path(path: Path) -> None:
    if not path.is_absolute():
        raise WebConfigurationError("web_config_requires_absolute_path")
    for item in (path, *path.parents):
        # is_junction covers Windows directory reparse points on newer Python.
        if item.is_symlink() or getattr(item, "is_junction", lambda: False)():
            raise WebConfigurationError("web_config_path_not_private")


def _private_permissions(path: Path, *, directory: bool = False) -> None:
    """Restrict an empty new object before writing secrets (Windows and POSIX)."""
    if os.name != "nt":
        os.chmod(path, 0o700 if directory else 0o600)
        return
    from ctypes import wintypes

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    convert = advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW
    convert.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.DWORD)]
    convert.restype = wintypes.BOOL
    set_security = advapi.SetFileSecurityW
    set_security.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.LPVOID]
    set_security.restype = wintypes.BOOL
    kernel.LocalFree.argtypes = [wintypes.LPVOID]
    kernel.LocalFree.restype = wintypes.LPVOID
    flags = "OICI" if directory else ""
    descriptor = wintypes.LPVOID()
    if not convert(f"D:P(A;{flags};FA;;;OW)(A;{flags};FA;;;SY)", 1, ctypes.byref(descriptor), None):
        raise WebConfigurationError("web_private_permissions_failed")
    try:
        if not set_security(str(path), 0x00000004 | 0x80000000, descriptor):
            raise WebConfigurationError("web_private_permissions_failed")
    finally:
        kernel.LocalFree(descriptor)


def _private_directory(path: Path) -> None:
    _check_path(path)
    path.mkdir(parents=True, exist_ok=True)
    _private_permissions(path, directory=True)


def _write_private(path: Path, content: str, *, replace: bool = False) -> None:
    _check_path(path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        # Exclusive empty creation; the ACL is private before secret bytes exist.
        descriptor = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        _private_permissions(temporary)
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            replace_path(temporary, path)
        else:
            # Hard-link publication is atomic and refuses an existing revision.
            delay = 0.02
            for attempt in range(8):
                try:
                    os.link(temporary, path)
                    break
                except PermissionError:
                    if attempt == 7:
                        raise
                    time.sleep(delay)
                    delay = min(0.25, delay * 1.7)
            temporary.unlink()
    finally:
        temporary.unlink(missing_ok=True)


def _read_json(path: Path) -> dict:
    _check_path(path)
    if not path.is_file() or path.stat().st_size > 65536:
        raise WebConfigurationError("web_config_invalid")
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise WebConfigurationError("web_config_invalid")
    return value


check_path = _check_path
private_directory = _private_directory
write_private = _write_private
read_json = _read_json


def _key(value) -> str:
    if not isinstance(value, str):
        raise WebConfigurationError("web_api_key_invalid")
    value = value.strip()
    if not 8 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise WebConfigurationError("web_api_key_invalid")
    return value


class WebConfigStore:
    """The active bundle is snapshotted at construction; saving never reloads it."""

    def __init__(self, state_dir: Path, *, config_path: Path | None = None, environ: Mapping[str, str] | None = None):
        values = os.environ if environ is None else environ
        self.path = Path(config_path or values.get("NC_WEB_CONFIG") or (Path(state_dir) / "web" / "config.json"))
        _check_path(self.path)
        self.root = self.path.parent
        self._lock = threading.RLock()
        self.load_error: str | None = None
        self._active = self._safe_revision()
        # Disabled bodies do not touch credential files. A configured body reads
        # once, on explicit demand, then keeps that startup credential in memory.
        self._active_key: str | None = None

    def _safe_revision(self) -> _Revision | None:
        try:
            return self._read_revision()
        except WebConfigurationError:
            self.load_error = "web_config_invalid"
            return None

    def _read_revision(self) -> _Revision | None:
        try:
            if not self.path.exists():
                return None
            index = _read_json(self.path)
            number = index.get("revision")
            if (set(index) != {"schema_version", "revision", "revision_file"}
                    or index.get("schema_version") != "web-config-index-v1"
                    or type(number) is not int or number < 1
                    or index.get("revision_file") != f"revisions/web-r{number}.json"):
                raise ValueError()
            revision = _Revision.model_validate(_read_json(self.root / index["revision_file"]))
            if revision.revision != number:
                raise ValueError()
            if revision.api_key_file not in {None, f"keys/web-r{number}.key"}:
                raise ValueError()
            return revision
        except (OSError, UnicodeError, ValueError):
            raise WebConfigurationError("web_config_invalid") from None

    def _read_key(self, revision: _Revision | None) -> str:
        if revision is None or revision.api_key_file is None:
            return ""
        try:
            path = self.root / revision.api_key_file
            _check_path(path)
            if not path.is_file() or path.stat().st_size > 8192:
                raise ValueError()
            return _key(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError):
            raise WebConfigurationError("web_api_key_unavailable") from None

    def load_settings(self) -> WebSettings | None:
        return self._active.settings if self._active else None

    def load_api_key(self) -> str:
        if self._active_key is None:
            try:
                self._active_key = self._read_key(self._active)
            except WebConfigurationError:
                self.load_error = "web_api_key_unavailable"
                self._active_key = ""
        return self._active_key

    def snapshot(self) -> dict:
        saved = self._safe_revision()
        active_revision = self._active.revision if self._active else 0
        saved_revision = saved.revision if saved else 0
        return {
            "active_revision": active_revision,
            "saved_revision": saved_revision,
            "restart_required": active_revision != saved_revision,
            "settings": (saved.settings if saved else self._active.settings if self._active else WebSettings()).model_dump(mode="json"),
            "authorization": saved.authorization.model_dump(mode="json") if saved and saved.authorization else None,
            "api_key_configured": bool(saved and saved.api_key_file),
            "active_api_key_configured": bool(self._active and self._active.api_key_file and self._active_key != ""),
            "load_error": self.load_error,
        }

    def _candidate(self, payload: dict, saved: _Revision | None):
        try:
            if not isinstance(payload, dict) or set(payload) - {"base_revision", "settings", "api_key", "clear_api_key", "authorization"}:
                raise ValueError()
            settings = WebSettings.model_validate(payload.get("settings") or (saved.settings.model_dump() if saved else {}))
            supplied = payload.get("api_key")
            if saved and settings.provider != saved.settings.provider and not supplied:
                raise WebConfigurationError("web_api_key_required")
            clear = payload.get("clear_api_key", False)
            if type(clear) is not bool or (clear and supplied):
                raise ValueError()
            key = "" if clear else (_key(supplied) if supplied not in {None, ""} else self._read_key(saved))
            auth = saved.authorization if saved else None
            if "authorization" in payload:
                auth = WebAuthorization.model_validate(payload["authorization"])
            if auth and auth.enabled and not key:
                raise WebConfigurationError("web_enabled_requires_api_key")
            return settings, key, auth
        except WebConfigurationError:
            raise
        except (TypeError, ValueError):
            raise WebConfigurationError("web_config_request_invalid") from None

    def test_configuration(self, payload: dict) -> tuple[WebSettings, str]:
        settings, key, _ = self._candidate(payload, self._safe_revision())
        if not key:
            raise WebConfigurationError("web_api_key_required")
        return settings, key

    def stage(self, payload: dict) -> dict:
        with self._lock:
            if not isinstance(payload, dict):
                raise WebConfigurationError("web_config_request_invalid")
            saved = self._safe_revision()
            expected = saved.revision if saved else 0
            if type(payload.get("base_revision")) is not int or payload["base_revision"] != expected:
                raise WebConfigurationError("web_config_revision_conflict")
            settings, key, auth = self._candidate(payload, saved)
            # A failed publication can leave immutable files; never overwrite them
            # or let that orphan prevent the next explicit save.
            number = expected + 1
            while ((self.root / f"keys/web-r{number}.key").exists()
                   or (self.root / f"revisions/web-r{number}.json").exists()):
                number += 1
            revision = _Revision(revision=number, settings=settings,
                                 api_key_file=f"keys/web-r{number}.key" if key else None, authorization=auth)
            try:
                for directory in (self.root, self.root / "keys", self.root / "revisions"):
                    _private_directory(directory)
                if key:
                    _write_private(self.root / revision.api_key_file, key + "\n")
                revision_file = f"revisions/web-r{number}.json"
                _write_private(self.root / revision_file, revision.model_dump_json(indent=2) + "\n")
                index = {"schema_version": "web-config-index-v1", "revision": number, "revision_file": revision_file}
                _write_private(self.path, json.dumps(index, indent=2) + "\n", replace=True)
            except FileExistsError:
                raise WebConfigurationError("web_config_revision_conflict") from None
            except (OSError, ValueError):
                raise WebConfigurationError("web_config_write_failed") from None
            # Active credential errors remain meaningful until restart, while a
            # successfully repaired index no longer reports a saved-config error.
            if self.load_error == "web_config_invalid":
                self.load_error = None
            return self.snapshot()

    def apply_manifest(self, manifest):
        """Apply the startup bundle without enabling unrelated channels or loops."""
        if not self._active or self._active.authorization is None:
            return manifest
        auth = self._active.authorization
        payload = manifest.model_dump(mode="json")
        web = dict(payload.get("web_access") or {})
        web.update(enabled=auth.enabled, conversation_enabled=auth.conversation_enabled)
        payload["web_access"] = web
        autonomous = dict(payload.get("autonomous_capabilities") or {})
        owners = dict(autonomous.get("owners") or {})
        owners["web"] = "shared" if auth.autonomous_enabled else "off"
        autonomous["owners"] = owners
        capabilities = [name for name in autonomous.get("allowed_capabilities", []) if name not in {"web.search.v1", "web.read.v1"}]
        if auth.autonomous_enabled:
            capabilities.extend(["web.search.v1", "web.read.v1"])
        autonomous["allowed_capabilities"] = capabilities
        payload["autonomous_capabilities"] = autonomous
        payload["revision"] = manifest.revision + self._active.revision
        return type(manifest).model_validate(payload)
