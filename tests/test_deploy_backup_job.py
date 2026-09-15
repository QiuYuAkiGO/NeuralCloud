from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.deploy import backup_job as job
from harness.deploy import snapshot
from harness.deploy.config import CORE_SERVICES, DeployError, DeploymentConfig


@pytest.fixture
def environment(tmp_path, monkeypatch):
    paths = {key: str(tmp_path / key) for key in ("state_root", "config_root", "backup_root", "current", "log_root")}
    for path in paths.values():
        Path(path).mkdir()
    (Path(paths["state_root"]) / "saved.json").write_text('{"retained":true}')
    (Path(paths["config_root"]) / "private.json").write_text('{"setting":1}')
    cfg = DeploymentConfig(config_file=tmp_path / "deploy.json", paths=paths,
                           enabled_components=(*CORE_SERVICES, "board", "qq_direct"))
    states = {name: {"LoadState": "loaded", "ActiveState": "active" if name in CORE_SERVICES else "inactive",
                     "MainPID": "15" if name in CORE_SERVICES else "0", "Result": "success"}
              for name in (*job.SERVICE_ORDER, job.DEFAULT_PROXY)}
    events = []
    lifecycle = {"value": "RUNNING"}

    def health(cfg, name):
        if name == "runtime":
            return {"lifecycle": lifecycle["value"], "health": {"journal_writable": True,
                    "cognitive_healthy": True, "homeostasis_healthy": True}}
        if name == "gui":
            return {"runtime": {"lifecycle": lifecycle["value"]}}
        return {"healthy": True}

    def control(action, unit, **kwargs):
        events.append((action, unit))
        name = next((key for key in states if job._unit(key) == unit), None)
        assert name is not None
        if action == "start":
            states[name].update(ActiveState="active", MainPID="15")
            if name == "runtime":
                lifecycle["value"] = "RUNNING"
        elif action == "stop":
            states[name].update(ActiveState="inactive", MainPID="0")
        else:
            raise AssertionError(action)

    monkeypatch.setattr(job.systemd, "unit_status", lambda name: states[name].copy())
    monkeypatch.setattr(job.systemd, "_managed_unit", lambda _: True)
    monkeypatch.setattr(job, "_proxy_state", lambda name: states[name].copy())
    monkeypatch.setattr(job.systemd, "_health", health)
    monkeypatch.setattr(job.systemd, "_pause_runtime", lambda _: lifecycle.update(value="PAUSED"))
    monkeypatch.setattr(job.systemd, "_systemctl", control)
    monkeypatch.setattr(job.systemd, "wait_healthy", lambda cfg, name: None)
    monkeypatch.setattr(job.snapshot, "_deployment_sources", lambda cfg: [
        snapshot.SourceSpec("state", cfg.path("state_root"), "state"),
        snapshot.SourceSpec("config", cfg.path("config_root"), "config")])
    return cfg, states, events, lifecycle


def test_paused_runtime_is_skipped_without_any_service_wake(environment):
    cfg, _, events, lifecycle = environment
    lifecycle["value"] = "PAUSED"
    result = job.run_backup(cfg)
    assert result == {"status": "skipped", "reason": "runtime_not_running", "services_woken": False}
    assert not events
    assert not (cfg.path("backup_root") / "scheduled-last-run.json").exists()


def test_stopped_core_is_not_started_by_backup(environment):
    cfg, states, events, _ = environment
    states["cognitive"]["ActiveState"] = "inactive"
    assert job.run_backup(cfg)["status"] == "skipped"
    assert events == []


def test_active_desktop_status_blocks_unattended_backup(environment, tmp_path):
    cfg, _, events, _ = environment
    status = tmp_path / "desktop.status.json"
    status.write_text('{"running":true}')
    direct = tmp_path / "direct.json"
    direct.write_text(json.dumps({"desktop_status_file": str(status)}))
    cfg = replace(cfg, bindings={"NC_QQ_DIRECT_CONFIG": str(direct)})
    with pytest.raises(DeployError, match="running writer"):
        job.run_backup(cfg)
    assert events == []


def test_originally_stopped_qq_and_board_are_not_started(environment):
    cfg, _, events, _ = environment
    result = job.run_backup(cfg, now=datetime(2026, 9, 15, 3, 30, tzinfo=UTC))
    assert result["status"] == "completed" and result["replication"] == "local-only"
    starts = [unit for action, unit in events if action == "start"]
    assert starts == [job._unit(name) for name in CORE_SERVICES]
    assert all("qq-direct" not in unit and "board" not in unit for _, unit in events)
    stored = cfg.path("backup_root") / result["snapshot"]
    assert snapshot.verify_snapshot(stored)["ok"]
    assert (stored / "data/state/saved.json").exists()


def test_proxy_is_stopped_first_and_only_prior_running_scope_resumes(environment):
    cfg, states, events, _ = environment
    for name in ("qq_direct", "board", job.DEFAULT_PROXY):
        states[name].update(ActiveState="active", MainPID="15")
    result = job.run_backup(cfg)
    assert result["status"] == "completed"
    assert events[0] == ("stop", job.DEFAULT_PROXY)
    assert events[-1] == ("start", job.DEFAULT_PROXY)
    starts = [unit for action, unit in events if action == "start"]
    assert starts == [job._unit(name) for name in job.SERVICE_ORDER] + [job.DEFAULT_PROXY]


def test_stop_timeout_blocks_export_and_all_restart(environment, monkeypatch):
    cfg, states, events, _ = environment
    original = job.systemd._systemctl
    exported = []

    def fail_first_stop(action, unit, **kwargs):
        if not events:
            events.append((action, unit))
            raise subprocess.TimeoutExpired(["systemctl", action, unit], 180)
        return original(action, unit, **kwargs)

    monkeypatch.setattr(job.systemd, "_systemctl", fail_first_stop)
    monkeypatch.setattr(job.snapshot, "export_snapshot", lambda *a, **k: exported.append(True))
    with pytest.raises(DeployError, match="automatic resume blocked"):
        job.run_backup(cfg)
    assert exported == [] and not any(action == "start" for action, _ in events)
    assert all(states[name]["ActiveState"] == "inactive" for name in CORE_SERVICES)
    report = json.loads((cfg.path("backup_root") / "scheduled-last-run.json").read_text())
    assert report["status"] == "failed" and report["restart_attempted"] is False


def test_export_failure_keeps_services_stopped(environment, monkeypatch):
    cfg, states, events, _ = environment
    monkeypatch.setattr(job.snapshot, "export_snapshot", lambda *a, **k: (_ for _ in ()).throw(snapshot.SnapshotError("changed source")))
    with pytest.raises(DeployError):
        job.run_backup(cfg)
    assert not any(action == "start" for action, _ in events)
    assert all(states[name]["ActiveState"] == "inactive" for name in CORE_SERVICES)


def test_failed_restart_is_cleaned_up_without_snapshot_restore(environment, monkeypatch):
    cfg, states, _, _ = environment
    monkeypatch.setattr(job.systemd, "wait_healthy", lambda cfg, name: (_ for _ in ()).throw(DeployError("failed restart")))
    with pytest.raises(DeployError):
        job.run_backup(cfg)
    assert all(states[name]["ActiveState"] == "inactive" for name in CORE_SERVICES)
    report = json.loads((cfg.path("backup_root") / "scheduled-last-run.json").read_text())
    assert report["restart_attempted"] is True
    assert snapshot.verify_snapshot(cfg.path("backup_root") / report["snapshot"])["ok"]


def _make_snapshot(source, root, moment, *, name=None):
    name = name or "scheduled-" + moment.strftime("%Y%m%dT%H%M%S%fZ")
    target = root / name
    snapshot.export_snapshot([snapshot.SourceSpec("state", source, "state")], target,
                             stopped_ack=True, desktop_status="not-configured")
    return target


def test_retention_keeps_seven_days_and_four_weekly_latest_without_deleting_manual(tmp_path):
    source, root = tmp_path / "source", tmp_path / "backups"
    source.mkdir()
    root.mkdir()
    (source / "saved.txt").write_text("retained")
    now = datetime(2026, 9, 15, 3, 30, tzinfo=UTC)
    paths = {_offset: _make_snapshot(source, root, now - timedelta(days=_offset)) for _offset in range(35)}
    duplicate = _make_snapshot(source, root, now - timedelta(hours=1))
    manual = _make_snapshot(source, root, now, name="manual-operator-snapshot")
    invalid = root / "scheduled-20260101T000000000000Z"
    invalid.mkdir()
    (invalid / "not-a-snapshot.txt").write_text("leave intact")
    result = job.prune_scheduled(root, now=now)
    assert all(paths[day].exists() for day in range(7))
    assert paths[9].exists()  # Sunday of the third retained week.
    assert paths[16].exists()  # Sunday of the fourth retained week.
    assert not paths[34].exists() and not duplicate.exists()
    assert manual.exists() and invalid.exists()
    assert invalid.name in result["invalid_preserved"]
    assert manual.name not in result["removed"]


def test_retention_refuses_a_linked_backup_root(tmp_path):
    actual, link = tmp_path / "actual", tmp_path / "link"
    actual.mkdir()
    try:
        link.symlink_to(actual, target_is_directory=True)
    except OSError:
        pytest.skip("host cannot create symlinks")
    with pytest.raises(snapshot.SnapshotError):
        job.prune_scheduled(link)


def test_optional_offsite_copy_is_verified_before_restarting(environment, tmp_path):
    cfg, _, _, _ = environment
    result = job.run_backup(cfg, offsite_dir=tmp_path / "mounted-offsite")
    assert result["replication"] == "offsite-directory"
    assert snapshot.verify_snapshot(result["offsite_snapshot"])["manifest_sha256"] == result["verification"]["manifest_sha256"]


def test_extra_writer_restores_only_if_originally_running(environment):
    cfg, states, events, _ = environment
    extra = "neuralcloud-napcat.service"
    states[extra] = {"LoadState": "loaded", "ActiveState": "active", "MainPID": "90", "Result": "success"}
    assert job.run_backup(cfg, extra_writer_units=(extra,))["status"] == "completed"
    assert ("stop", extra) in events
    starts = [unit for action, unit in events if action == "start"]
    assert starts[0] == extra
    events.clear()
    states[extra].update(ActiveState="inactive", MainPID="0")
    assert job.run_backup(cfg, extra_writer_units=(extra,))["status"] == "completed"
    assert all(unit != extra for _, unit in events)


def test_timer_templates_pass_real_systemd_parser(tmp_path):
    if os.name == "nt":
        if shutil.which("wsl.exe") is None:
            pytest.skip("WSL unavailable")
        command = ["wsl.exe", "-d", "Ubuntu-24.04", "--exec"]
    else:
        if shutil.which("systemd-analyze") is None:
            pytest.skip("systemd-analyze unavailable")
        command = []
    probe = subprocess.run([*command, "systemd-analyze", "--version"], capture_output=True, timeout=30)
    if probe.returncode:
        pytest.skip("systemd-analyze unavailable")
    files = []
    for name in ("neuralcloud-backup.service", "neuralcloud-backup.timer"):
        content = (Path(__file__).parents[1] / "deploy" / name).read_text()
        content = content.replace("/opt/neuralcloud/current/.venv/bin/python", "/usr/bin/python3")
        path = tmp_path / name
        path.write_text(content)
        if command:
            response = subprocess.run([*command, "wslpath", "-a", str(path)], capture_output=True, check=True, timeout=15)
            files.append(response.stdout.decode().strip())
        else:
            files.append(str(path))
    result = subprocess.run([*command, "systemd-analyze", "verify", "--man=no", *files], capture_output=True, timeout=30)
    assert result.returncode == 0, result.stderr.decode(errors="replace")
