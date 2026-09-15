#!/usr/bin/env bash
# Independent, pinned NapCat installation. No service is started without --start.
set -euo pipefail
exec python3 - "$@" <<'PYTHON'
"""Keep the installer self-contained; use argv lists, never shell interpolation."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.parse
import urllib.request
import zipfile

NAPCAT_VERSION = "v4.18.28"
NAPCAT_SHA256 = "bcdd8bdb9e44bd0cf6a90908e572141787fd9e98cb8d8eecc5adf25bbdcabb94"
NAPCAT_URL = f"https://github.com/NapNeko/NapCatQQ/releases/download/{NAPCAT_VERSION}/NapCat.Shell.zip"
QQ_VERSION = "3.2.23-44343"
QQ_SHA256 = "a4252719c1beb8adce0da09ebfc310ce50c79ea548f5cce429505765d0bfba84"
QQ_URL = "https://dldir1.qq.com/qqfile/qq/QQNT/94704804/linuxqq_3.2.23-44343_amd64.deb"
OWNER = "neuralcloud-napcat-installer-v1"
UNIT_MARKER = f"# Managed by {OWNER}\n"
UNIT_NAME = "neuralcloud-napcat.service"
SYSTEMD_UNIT_DIR = Path("/etc/systemd/system")
INSTALL_LOCK_PATH = Path("/run/lock/neuralcloud-napcat-install.lock")


def fail(reason):
    raise ValueError(reason)


def safe_path(value):
    """The restricted alphabet is also safe in systemd path/environment fields."""
    if (not re.fullmatch(r"/[A-Za-z0-9_./-]+", value)
            or ".." in PurePosixPath(value).parts or str(PurePosixPath(value)) != value
            or len(PurePosixPath(value).parts) < 3):
        fail("use_a_normalized_absolute_path_below_a_private_directory")
    path = Path(value)
    for part in (path, *path.parents):
        if part.is_symlink():
            fail("symlink_in_installation_path")
    return path


def read_token(value):
    path = Path(value)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        fail("token_requires_an_absolute_regular_private_file")
    if any(part.is_symlink() for part in path.parents):
        fail("symlink_in_token_file_path")
    info = path.stat()
    if info.st_size > 8192 or (os.name == "posix" and info.st_mode & 0o077):
        fail("token_file_must_have_private_permissions_0600")
    try:
        secret = path.read_text(encoding="utf-8-sig").strip()
    except (OSError, UnicodeError):
        fail("token_file_unreadable")
    if not 32 <= len(secret) <= 4096 or not secret.isascii() or not secret.isprintable() or any(c.isspace() for c in secret):
        fail("token_must_be_at_least_32_non_whitespace_ascii_characters")
    return secret


def sha256(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path, expected):
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        fail("archive_sha256_must_be_64_lowercase_hex_characters")
    if Path(path).is_symlink() or not Path(path).is_file() or sha256(path) != expected:
        fail("archive_sha256_mismatch")


def checked(command):
    result = subprocess.run(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if result.returncode:
        fail(f"command_failed_{Path(command[0]).name}")
    return result.stdout.strip()


def extract_zip(source, destination):
    """Validate every entry before extracting even a single file."""
    with zipfile.ZipFile(source) as archive:
        for entry in archive.infolist():
            name = PurePosixPath(entry.filename)
            mode = entry.external_attr >> 16
            if (name.is_absolute() or ".." in name.parts or "\\" in entry.filename
                    or ":" in entry.filename or stat.S_ISLNK(mode)):
                fail("unsafe_napcat_archive_entry")
        archive.extractall(destination)


def extract_deb(source, destination):
    if sys.version_info < (3, 12):
        fail("python_3_12_required_for_safe_deb_extraction")
    with subprocess.Popen(["dpkg-deb", "--fsys-tarfile", str(source)], stdout=subprocess.PIPE,
                          stderr=subprocess.DEVNULL) as process:
        try:
            with tarfile.open(fileobj=process.stdout, mode="r|") as archive:
                archive.extractall(destination, filter=qq_tar_filter)
        finally:
            process.stdout.close()
        if process.wait():
            fail("qq_package_extraction_failed")


def qq_tar_filter(member, destination):
    # Desktop launchers/icons and /usr/bin/qq symlinks belong to a system package install.
    # Only extract the private application tree; reject unsafe links inside that tree.
    path = PurePosixPath(member.name)
    if path.is_absolute() or ".." in path.parts:
        fail("unsafe_qq_archive_entry")
    if path.parts[:2] != ("opt", "QQ"):
        return None
    return tarfile.data_filter(member, destination)


def secure_url(url):
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password or parsed.fragment:
        fail("download_url_requires_https_without_credentials")
    return url


class HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        secure_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def obtain(local, url, expected, destination):
    if local:
        source = Path(local)
        if not source.is_absolute():
            fail("archive_file_requires_absolute_path")
    else:
        source = destination
        opener = urllib.request.build_opener(HTTPSRedirect())
        with opener.open(secure_url(url), timeout=45) as response, source.open("xb") as out:
            shutil.copyfileobj(response, out, length=1024 * 1024)
    verify_archive(source, expected)
    return source


def configurations(account, onebot_token, webui_token, ws_port, webui_port):
    return {
        f"onebot11_{account}.json": {
            "network": {
                "httpServers": [], "httpClients": [], "websocketClients": [],
                "websocketServers": [{
                    "name": "neuralcloud", "enable": True, "host": "127.0.0.1", "port": ws_port,
                    "messagePostFormat": "array", "reportSelfMessage": True, "token": onebot_token,
                    "enableForcePushEvent": True, "debug": False, "heartInterval": 30000,
                }],
            },
            "enableLocalFile2Url": True, "parseMultMsg": True, "musicSignUrl": "",
        },
        "webui.json": {
            "host": "127.0.0.1", "port": webui_port, "token": webui_token, "loginRate": 3,
            "accessControlMode": "none", "ipWhitelist": [], "ipBlacklist": [], "enableXForwardedFor": False,
        },
        "napcat.json": {
            "fileLog": False, "consoleLog": False, "fileLogLevel": "error", "consoleLogLevel": "error",
            "packetServer": "",
        },
        # Unknown accounts must not inherit a working OneBot server.
        "onebot11.json": {
            "network": {"httpServers": [], "httpClients": [], "websocketClients": [], "websocketServers": []},
            "enableLocalFile2Url": False,
        },
    }


def contains_config(actual, required):
    """NapCat may add defaults. Existing security-sensitive entries must still match."""
    if isinstance(required, dict):
        return isinstance(actual, dict) and all(k in actual and contains_config(actual[k], v) for k, v in required.items())
    if isinstance(required, list):
        return isinstance(actual, list) and len(actual) == len(required) and all(
            contains_config(a, r) for a, r in zip(actual, required))
    return type(actual) is type(required) and actual == required


def write_json(path, value, uid=0, gid=0):
    with path.open("x", encoding="utf-8") as stream:
        os.fchmod(stream.fileno(), 0o600)
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    os.chown(path, uid, gid)


def service_text(args, release):
    return UNIT_MARKER + f"""[Unit]
Description=NeuralCloud isolated NapCat QQ bridge
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=120
StartLimitBurst=5

[Service]
Type=simple
User={args.user}
Group={args.user}
WorkingDirectory={args.home}
Environment=HOME={args.home}
Environment=XDG_CONFIG_HOME={args.home}/.config
Environment=NAPCAT_WORKDIR={args.state_dir}
ExecStart=/usr/bin/xvfb-run -a {release}/opt/QQ/qq --no-sandbox -q {args.account}
Restart=on-failure
RestartSec=10
TimeoutStopSec=60
KillMode=control-group
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths={args.home} {args.state_dir}
StandardOutput=null
StandardError=journal

[Install]
WantedBy=multi-user.target
"""


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--account", required=True, help="QQ account identity; no group/contact roster is installed")
    result.add_argument("--onebot-token-file", required=True)
    result.add_argument("--webui-token-file", required=True)
    result.add_argument("--qq-deb", help="Absolute path to an already downloaded, pinned official amd64 .deb")
    result.add_argument("--qq-url", default=QQ_URL, help="Used only when --qq-deb is omitted; never downloads latest")
    result.add_argument("--qq-sha256", default=QQ_SHA256, help="Pinned official QQ44343 checksum; required to change with any other package")
    result.add_argument("--qq-version", default=QQ_VERSION, help="Must match dpkg-deb Version exactly; build >= 40768")
    result.add_argument("--napcat-zip", help="Absolute path to the pinned v4.18.28 ZIP for offline installation")
    result.add_argument("--napcat-url", default=NAPCAT_URL, help="Mirror permitted only with the fixed official SHA256")
    result.add_argument("--user", default="neuralcloud-qq")
    result.add_argument("--home", default="/var/lib/neuralcloud-qq")
    result.add_argument("--prefix", default="/opt/neuralcloud-napcat")
    result.add_argument("--state-dir", default="/var/lib/neuralcloud-qq/napcat")
    result.add_argument("--ws-port", type=int, default=6301)
    result.add_argument("--webui-port", type=int, default=6799)
    result.add_argument("--install-dependencies", action="store_true", help="Explicitly install Ubuntu 24.04 libraries, xvfb and xauth")
    result.add_argument("--start", action="store_true", help="Explicitly start only this bridge; may require phone QR login")
    result.add_argument("--enable-on-boot", action="store_true", help="Requires --start; enable only this unit")
    result.add_argument("--check", action="store_true", help="Validate inputs and print a redacted plan; no writes/downloads/services")
    return result


def validate(args):
    if not re.fullmatch(r"[1-9][0-9]{4,19}", args.account):
        fail("invalid_qq_account")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]{0,30}", args.user) or args.user == "root":
        fail("a_dedicated_non_root_service_user_is_required")
    for value in (args.home, args.prefix, args.state_dir):
        safe_path(value)
    if args.home in {args.prefix, args.state_dir} or args.prefix in {args.state_dir, "/opt/QQ"}:
        fail("home_code_and_state_require_distinct_owned_paths")
    code = PurePosixPath(args.prefix)
    if any(code.is_relative_to(PurePosixPath(value)) or PurePosixPath(value).is_relative_to(code)
           for value in (args.home, args.state_dir)):
        fail("installation_code_must_not_overlap_private_home_or_state")
    if not all(1024 <= port <= 65535 for port in (args.ws_port, args.webui_port)) or args.ws_port == args.webui_port:
        fail("invalid_or_colliding_loopback_ports")
    match = re.fullmatch(r"[0-9]+(?:\.[0-9]+)+-([0-9]+)", args.qq_version)
    if not match or int(match.group(1)) < 40768:
        fail("qq_version_requires_an_explicit_compatible_build_40768_or_newer")
    if not re.fullmatch(r"[0-9a-f]{64}", args.qq_sha256):
        fail("qq_sha256_must_be_64_lowercase_hex_characters")
    if args.enable_on_boot and not args.start:
        fail("enable_on_boot_requires_explicit_start")
    onebot = read_token(args.onebot_token_file)
    webui = read_token(args.webui_token_file)
    if onebot == webui:
        fail("onebot_and_webui_require_distinct_tokens")
    for local, url, checksum in ((args.qq_deb, args.qq_url, args.qq_sha256),
                                  (args.napcat_zip, args.napcat_url, NAPCAT_SHA256)):
        if local:
            if not Path(local).is_absolute():
                fail("archive_file_requires_absolute_path")
            verify_archive(local, checksum)
        else:
            secure_url(url)
    return configurations(args.account, onebot, webui, args.ws_port, args.webui_port)


def manifest(args):
    return {
        "schema_version": "napcat-linux-release-v1", "owner": OWNER,
        "napcat_version": NAPCAT_VERSION, "napcat_sha256": NAPCAT_SHA256,
        "napcat_source_url": args.napcat_url, "qq_version": args.qq_version,
        "qq_sha256": args.qq_sha256, "qq_source_url": args.qq_url,
        "architecture": "amd64", "user": args.user, "home": args.home,
        "prefix": args.prefix, "state_dir": args.state_dir, "account": args.account,
        "unit": UNIT_NAME, "ws_url": f"ws://127.0.0.1:{args.ws_port}",
        "webui_url": f"http://127.0.0.1:{args.webui_port}/webui",
    }


def install(args, configs):
    import fcntl
    import grp
    import pwd
    if platform.system() != "Linux" or platform.machine() not in {"x86_64", "amd64"} or os.geteuid() != 0:
        fail("installation_requires_root_on_amd64_linux")
    if sys.version_info < (3, 12):
        fail("python_3_12_or_newer_required")
    prefix, home, state = map(Path, (args.prefix, args.home, args.state_dir))
    expected = manifest(args)
    unit_path = SYSTEMD_UNIT_DIR / UNIT_NAME
    release = prefix / "releases" / f"napcat-{NAPCAT_VERSION}-qq-{args.qq_version}"
    expected_unit = service_text(args, release)
    lock_path = INSTALL_LOCK_PATH
    if lock_path.is_symlink():
        fail("installation_lock_must_not_be_a_symlink")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        marker = prefix / "release-manifest.json"
        if prefix.exists():
            if not marker.is_file() or marker.is_symlink() or json.loads(marker.read_text()) != expected:
                fail("existing_prefix_not_owned_or_configuration_changed")
        if unit_path.exists() and (unit_path.is_symlink() or unit_path.read_text() != expected_unit):
            fail("existing_service_not_owned_or_configuration_changed")
        try:
            user = pwd.getpwnam(args.user)
        except KeyError:
            user = None
        if user is not None:
            if user.pw_uid == 0 or user.pw_dir != args.home or user.pw_shell not in {"/usr/sbin/nologin", "/sbin/nologin", "/bin/false"}:
                fail("existing_service_user_identity_mismatch")
            try:
                if grp.getgrnam(args.user).gr_gid != user.pw_gid:
                    fail("existing_service_user_group_mismatch")
            except KeyError:
                fail("existing_service_user_group_missing")
        if home.exists() and (user is None or home.stat().st_uid != user.pw_uid):
            fail("existing_home_not_owned_by_service_user")
        if state.exists() and not marker.exists():
            fail("existing_state_requires_owned_installation_manifest")
        for directory in (state / "config", state / "cache", state / "logs", state / "plugins", home / ".config"):
            safe_path(str(directory))
        for name, required in configs.items():
            path = state / "config" / name
            if path.exists() and (path.is_symlink() or not contains_config(json.loads(path.read_text()), required)):
                fail("existing_private_configuration_changed_stop_and_review")
        active = subprocess.run(["systemctl", "is-active", "--quiet", UNIT_NAME]).returncode == 0
        if active and (not marker.is_file() or not unit_path.is_file()):
            fail("active_service_not_owned_by_this_installer")
        if not active:
            for port in (args.ws_port, args.webui_port):
                with socket.socket() as probe:
                    try:
                        probe.bind(("127.0.0.1", port))
                    except OSError:
                        fail("requested_loopback_port_is_already_in_use")
        if args.install_dependencies:
            os_release = Path("/etc/os-release").read_text()
            if 'ID=ubuntu' not in os_release or 'VERSION_ID="24.04"' not in os_release:
                fail("automatic_dependency_installation_requires_ubuntu_24_04")
            checked(["apt-get", "install", "-y", "xvfb", "xauth", "libnss3", "libgbm1", "libasound2t64",
                     "libgtk-3-0", "libxss1", "libxtst6", "libatk-bridge2.0-0", "libdrm2", "libxkbcommon0", "libcups2"])
        for name in ("dpkg-deb", "xvfb-run", "xauth", "systemctl", "ldd", "useradd"):
            if not shutil.which(name):
                fail(f"missing_dependency_{name}")
        if user is None:
            checked(["useradd", "--system", "--user-group", "--create-home", "--home-dir", args.home,
                     "--shell", "/usr/sbin/nologin", args.user])
            user = pwd.getpwnam(args.user)
        home.mkdir(parents=True, exist_ok=True)
        os.chmod(home, 0o700)
        os.chown(home, user.pw_uid, user.pw_gid)
        if not marker.exists():
            # Downloads/extraction happen in a new private staging directory. QQ's system installation is untouched.
            with tempfile.TemporaryDirectory(prefix="neuralcloud-napcat-") as temporary:
                temporary = Path(temporary)
                qq_deb = obtain(args.qq_deb, args.qq_url, args.qq_sha256, temporary / "QQ.deb")
                napcat_zip = obtain(args.napcat_zip, args.napcat_url, NAPCAT_SHA256, temporary / "NapCat.Shell.zip")
                if checked(["dpkg-deb", "-f", str(qq_deb), "Version"]) != args.qq_version:
                    fail("qq_deb_version_does_not_match_pinned_version")
                if checked(["dpkg-deb", "-f", str(qq_deb), "Architecture"]) != "amd64":
                    fail("qq_deb_architecture_mismatch")
                stage = temporary / "release"
                stage.mkdir(mode=0o755)
                os.chmod(stage, 0o755)
                extract_deb(qq_deb, stage)
                extract_zip(napcat_zip, stage / "napcat")
                binary = stage / "opt/QQ/qq"
                package = stage / "opt/QQ/resources/app/package.json"
                if not binary.is_file() or not package.is_file() or not (stage / "napcat/napcat.mjs").is_file():
                    fail("unexpected_qq_or_napcat_archive_layout")
                if "not found" in checked(["ldd", str(binary)]):
                    fail("qq_shared_libraries_missing_install_ubuntu_dependencies")
                metadata = json.loads(package.read_text())
                metadata["main"] = "./loadNapCat.cjs"
                package.write_text(json.dumps(metadata, indent=2) + "\n")
                loader_url = (release / "napcat/napcat.mjs").as_uri()
                package.with_name("loadNapCat.cjs").write_text(
                    f"(async () => {{ await import({json.dumps(loader_url)}); }})();\n")
                for item in stage.rglob("*"):
                    if not item.is_symlink():
                        mode = 0o755 if item.is_dir() or item.stat().st_mode & 0o111 else 0o644
                        os.chmod(item, mode)
                prefix.mkdir(parents=True, exist_ok=False)
                os.chmod(prefix, 0o755)
                release.parent.mkdir()
                os.chmod(release.parent, 0o755)
                shutil.move(str(stage), release)
                write_json(marker, expected)
        elif not (release / "opt/QQ/qq").is_file() or not (release / "napcat/napcat.mjs").is_file():
            fail("owned_release_is_incomplete_restore_the_recorded_release")
        for directory in (state, state / "config", state / "cache", state / "logs", state / "plugins", home / ".config"):
            directory.mkdir(parents=True, exist_ok=True)
            os.chmod(directory, 0o700)
            os.chown(directory, user.pw_uid, user.pw_gid)
        for name, value in configs.items():
            path = state / "config" / name
            if not path.exists():
                write_json(path, value, user.pw_uid, user.pw_gid)
        if not unit_path.exists():
            with unit_path.open("x") as stream:
                os.fchmod(stream.fileno(), 0o644)
                stream.write(expected_unit)
            checked(["systemctl", "daemon-reload"])
        if args.enable_on_boot:
            checked(["systemctl", "enable", UNIT_NAME])
        if args.start:
            checked(["systemctl", "start", UNIT_NAME])
        print(json.dumps({"result": "prepared", "unit": UNIT_NAME, "start_requested": args.start,
                          "login_verified": False, "qqdirect_started": False,
                          "release_manifest": str(marker)}, ensure_ascii=False))


def main(argv=None):
    args = parser().parse_args(argv)
    configs = validate(args)
    if args.check:
        print(json.dumps({"result": "configuration_valid", "plan": manifest(args),
                          "start_requested": args.start, "credentials": "validated_not_displayed"}, ensure_ascii=False))
        return 0
    install(args, configs)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as error:
        # OS/network/JSON errors can contain private paths or data. Only our reason codes are shown.
        reason = str(error) if type(error) is ValueError and re.fullmatch(r"[a-z0-9_]+", str(error)) else type(error).__name__
        print(json.dumps({"error": reason}), file=sys.stderr)
        raise SystemExit(1)
PYTHON
