from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from harness.runtime.ownership import (
    DevelopmentModeRequired,
    SharedDomainAlreadyOwned,
    SharedDomainLease,
    require_development,
    require_service_token,
    resolve_control_mode,
)


@pytest.mark.parametrize("backend", ["demo", "live", "ollama", "development_debug"])
def test_model_backend_does_not_grant_authority(backend):
    assert resolve_control_mode(environ={"NC_MODE": backend}) == "study_runtime"
    with pytest.raises(DevelopmentModeRequired):
        require_development("debug.mutate", control_mode="study_runtime")


def test_explicit_development_and_conflicting_manifest():
    assert resolve_control_mode(environ={"NC_CONTROL_MODE": "development_debug"}) == "development_debug"
    require_development("isolated", control_mode="development_debug")
    with pytest.raises(ValueError, match="conflicting_control_modes"):
        resolve_control_mode(manifest={"control_mode": "study_runtime"}, environ={"NC_CONTROL_MODE": "development_debug"})


@pytest.mark.parametrize("mode", ["", "dev", "live", "DEVELOPMENT_DEBUG"])
def test_invalid_authority_rejected(mode):
    with pytest.raises(ValueError, match="invalid_control_mode"):
        resolve_control_mode(environ={"NC_CONTROL_MODE": mode})


def test_environment_manifest_is_fully_validated(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"control_mode": "development_debug"}))
    with pytest.raises(ValueError):
        resolve_control_mode(environ={"NC_RUNTIME_MANIFEST_PATH": str(path)})
    with pytest.raises(FileNotFoundError):
        resolve_control_mode(environ={"NC_RUNTIME_MANIFEST_PATH": str(tmp_path / "missing.json")})


def test_normal_different_state_paths_are_same_owner(tmp_path):
    with SharedDomainLease("cognition", tmp_path / "a", lease_root=tmp_path, control_mode="study_runtime"):
        with pytest.raises(SharedDomainAlreadyOwned):
            SharedDomainLease("cognition", tmp_path / "b", lease_root=tmp_path, control_mode="study_runtime").acquire()
        # A different domain is a separate service in the same shared mind.
        with SharedDomainLease("homeostasis", tmp_path / "a", lease_root=tmp_path, control_mode="study_runtime"):
            pass
    with SharedDomainLease("cognition", tmp_path / "b", lease_root=tmp_path, control_mode="study_runtime"):
        pass


def test_isolation_requires_development_and_still_locks_identical_state(tmp_path):
    with pytest.raises(DevelopmentModeRequired):
        SharedDomainLease("cognition", "other", shared_id="alternate", control_mode="development_debug", lease_root=tmp_path)
    with pytest.raises(DevelopmentModeRequired):
        SharedDomainLease("cognition", "isolated", isolated=True, lease_root=tmp_path, control_mode="study_runtime")
    with SharedDomainLease("cognition", "shared", lease_root=tmp_path, control_mode="study_runtime"):
        with SharedDomainLease("cognition", "dev", isolated=True, lease_root=tmp_path, control_mode="development_debug"):
            with pytest.raises(SharedDomainAlreadyOwned):
                SharedDomainLease("cognition", "dev", isolated=True, lease_root=tmp_path, control_mode="development_debug").acquire()
            with pytest.raises(SharedDomainAlreadyOwned):
                SharedDomainLease("cognition", "different", lease_root=tmp_path, control_mode="development_debug").acquire()


def test_lease_metadata_has_no_private_state_path(tmp_path):
    lease = SharedDomainLease("cognition", "private-state-value", lease_root=tmp_path, control_mode="study_runtime")
    with lease:
        # Windows may disallow reading the first locked byte; inspect after release.
        pass
    metadata = lease.path.read_text()
    assert "private-state-value" not in metadata
    assert json.loads(metadata)["pid"] == os.getpid()


def test_process_lock_releases_after_hard_process_exit(tmp_path):
    source = """
import sys
from pathlib import Path
from harness.runtime.ownership import SharedDomainLease
lease = SharedDomainLease('cognition', 'child-state', lease_root=Path(sys.argv[1]), control_mode='study_runtime')
lease.acquire()
print('locked', flush=True)
sys.stdin.read()
"""
    proc = subprocess.Popen([sys.executable, "-c", source, str(tmp_path)], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "locked"
        with pytest.raises(SharedDomainAlreadyOwned):
            SharedDomainLease("cognition", "parent-state", lease_root=tmp_path, control_mode="study_runtime").acquire()
    finally:
        proc.kill()  # Only this test's own child, never a service process.
        proc.communicate(timeout=10)
    with SharedDomainLease("cognition", "parent-state", lease_root=tmp_path, control_mode="study_runtime"):
        pass


@pytest.mark.parametrize("value", [None, "", "short", "development-" + "a" * 40, "placeholder" + "a" * 40, "a" * 32 + " ", "a" * 32 + "\n"])
def test_shared_services_reject_weak_credentials_without_echoing_them(value):
    with pytest.raises(ValueError) as exc:
        require_service_token(value, name="control", control_mode="study_runtime")
    assert str(exc.value) in {"service_token_required: control", "secure_service_token_required: control"}
    if value:
        assert value not in str(exc.value)


def test_development_short_tokens_require_explicit_authority():
    assert require_service_token("development-token", name="service", control_mode="development_debug") == "development-token"
    assert require_service_token("ad" * 32, name="service", control_mode="study_runtime") == "ad" * 32
    with pytest.raises(ValueError):
        require_service_token(" ", name="service", control_mode="development_debug")
