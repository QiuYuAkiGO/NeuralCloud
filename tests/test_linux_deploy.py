"""Deployment boundaries: release integrity, secrets, ownership, and stop gates."""
from __future__ import annotations

import io
import json
import os
import shutil
import subprocess
import tarfile
from dataclasses import replace

import pytest

from harness.deploy.config import DeployError, DeploymentConfig, load_config
from harness.deploy.package import create_package, extract_package, verify_release
from harness.deploy.runner import read_token, service_environment
from harness.deploy import systemd
from harness.deploy import runner


def config_file(tmp_path, **overrides):
    path = tmp_path / "deploy.json"
    path.write_text(json.dumps({"schema_version": 1, **overrides}), encoding="utf-8")
    return path


def test_gui_health_uses_runtime_availability_without_rendering_memory(tmp_path, monkeypatch):
    cfg = load_config(config_file(tmp_path))
    requests = []

    def respond(request, *, timeout):
        requests.append(request.full_url)
        return io.BytesIO(b'{"available":true,"runtime":{"lifecycle":"RUNNING"}}')

    monkeypatch.setattr(systemd, "urlopen", respond)
    assert systemd._health(cfg, "gui")["runtime"]["lifecycle"] == "RUNNING"
    assert requests == [f"http://127.0.0.1:{cfg.ports['gui']}/api/ops/availability"]


def test_config_requires_explicit_isolated_development(tmp_path):
    cfg = load_config(config_file(tmp_path, mode="demo"))
    assert not cfg.development
    assert cfg.manifest_path.endswith("shared-misty-regulation-v1.json")
    cfg = load_config(config_file(tmp_path, mode="demo", development=True))
    assert cfg.manifest_path.endswith("development-demo.json")
    assert cfg.startup_timeout_seconds == 600 and cfg.stop_timeout_seconds == 180


@pytest.mark.parametrize("override", [
    {"service_user": "root"},
    {"ports": {"gui": 8766}},
    {"ports": {"gui": 8770}},
    {"enabled_components": ["gui"]},
    {"bindings": {"NC_CONTROL_TOKEN": "a" * 40}},
    {"bindings": {"NC_GUI_CONFIG": "%LOCALAPPDATA%/file"}},
    {"paths": {"current": "/etc/current"}},
    {"paths": {"state_root": "/opt/neuralcloud/releases/state"}},
    {"paths": {"state_root": "/var/lib/a space"}},
    {"public_origin": "http://example.com"},
    {"unknown": True},
])
def test_config_rejects_unsafe_values(tmp_path, override):
    with pytest.raises(DeployError):
        load_config(config_file(tmp_path, **override))


@pytest.mark.parametrize("value", [65536, "65536"])
def test_aux_context_binding_survives_runner_environment_sanitization(tmp_path, monkeypatch, value):
    from harness.config import Settings

    cfg = load_config(config_file(tmp_path, bindings={"NC_AUX_LLM_NUM_CTX": value}))
    assert cfg.bindings["NC_AUX_LLM_NUM_CTX"] == "65536"
    monkeypatch.setattr(runner, "read_token", lambda path: "a" * 48 if "control" in str(path) else "b" * 48)
    for service in cfg.services:
        assert service_environment(cfg, service, release=tmp_path)["NC_AUX_LLM_NUM_CTX"] == "65536"
    cfg = replace(cfg, paths={**cfg.paths, "current": str(tmp_path)})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(runner.os, "environ", {**os.environ, "NC_AUX_LLM_NUM_CTX": "8192",
                                              "NC_UNTRUSTED_SETTING": "ambient"})
    monkeypatch.setattr(runner.sys, "path", list(runner.sys.path))
    monkeypatch.setattr(runner.sys, "argv", list(runner.sys.argv))
    observed = []

    def capture(module, *, run_name):
        observed.append((module, run_name, Settings(_env_file=None).aux_llm_num_ctx))
        assert "NC_UNTRUSTED_SETTING" not in os.environ

    monkeypatch.setattr(runner.runpy, "run_module", capture)
    runner.run_service(cfg, "cognitive")
    assert observed == [("harness.cognitive.service", "__main__", 65536)]


@pytest.mark.parametrize("value", [True, False, None, "", "64k", "65536.0", 65536.0, 0, -1, 1023, "1023"])
def test_aux_context_binding_rejects_invalid_or_too_small_values(tmp_path, value):
    with pytest.raises(DeployError, match="invalid NC_AUX_LLM_NUM_CTX"):
        load_config(config_file(tmp_path, bindings={"NC_AUX_LLM_NUM_CTX": value}))


def test_units_have_shared_owner_and_bounded_shutdown(tmp_path):
    cfg = load_config(config_file(tmp_path))
    units = systemd.render_units(cfg)
    for name, content in units.items():
        if name.endswith(".service"):
            assert "User=neuralcloud" in content
            assert "TimeoutStopSec=180" in content
            assert "KillMode=control-group" in content
            assert "ExecStartPost=" in content
            assert 'WorkingDirectory=/opt/neuralcloud/current' in content
            assert '\\opt' not in content
            assert "--workers" not in content
            assert "NC_SERVICE_TOKEN=" not in content and "NC_CONTROL_TOKEN=" not in content
    assert "Requires=neuralcloud-cognitive.service neuralcloud-homeostasis.service" in units["neuralcloud-runtime.service"]
    assert "neuralcloud-runtime.service" in units["neuralcloud-gui.service"]


def test_rendered_units_pass_real_systemd_analyze(tmp_path):
    """Use the real Linux parser, including from Windows via the available WSL."""
    if os.name == "nt":
        if shutil.which("wsl.exe") is None:
            pytest.skip("WSL systemd-analyze is unavailable")
        command = ["wsl.exe", "-d", "Ubuntu-24.04", "--exec"]
    else:
        if shutil.which("systemd-analyze") is None:
            pytest.skip("systemd-analyze is unavailable")
        command = []
    probe = subprocess.run([*command, "systemd-analyze", "--version"], capture_output=True, timeout=30)
    if probe.returncode:
        pytest.skip("systemd-analyze is unavailable in the selected Linux environment")
    cfg = load_config(config_file(tmp_path))
    names = []
    for name, content in systemd.render_units(cfg).items():
        # Verification needs an installed executable; only substitute its path.
        # Every systemd directive, dependency, and argument stays as rendered.
        content = content.replace("/opt/neuralcloud/current/.venv/bin/python", "/usr/bin/python3")
        path = tmp_path / name
        path.write_text(content, encoding="utf-8")
        if command:
            converted = subprocess.run([*command, "wslpath", "-a", str(path)], capture_output=True, timeout=15, check=True)
            names.append(converted.stdout.decode("utf-8").strip())
        else:
            names.append(str(path))
    checked = subprocess.run([*command, "systemd-analyze", "verify", "--man=no", *names], capture_output=True, timeout=30)
    assert checked.returncode == 0, checked.stderr.decode("utf-8", errors="replace")


def test_tokens_are_private_distinct_and_injected_by_role(tmp_path, monkeypatch):
    a, b = tmp_path / "control.token", tmp_path / "service.token"
    a.write_text("a" * 48)
    b.write_text("b" * 48)
    a.chmod(0o600)
    b.chmod(0o600)
    assert read_token(a) == "a" * 48
    cfg = DeploymentConfig(config_file=tmp_path / "config.json", bindings={
        "NC_CONTROL_TOKEN_FILE": str(a), "NC_SERVICE_TOKEN_FILE": str(b),
        "NC_BOARD_CONFIG": str(tmp_path / "board.json")}, development=True, mode="demo")
    env = service_environment(cfg, "runtime", release=tmp_path)
    assert env["NC_CONTROL_TOKEN"] == "a" * 48 and env["NC_SERVICE_TOKEN"] == "b" * 48
    assert env["NC_CONTROL_MODE"] == "development_debug" and env["NC_ISOLATED_DEVELOPMENT"] == "1"
    assert env["NC_RUNTIME_HOST"] == "127.0.0.1"
    gui = service_environment(cfg, "gui", release=tmp_path)
    assert "NC_SERVICE_TOKEN" not in gui
    assert "NC_BOARD_CONFIG" not in gui
    assert service_environment(cfg, "cognitive", release=tmp_path)["NC_BOARD_CONFIG"] == env["NC_BOARD_CONFIG"]
    assert "NC_BOARD_CONFIG" not in service_environment(cfg, "homeostasis", release=tmp_path)
    b.write_text("a" * 48)
    with pytest.raises(DeployError, match="differ"):
        service_environment(cfg, "runtime", release=tmp_path)


def test_token_placeholder_is_rejected_without_echo(tmp_path):
    token = tmp_path / "token"
    token.write_text("development-do-not-use-a-real-secret")
    token.chmod(0o600)
    with pytest.raises(DeployError, match="invalid private deployment token"):
        read_token(token)


def source_tree(tmp_path):
    root = tmp_path / "source"
    root.mkdir()
    for name in ("pyproject.toml", "uv.lock", "README.md"):
        (root / name).write_text("sample\n")
    for name in ("harness/module.py", "gui/static/index.html", "presets/builtin/misty.json", "data/secret.json", ".env"):
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("asset")
    return root


def test_package_preserves_resources_excludes_state_and_detects_tampering(tmp_path):
    source = source_tree(tmp_path)
    archive = tmp_path / "release.tar.gz"
    result = create_package(source, archive, "r1")
    assert result["files"] == 6
    release = extract_package(archive, tmp_path / "releases")
    assert (release / "gui/static/index.html").exists()
    assert (release / "presets/builtin/misty.json").exists()
    assert not (release / "data").exists() and not (release / ".env").exists()
    assert verify_release(release)["verified_files"] == 6
    (release / "harness/module.py").write_text("tampered")
    with pytest.raises(DeployError, match="hash mismatch"):
        verify_release(release)
    with pytest.raises(DeployError, match="already exists"):
        extract_package(archive, tmp_path / "releases")


def test_release_verification_rejects_extra_importable_source(tmp_path):
    source = source_tree(tmp_path)
    archive = tmp_path / "release.tar.gz"
    create_package(source, archive, "r1")
    release = extract_package(archive, tmp_path / "releases")
    (release / "harness/unexpected.py").write_text("raise RuntimeError('untracked source')")
    with pytest.raises(DeployError, match="unexpected source files"):
        verify_release(release)


def test_archive_refuses_linked_parent_before_writing(tmp_path):
    source = source_tree(tmp_path)
    archive = tmp_path / "release.tar.gz"
    create_package(source, archive, "r1")
    actual = tmp_path / "actual"
    actual.mkdir()
    link = tmp_path / "link"
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    with pytest.raises(DeployError, match="links or reparse"):
        extract_package(archive, link / "releases")
    assert list(actual.iterdir()) == []


@pytest.mark.parametrize("name,kind", [("../escape", "file"), ("/escape", "file"), ("safe", "symlink"), ("safe", "hardlink")])
def test_archive_rejects_paths_and_links_before_writing(tmp_path, name, kind):
    archive = tmp_path / "evil.tar"
    with tarfile.open(archive, "w") as handle:
        info = tarfile.TarInfo(name)
        if kind != "file":
            info.type = tarfile.SYMTYPE if kind == "symlink" else tarfile.LNKTYPE
            info.linkname = "/etc/passwd"
        else:
            info.size = 1
        handle.addfile(info, io.BytesIO(b"x") if kind == "file" else None)
    with pytest.raises(DeployError):
        extract_package(archive, tmp_path / "releases")
    assert not (tmp_path / "releases").exists()


def test_no_switch_when_writer_failed_to_stop(tmp_path, monkeypatch):
    release = tmp_path / "releases" / "new"
    python = release / ".venv/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("")
    cfg = DeploymentConfig(config_file=tmp_path / "config", paths={"current": str(tmp_path / "current"), "releases": str(release.parent)})
    monkeypatch.setattr(systemd, "ensure_linux", lambda: None)
    monkeypatch.setattr(systemd, "verify_release", lambda _: {})
    monkeypatch.setattr(systemd, "stop_services", lambda _: (_ for _ in ()).throw(DeployError("writer still active")))
    switches = []
    monkeypatch.setattr(systemd, "_switch_release", lambda *args: switches.append(args))
    with pytest.raises(DeployError, match="writer still active"):
        systemd.activate(cfg, "new")
    assert not switches


def test_stop_timeout_blocks_following_services_and_activation(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")
    stopped = []
    monkeypatch.setattr(systemd, "_systemctl", lambda *args, **kwargs: stopped.append(args))
    monkeypatch.setattr(systemd, "_managed_unit", lambda _: True)
    calls = 0

    def state(_):
        nonlocal calls
        calls += 1
        return {"ActiveState": "active", "MainPID": "1"} if calls == 1 else {"ActiveState": "failed", "MainPID": "0", "Result": "timeout"}

    monkeypatch.setattr(systemd, "unit_status", state)
    with pytest.raises(DeployError, match="did not stop cleanly"):
        systemd.stop_services(cfg)
    assert len(stopped) == 1


def test_removed_optional_writer_is_stopped(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")
    stopped = []
    monkeypatch.setattr(systemd, "_managed_unit", lambda _: True)
    monkeypatch.setattr(systemd, "unit_status", lambda name: {"LoadState": "loaded", "ActiveState": "inactive", "MainPID": "0", "Result": "success"})
    monkeypatch.setattr(systemd, "_systemctl", lambda *args, **kwargs: stopped.append(args))
    systemd.stop_services(cfg)
    assert stopped[0] == ("stop", "neuralcloud-qq-direct.service")
    assert stopped[-1] == ("stop", "neuralcloud-board.service")


def test_preflight_refuses_an_unmanaged_listener(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")

    class Listener:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def settimeout(self, _):
            pass

        def connect_ex(self, address):
            return 0 if address[1] == 8790 else 1

    monkeypatch.setattr(systemd.socket, "socket", Listener)
    monkeypatch.setattr(systemd.shutil, "which", lambda _: "/usr/bin/tool")
    monkeypatch.setattr(systemd, "unit_status", lambda _: {"ActiveState": "active", "MainPID": "2000"})
    monkeypatch.setattr(systemd, "_managed_unit", lambda _: True)
    monkeypatch.setattr(systemd, "_pid_listens_on_port", lambda *_: False)
    result = systemd.preflight(cfg)
    assert result["unmanaged_port_conflicts"] == ["gui"]
    assert result["ready"] is False


def test_stop_refuses_an_unmanaged_same_named_unit(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")
    monkeypatch.setattr(systemd, "unit_status", lambda _: {"LoadState": "loaded", "ActiveState": "active", "MainPID": "2"})
    monkeypatch.setattr(systemd, "_managed_unit", lambda _: False)
    with pytest.raises(DeployError, match="unmanaged unit"):
        systemd.stop_services(cfg)


def test_start_gates_each_domain_before_runtime_and_qq(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config", enabled_components=("cognitive", "homeostasis", "runtime", "gui", "qq_direct"))
    events = []
    monkeypatch.setattr(systemd, "_systemctl", lambda *args, **kwargs: events.append(args))
    monkeypatch.setattr(systemd, "wait_healthy", lambda cfg, name: events.append(("healthy", name)))
    systemd.start_services(cfg)
    assert events == [(action, value) for name in cfg.services
                      for action, value in [("start", systemd.unit_name(name)), ("healthy", name)]]


def test_runtime_health_uses_actual_schema(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")
    monkeypatch.setattr(systemd, "unit_status", lambda _: {"ActiveState": "active"})
    monkeypatch.setattr(systemd, "_health", lambda *_: {"lifecycle": "RUNNING", "health": {
        "journal_writable": True, "cognitive_healthy": True, "homeostasis_healthy": True}})
    systemd.wait_healthy(cfg, "runtime")


def test_pause_acknowledgement_is_polled_until_async_pause_completes(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")
    states = iter(("RUNNING", "RUNNING", "PAUSING", "PAUSED"))
    observed = []

    def health(*args, **kwargs):
        state = next(states)
        observed.append(state)
        return {"lifecycle": state}

    class Acknowledged:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return b'{"accepted":true}'

    posts = []
    monkeypatch.setattr(systemd, "_health", health)
    monkeypatch.setattr(systemd, "read_token", lambda _: "a" * 48)
    monkeypatch.setattr(systemd, "urlopen", lambda request, **kwargs: posts.append(request.method) or Acknowledged())
    monkeypatch.setattr(systemd.time, "sleep", lambda _: None)
    systemd._pause_runtime(cfg)
    assert posts == ["POST"] and observed == ["RUNNING", "RUNNING", "PAUSING", "PAUSED"]


def test_pause_polling_fault_blocks_stop(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "config")
    states = iter(("PAUSING", "FAULTED"))
    monkeypatch.setattr(systemd, "_health", lambda *args, **kwargs: {"lifecycle": next(states)})
    monkeypatch.setattr(systemd, "read_token", lambda _: "a" * 48)
    with pytest.raises(DeployError, match="pause failed"):
        systemd._pause_runtime(cfg)


def test_rollback_preserves_state_and_only_activates_code(tmp_path, monkeypatch):
    (tmp_path / "deployment.json").write_text(json.dumps({"previous_release": "old"}))
    data = tmp_path / "new-messages.sqlite3"
    data.write_bytes(b"new production data")
    cfg = DeploymentConfig(config_file=tmp_path / "config", paths={"state_root": str(tmp_path)})
    releases = []
    monkeypatch.setattr(systemd, "activate", lambda cfg, release, **kwargs: releases.append(release) or {})
    assert systemd.rollback(cfg, None)["state_preserved"] is True
    assert releases == ["old"] and data.read_bytes() == b"new production data"
