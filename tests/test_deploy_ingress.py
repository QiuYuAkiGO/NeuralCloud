import json
import os
from pathlib import Path
import shutil
import subprocess
import types

import pytest

from harness.deploy.ingress import render_caddy


HASH = "$2b$12$" + "a" * 53


def test_proxy_requires_auth_for_entire_site_and_removes_credentials():
    text = render_caddy(public_origin="https://misty.example:12345", password_hash=HASH)
    assert "http://:8879 {" in text
    assert "http://127.0.0.1:8879" not in text
    assert "bind 127.0.0.1" in text
    assert "basic_auth {" in text and "basic_auth /" not in text
    assert "header_up -Authorization" in text
    assert "header_up X-Forwarded-Proto https" in text
    assert "not host misty.example" in text
    assert "admin off" in text
    assert 'Content-Security-Policy "frame-ancestors \'none\'"' in text
    assert "X-Frame-Options DENY" in text


def test_unconfigured_ingress_is_maintenance_without_backend_access():
    text = render_caddy(public_origin=None, password_hash=HASH)
    assert "503" in text and "reverse_proxy" not in text
    assert "http://:8879 {" in text
    assert "X-Frame-Options DENY" in text


@pytest.mark.parametrize("kwargs", [
    {"password_hash": "plaintext"}, {"username": "admin\nrespond 200"},
    {"public_origin": "http://misty.example"}, {"public_origin": "https://misty.example\nlocalhost"},
    {"public_origin": "https://misty.example/{evil}"}, {"proxy_port": 8790},
])
def test_rejects_insecure_or_injected_proxy_configuration(kwargs):
    args = {"public_origin": "https://misty.example", "password_hash": HASH}
    args.update(kwargs)
    with pytest.raises(ValueError):
        render_caddy(**args)


def test_caddy_adapter_keeps_loopback_listener_without_an_implicit_ip_host(tmp_path):
    binary = os.environ.get("CADDY_BIN") or shutil.which("caddy")
    if not binary:
        pytest.skip("Caddy not installed; set CADDY_BIN to validate the generated routing")
    config = tmp_path / "Caddyfile"
    config.write_text(render_caddy(public_origin="https://misty.example:12345", password_hash=HASH))
    result = subprocess.run([binary, "adapt", "--config", str(config), "--adapter", "caddyfile"],
                            capture_output=True, text=True, check=True)
    servers = json.loads(result.stdout)["apps"]["http"]["servers"]
    assert len(servers) == 1
    server = next(iter(servers.values()))
    assert server["listen"] == ["127.0.0.1:8879"]
    # The expected domain belongs only to the explicit negative host gate.
    # The original bug generated a second, outer Host=127.0.0.1 matcher.
    assert '"host": ["127.0.0.1"]' not in json.dumps(server)
    assert "misty.example" in json.dumps(server)


@pytest.fixture
def installer():
    script = Path(__file__).parents[1] / "tools/deploy/install-ingress.sh"
    source = script.read_text(encoding="utf-8").split("<<'PYTHON'\n", 1)[1].rsplit("\nPYTHON", 1)[0]
    module = types.ModuleType("ingress_installer_under_test")
    exec(compile(source, str(script), "exec"), module.__dict__)
    return module


def test_proxy_unit_cannot_read_application_secrets_or_write_system_config(installer):
    unit = installer.render_unit(Path("/usr/local/bin/caddy"))
    assert "User=neuralcloud-proxy\nGroup=neuralcloud-proxy" in unit
    assert "--config /etc/neuralcloud-proxy/Caddyfile" in unit
    assert "Environment=HOME=/var/lib/neuralcloud-proxy" in unit
    assert "StateDirectory=neuralcloud-proxy" in unit
    assert "StateDirectoryMode=0700" in unit
    assert "ProtectSystem=strict" in unit
    assert "ProtectHome=true" in unit
    assert "CapabilityBoundingSet=\n" in unit
    assert "/etc/neuralcloud/" not in unit
    assert "ExecReload=" not in unit  # The Caddy admin API is disabled.


@pytest.mark.parametrize("path", ["relative/file", "/etc/../tmp/Caddyfile", "/tmp/a b", "/tmp/a%20b"])
def test_installer_rejects_unsafe_systemd_paths(installer, path):
    with pytest.raises(installer.PreparationError, match="normalized_absolute"):
        installer.safe_path(path)


@pytest.mark.skipif(os.name != "posix", reason="Linux filesystem permissions")
def test_installer_rejects_readable_or_symlinked_hash(installer, tmp_path):
    secret = tmp_path / "private.bcrypt"
    secret.write_text(HASH)
    secret.chmod(0o644)
    with pytest.raises(installer.PreparationError, match="private_0600"):
        installer.regular_file(secret, private=True)
    secret.chmod(0o600)
    link = tmp_path / "link.bcrypt"
    link.symlink_to(secret)
    with pytest.raises(installer.PreparationError, match="symlink"):
        installer.regular_file(link, private=True)


@pytest.fixture
def staged_installer(installer, tmp_path, monkeypatch):
    if os.name != "posix":
        pytest.skip("Linux installation flow")
    release = tmp_path / "release"
    (release / ".venv/bin").mkdir(parents=True)
    python = release / ".venv/bin/python"
    python.write_text("unused mocked executable")
    python.chmod(0o755)
    config = tmp_path / "deploy.json"
    config.write_text(json.dumps({"schema_version": 1, "public_origin": "https://misty.example:12345",
                                 "paths": {"install_root": str(tmp_path), "current": str(release),
                                           "releases": str(tmp_path / "releases")}}))
    private_hash = tmp_path / "private.bcrypt"
    private_hash.write_text(HASH)
    private_hash.chmod(0o600)
    monkeypatch.setattr(installer, "ensure_host", lambda: None)
    monkeypatch.setattr(installer, "caddy_version", lambda path: (Path(path), "v2.11.4"))
    monkeypatch.setattr(installer, "proxy_account", lambda: (os.getuid(), os.getgid()))
    monkeypatch.setattr(installer, "CONFIG_ROOT", tmp_path / "proxy-config")
    monkeypatch.setattr(installer, "STATE_ROOT", tmp_path / "proxy-state")
    monkeypatch.setattr(installer, "UNIT_PATH", tmp_path / "neuralcloud-proxy.service")
    monkeypatch.setattr(installer.os, "chown", lambda *args: None)
    monkeypatch.setattr(installer.os, "fchown", lambda *args: None)
    calls = []

    def command(argv, **kwargs):
        calls.append(argv)
        if "harness.deploy.ingress" in argv:
            from harness.deploy.ingress import main
            main(argv[3:])
        return ""

    monkeypatch.setattr(installer, "checked", command)
    args = types.SimpleNamespace(config=str(config), hash_file=str(private_hash),
                                 caddy="/usr/local/bin/caddy", python=None, username="admin",
                                 proxy_port=8879, maintenance=False)
    return installer, args, calls


def test_prepare_validates_private_config_and_never_starts_or_enables(staged_installer, capsys):
    installer, args, calls = staged_installer
    installer.prepare(args)
    target = installer.CONFIG_ROOT / "Caddyfile"
    assert target.stat().st_mode & 0o777 == 0o640
    assert installer.CONFIG_ROOT.stat().st_mode & 0o777 == 0o750
    assert installer.STATE_ROOT.stat().st_mode & 0o777 == 0o700
    assert target.read_text() == render_caddy(public_origin="https://misty.example:12345", password_hash=HASH)
    assert len([call for call in calls if "validate" in call]) == 2
    assert any(call[:3] == ["runuser", "-u", "neuralcloud-proxy"] for call in calls)
    assert [call for call in calls if call[0] == "systemctl"] == [["systemctl", "daemon-reload"]]
    manifest = (installer.CONFIG_ROOT / "install-manifest.json").read_text()
    assert HASH not in capsys.readouterr().out + manifest + installer.UNIT_PATH.read_text()
    installer.prepare(args)  # Idempotent re-preparation keeps explicit lifecycle control.


def test_invalid_candidate_does_not_replace_previous_configuration(staged_installer, monkeypatch):
    installer, args, calls = staged_installer
    installer.prepare(args)
    target = installer.CONFIG_ROOT / "Caddyfile"
    previous = target.read_bytes()
    checked = installer.checked

    def rejected(argv, **kwargs):
        if "validate" in argv:
            installer.fail("invalid_candidate")
        return checked(argv, **kwargs)

    monkeypatch.setattr(installer, "checked", rejected)
    calls.clear()
    with pytest.raises(installer.PreparationError, match="invalid_candidate"):
        installer.prepare(args)
    assert target.read_bytes() == previous
    assert not any(call[0] == "systemctl" for call in calls)


def test_prepare_refuses_an_existing_unmanaged_unit(staged_installer):
    installer, args, calls = staged_installer
    installer.UNIT_PATH.write_text("[Service]\nExecStart=/existing/service\n")
    with pytest.raises(installer.PreparationError, match="unmanaged_systemd_unit"):
        installer.prepare(args)
    assert not calls
    assert not installer.CONFIG_ROOT.exists()
