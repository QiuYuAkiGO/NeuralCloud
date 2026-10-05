"""Ordered systemd lifecycle; failed stops never permit a release switch."""
from __future__ import annotations

import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from .config import CORE_SERVICES, SERVICE_MODULES, DeployError, DeploymentConfig, ensure_linux, validate_release_id
from .package import extract_package, verify_release
from .runner import read_token


def unit_name(service: str) -> str:
    return f"neuralcloud-{service.replace('_', '-')}.service"


def _quote(value: object) -> str:
    return '"' + str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def render_units(cfg: DeploymentConfig) -> dict[str, str]:
    units = {}
    current = PurePosixPath(cfg.paths["current"])
    config_path = str(cfg.config_file)
    if not config_path.startswith("/"):
        config_path = str(PurePosixPath(cfg.paths["config_root"]) / "deploy.json")
    for service in cfg.services:
        dependencies = []
        if service == "runtime":
            dependencies = ["cognitive", "homeostasis"]
        elif service in {"gui", "qq_direct"}:
            dependencies = ["runtime"]
        after = " ".join(unit_name(name) for name in dependencies)
        ordered_after = after + (" neuralcloud-board.service" if service == "runtime" and "board" in cfg.services else "")
        command = [current / ".venv/bin/python", "-m", "harness.deploy", "--config", config_path, "run", service]
        wait_command = [current / ".venv/bin/python", "-m", "harness.deploy", "--config", config_path,
                        "wait", service, "--boot-only"]
        units[unit_name(service)] = (
            "# Managed by harness.deploy; configuration contains file references only.\n"
            "[Unit]\n"
            f"Description=NeuralCloud {service}\n"
            f"After=network-online.target {ordered_after}\nWants=network-online.target\n"
            + (f"Requires={after}\n" if after else "")
            + "PartOf=neuralcloud.target\n\n[Service]\nType=simple\n"
            f"User={cfg.service_user}\nGroup={cfg.service_user}\n"
            f"WorkingDirectory={current}\n"
            f"ExecStart={' '.join(_quote(arg) for arg in command)}\n"
            f"ExecStartPost={' '.join(_quote(arg) for arg in wait_command)}\n"
            "Environment=PYTHONUNBUFFERED=1\nEnvironment=PYTHONDONTWRITEBYTECODE=1\n"
            "UMask=0077\nNoNewPrivileges=true\nPrivateTmp=true\n"
            "Restart=on-failure\nRestartSec=5\nKillSignal=SIGTERM\nKillMode=control-group\n"
            f"TimeoutStartSec={cfg.startup_timeout_seconds}\nTimeoutStopSec={cfg.stop_timeout_seconds}\n"
            "StandardOutput=journal\nStandardError=journal\n\n[Install]\nWantedBy=neuralcloud.target\n"
        )
    units["neuralcloud.target"] = (
        "# Managed by harness.deploy\n[Unit]\nDescription=NeuralCloud shared runtime\n"
        "Wants=" + " ".join(unit_name(name) for name in CORE_SERVICES) + "\n"
        "After=network-online.target\n\n[Install]\nWantedBy=multi-user.target\n"
    )
    return units


def _systemctl(*args: str, timeout: float = 210, check: bool = True) -> subprocess.CompletedProcess:
    result = subprocess.run(["systemctl", *args], text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        # Do not echo journal output or environment-containing error diagnostics.
        raise DeployError(f"systemctl {args[0]} failed; inspect the selected unit journal")
    return result


def unit_status(service: str) -> dict[str, str]:
    result = _systemctl("show", unit_name(service), "--property=LoadState,ActiveState,SubState,Result,MainPID,FragmentPath", check=False)
    return dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)


def _managed_unit(state: dict[str, str]) -> bool:
    fragment = state.get("FragmentPath")
    if not fragment:
        return False
    try:
        return Path(fragment).read_text(encoding="utf-8").startswith("# Managed by harness.deploy")
    except OSError:
        return False


def status(cfg: DeploymentConfig) -> dict:
    return {"release": cfg.release_id, "services": {name: unit_status(name) for name in cfg.services}}


def _health(cfg: DeploymentConfig, service: str, *, timeout: float = 5) -> dict:
    bindings = cfg.effective_bindings()
    if service == "gui":
        path, token = "/api/ops/availability", None
    elif service == "runtime":
        path, token = "/v1/status", read_token(bindings["NC_CONTROL_TOKEN_FILE"])
    elif service in {"cognitive", "homeostasis"}:
        path, token = "/v1/health", read_token(bindings["NC_SERVICE_TOKEN_FILE"])
    elif service == "board":
        path, token = "/health", read_token(bindings["NC_BOARD_TOKEN_FILE"])
    else:
        # The direct worker has a private local status file, not a network port.
        direct = json.loads(Path(bindings["NC_QQ_DIRECT_CONFIG"]).read_text(encoding="utf-8-sig"))
        return json.loads(Path(direct["status_file"]).read_text(encoding="utf-8-sig"))
    request = Request(f"http://127.0.0.1:{cfg.ports[service]}{path}")
    if token:
        request.add_header("Authorization", "Bearer " + token)
    with urlopen(request, timeout=timeout) as response:
        payload = json.load(response)
    if not isinstance(payload, dict):
        raise DeployError("invalid service health response")
    return payload


def wait_healthy(cfg: DeploymentConfig, service: str, *, boot_only: bool = False) -> None:
    deadline = time.monotonic() + cfg.startup_timeout_seconds
    while time.monotonic() < deadline:
        unit = unit_status(service)
        if unit.get("ActiveState") == "failed":
            raise DeployError(f"{service} failed during startup")
        try:
            body = _health(cfg, service)
            if service == "runtime":
                health = body.get("health", {})
                ready = (body.get("lifecycle") in {"RUNNING", "PAUSED"} if boot_only else body.get("lifecycle") == "RUNNING")
                ready = ready and all(health.get(key) is True for key in ("journal_writable", "cognitive_healthy", "homeostasis_healthy"))
            elif service == "gui":
                ready = isinstance(body.get("runtime"), dict) and body["runtime"].get("lifecycle") in ({"RUNNING", "PAUSED"} if boot_only else {"RUNNING"})
            elif service == "qq_direct":
                stamp = datetime.fromisoformat(body.get("heartbeat_at", ""))
                main_pid = int(unit.get("MainPID", "0"))
                repository = body.get("repository_root")
                ready = (body.get("service") == "qq-direct-onebot-v1"
                    and body.get("running") is True
                    and type(body.get("pid")) is int and body["pid"] == main_pid and main_pid > 0
                    and isinstance(repository, str) and Path(repository).is_absolute()
                    and Path(repository).resolve() == cfg.path("current").resolve()
                    and stamp.tzinfo is not None
                    and 0 <= (datetime.now(timezone.utc) - stamp).total_seconds() <= 30)
                # A booted worker can wait for NapCat login and reconnect on its
                # own. Do not kill it through ExecStartPost while the account is
                # offline; explicit deployment verification still needs QQ online.
                ready = ready and (boot_only or body.get("connected") is True)
            elif service == "board":
                ready = body.get("status") == "healthy"
            else:
                ready = body.get("healthy") is True
            if ready and unit.get("ActiveState") in ({"active", "activating"} if boot_only else {"active"}):
                return
        except (OSError, ValueError, TypeError, HTTPError, URLError, DeployError, KeyError):
            pass
        time.sleep(0.5)
    raise DeployError(f"{service} did not become ready; inspect status (PAUSED requires explicit review/resume)")


def _pause_runtime(cfg: DeploymentConfig) -> None:
    deadline = time.monotonic() + cfg.stop_timeout_seconds
    body = _health(cfg, "runtime", timeout=min(5, cfg.stop_timeout_seconds))
    if body.get("lifecycle") not in {"RUNNING", "PAUSING"}:
        return
    token = read_token(cfg.effective_bindings()["NC_CONTROL_TOKEN_FILE"])
    request = Request(f"http://127.0.0.1:{cfg.ports['runtime']}/v1/pause",
                      data=b'{"reason":"deployment maintenance"}', method="POST",
                      headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"})
    try:
        if body.get("lifecycle") == "RUNNING":
            # HTTP 200 acknowledges queued ingress, not completed maintenance.
            with urlopen(request, timeout=max(0.01, min(90, deadline - time.monotonic()))) as response:
                response.read()
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise DeployError("runtime pause timed out; stop/activation blocked")
            state = _health(cfg, "runtime", timeout=min(5, remaining)).get("lifecycle")
            if state == "PAUSED":
                return
            if state not in {"RUNNING", "PAUSING"}:
                raise DeployError("runtime pause failed; stop/activation blocked")
            time.sleep(min(0.25, max(0, deadline - time.monotonic())))
    except (OSError, ValueError, HTTPError, URLError):
        raise DeployError("runtime maintenance drain failed; stop/activation blocked") from None


def stop_services(cfg: DeploymentConfig) -> None:
    # Include removed optional components: configuration shrink must not leave an
    # old QQ writer alive or let it return at the next boot.
    for name in ("qq_direct", "gui", "runtime", "cognitive", "homeostasis", "board"):
        old = unit_status(name)
        if old.get("LoadState") == "not-found":
            continue
        if not _managed_unit(old):
            raise DeployError(f"refusing to stop unmanaged unit {unit_name(name)}")
        if name == "runtime" and old.get("ActiveState") == "active":
            _pause_runtime(cfg)
        _systemctl("stop", unit_name(name), timeout=cfg.stop_timeout_seconds + 15)
        state = unit_status(name)
        if state.get("ActiveState") not in {"inactive", "failed"} or state.get("MainPID", "0") != "0":
            raise DeployError(f"{name} still has a writer; activation blocked")
        if state.get("Result", "success") not in {"", "success"}:
            raise DeployError(f"{name} did not stop cleanly; activation blocked")


def start_services(cfg: DeploymentConfig, *, core_only: bool = False) -> None:
    names = tuple(name for name in ("cognitive", "homeostasis", "board", "runtime", "gui", "qq_direct")
                  if name in cfg.services and (not core_only or name in CORE_SERVICES))
    # systemd starts dependencies; only this command gates traffic on health.
    for name in names:
        _systemctl("start", unit_name(name), timeout=cfg.startup_timeout_seconds + 15)
        wait_healthy(cfg, name)


def _ensure_directory(path: Path, *, mode: int, uid: int, gid: int) -> None:
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise DeployError("managed persistent directories must not traverse symlinks")
    path.mkdir(parents=True, exist_ok=True, mode=mode)
    os.chmod(path, mode)
    os.chown(path, uid, gid)


def install(cfg: DeploymentConfig, *, archive: Path | None, uv: str = "uv", skip_sync: bool = False,
            unit_directory: Path = Path("/etc/systemd/system")) -> dict:
    ensure_linux()
    if os.geteuid() != 0:
        raise DeployError("install requires root to install systemd units and assign private state ownership")
    import pwd
    try:
        account = pwd.getpwnam(cfg.service_user)
    except KeyError:
        raise DeployError("create the configured Linux service user before install") from None
    for key in ("install_root", "releases"):
        _ensure_directory(cfg.path(key), mode=0o755, uid=0, gid=0)
    for key in ("state_root", "config_root", "log_root", "backup_root"):
        _ensure_directory(cfg.path(key), mode=0o700, uid=account.pw_uid, gid=account.pw_gid)
    for child in ("runtime", "cognitive", "homeostasis/domain", "homeostasis/profiles", "runtime/manifests", "presets"):
        _ensure_directory(cfg.path("state_root") / child, mode=0o700, uid=account.pw_uid, gid=account.pw_gid)
    # The deployment file has no token values but may contain sensitive topology.
    os.chmod(cfg.config_file, 0o600)
    os.chown(cfg.config_file, account.pw_uid, account.pw_gid)
    token_keys = ("NC_CONTROL_TOKEN_FILE", "NC_SERVICE_TOKEN_FILE") + (("NC_BOARD_TOKEN_FILE",) if "board" in cfg.services else ())
    for key in token_keys:
        token_file = Path(cfg.effective_bindings()[key])
        if not token_file.exists() and not token_file.is_symlink():
            descriptor = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as handle:
                handle.write(secrets.token_urlsafe(48) + "\n")
        read_token(token_file)
        os.chown(token_file, account.pw_uid, account.pw_gid)
    release = extract_package(archive, cfg.path("releases")) if archive is not None else None
    if release is not None and not skip_sync:
        resolved_uv = shutil.which(uv)
        if resolved_uv is None:
            raise DeployError("uv is required to synchronize the locked Python environment")
        result = subprocess.run([resolved_uv, "sync", "--locked", "--python", "3.12", "--no-managed-python", "--no-python-downloads",
                                 "--extra", "gui", "--extra", "qq", "--no-dev"],
                                cwd=release, capture_output=True, text=True, timeout=1800)
        if result.returncode:
            raise DeployError("locked dependency install failed; release retained for inspection")
        verify_release(release)
    unit_directory.mkdir(parents=True, exist_ok=True)
    for name, content in render_units(cfg).items():
        target = unit_directory / name
        if target.is_symlink() or (target.exists() and not target.read_text().startswith("# Managed by harness.deploy")):
            raise DeployError(f"refusing to overwrite unmanaged systemd unit {name}")
        target.write_text(content, encoding="utf-8")
        target.chmod(0o644)
    _systemctl("daemon-reload")
    return {"installed_release": release.name if release else None, "activated": False, "units": list(render_units(cfg))}


def _switch_release(cfg: DeploymentConfig, release: Path) -> None:
    current = cfg.path("current")
    if current.exists() and not current.is_symlink():
        raise DeployError("current must be a managed release symlink")
    temporary = current.with_name(current.name + ".next")
    if temporary.exists() or temporary.is_symlink():
        raise DeployError("a pending release switch exists; inspect it first")
    temporary.symlink_to(release, target_is_directory=True)
    os.replace(temporary, current)


def activate(cfg: DeploymentConfig, release_id: str, *, core_only: bool = False) -> dict:
    ensure_linux()
    release = cfg.path("releases") / validate_release_id(release_id)
    verify_release(release)
    if not (release / ".venv/bin/python").exists():
        raise DeployError("release Python environment is missing")
    old = cfg.release_id
    stop_services(cfg)
    requested = set(CORE_SERVICES if core_only else cfg.services)
    for name in SERVICE_MODULES:
        if name not in requested and unit_status(name).get("LoadState") != "not-found":
            _systemctl("disable", unit_name(name))
    _switch_release(cfg, release)
    try:
        start_services(cfg, core_only=core_only)
    except Exception:
        # Recovery may have written new state; never silently restore an old snapshot.
        for name in reversed(cfg.services):
            _systemctl("stop", unit_name(name), timeout=cfg.stop_timeout_seconds + 15, check=False)
        raise
    marker = cfg.path("state_root") / "deployment.json"
    marker.write_text(json.dumps({"release": release_id, "previous_release": old,
                                  "activated_at": time.time(), "snapshot_restored": False}, indent=2) + "\n")
    _systemctl("enable", "neuralcloud.target")
    for name in cfg.services:
        if not core_only or name in CORE_SERVICES:
            _systemctl("enable", unit_name(name))
    return {"release": release_id, "previous_release": old, "services": list(CORE_SERVICES if core_only else cfg.services)}


def rollback(cfg: DeploymentConfig, release_id: str | None, *, core_only: bool = False) -> dict:
    if release_id is None:
        marker = cfg.path("state_root") / "deployment.json"
        release_id = json.loads(marker.read_text())["previous_release"]
    if not release_id:
        raise DeployError("no previous release recorded")
    result = activate(cfg, release_id, core_only=core_only)
    result["state_preserved"] = True
    return result


def _pid_listens_on_port(pid: int, port: int) -> bool:
    if pid <= 1:
        return False
    try:
        sockets = set()
        for family in ("tcp", "tcp6"):
            for line in Path(f"/proc/{pid}/net/{family}").read_text().splitlines()[1:]:
                fields = line.split()
                if len(fields) > 9 and fields[3] == "0A" and int(fields[1].rsplit(":", 1)[1], 16) == port:
                    sockets.add("socket:[" + fields[9] + "]")
        for descriptor in Path(f"/proc/{pid}/fd").iterdir():
            try:
                if os.readlink(descriptor) in sockets:
                    return True
            except OSError:
                continue
    except (OSError, ValueError):
        return False
    return False


def preflight(cfg: DeploymentConfig) -> dict:
    checks = {"linux": os.name == "posix" and Path("/proc").exists(), "systemctl": shutil.which("systemctl") is not None,
              "python_3_12": __import__("sys").version_info[:2] == (3, 12), "uv": shutil.which("uv") is not None}
    occupied, conflicts = [], []
    for name, port in cfg.ports.items():
        if name not in cfg.services:
            continue
        with socket.socket() as probe:
            probe.settimeout(0.15)
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                occupied.append(name)
                state = unit_status(name) if checks["systemctl"] else {}
                if not (_managed_unit(state) and state.get("ActiveState") == "active"
                        and _pid_listens_on_port(int(state.get("MainPID", "0")), port)):
                    conflicts.append(name)
    return {"checks": checks, "occupied_ports": occupied, "unmanaged_port_conflicts": conflicts, "paths": cfg.paths,
            "services": list(cfg.services), "public_origin": cfg.public_origin,
            "ready": all(checks.values()) and not conflicts}
