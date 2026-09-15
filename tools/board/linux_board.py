"""Install and manage only the Linux-owned Misty thought-board service.

This helper runs under the selected WSL user's Python, never inside MistyAI's
environment. It deliberately leaves Docker, QQ and all shared-mind units alone.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import sys
import time
import tomllib
import urllib.error
import urllib.request

from harness.files import replace_path

MARKER = "neuralcloud-board-launcher-v1"
UNIT = "neuralcloud-board.service"
BODY_ID = "misty-linux-board-01"
BOARD_ID = "misty-thoughts"
PORT = 8793
PROVIDER_ID = "misty-linux-thought-board"


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def run(*args: str, check: bool = True, timeout: int = 45) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, capture_output=True, text=True, timeout=timeout)


def ensure_plain_path(path: Path) -> None:
    for component in (path, *path.parents):
        if component.is_symlink():
            raise ValueError("board_path_symlink_rejected")


def atomic_text(path: Path, content: str, mode: int = 0o600) -> None:
    ensure_plain_path(path)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        replace_path(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def systemd_quote(value: Path | str) -> str:
    text = str(value)
    if any(char in text for char in ("\n", "\r", "\x00")):
        raise ValueError("board_invalid_unit_path")
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%") + '"'


def systemd_directory(value: Path) -> str:
    # WorkingDirectory is one path, not an ExecStart-style token list. systemd
    # preserves surrounding quotes here and would reject a quoted absolute path.
    text = str(value)
    if not value.is_absolute() or any(char in text for char in ("\n", "\r", "\x00")):
        raise ValueError("board_invalid_unit_path")
    return text.replace("%", "%%")


def require_port_available() -> None:
    # Match the Linux listener's socket policy. After a clean shutdown, an
    # established HTTP connection may leave TIME_WAIT on this address; it is
    # safe to rebind and must not prevent an immediate Stop -> Start. We do not
    # use SO_REUSEPORT, so another live listener still prevents this bind.
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", PORT))
        except OSError as error:
            if error.errno == errno.EADDRINUSE:
                raise ValueError("board_port_already_in_use") from error
            raise


class BoardDeployment:
    def __init__(self, workspace: Path, *, state_dir: Path | None = None, unit_dir: Path | None = None):
        self.workspace = workspace.absolute()
        self.state = state_dir or Path.home() / ".local/share/neuralcloud/board"
        self.unit_path = (unit_dir or Path("/etc/systemd/system")) / UNIT
        self.owner_path = self.state / "deployment.json"
        self.token_path = self.state / "service.token"
        self.python = self.state / ".venv/bin/python"
        self.fingerprint_path = self.state / "source.sha256"

    def validate_ownership(self) -> bool:
        for path in (self.state, self.owner_path, self.unit_path, self.token_path):
            ensure_plain_path(path)
        owned = self.owner_path.exists()
        if owned:
            owner = json.loads(self.owner_path.read_text(encoding="utf-8"))
            expected = {"owner": MARKER, "workspace": str(self.workspace), "body_instance_id": BODY_ID, "board_id": BOARD_ID}
            if owner != expected:
                raise ValueError("board_deployment_owner_mismatch")
        if self.unit_path.exists():
            unit = self.unit_path.read_text(encoding="utf-8")
            if not owned or not unit.startswith(f"# {MARKER}\n") or f"ExecStart={systemd_quote(self.python)} -m harness.board.service\n" not in unit:
                raise ValueError("board_unit_not_owned")
        if not owned and self.state.exists() and any(self.state.iterdir()):
            raise ValueError("board_state_not_owned")
        return owned

    def active(self) -> bool:
        return run("systemctl", "is-active", "--quiet", UNIT, check=False).returncode == 0

    def health(self) -> dict:
        token = self.token_path.read_text(encoding="utf-8").strip()
        if len(token) < 32 or any(char.isspace() for char in token):
            raise ValueError("board_invalid_service_token")
        request = urllib.request.Request(f"http://127.0.0.1:{PORT}/health", headers={"Authorization": f"Bearer {token}"})
        opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())
        with opener.open(request, timeout=2) as response:
            payload = response.read(65537)
        if len(payload) > 65536:
            raise ValueError("board_health_too_large")
        health = json.loads(payload)
        if health.get("provider_id") != PROVIDER_ID or health.get("body_instance_id") != BODY_ID or health.get("status") != "healthy":
            raise ValueError("board_health_identity_or_status_mismatch")
        return health

    def source_fingerprint(self) -> str:
        digest = hashlib.sha256()
        paths = sorted((self.workspace / "harness/board").glob("*.py"))
        paths += sorted((self.workspace / "harness/embodiment").rglob("*.py"))
        paths += sorted((self.workspace / "harness/research").rglob("*.py"))
        if (self.workspace / "harness/__init__.py").exists():
            paths += [self.workspace / "harness/__init__.py"]
        paths += [self.workspace / "uv.lock", self.workspace / "tools/board/linux_board.py"]
        for path in paths:
            digest.update(str(path.relative_to(self.workspace)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()

    def unit_text(self) -> str:
        return "\n".join([
            f"# {MARKER}", "[Unit]", "Description=Misty Linux thought-board body capability",
            "After=network.target", "StartLimitIntervalSec=60", "StartLimitBurst=5", "", "[Service]",
            "Type=simple", "User=root", f"WorkingDirectory={systemd_directory(self.workspace)}",
            f"Environment={systemd_quote('PYTHONPATH=' + str(self.workspace))}",
            "Environment=PYTHONDONTWRITEBYTECODE=1", "Environment=PYTHONUNBUFFERED=1",
            f"Environment={systemd_quote('NC_BOARD_STATE_DIR=' + str(self.state))}",
            f"Environment={systemd_quote('NC_BOARD_TOKEN_FILE=' + str(self.token_path))}",
            "Environment=NC_BOARD_HOST=127.0.0.1", f"Environment=NC_BOARD_PORT={PORT}",
            f"Environment=NC_BOARD_BODY_INSTANCE_ID={BODY_ID}", f"Environment=NC_BOARD_ID={BOARD_ID}",
            f"ExecStart={systemd_quote(self.python)} -m harness.board.service", "Restart=on-failure",
            "RestartSec=3", "TimeoutStopSec=20", "UMask=0077", "NoNewPrivileges=true",
            "PrivateTmp=true", "ProtectSystem=strict", "ProtectHome=read-only",
            f"ReadWritePaths={systemd_quote(self.state)}", "", "[Install]", "WantedBy=multi-user.target", "",
        ])

    def install_dependencies(self) -> None:
        uv = shutil.which("uv")
        if not uv:
            raise ValueError("board_requires_native_uv")
        if not self.python.exists():
            run(uv, "venv", "--python", sys.executable, str(self.state / ".venv"))
        lock_path = self.workspace / "uv.lock"
        lock = tomllib.loads(lock_path.read_text(encoding="utf-8"))
        versions: dict[str, set[str]] = {}
        for package in lock["package"]:
            if "registry" in package.get("source", {}):
                versions.setdefault(package["name"], set()).add(package["version"])
        required = ("fastapi", "uvicorn", "httpx", "pydantic")
        if any(len(versions.get(name, set())) != 1 for name in required):
            raise ValueError("board_dependency_lock_ambiguous")
        constraints = self.state / "dependency-constraints.txt"
        atomic_text(constraints, "".join(f"{name}=={next(iter(values))}\n" for name, values in sorted(versions.items()) if len(values) == 1))
        requirements = [f"{name}=={next(iter(versions[name]))}" for name in required]
        run(uv, "pip", "install", "--python", str(self.python), "--constraints", str(constraints), *requirements, timeout=180)
        result = run(str(self.python), "-c", "import sys; sys.path.insert(0, sys.argv[1]); import harness.board.service", str(self.workspace))
        if result.returncode:
            raise ValueError("board_import_failed")

    def status(self) -> dict:
        owned = self.validate_ownership()
        active = self.active()
        result = {"owner": MARKER, "owned": owned, "active": active, "body_instance_id": BODY_ID,
                  "board_id": BOARD_ID, "state_dir": str(self.state), "token_file": str(self.token_path), "port": PORT}
        if active:
            if not owned:
                raise ValueError("board_active_unit_not_owned")
            result["health"] = self.health()
        return result

    def start(self) -> dict:
        if os.geteuid() != 0:
            raise ValueError("board_system_unit_requires_root")
        owned = self.validate_ownership()
        was_active = self.active()
        if was_active and not owned:
            raise ValueError("board_active_unit_not_owned")
        if not (self.workspace / "harness/board/service.py").is_file():
            raise ValueError("board_service_source_missing")
        fingerprint = self.source_fingerprint()
        current = self.fingerprint_path.read_text().strip() if self.fingerprint_path.exists() else ""
        if was_active:
            self.health()  # Never replace an unexpected service at this port.
            if current == fingerprint and self.unit_path.read_text() == self.unit_text():
                return self.status() | {"action": "already_running"}
        else:
            require_port_available()
        self.state.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.state.chmod(0o700)
        atomic_text(self.owner_path, json.dumps({"owner": MARKER, "workspace": str(self.workspace), "body_instance_id": BODY_ID, "board_id": BOARD_ID}) + "\n")
        if not self.token_path.exists():
            atomic_text(self.token_path, secrets.token_urlsafe(48) + "\n")
        self.token_path.chmod(0o600)
        self.install_dependencies()
        desired_unit = self.unit_text()
        if not self.unit_path.exists() or self.unit_path.read_text() != desired_unit:
            atomic_text(self.unit_path, desired_unit, 0o644)
            run("systemctl", "daemon-reload")
        run("systemctl", "enable", UNIT)
        run("systemctl", "restart" if was_active else "start", UNIT)
        deadline = time.monotonic() + 25
        while time.monotonic() < deadline:
            try:
                result = self.status()
                if result["active"]:
                    atomic_text(self.fingerprint_path, fingerprint + "\n")
                    return result | {"action": "restarted" if was_active else "started"}
            except (OSError, ValueError, urllib.error.URLError):
                pass
            time.sleep(0.3)
        # Do not leave a newly launched, unverified body repeatedly restarting.
        if not was_active:
            run("systemctl", "stop", UNIT, check=False)
        raise ValueError("board_readiness_failed_see_journalctl")

    def stop(self) -> dict:
        owned = self.validate_ownership()
        if not owned:
            if self.active():
                raise ValueError("board_active_unit_not_owned")
            return {"owner": MARKER, "action": "not_installed", "active": False}
        run("systemctl", "stop", UNIT)
        # Explicit pause persists through the next Linux boot. Start enables it.
        run("systemctl", "disable", UNIT)
        return self.status() | {"action": "stopped"}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("start", "stop", "status"))
    parser.add_argument("--workspace", type=Path, required=True)
    args = parser.parse_args()
    try:
        deployment = BoardDeployment(args.workspace)
        result = getattr(deployment, args.action)()
        print(json.dumps(result, ensure_ascii=True))
    except subprocess.CalledProcessError:
        # Dependency resolver/service output is deliberately not relayed, since
        # environment-configured indexes can contain authentication material.
        print(json.dumps({"error": "board_subprocess_failed_see_local_journal"}), file=sys.stderr)
        raise SystemExit(1)
    except Exception as error:
        safe = str(error) if isinstance(error, ValueError) else type(error).__name__
        print(json.dumps({"error": safe}), file=sys.stderr)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
