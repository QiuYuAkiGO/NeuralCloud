"""Small systemd process entrypoint; credentials never appear in argv or logs."""
from __future__ import annotations

import os
import runpy
import stat
import sys
from pathlib import Path

from .config import SERVICE_MODULES, DeployError, DeploymentConfig


def read_token(path: str | Path) -> str:
    target = Path(path)
    if not target.is_absolute():
        raise DeployError("token path must be absolute")
    for item in (target, *target.parents):
        if item.is_symlink():
            raise DeployError("token paths must not traverse symlinks")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(target, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise DeployError("token must be a bounded regular file")
        if os.name == "posix" and info.st_mode & 0o077:
            raise DeployError("token file permissions must be private (0600)")
        with os.fdopen(descriptor, "r", encoding="utf-8-sig") as handle:
            descriptor = -1
            token = handle.read().strip()
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    from harness.runtime.ownership import require_service_token
    try:
        return require_service_token(token, name="deployment", control_mode="study_runtime")
    except ValueError:
        raise DeployError("invalid private deployment token") from None


def service_environment(cfg: DeploymentConfig, service: str, *, release: Path | None = None) -> dict[str, str]:
    if service not in cfg.services:
        raise DeployError("service is not enabled")
    root = release or cfg.path("current").resolve(strict=True)
    values = cfg.effective_bindings()
    control = read_token(values["NC_CONTROL_TOKEN_FILE"])
    internal = read_token(values["NC_SERVICE_TOKEN_FILE"])
    if control == internal:
        raise DeployError("control and service tokens must differ")
    values.update({
        "NC_MODE": cfg.mode,
        "NC_CONTROL_MODE": "development_debug" if cfg.development else "study_runtime",
        "NC_ISOLATED_DEVELOPMENT": "1" if cfg.development else "0",
        "NC_EMBODIMENT_PROVIDER": "none",
        "NC_RUNTIME_MANIFEST_PATH": str(root / cfg.manifest_path),
        "NC_COGNITIVE_MANIFEST_PATH": str(root / cfg.manifest_path),
        "NC_HOMEOSTASIS_PROFILE_PATH": str(root / cfg.profile_path),
        "XDG_STATE_HOME": str(cfg.path("state_root")),
        "PYTHONUNBUFFERED": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for name, port in cfg.ports.items():
        values[f"NC_{name.upper()}_HOST"] = "127.0.0.1"
        values[f"NC_{name.upper()}_PORT"] = str(port)
        values[f"NC_{name.upper()}_URL"] = f"http://127.0.0.1:{port}"
    if service in {"cognitive", "homeostasis", "runtime"}:
        values["NC_SERVICE_TOKEN"] = internal
    if service in {"gui", "runtime"}:
        values["NC_CONTROL_TOKEN"] = control
    if cfg.public_origin and service == "gui":
        values["NC_PUBLIC_ORIGIN"] = cfg.public_origin
    # Provider configuration belongs to Runtime only, never the GUI or domains.
    for name in ("NC_BOARD_CONFIG", "NC_VOICE_CONFIG", "NC_WEB_CONFIG", "NC_MISTYAI_TOKEN_FILE"):
        if service != "runtime":
            values.pop(name, None)
    if service != "cognitive":
        values.pop("NC_DAILY_REVISION_CONFIG", None)
    if service == "qq_direct" and not values.get("NC_QQ_DIRECT_CONFIG"):
        raise DeployError("qq_direct requires NC_QQ_DIRECT_CONFIG")
    return values


def run_service(cfg: DeploymentConfig, service: str) -> None:
    root = cfg.path("current").resolve(strict=True)
    values = service_environment(cfg, service, release=root)
    # No ambient NC_* setting can accidentally select another state or provider.
    for key in tuple(os.environ):
        if key.startswith("NC_") or key in {"PYTHONPATH", "PYTHONHOME"}:
            os.environ.pop(key, None)
    os.environ.update(values)
    os.chdir(root)
    sys.path.insert(0, str(root))
    sys.argv = [SERVICE_MODULES[service]]
    if service == "qq_direct":
        sys.argv += ["--config", values["NC_QQ_DIRECT_CONFIG"]]
    runpy.run_module(SERVICE_MODULES[service], run_name="__main__")
