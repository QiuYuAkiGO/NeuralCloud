"""Launch bindings, HTTP clients, and probe result helpers for Misty self-check."""
from __future__ import annotations

from contextlib import closing
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
import hashlib
import json
import os
import socket
import sqlite3
import time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

ROOT = Path(__file__).resolve().parents[2]
FAIL_STATUSES = frozenset({"fail", "unavailable"})
SHANGHAI = ZoneInfo("Asia/Shanghai")


def utc_now() -> datetime:
    return datetime.now(UTC)


def shanghai_day(moment: datetime | None = None) -> str:
    return (moment or utc_now()).astimezone(SHANGHAI).date().isoformat()


def expand_env(value: str) -> str:
    return os.path.expandvars(os.path.expanduser(value))


def read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def fingerprint(path: Path | str) -> str | None:
    path = Path(path)
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else None


def listening(port: int, host: str = "127.0.0.1") -> bool:
    with socket.socket() as connection:
        connection.settimeout(0.3)
        return connection.connect_ex((host, int(port))) == 0


def readonly_connect(path: Path) -> sqlite3.Connection:
    resolved = path.resolve()
    return sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True)


def qq_send_count(watcher_config: Path | None) -> int | None:
    if watcher_config is None or not watcher_config.is_file():
        return None
    watcher = read_json(watcher_config)
    path = Path(watcher.get("state_db") or "")
    if not path.is_file():
        return None
    with closing(readonly_connect(path)) as database:
        database.row_factory = sqlite3.Row
        try:
            return int(database.execute("SELECT count(*) FROM qq_watch_sends").fetchone()[0])
        except sqlite3.Error:
            return None


def token_from_file(path: Path | None) -> str | None:
    if path is None or not path.is_file():
        return None
    value = path.read_text(encoding="utf-8").strip()
    return value or None


@dataclass
class ProbeResult:
    path: str
    tier: int
    status: str
    evidence: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    triage: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "path": self.path, "tier": self.tier, "status": self.status,
            "evidence": self.evidence, "detail": self.detail, "triage": self.triage,
        }


def probe(path: str, tier: int, status: str, *, detail: str = "", evidence: dict | None = None,
          triage: list[str] | None = None) -> ProbeResult:
    return ProbeResult(path=path, tier=tier, status=status, detail=detail,
                       evidence=evidence or {}, triage=triage or [])


@dataclass
class LaunchContext:
    root: Path
    launch_config: Path
    profile: str
    bindings: dict[str, str]
    ports: dict[str, int]
    manifest_path: Path | None
    control_token: str | None
    service_token: str | None
    runtime_url: str
    cognitive_url: str
    homeostasis_url: str
    gui_url: str
    runtime_state: Path | None
    watcher_config: Path | None
    gui_config: Path | None

    def binding(self, name: str) -> Path | None:
        value = self.bindings.get(name)
        return Path(value) if value else None


def load_launch(path: Path | None = None, profile: str = "shared") -> LaunchContext:
    launch_path = path or (ROOT / "launch.json")
    payload = read_json(launch_path)
    if _is_linux_deploy(payload):
        return _load_deploy(launch_path)
    bindings: dict[str, str] = {}
    ports = {"gui": 8790, "runtime": 8770, "cognitive": 8771, "homeostasis": 8772}
    manifest_path = None
    if "profiles" in payload:
        selected = dict(payload["profiles"][profile])
        raw_bindings = dict(selected.get("bindings") or {})
        ports = {**ports, **dict(selected.get("ports") or {})}
        if selected.get("manifest_path"):
            manifest_path = ROOT / selected["manifest_path"]
    else:
        raw_bindings = {key: value for key, value in payload.items() if isinstance(value, str)}
    for key, value in raw_bindings.items():
        bindings[key] = expand_env(str(value))
    runtime_port = int(ports["runtime"])
    cognitive_port = int(ports["cognitive"])
    homeostasis_port = int(ports["homeostasis"])
    gui_port = int(ports["gui"])
    runtime_state = _absolute(bindings.get("NC_RUNTIME_STATE_DIR"))
    watcher = _absolute(bindings.get("NC_QQ_WATCHER_CONFIG"))
    gui_config = _absolute(bindings.get("NC_GUI_CONFIG"))
    control = token_from_file(_absolute(bindings.get("NC_CONTROL_TOKEN_FILE")))
    service = token_from_file(_absolute(bindings.get("NC_SERVICE_TOKEN_FILE")))
    return LaunchContext(
        root=ROOT, launch_config=launch_path, profile=profile, bindings=bindings, ports=ports,
        manifest_path=manifest_path, control_token=control, service_token=service,
        runtime_url=f"http://127.0.0.1:{runtime_port}",
        cognitive_url=f"http://127.0.0.1:{cognitive_port}",
        homeostasis_url=f"http://127.0.0.1:{homeostasis_url_port(homeostasis_port)}",
        gui_url=f"http://127.0.0.1:{gui_port}",
        runtime_state=runtime_state, watcher_config=watcher, gui_config=gui_config,
    )


def homeostasis_url_port(port: int) -> int:
    return port


def _is_linux_deploy(payload: Any) -> bool:
    return (isinstance(payload, dict) and payload.get("schema_version") == 1
            and "profiles" not in payload
            and ("enabled_components" in payload or "service_user" in payload))


def _load_deploy(launch_path: Path) -> LaunchContext:
    from harness.deploy.config import load_config

    cfg = load_config(launch_path)
    bindings = {str(key): str(value) for key, value in cfg.effective_bindings().items()}
    current = cfg.path("current")
    root = current if current.exists() else ROOT
    manifest = Path(cfg.manifest_path)
    if not manifest.is_absolute():
        manifest = root / manifest
    return LaunchContext(
        root=root, launch_config=launch_path, profile="linux-deploy", bindings=bindings,
        ports=dict(cfg.ports), manifest_path=manifest if manifest.is_file() else root / cfg.manifest_path,
        control_token=token_from_file(_absolute(bindings.get("NC_CONTROL_TOKEN_FILE"))),
        service_token=token_from_file(_absolute(bindings.get("NC_SERVICE_TOKEN_FILE"))),
        runtime_url=f"http://127.0.0.1:{int(cfg.ports['runtime'])}",
        cognitive_url=f"http://127.0.0.1:{int(cfg.ports['cognitive'])}",
        homeostasis_url=f"http://127.0.0.1:{int(cfg.ports['homeostasis'])}",
        gui_url=f"http://127.0.0.1:{int(cfg.ports['gui'])}",
        runtime_state=_absolute(bindings.get("NC_RUNTIME_STATE_DIR")),
        watcher_config=_absolute(bindings.get("NC_QQ_WATCHER_CONFIG")),
        gui_config=_absolute(bindings.get("NC_GUI_CONFIG")),
    )


def _absolute(value: str | None) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else (ROOT / path)


class ApiClient:
    def __init__(self, base_url: str, token: str | None, *, timeout: float = 20):
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.client = httpx.Client(base_url=base_url.rstrip("/"), headers=headers, timeout=timeout)
        self.calls: list[tuple[str, str]] = []

    def request(self, method: str, path: str, **kwargs) -> httpx.Response:
        self.calls.append((method.upper(), path))
        return self.client.request(method, path, **kwargs)

    def json(self, method: str, path: str, **kwargs) -> tuple[int, Any]:
        try:
            response = self.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            return 0, {"error": type(exc).__name__, "detail": str(exc)}
        try:
            payload = response.json()
        except ValueError:
            payload = {"text": response.text[:500]}
        return response.status_code, payload

    def close(self) -> None:
        self.client.close()


def journal_fetch(client: ApiClient, *, record_type: str | None = None, correlation_id: str | None = None,
                  window: str = "24h", limit: int = 100) -> tuple[int, list[dict[str, Any]]]:
    params: dict[str, Any] = {"window": window, "limit": limit}
    if record_type:
        params["record_type"] = record_type
    if correlation_id:
        params["correlation_id"] = correlation_id
    status, payload = client.json("GET", "/v1/monitor/journal", params=params)
    if status != 200 or not isinstance(payload, dict):
        return status, []
    return status, list(payload.get("items") or [])


def journal_items(client: ApiClient, *, record_type: str | None = None, correlation_id: str | None = None,
                  window: str = "24h", limit: int = 100) -> list[dict[str, Any]]:
    _, items = journal_fetch(client, record_type=record_type, correlation_id=correlation_id,
                             window=window, limit=limit)
    return items


def wait_for(predicate, *, timeout: float, interval: float = 0.4):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = predicate()
        if last:
            return last
        time.sleep(interval)
    return last


def ledger_path(root: Path | None = None) -> Path:
    if root is None:
        override = os.environ.get("NC_SELF_CHECK_LEDGER_DIR")
        root = Path(override) if override else ROOT
    return Path(root) / "logs" / "self-check" / "ledger.json"


def load_ledger(root: Path | None = None) -> dict[str, Any]:
    path = ledger_path(root)
    if not path.is_file():
        return {"day": shanghai_day(), "paths": {}}
    try:
        payload = read_json(path)
    except (OSError, ValueError):
        return {"day": shanghai_day(), "paths": {}}
    if payload.get("day") != shanghai_day():
        return {"day": shanghai_day(), "paths": {}}
    payload.setdefault("paths", {})
    return payload


def save_ledger(ledger: dict[str, Any], root: Path | None = None) -> None:
    path = ledger_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(ledger, ensure_ascii=False, indent=2), encoding="utf-8")


def mark_ran(path_name: str, root: Path | None = None) -> None:
    ledger = load_ledger(root)
    ledger["paths"][path_name] = utc_now().isoformat()
    save_ledger(ledger, root)


def already_ran(path_name: str, root: Path | None = None) -> bool:
    return path_name in load_ledger(root).get("paths", {})
