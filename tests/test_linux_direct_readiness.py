"""The protocol worker can boot offline without weakening deployment checks."""
from datetime import datetime, timedelta, timezone

import pytest

from harness.deploy import systemd
from harness.deploy.config import DeployError, DeploymentConfig


@pytest.fixture
def direct_readiness(tmp_path, monkeypatch):
    cfg = DeploymentConfig(config_file=tmp_path / "deploy.json",
        paths={"current": str(tmp_path / "current")}, startup_timeout_seconds=1)
    status = dict(service="qq-direct-onebot-v1", running=True, connected=False,
        pid=1234, repository_root=str(tmp_path / "current"), state="disconnected",
        heartbeat_at=datetime.now(timezone.utc).isoformat())
    unit = {"ActiveState": "activating", "MainPID": "1234"}
    monkeypatch.setattr(systemd, "unit_status", lambda _: unit)
    monkeypatch.setattr(systemd, "_health", lambda *_: status)
    ticks = iter((0, 0, 2))
    monkeypatch.setattr(systemd.time, "monotonic", lambda: next(ticks))
    monkeypatch.setattr(systemd.time, "sleep", lambda _: None)
    return cfg, status, unit


def test_boot_completes_while_napcat_waits_for_login(direct_readiness):
    cfg, _, _ = direct_readiness
    systemd.wait_healthy(cfg, "qq_direct", boot_only=True)


def test_explicit_verification_still_requires_connection(direct_readiness):
    cfg, _, unit = direct_readiness
    unit["ActiveState"] = "active"
    with pytest.raises(DeployError, match="did not become ready"):
        systemd.wait_healthy(cfg, "qq_direct")


def test_connected_current_worker_passes_explicit_verification(direct_readiness):
    cfg, status, unit = direct_readiness
    status["connected"] = True
    unit["ActiveState"] = "active"
    systemd.wait_healthy(cfg, "qq_direct")


@pytest.mark.parametrize("changes", [
    {"pid": 4321}, {"pid": "1234"}, {"pid": True},
    {"repository_root": "/old/neuralcloud/release"}, {"repository_root": "current"},
    {"repository_root": None}, {"service": "other-worker"}, {"running": False},
    {"heartbeat_at": (datetime.now(timezone.utc) - timedelta(seconds=40)).isoformat()},
    {"heartbeat_at": (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat()},
    {"heartbeat_at": datetime.now().isoformat()}, {"heartbeat_at": None},
])
def test_boot_rejects_stale_or_unbound_status(direct_readiness, changes):
    cfg, status, _ = direct_readiness
    status.update(changes)
    with pytest.raises(DeployError, match="did not become ready"):
        systemd.wait_healthy(cfg, "qq_direct", boot_only=True)
