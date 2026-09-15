"""Explicit development authority and process ownership of shared domains.

Model backend selection (demo/live/ollama) never grants mutation authority.
The lock is held by an open descriptor, not by trusting a stale PID file.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, TextIO, cast

ControlMode = Literal["development_debug", "study_runtime"]


class DevelopmentModeRequired(PermissionError):
    """The requested operation is limited to explicit development mode."""


class SharedDomainAlreadyOwned(RuntimeError):
    """Another process owns this shared domain (regardless of state path)."""


def _mode(value: object) -> ControlMode:
    if value not in ("development_debug", "study_runtime"):
        raise ValueError("invalid_control_mode")
    return cast(ControlMode, value)


def resolve_control_mode(
    *, manifest: object | None = None, environ: Mapping[str, str] | None = None
) -> ControlMode:
    """Resolve trusted launch authority, rejecting conflicting declarations.

    Callers may pass a validated manifest or a trusted local manifest mapping.
    Environment manifest paths use the project's full manifest validator.
    """
    env = os.environ if environ is None else environ
    modes: list[ControlMode] = []
    if manifest is not None:
        value = manifest.get("control_mode") if isinstance(manifest, Mapping) else getattr(manifest, "control_mode", None)
        modes.append(_mode(value))
    else:
        from harness.research.manifest import load_experiment_manifest

        paths = {
            env[key]
            for key in ("NC_RUNTIME_MANIFEST_PATH", "NC_COGNITIVE_MANIFEST_PATH", "NC_EXPERIMENT_MANIFEST")
            if env.get(key)
        }
        for path in sorted(paths):
            modes.append(_mode(load_experiment_manifest(Path(path)).control_mode))
    if "NC_CONTROL_MODE" in env:
        modes.append(_mode(env["NC_CONTROL_MODE"]))
    if len(set(modes)) > 1:
        raise ValueError("conflicting_control_modes")
    return modes[0] if modes else "study_runtime"


def require_development(operation: str, *, control_mode: ControlMode | None = None) -> None:
    mode = resolve_control_mode() if control_mode is None else _mode(control_mode)
    if mode != "development_debug":
        raise DevelopmentModeRequired(f"development_mode_required: {operation}")


def require_service_token(
    value: str | None, *, name: str, control_mode: ControlMode | None = None
) -> str:
    """Validate a launch credential without ever including its value in errors."""
    mode = resolve_control_mode() if control_mode is None else _mode(control_mode)
    if not re.fullmatch(r"[a-zA-Z0-9_.-]{1,64}", name):
        raise ValueError("invalid_service_token_name")
    valid = isinstance(value, str) and 1 <= len(value) <= 4096 and re.fullmatch(r"[!-~]+", value)
    if not valid:
        raise ValueError(f"service_token_required: {name}")
    assert isinstance(value, str)
    if mode != "development_debug":
        lowered = value.lower()
        placeholders = ("development", "dummy", "example", "placeholder", "replace", "test", "your", "changeme", "change-me")
        if len(value) < 32 or any(lowered.startswith(prefix) for prefix in placeholders):
            raise ValueError(f"secure_service_token_required: {name}")
    return value


def _lease_root() -> Path:
    if os.name == "nt":
        return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "NeuralCloud" / "SharedDomains"
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")) / "neuralcloud" / "shared-domains"


class SharedDomainLease:
    """One owner per shared identity and domain, even across different databases.

    Explicit development isolation uses a state-specific lock; it cannot bypass
    duplicate ownership of the same isolated state. Normal startup never unlinks
    a lock file, so an exiting owner cannot erase a newer owner's lock.
    """

    def __init__(
        self,
        domain: str,
        state_identity: str | Path,
        *,
        shared_id: str = "misty",
        control_mode: ControlMode | None = None,
        isolated: bool = False,
        lease_root: Path | None = None,
    ) -> None:
        for value in (domain, shared_id):
            if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,63}", value):
                raise ValueError("invalid_shared_domain_identity")
        self.control_mode = resolve_control_mode() if control_mode is None else _mode(control_mode)
        if isolated:
            require_development("domain.isolate", control_mode=self.control_mode)
        elif shared_id != "misty":
            raise DevelopmentModeRequired("alternate_shared_id_requires_explicit_development_isolation")
        identity = str(state_identity)
        if not identity or len(identity) > 4096:
            raise ValueError("invalid_state_identity")
        if isinstance(state_identity, Path):
            identity = os.path.normcase(str(state_identity.resolve()))
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        suffix = f".development.{digest}" if isolated else ""
        self.path = (lease_root if lease_root is not None else _lease_root()) / f"{shared_id}.{domain}{suffix}.lock"
        self.domain = domain
        self.shared_id = shared_id
        self.state_identity_sha256 = digest
        self.isolated = isolated
        self._file: TextIO | None = None

    def acquire(self) -> SharedDomainLease:
        if self._file is not None:
            return self
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+", encoding="utf-8")
        try:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(" ")
                handle.flush()
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            handle.close()
            raise SharedDomainAlreadyOwned(f"shared_domain_already_owned: {self.shared_id}/{self.domain}") from exc
        self._file = handle
        try:
            handle.seek(0)
            handle.truncate()
            handle.write(json.dumps({
                "schema_version": "shared-domain-owner-v1", "pid": os.getpid(),
                "domain": self.domain, "shared_id": self.shared_id,
                "state_identity_sha256": self.state_identity_sha256,
                "control_mode": self.control_mode, "isolated": self.isolated,
            }))
            handle.flush()
        except BaseException:
            self.release()
            raise
        return self

    def release(self) -> None:
        handle, self._file = self._file, None
        if handle is not None:
            # Closing releases the OS lock even after a crashed prior owner.
            handle.close()

    def __enter__(self) -> SharedDomainLease:
        return self.acquire()

    def __exit__(self, *_: Any) -> None:
        self.release()
