"""Exercise the self-contained installer without downloading or starting a QQ client."""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import stat
import sys
import tarfile
import types
import zipfile

import pytest


@pytest.fixture
def installer():
    script = Path(__file__).parents[1] / "tools/deploy/install-napcat.sh"
    source = script.read_text(encoding="utf-8").split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]
    module = types.ModuleType("napcat_installer_under_test")
    exec(compile(source, str(script), "exec"), module.__dict__)
    return module


@pytest.fixture
def inputs(tmp_path):
    onebot, webui = tmp_path / "onebot.token", tmp_path / "webui.token"
    onebot.write_text("onebot-private-test-value-" + "a" * 32)
    webui.write_text("webui-private-test-value-" + "b" * 32)
    onebot.chmod(0o600)
    webui.chmod(0o600)
    return ["--account", "2015188508", "--onebot-token-file", str(onebot),
            "--webui-token-file", str(webui)]


def test_check_is_read_only_and_does_not_print_secrets(installer, inputs, tmp_path, monkeypatch, capsys):
    before = set(tmp_path.rglob("*"))

    def unexpected(*args, **kwargs):
        pytest.fail("check must not install, download or operate services")

    monkeypatch.setattr(installer, "install", unexpected)
    monkeypatch.setattr(installer, "obtain", unexpected)
    assert installer.main(inputs + ["--check"]) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["start_requested"] is False
    assert output["plan"]["napcat_sha256"] == installer.NAPCAT_SHA256
    assert output["plan"]["qq_sha256"] == installer.QQ_SHA256
    assert "private-test-value" not in json.dumps(output)
    assert set(tmp_path.rglob("*")) == before


@pytest.mark.parametrize("arguments,reason", [
    (["--enable-on-boot"], "enable_on_boot_requires_explicit_start"),
    (["--user", "root"], "a_dedicated_non_root_service_user_is_required"),
    (["--prefix", "/opt/QQ"], "home_code_and_state_require_distinct_owned_paths"),
    (["--prefix", "/var/lib/neuralcloud-qq/code"], "installation_code_must_not_overlap_private_home_or_state"),
    (["--home", "/tmp/../root"], "use_a_normalized_absolute_path_below_a_private_directory"),
    (["--ws-port", "6799"], "invalid_or_colliding_loopback_ports"),
    (["--qq-version", "3.2.1-12345"], "qq_version_requires_an_explicit_compatible_build_40768_or_newer"),
    (["--napcat-url", "http://example.org/NapCat.zip"], "download_url_requires_https_without_credentials"),
])
def test_reject_unsafe_configuration_before_mutation(installer, inputs, arguments, reason):
    with pytest.raises(ValueError, match=reason):
        installer.validate(installer.parser().parse_args(inputs + arguments))


def test_reject_reused_or_short_tokens(installer, inputs):
    onebot = Path(inputs[3])
    webui = Path(inputs[5])
    webui.write_text(onebot.read_text())
    with pytest.raises(ValueError, match="onebot_and_webui_require_distinct_tokens"):
        installer.validate(installer.parser().parse_args(inputs))
    webui.write_text("short")
    with pytest.raises(ValueError, match="token_must_be_at_least_32"):
        installer.validate(installer.parser().parse_args(inputs))


@pytest.mark.skipif(os.name != "posix", reason="POSIX permission semantics")
def test_reject_readable_token_and_symlink(installer, inputs, tmp_path):
    token = Path(inputs[3])
    token.chmod(0o644)
    with pytest.raises(ValueError, match="private_permissions"):
        installer.read_token(str(token))
    token.chmod(0o600)
    link = tmp_path / "alias.token"
    link.symlink_to(token)
    with pytest.raises(ValueError, match="absolute_regular_private_file"):
        installer.read_token(str(link))


def test_corrupt_offline_archive_is_rejected(installer, inputs, tmp_path):
    source = tmp_path / "NapCat.Shell.zip"
    source.write_bytes(b"untrusted or truncated")
    with pytest.raises(ValueError, match="archive_sha256_mismatch"):
        installer.validate(installer.parser().parse_args(inputs + ["--napcat-zip", str(source)]))


@pytest.mark.parametrize("name,is_symlink", [("../escape", False), ("/etc/test", False),
                                            ("C:/escape", False), ("..\\escape", False), ("alias", True)])
def test_zip_traversal_rejected_before_any_extract(installer, tmp_path, name, is_symlink):
    source, destination = tmp_path / "archive.zip", tmp_path / "extract"
    with zipfile.ZipFile(source, "w") as archive:
        archive.writestr("good.txt", "must not be extracted before validation")
        info = zipfile.ZipInfo(name)
        if is_symlink:
            info.external_attr = (stat.S_IFLNK | 0o777) << 16
        archive.writestr(info, "bad")
    with pytest.raises(ValueError, match="unsafe_napcat_archive_entry"):
        installer.extract_zip(source, destination)
    assert not destination.exists()


def test_only_target_account_has_onebot_and_defaults_cannot_relax_binding(installer):
    config = installer.configurations("123456", "a" * 40, "b" * 40, 6301, 6799)
    active = config["onebot11_123456.json"]
    assert config["onebot11.json"]["network"]["websocketServers"] == []
    server = active["network"]["websocketServers"][0]
    assert (server["host"], server["port"], server["messagePostFormat"]) == ("127.0.0.1", 6301, "array")
    assert config["webui.json"]["host"] == "127.0.0.1"
    assert active["enableLocalFile2Url"] is True
    actual = json.loads(json.dumps(active))
    actual["added_by_napcat"] = "default"
    assert installer.contains_config(actual, active)
    actual["network"]["websocketServers"][0]["host"] = "0.0.0.0"
    assert not installer.contains_config(actual, active)


def test_qq_tar_ignores_system_launcher_and_rejects_escaping_application_links(installer, tmp_path):
    launcher = tarfile.TarInfo("./usr/bin/qq")
    launcher.type = tarfile.SYMTYPE
    launcher.linkname = "/opt/QQ/qq"
    assert installer.qq_tar_filter(launcher, str(tmp_path)) is None
    app = tarfile.TarInfo("./opt/QQ/resources/app/package.json")
    assert installer.qq_tar_filter(app, str(tmp_path)).name == app.name
    unsafe = tarfile.TarInfo("./opt/QQ/unsafe")
    unsafe.type = tarfile.SYMTYPE
    unsafe.linkname = "/etc/passwd"
    with pytest.raises(tarfile.FilterError):
        installer.qq_tar_filter(unsafe, str(tmp_path))


def test_unit_uses_isolated_binary_home_and_no_credential_environment(installer, inputs):
    args = installer.parser().parse_args(inputs)
    unit = installer.service_text(args, "/opt/neuralcloud-napcat/releases/example")
    assert "User=neuralcloud-qq\n" in unit
    assert "Environment=HOME=/var/lib/neuralcloud-qq\n" in unit
    assert "Environment=NAPCAT_WORKDIR=/var/lib/neuralcloud-qq/napcat\n" in unit
    assert "ExecStart=/usr/bin/xvfb-run -a /opt/neuralcloud-napcat/releases/example/opt/QQ/qq" in unit
    assert "Restart=on-failure\n" in unit
    assert "765681548" not in unit
    assert "TOKEN=" not in unit and "PASSWORD=" not in unit


@pytest.mark.skipif(os.name != "posix" or sys.version_info < (3, 12), reason="Linux installer filesystem integration")
def test_prepare_is_repeatable_and_never_starts_or_touches_existing_qq(installer, inputs, tmp_path, monkeypatch):
    import grp
    import pwd

    home = tmp_path / "private-home"
    home.mkdir()
    prefix = tmp_path / "code"
    units = tmp_path / "systemd"
    units.mkdir()
    old_unit = units / "nc_765681548.service"
    old_unit.write_text("Existing user-owned QQ service\n")
    qq_deb = tmp_path / "QQ.deb"
    qq_deb.write_bytes(b"fake fixture; package extraction is replaced")
    napcat = tmp_path / "NapCat.zip"
    with zipfile.ZipFile(napcat, "w") as archive:
        archive.writestr("napcat.mjs", "// fixture")
    monkeypatch.setattr(installer, "NAPCAT_SHA256", hashlib.sha256(napcat.read_bytes()).hexdigest())
    monkeypatch.setattr(installer, "SYSTEMD_UNIT_DIR", units)
    monkeypatch.setattr(installer, "INSTALL_LOCK_PATH", tmp_path / "install.lock")
    monkeypatch.setattr(installer.platform, "system", lambda: "Linux")
    monkeypatch.setattr(installer.platform, "machine", lambda: "x86_64")
    monkeypatch.setattr(installer.os, "geteuid", lambda: 0)
    monkeypatch.setattr(installer.os, "chown", lambda *args: None)
    user = types.SimpleNamespace(pw_uid=os.getuid(), pw_gid=os.getgid(), pw_dir=str(home), pw_shell="/usr/sbin/nologin")
    # The fixture may run as root; keep installer identity checks realistic while writes stay in tmp_path.
    if user.pw_uid == 0:
        user.pw_uid = 12345
        home_stat = home.stat()
        original_stat = installer.Path.stat

        def fake_stat(path, *args, **kwargs):
            if path == home:
                values = list(home_stat)
                values[4] = user.pw_uid
                return os.stat_result(values)
            return original_stat(path, *args, **kwargs)

        monkeypatch.setattr(installer.Path, "stat", fake_stat)
    monkeypatch.setattr(pwd, "getpwnam", lambda name: user)
    monkeypatch.setattr(grp, "getgrnam", lambda name: types.SimpleNamespace(gr_gid=user.pw_gid))
    monkeypatch.setattr(installer.shutil, "which", lambda name: f"/usr/bin/{name}")
    commands = []

    def run(command, **kwargs):
        commands.append(command)
        if command[:2] == ["systemctl", "is-active"]:
            return types.SimpleNamespace(returncode=3, stdout="", stderr="")
        output = ""
        if command[0] == "dpkg-deb":
            output = installer.QQ_VERSION if command[-1] == "Version" else "amd64"
        return types.SimpleNamespace(returncode=0, stdout=output, stderr="")

    def extract(source, target):
        app = target / "opt/QQ/resources/app"
        app.mkdir(parents=True)
        (app / "package.json").write_text('{"main":"old.js"}')
        binary = target / "opt/QQ/qq"
        binary.write_text("#!/bin/sh\nexit 0\n")
        binary.chmod(0o755)

    monkeypatch.setattr(installer.subprocess, "run", run)
    monkeypatch.setattr(installer, "extract_deb", extract)
    args = installer.parser().parse_args(inputs + ["--home", str(home), "--prefix", str(prefix),
        "--state-dir", str(home / "napcat"), "--qq-deb", str(qq_deb),
        "--qq-sha256", hashlib.sha256(qq_deb.read_bytes()).hexdigest(), "--napcat-zip", str(napcat),
        "--ws-port", "65031", "--webui-port", "65032"])
    configs = installer.validate(args)
    installer.install(args, configs)
    first_manifest = (prefix / "release-manifest.json").read_bytes()
    installer.install(args, configs)
    assert (prefix / "release-manifest.json").read_bytes() == first_manifest
    assert old_unit.read_text() == "Existing user-owned QQ service\n"
    assert not any(command[:2] in (["systemctl", "start"], ["systemctl", "enable"],
                                   ["systemctl", "stop"], ["systemctl", "restart"]) for command in commands)
    assert not any("765681548" in " ".join(command) for command in commands)
    unit = units / installer.UNIT_NAME
    unit.write_text("unexpected existing service")
    with pytest.raises(ValueError, match="existing_service_not_owned"):
        installer.install(args, configs)
