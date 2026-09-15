"""Validated deployment configuration; contains paths, never credential bytes."""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

CORE_SERVICES = ("cognitive", "homeostasis", "runtime", "gui")
OPTIONAL_SERVICES = ("board", "qq_direct")
SERVICE_MODULES = {
    "cognitive": "harness.cognitive.service",
    "homeostasis": "harness.homeostasis.service",
    "runtime": "harness.runtime.service",
    "gui": "gui.runtime_server",
    "board": "harness.board.service",
    "qq_direct": "harness.qq.direct",
}
DEFAULT_PATHS = {
    "install_root": "/opt/neuralcloud",
    "releases": "/opt/neuralcloud/releases",
    "current": "/opt/neuralcloud/current",
    "config_root": "/etc/neuralcloud",
    "state_root": "/var/lib/neuralcloud",
    "log_root": "/var/log/neuralcloud",
    "backup_root": "/var/backups/neuralcloud",
}
DEFAULT_PORTS = {"gui": 8790, "runtime": 8770, "cognitive": 8771, "homeostasis": 8772, "board": 8793}
SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}\Z")


class DeployError(RuntimeError):
    pass


def validate_release_id(value: str) -> str:
    if not isinstance(value, str) or not SAFE_ID.fullmatch(value) or value in {".", ".."}:
        raise DeployError("invalid release id")
    return value


def _absolute(value: object, name: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(r"/[A-Za-z0-9_./-]+", value):
        raise DeployError(f"{name} must be an absolute Linux path using letters, digits, /, _, ., or -")
    path = PurePosixPath(value)
    if ".." in path.parts or str(path) == "/":
        raise DeployError(f"unsafe path for {name}")
    return str(path)


@dataclass(frozen=True)
class DeploymentConfig:
    config_file: Path
    service_user: str = "neuralcloud"
    paths: dict[str, str] = field(default_factory=lambda: dict(DEFAULT_PATHS))
    ports: dict[str, int] = field(default_factory=lambda: dict(DEFAULT_PORTS))
    bindings: dict[str, str] = field(default_factory=dict)
    enabled_components: tuple[str, ...] = CORE_SERVICES
    public_origin: str | None = None
    startup_timeout_seconds: int = 600
    stop_timeout_seconds: int = 180
    mode: str = "live"
    development: bool = False
    manifest_path: str = "presets/runtime/shared-misty-regulation-v1.json"
    profile_path: str = "presets/homeostasis/qq-social-drive-v1.json"

    def path(self, key: str) -> Path:
        return Path(self.paths[key])

    @property
    def release_id(self) -> str | None:
        return self.path("current").resolve().name if self.path("current").exists() else None

    @property
    def services(self) -> tuple[str, ...]:
        return tuple(name for name in CORE_SERVICES + OPTIONAL_SERVICES if name in self.enabled_components)

    def effective_bindings(self) -> dict[str, str]:
        state, conf = PurePosixPath(self.paths["state_root"]), PurePosixPath(self.paths["config_root"])
        values = {
            "NC_RUNTIME_STATE_DIR": str(state / "runtime"),
            "NC_COGNITIVE_STATE_PATH": str(state / "cognitive" / "state.json"),
            "NC_HOMEOSTASIS_STATE_DIR": str(state / "homeostasis" / "domain"),
            "NC_SHARED_MEMORY_DB": str(state / "shared-memory.sqlite3"),
            "NC_GUI_CONFIG": str(conf / "gui-settings.json"),
            "NC_GUI_PRESETS": str(state / "presets"),
            "NC_SERVICE_TOKEN_FILE": str(conf / "service.token"),
            "NC_CONTROL_TOKEN_FILE": str(conf / "control.token"),
            "NC_TRACE_DIR": str(PurePosixPath(self.paths["log_root"]) / "traces"),
            "NC_CALL_LOG_DIR": str(PurePosixPath(self.paths["log_root"]) / "calls"),
            "NC_PROFILE_REVISION_DIR": str(state / "homeostasis" / "profiles"),
            "NC_MANIFEST_REVISION_DIR": str(state / "runtime" / "manifests"),
            "NC_PENDING_START_PATH": str(state / "runtime" / "pending-start.json"),
        }
        values.update(self.bindings)
        if "board" in self.services:
            values.setdefault("NC_BOARD_STATE_DIR", str(state / "board"))
            values.setdefault("NC_BOARD_TOKEN_FILE", str(conf / "board.token"))
        return values


def load_config(path: str | Path) -> DeploymentConfig:
    source = Path(path).absolute()
    if source.is_symlink() or source.stat().st_size > 131072:
        raise DeployError("deployment configuration must be a regular bounded file")
    raw = json.loads(source.read_text(encoding="utf-8-sig"))
    allowed = {"schema_version", "service_user", "paths", "ports", "bindings", "enabled_components",
               "public_origin", "startup_timeout_seconds", "stop_timeout_seconds", "mode", "development",
               "manifest_path", "profile_path"}
    if not isinstance(raw, dict) or raw.get("schema_version") != 1 or set(raw) - allowed:
        raise DeployError("invalid deployment schema or unknown fields")
    user = raw.get("service_user", "neuralcloud")
    if not isinstance(user, str) or not re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", user) or user == "root":
        raise DeployError("service_user must name a non-root Linux user")
    overrides = raw.get("paths", {})
    if not isinstance(overrides, dict) or set(overrides) - set(DEFAULT_PATHS):
        raise DeployError("invalid deployment paths")
    paths = {key: _absolute(value, key) for key, value in {**DEFAULT_PATHS, **overrides}.items()}
    for key in ("releases", "current"):
        if not PurePosixPath(paths[key]).is_relative_to(PurePosixPath(paths["install_root"])):
            raise DeployError(f"{key} must stay under install_root")
    if paths["releases"] == paths["current"]:
        raise DeployError("current and releases must differ")
    for key in ("state_root", "config_root", "log_root", "backup_root"):
        if PurePosixPath(paths[key]).is_relative_to(PurePosixPath(paths["releases"])):
            raise DeployError("persistent paths cannot live inside releases")
    port_overrides = raw.get("ports", {})
    if not isinstance(port_overrides, dict) or set(port_overrides) - set(DEFAULT_PORTS):
        raise DeployError("unknown service port")
    ports = {**DEFAULT_PORTS, **port_overrides}
    if any(type(port) is not int or not 1024 <= port <= 65535 or port == 8766 for port in ports.values()):
        raise DeployError("ports must be unprivileged; 8766 belongs to the QQ Agent")
    if len(set(ports.values())) != len(ports):
        raise DeployError("service ports must be distinct")
    components = raw.get("enabled_components", list(CORE_SERVICES))
    if not isinstance(components, list) or any(item not in SERVICE_MODULES for item in components):
        raise DeployError("invalid enabled_components")
    if len(components) != len(set(components)) or not set(CORE_SERVICES).issubset(components):
        raise DeployError("all four core services must be enabled exactly once")
    bindings = raw.get("bindings", {})
    if not isinstance(bindings, dict):
        raise DeployError("bindings must be an object")
    clean = {}
    budgets = {"NC_QQ_INPUT_TOKEN_BUDGET": 8192, "NC_QQ_MEMORY_RECALL_MAX_CHARACTERS": 1,
               "NC_AUX_LLM_NUM_CTX": 1024}
    for key, value in bindings.items():
        if key in budgets:
            if isinstance(value, bool) or not str(value).isdigit() or int(value) < budgets[key]:
                raise DeployError(f"invalid {key}")
            clean[key] = str(value)
        elif re.fullmatch(r"NC_[A-Z0-9_]+(?:_PATH|_DIR|_DB|_FILE|_CONFIG|_PRESETS)", key):
            clean[key] = _absolute(value, key)
        else:
            raise DeployError(f"unsupported binding {key}; credentials must use file references")
    mode = raw.get("mode", "live")
    development = raw.get("development", False)
    if mode not in {"demo", "live", "ollama"} or type(development) is not bool:
        raise DeployError("invalid model mode or development flag")
    origin = raw.get("public_origin")
    if origin is not None:
        if not isinstance(origin, str):
            raise DeployError("public_origin must be a bare HTTPS origin")
        parsed = urlsplit(origin)
        if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise DeployError("public_origin must be a bare HTTPS origin")
        origin = origin.rstrip("/")
    manifest = raw.get("manifest_path", "presets/runtime/development-demo.json" if development else "presets/runtime/shared-misty-regulation-v1.json")
    profile = raw.get("profile_path", "presets/homeostasis/functional-proxy-v1.json" if development else "presets/homeostasis/qq-social-drive-v1.json")
    for value in (manifest, profile):
        if not isinstance(value, str) or any(c in value for c in "\r\n\x00%\\") or ".." in PurePosixPath(value).parts:
            raise DeployError("invalid preset path")
    startup, stop = raw.get("startup_timeout_seconds", 600), raw.get("stop_timeout_seconds", 180)
    if type(startup) is not int or not 10 <= startup <= 1800 or type(stop) is not int or not 30 <= stop <= 600:
        raise DeployError("invalid startup/stop timeout")
    cfg = DeploymentConfig(source, user, paths, ports, clean, tuple(components), origin, startup, stop, mode, development, manifest, profile)
    if cfg.effective_bindings()["NC_CONTROL_TOKEN_FILE"] == cfg.effective_bindings()["NC_SERVICE_TOKEN_FILE"]:
        raise DeployError("control and service token files must differ")
    return cfg


def ensure_linux() -> None:
    if os.name != "posix" or not Path("/proc").exists():
        raise DeployError("this command requires Linux")
