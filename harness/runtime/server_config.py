"""Private, versioned SSH server-body configuration, activated only at Runtime startup."""

from __future__ import annotations

import json
import os
import re
import threading
from pathlib import Path
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from harness.runtime.web_config import (
    WebConfigurationError,
    check_path as _web_check_path,
    private_directory as _web_private_directory,
    read_json as _web_read_json,
    write_private as _web_write_private,
)


class ServerConfigurationError(ValueError):
    """A deliberately sanitized configuration failure."""


_HOST = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?(?:\.[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)*|localhost|\d{1,3}(?:\.\d{1,3}){3}|\[[0-9a-fA-F:]+])$")
_FINGERPRINT = re.compile(r"^SHA256:[A-Za-z0-9+/]{43}={0,2}$")
_HOST_KEY_ALGS = {"ssh-ed25519", "ssh-rsa", "ecdsa-sha2-nistp256", "ecdsa-sha2-nistp384", "ecdsa-sha2-nistp521", "ssh-dss"}


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    host: str = Field(default="127.0.0.1", min_length=1, max_length=253)
    port: int = Field(default=22, ge=1, le=65535, strict=True)
    username: str = Field(default="misty-ops", min_length=1, max_length=32, pattern=r"^[A-Za-z_][A-Za-z0-9_-]*$")
    host_key: str = Field(default="", max_length=8192)
    body_instance_id: str = Field(default="server-01", min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    connect_timeout_seconds: float = Field(default=10, gt=0, le=60, allow_inf_nan=False)
    default_timeout_seconds: float = Field(default=60, gt=0, le=300, allow_inf_nan=False)
    daily_commands: int = Field(default=200, ge=1, le=100000, strict=True)
    autonomous_daily_commands: int = Field(default=40, ge=0, le=100000, strict=True)
    extra_high_risk_patterns: tuple[str, ...] = ()
    forbidden_patterns: tuple[str, ...] = ()
    sudo_requires_approval: bool = False
    approval_timeout_seconds: int = Field(default=1800, ge=60, le=86400, strict=True)

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip()
        if not _HOST.fullmatch(value) or any(c.isspace() for c in value):
            raise ValueError("server_host_invalid")
        return value

    @field_validator("host_key")
    @classmethod
    def _host_key(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if _FINGERPRINT.fullmatch(value):
            return value
        parts = value.split()
        if parts and parts[0] in _HOST_KEY_ALGS and len(parts) >= 2:
            return value
        if len(parts) >= 3 and parts[1] in _HOST_KEY_ALGS:
            return value
        raise ValueError("server_host_key_invalid")

    @field_validator("extra_high_risk_patterns", "forbidden_patterns", mode="before")
    @classmethod
    def _pattern_list(cls, value):
        if value is None:
            return ()
        if isinstance(value, list):
            return tuple(value)
        return value

    @field_validator("extra_high_risk_patterns", "forbidden_patterns")
    @classmethod
    def _patterns(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(value) > 32:
            raise ValueError("server_policy_pattern_invalid")
        import re as _re
        for item in value:
            if not isinstance(item, str) or not item or len(item) > 256:
                raise ValueError("server_policy_pattern_invalid")
            try:
                _re.compile(item)
            except _re.error:
                raise ValueError("server_policy_pattern_invalid") from None
        return value

    @model_validator(mode="after")
    def validate_quota(self):
        if self.autonomous_daily_commands > self.daily_commands:
            raise ValueError("server_autonomous_quota_exceeds_total")
        return self


class ServerAuthorization(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = Field(strict=True)
    autonomous_enabled: bool = Field(strict=True)


class _Revision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["server-config-v1"] = "server-config-v1"
    revision: int = Field(ge=1, strict=True)
    settings: ServerSettings
    private_key_file: str | None = None
    authorization: ServerAuthorization | None = None


_FILE_CODES = {
    "web_config_requires_absolute_path": "server_config_requires_absolute_path",
    "web_config_path_not_private": "server_config_path_not_private",
    "web_private_permissions_failed": "server_private_permissions_failed",
    "web_config_invalid": "server_config_invalid",
}


def _translate_file_error(exc: WebConfigurationError) -> ServerConfigurationError:
    return ServerConfigurationError(_FILE_CODES.get(str(exc), str(exc)))


def _check_path(path: Path) -> None:
    try:
        _web_check_path(path)
    except WebConfigurationError as exc:
        raise _translate_file_error(exc) from None


def _private_directory(path: Path) -> None:
    try:
        _web_private_directory(path)
    except WebConfigurationError as exc:
        raise _translate_file_error(exc) from None


def _write_private(path: Path, content: str, *, replace: bool = False) -> None:
    try:
        _web_write_private(path, content, replace=replace)
    except WebConfigurationError as exc:
        raise _translate_file_error(exc) from None


def _read_json(path: Path) -> dict:
    try:
        return _web_read_json(path)
    except WebConfigurationError as exc:
        raise _translate_file_error(exc) from None


def _private_key(value) -> str:
    if not isinstance(value, str):
        raise ServerConfigurationError("server_private_key_invalid")
    text = value.replace("\r\n", "\n").strip()
    if "BEGIN" not in text or "PRIVATE KEY" not in text or not 64 <= len(text) <= 16384 or "\0" in text:
        raise ServerConfigurationError("server_private_key_invalid")
    try:
        import asyncssh
        asyncssh.import_private_key(text)
    except Exception:
        raise ServerConfigurationError("server_private_key_invalid") from None
    return text + "\n"


class ServerConfigStore:
    """The active bundle is snapshotted at construction; saving never reloads it."""

    def __init__(self, state_dir: Path, *, config_path: Path | None = None, environ: Mapping[str, str] | None = None):
        values = os.environ if environ is None else environ
        self.path = Path(config_path or values.get("NC_SERVER_CONFIG") or (Path(state_dir) / "server" / "config.json"))
        _check_path(self.path)
        self.root = self.path.parent
        self._lock = threading.RLock()
        self.load_error: str | None = None
        self._active = self._safe_revision()
        self._active_key: str | None = None

    def _safe_revision(self) -> _Revision | None:
        try:
            return self._read_revision()
        except ServerConfigurationError:
            self.load_error = "server_config_invalid"
            return None

    def _read_revision(self) -> _Revision | None:
        try:
            if not self.path.exists():
                return None
            index = _read_json(self.path)
            number = index.get("revision")
            if (set(index) != {"schema_version", "revision", "revision_file"}
                    or index.get("schema_version") != "server-config-index-v1"
                    or type(number) is not int or number < 1
                    or index.get("revision_file") != f"revisions/server-r{number}.json"):
                raise ValueError()
            revision = _Revision.model_validate(_read_json(self.root / index["revision_file"]))
            if revision.revision != number:
                raise ValueError()
            if revision.private_key_file not in {None, f"keys/server-r{number}.key"}:
                raise ValueError()
            return revision
        except (OSError, UnicodeError, ValueError):
            raise ServerConfigurationError("server_config_invalid") from None

    def _read_key(self, revision: _Revision | None) -> str:
        if revision is None or revision.private_key_file is None:
            return ""
        try:
            path = self.root / revision.private_key_file
            _check_path(path)
            if not path.is_file() or path.stat().st_size > 16384:
                raise ValueError()
            return _private_key(path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, ServerConfigurationError):
            raise ServerConfigurationError("server_private_key_unavailable") from None

    def load_settings(self) -> ServerSettings | None:
        return self._active.settings if self._active else None

    def load_private_key(self) -> str:
        if self._active_key is None:
            try:
                self._active_key = self._read_key(self._active)
            except ServerConfigurationError:
                self.load_error = "server_private_key_unavailable"
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
            "settings": (saved.settings if saved else self._active.settings if self._active else ServerSettings()).model_dump(mode="json"),
            "authorization": saved.authorization.model_dump(mode="json") if saved and saved.authorization else None,
            "private_key_configured": bool(saved and saved.private_key_file),
            "active_private_key_configured": bool(self._active and self._active.private_key_file and self._active_key != ""),
            "pending_generated_key": (self.root / "keys" / "pending.key").is_file(),
            "load_error": self.load_error,
        }

    def _candidate(self, payload: dict, saved: _Revision | None):
        try:
            allowed = {"base_revision", "settings", "private_key", "clear_private_key", "use_generated_key", "authorization"}
            if not isinstance(payload, dict) or set(payload) - allowed:
                raise ValueError()
            settings = ServerSettings.model_validate(payload.get("settings") or (saved.settings.model_dump() if saved else {}))
            supplied = payload.get("private_key")
            clear = payload.get("clear_private_key", False)
            generated = payload.get("use_generated_key", False)
            if type(clear) is not bool or type(generated) is not bool or (clear and (supplied or generated)):
                raise ValueError()
            if generated:
                pending = self.root / "keys" / "pending.key"
                _check_path(pending)
                key = _private_key(pending.read_text(encoding="utf-8-sig"))
            elif clear:
                key = ""
            elif supplied not in {None, ""}:
                key = _private_key(supplied)
            else:
                key = self._read_key(saved)
            auth = saved.authorization if saved else None
            if "authorization" in payload:
                auth = ServerAuthorization.model_validate(payload["authorization"])
            if auth and auth.enabled and (not key or not settings.host_key):
                raise ServerConfigurationError("server_enabled_requires_key_and_host_key")
            return settings, key, auth
        except ServerConfigurationError:
            raise
        except (TypeError, ValueError, OSError, UnicodeError):
            raise ServerConfigurationError("server_config_request_invalid") from None

    def test_configuration(self, payload: dict) -> tuple[ServerSettings, str]:
        settings, key, _ = self._candidate(payload, self._safe_revision())
        if not key or not settings.host_key:
            raise ServerConfigurationError("server_private_key_required")
        return settings, key

    def generate_key(self) -> dict:
        import asyncssh
        key = asyncssh.generate_private_key("ssh-ed25519")
        private = key.export_private_key("openssh")
        public = key.export_public_key("openssh")
        if isinstance(private, bytes):
            private = private.decode("utf-8")
        if isinstance(public, bytes):
            public = public.decode("utf-8")
        with self._lock:
            for directory in (self.root, self.root / "keys"):
                _private_directory(directory)
            _write_private(self.root / "keys" / "pending.key", private if private.endswith("\n") else private + "\n", replace=True)
        return {"public_key": public.strip(), "key_type": "ssh-ed25519"}

    def stage(self, payload: dict) -> dict:
        with self._lock:
            if not isinstance(payload, dict):
                raise ServerConfigurationError("server_config_request_invalid")
            saved = self._safe_revision()
            expected = saved.revision if saved else 0
            if type(payload.get("base_revision")) is not int or payload["base_revision"] != expected:
                raise ServerConfigurationError("server_config_revision_conflict")
            settings, key, auth = self._candidate(payload, saved)
            number = expected + 1
            while ((self.root / f"keys/server-r{number}.key").exists()
                   or (self.root / f"revisions/server-r{number}.json").exists()):
                number += 1
            revision = _Revision(revision=number, settings=settings,
                                 private_key_file=f"keys/server-r{number}.key" if key else None, authorization=auth)
            try:
                for directory in (self.root, self.root / "keys", self.root / "revisions"):
                    _private_directory(directory)
                if key:
                    _write_private(self.root / revision.private_key_file, key if key.endswith("\n") else key + "\n")
                revision_file = f"revisions/server-r{number}.json"
                _write_private(self.root / revision_file, revision.model_dump_json(indent=2) + "\n")
                index = {"schema_version": "server-config-index-v1", "revision": number, "revision_file": revision_file}
                _write_private(self.path, json.dumps(index, indent=2) + "\n", replace=True)
            except FileExistsError:
                raise ServerConfigurationError("server_config_revision_conflict") from None
            except (OSError, ValueError):
                raise ServerConfigurationError("server_config_write_failed") from None
            if self.load_error == "server_config_invalid":
                self.load_error = None
            return self.snapshot()

    def apply_manifest(self, manifest):
        if not self._active or self._active.authorization is None:
            return manifest
        auth = self._active.authorization
        payload = manifest.model_dump(mode="json")
        access = dict(payload.get("server_access") or {})
        access.update(enabled=auth.enabled, approval_timeout_seconds=self._active.settings.approval_timeout_seconds,
                      sudo_requires_approval=self._active.settings.sudo_requires_approval)
        payload["server_access"] = access
        autonomous = dict(payload.get("autonomous_capabilities") or {})
        owners = dict(autonomous.get("owners") or {})
        owners["server"] = "shared" if auth.autonomous_enabled else "off"
        autonomous["owners"] = owners
        capabilities = [name for name in autonomous.get("allowed_capabilities", [])
                        if name not in {"server.observe.v1", "server.execute.v1"}]
        if auth.autonomous_enabled:
            capabilities.extend(["server.observe.v1", "server.execute.v1"])
        autonomous["allowed_capabilities"] = capabilities
        payload["autonomous_capabilities"] = autonomous
        payload["revision"] = manifest.revision + self._active.revision
        return type(manifest).model_validate(payload)
