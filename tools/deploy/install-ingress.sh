#!/usr/bin/env bash
# Prepare only: starting/enabling neuralcloud-proxy remains an explicit step.
set -euo pipefail
exec python3 - "$@" <<'PYTHON'
"""Install a dedicated Caddy service without touching system-wide Caddy or FRP."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import subprocess
import sys
import tempfile

OWNER = "neuralcloud-ingress-installer-v1"
UNIT_MARKER = f"# Managed by {OWNER}\n"
USER = "neuralcloud-proxy"
UNIT_NAME = "neuralcloud-proxy.service"
CONFIG_ROOT = Path("/etc/neuralcloud-proxy")
STATE_ROOT = Path("/var/lib/neuralcloud-proxy")
UNIT_PATH = Path("/etc/systemd/system") / UNIT_NAME
LOCK_PATH = Path("/run/lock/neuralcloud-ingress-install.lock")


class PreparationError(RuntimeError):
    pass


def fail(reason):
    raise PreparationError(reason)


def safe_path(value, *, allow_symlink=False):
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", str(value))
            or ".." in PurePosixPath(value).parts
            or str(PurePosixPath(value)) != str(value) or str(value) == "/"):
        fail("use_normalized_absolute_paths_without_spaces")
    path = Path(value)
    if not allow_symlink and any(item.is_symlink() for item in (path, *path.parents)):
        fail("symlink_in_private_installation_path")
    return path


def regular_file(path, *, private=False, max_bytes=None):
    safe_path(path)
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or (max_bytes is not None and info.st_size > max_bytes):
        fail("expected_a_bounded_regular_file")
    if private and info.st_mode & 0o077:
        fail("bcrypt_hash_file_must_be_private_0600")
    return info


def checked(argv, *, cwd=None, env=None):
    # Never echo a generated Caddyfile or command output: they contain the hash.
    result = subprocess.run(argv, cwd=cwd, env=env, text=True,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if result.returncode:
        fail(f"command_failed_{Path(argv[0]).name}")
    return result.stdout.strip()


def caddy_version(path):
    path = safe_path(safe_path(path, allow_symlink=True).resolve(strict=True))
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or not os.access(path, os.X_OK):
        fail("caddy_binary_must_be_executable")
    if info.st_uid != 0 or info.st_mode & 0o022:
        fail("caddy_binary_must_be_root_owned_and_not_group_or_world_writable")
    version = checked([str(path), "version"])
    match = re.match(r"v?(\d+)\.(\d+)\.(\d+)(?:\s|$|[-+])", version)
    if not match or tuple(map(int, match.groups())) < (2, 8, 0):
        fail("caddy_2_8_or_newer_required_for_basic_auth")
    return path, match.group(0).strip()


def render_unit(caddy):
    caddy = Path(caddy).as_posix()
    config_root, state_root = CONFIG_ROOT.as_posix(), STATE_ROOT.as_posix()
    return UNIT_MARKER + f"""[Unit]
Description=NeuralCloud private authenticated ingress
After=network.target

[Service]
Type=simple
User={USER}
Group={USER}
WorkingDirectory={state_root}
Environment=HOME={state_root}
Environment=XDG_CONFIG_HOME={state_root}/config
Environment=XDG_DATA_HOME={state_root}/data
ExecStartPre={caddy} validate --config {config_root}/Caddyfile --adapter caddyfile
ExecStart={caddy} run --config {config_root}/Caddyfile --adapter caddyfile
Restart=on-failure
RestartSec=3
TimeoutStopSec=30
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
StateDirectory={USER}
StateDirectoryMode=0700
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
CapabilityBoundingSet=
AmbientCapabilities=

[Install]
WantedBy=multi-user.target
"""


def check_managed_directory(path):
    safe_path(path)
    if not path.exists():
        return
    if not path.is_dir():
        fail("installation_directory_is_not_a_directory")
    marker = path / ".neuralcloud-ingress-owner"
    if marker.is_symlink() or not marker.is_file() or marker.read_text() != OWNER + "\n":
        fail("refusing_to_reuse_an_unmanaged_proxy_directory")


def atomic_write(path, content, mode, uid=0, gid=0):
    safe_path(path)
    descriptor, name = tempfile.mkstemp(prefix=".ingress-", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            os.fchmod(handle.fileno(), mode)
            os.fchown(handle.fileno(), uid, gid)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def proxy_account():
    import grp
    import pwd

    try:
        account = pwd.getpwnam(USER)
    except KeyError:
        try:
            grp.getgrnam(USER)
        except KeyError:
            checked(["groupadd", "--system", USER])
        checked(["useradd", "--system", "--gid", USER, "--home-dir", str(STATE_ROOT),
                 "--no-create-home", "--shell", "/usr/sbin/nologin", USER])
        account = pwd.getpwnam(USER)
    group = grp.getgrnam(USER)
    if (account.pw_uid == 0 or account.pw_gid != group.gr_gid
            or account.pw_dir != str(STATE_ROOT)
            or account.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin"}):
        fail("existing_proxy_user_is_not_an_isolated_service_account")
    return account.pw_uid, account.pw_gid


def ensure_host():
    if sys.platform != "linux" or os.geteuid() != 0 or not Path("/run/systemd/system").is_dir():
        fail("run_as_root_on_a_running_systemd_linux_host")
    for program in ("systemctl", "runuser", "useradd", "groupadd"):
        if not shutil.which(program):
            fail(f"missing_prerequisite_{program}")


def prepare(args):
    ensure_host()
    config = safe_path(args.config)
    private_hash = safe_path(args.hash_file)
    regular_file(config, max_bytes=131072)
    regular_file(private_hash, private=True, max_bytes=1024)
    digest = private_hash.read_text(encoding="utf-8-sig").strip()
    if not re.fullmatch(r"\$2[aby]\$\d{2}\$[./A-Za-z0-9]{53}", digest):
        fail("invalid_bcrypt_hash_file")
    raw = json.loads(config.read_text(encoding="utf-8-sig"))
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        fail("invalid_deployment_configuration")
    # current is intentionally a release symlink; private config/state are not.
    release = safe_path(raw.get("paths", {}).get("current", "/opt/neuralcloud/current"),
                        allow_symlink=True).resolve(strict=True)
    python = safe_path(args.python, allow_symlink=True) if args.python else release / ".venv/bin/python"
    if not python.is_file() or not os.access(python, os.X_OK):
        fail("deployment_virtualenv_python_is_missing")
    caddy, version = caddy_version(args.caddy)
    for path in (CONFIG_ROOT, STATE_ROOT):
        check_managed_directory(path)
    safe_path(UNIT_PATH)
    if UNIT_PATH.exists() and (not UNIT_PATH.is_file() or not UNIT_PATH.read_text().startswith(UNIT_MARKER)):
        fail("refusing_to_replace_an_unmanaged_systemd_unit")
    env = {key: value for key, value in os.environ.items()
           if not key.startswith("NC_") and key not in {"PYTHONPATH", "PYTHONHOME"}}
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    # Validate deployment schema, renderer inputs and Caddy syntax before users,
    # persistent configuration or systemd units are changed.
    with tempfile.TemporaryDirectory(prefix="neuralcloud-ingress-") as staging:
        candidate = Path(staging) / "Caddyfile"
        argv = [str(python), "-m", "harness.deploy.ingress", "--config", str(config),
                "--hash-file", str(private_hash), "--output", str(candidate),
                "--username", args.username, "--proxy-port", str(args.proxy_port)]
        if args.maintenance:
            argv.append("--maintenance")
        checked(argv, cwd=release, env=env)
        checked([str(caddy), "validate", "--config", str(candidate), "--adapter", "caddyfile"],
                env={**env, "HOME": staging, "XDG_CONFIG_HOME": staging, "XDG_DATA_HOME": staging})
        rendered = candidate.read_text(encoding="utf-8")
    uid, gid = proxy_account()
    for path, owner, group, mode in ((CONFIG_ROOT, 0, gid, 0o750), (STATE_ROOT, uid, gid, 0o700)):
        path.mkdir(mode=mode, parents=True, exist_ok=True)
        os.chown(path, owner, group)
        os.chmod(path, mode)
        atomic_write(path / ".neuralcloud-ingress-owner", OWNER + "\n", 0o600, owner, group)
    # Stage under the real parent and validate as the actual service user. This
    # catches unreadable binaries/configuration and state-directory permissions.
    candidate = CONFIG_ROOT / ".Caddyfile.pending"
    try:
        atomic_write(candidate, rendered, 0o640, 0, gid)
        checked(["runuser", "-u", USER, "--", "env", f"HOME={STATE_ROOT}",
                 f"XDG_CONFIG_HOME={STATE_ROOT}/config", f"XDG_DATA_HOME={STATE_ROOT}/data",
                 str(caddy), "validate", "--config", str(candidate), "--adapter", "caddyfile"])
        os.replace(candidate, CONFIG_ROOT / "Caddyfile")
    finally:
        if candidate.exists():
            candidate.unlink()
    atomic_write(UNIT_PATH, render_unit(caddy), 0o644)
    atomic_write(CONFIG_ROOT / "install-manifest.json", json.dumps({
        "installer": OWNER, "caddy_version": version, "caddy_binary": str(caddy),
        "deployment_config": str(config), "source_release": str(release),
        "unit": UNIT_NAME, "proxy_port": args.proxy_port,
        "caddyfile": str(CONFIG_ROOT / "Caddyfile"), "state_root": str(STATE_ROOT),
    }, indent=2) + "\n", 0o640, 0, gid)
    checked(["systemctl", "daemon-reload"])
    print(f"Prepared {UNIT_NAME}; no start, restart or enable was requested.")
    print(f"Configuration: {CONFIG_ROOT}/Caddyfile (root:{USER} 0640)")
    print(f"When ready: sudo systemctl start {UNIT_NAME}")
    print(f"Optional boot activation: sudo systemctl enable {UNIT_NAME}")
    print("After later configuration changes, explicitly restart this service; Caddy admin API is disabled.")


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="/etc/neuralcloud/deploy.json")
    parser.add_argument("--hash-file", required=True, help="absolute path to a private (0600) bcrypt hash")
    parser.add_argument("--caddy", default="/usr/local/bin/caddy", help="existing root-owned Caddy >= 2.8 executable")
    parser.add_argument("--python", help="deployment virtualenv Python; defaults to current/.venv/bin/python")
    parser.add_argument("--username", default="admin")
    parser.add_argument("--proxy-port", type=int, default=8879)
    parser.add_argument("--maintenance", action="store_true", help="install a 503 response without backend access")
    args = parser.parse_args(argv)
    if sys.platform != "linux" or os.geteuid() != 0:
        fail("run_as_root_on_linux")
    import fcntl
    safe_path(LOCK_PATH)
    descriptor = os.open(LOCK_PATH, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        prepare(args)
    finally:
        os.close(descriptor)


if __name__ == "__main__":
    try:
        main()
    except PreparationError as error:
        print(f"Ingress preparation failed: {error}", file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, KeyError, TypeError):
        # Detailed subprocess output can contain authentication hashes.
        print("Ingress preparation failed; check prerequisites, private file permissions and configuration.", file=sys.stderr)
        sys.exit(1)
PYTHON
