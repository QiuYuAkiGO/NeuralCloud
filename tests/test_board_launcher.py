"""Deployment isolation and pause ownership tests; no WSL/service mutations."""

from __future__ import annotations

import importlib.util
import json
import os
from pathlib import Path
import shutil
import socket
import subprocess
import sys
from types import SimpleNamespace

import pytest


MODULE_PATH = Path(__file__).parents[1] / "tools/board/linux_board.py"
SPEC = importlib.util.spec_from_file_location("linux_board_launcher_test", MODULE_PATH)
assert SPEC and SPEC.loader
board = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(board)


@pytest.fixture
def deployment(tmp_path: Path):
    workspace = tmp_path / "repository"
    workspace.mkdir()
    unit_dir = tmp_path / "units"
    unit_dir.mkdir()
    return board.BoardDeployment(workspace, state_dir=tmp_path / "board", unit_dir=unit_dir)


def write_owner(deployment) -> None:
    deployment.state.mkdir()
    deployment.owner_path.write_text(json.dumps({
        "owner": board.MARKER, "workspace": str(deployment.workspace),
        "body_instance_id": board.BODY_ID, "board_id": board.BOARD_ID,
    }))


def test_foreign_unit_rejected_before_any_systemctl(deployment, monkeypatch):
    deployment.unit_path.write_text("[Service]\nExecStart=/usr/bin/other-service\n")
    monkeypatch.setattr(board, "run", lambda *args, **kwargs: pytest.fail("must not touch foreign service"))
    with pytest.raises(ValueError, match="board_unit_not_owned"):
        deployment.stop()


def test_existing_data_without_owner_is_preserved(deployment):
    deployment.state.mkdir()
    database = deployment.state / "board.sqlite3"
    database.write_bytes(b"existing data")
    with pytest.raises(ValueError, match="board_state_not_owned"):
        deployment.validate_ownership()
    assert database.read_bytes() == b"existing data"


def test_other_workspace_cannot_stop_owner(deployment, monkeypatch):
    write_owner(deployment)
    owner = json.loads(deployment.owner_path.read_text())
    owner["workspace"] = "/other/project"
    deployment.owner_path.write_text(json.dumps(owner))
    monkeypatch.setattr(board, "run", lambda *args, **kwargs: pytest.fail("must not touch another workspace"))
    with pytest.raises(ValueError, match="board_deployment_owner_mismatch"):
        deployment.stop()


def test_pause_disables_only_owned_board_and_retains_data(deployment, monkeypatch):
    write_owner(deployment)
    deployment.unit_path.write_text(deployment.unit_text())
    database = deployment.state / "board.sqlite3"
    database.write_bytes(b"entries")
    commands = []

    def fake_run(*args, **kwargs):
        commands.append(args)
        return SimpleNamespace(returncode=3 if args[1] == "is-active" else 0)

    monkeypatch.setattr(board, "run", fake_run)
    result = deployment.stop()
    assert result["action"] == "stopped"
    assert commands == [
        ("systemctl", "stop", board.UNIT),
        ("systemctl", "disable", board.UNIT),
        ("systemctl", "is-active", "--quiet", board.UNIT),
    ]
    assert database.read_bytes() == b"entries"


def test_inactive_missing_board_stop_does_not_create_state(deployment, monkeypatch):
    commands = []
    monkeypatch.setattr(board, "run", lambda *args, **kwargs: commands.append(args) or SimpleNamespace(returncode=3))
    assert deployment.stop()["action"] == "not_installed"
    assert commands == [("systemctl", "is-active", "--quiet", board.UNIT)]
    assert not deployment.state.exists()


def test_active_unit_without_owner_cannot_be_stopped(deployment, monkeypatch):
    commands = []
    monkeypatch.setattr(board, "run", lambda *args, **kwargs: commands.append(args) or SimpleNamespace(returncode=0))
    with pytest.raises(ValueError, match="board_active_unit_not_owned"):
        deployment.stop()
    assert commands == [("systemctl", "is-active", "--quiet", board.UNIT)]


def test_unit_contains_only_board_and_no_credentials(deployment):
    unit = deployment.unit_text()
    assert "-m harness.board.service" in unit
    assert "NC_BOARD_TOKEN_FILE=" in unit
    assert "NC_BOARD_HOST=127.0.0.1" in unit
    assert "User=root" in unit
    assert f"WorkingDirectory={deployment.workspace}\n" in unit
    assert "NoNewPrivileges=true" in unit
    assert "ProtectSystem=strict" in unit
    assert "ReadWritePaths=" in unit
    assert "mistyai" not in unit.lower()
    assert "docker" not in unit.lower()
    assert "Bearer" not in unit


def test_unit_quote_escapes_systemd_expansion():
    assert board.systemd_quote('/path with space/"%n') == '"/path with space/\\"%%n"'
    with pytest.raises(ValueError, match="board_invalid_unit_path"):
        board.systemd_quote("/path\nExecStart=/bin/other")


def test_source_change_invalidates_reuse(deployment):
    source = deployment.workspace / "harness/board/service.py"
    source.parent.mkdir(parents=True)
    source.write_text("version = 1\n")
    (deployment.workspace / "uv.lock").write_text("version = 1\n")
    helper = deployment.workspace / "tools/board/linux_board.py"
    helper.parent.mkdir(parents=True)
    helper.write_text("deployment = 1\n")
    before = deployment.source_fingerprint()
    source.write_text("version = 2\n")
    assert deployment.source_fingerprint() != before


def test_no_service_source_prevents_install(deployment, monkeypatch):
    monkeypatch.setattr(board.os, "geteuid", lambda: 0, raising=False)
    monkeypatch.setattr(deployment, "active", lambda: False)
    monkeypatch.setattr(deployment, "install_dependencies", lambda: pytest.fail("must fail before installation"))
    with pytest.raises(ValueError, match="board_service_source_missing"):
        deployment.start()
    assert not deployment.state.exists()


def test_health_requires_expected_body_and_healthy_status(deployment, monkeypatch):
    write_owner(deployment)
    deployment.token_path.write_text("A" * 48)

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, limit):
            return json.dumps({"body_instance_id": "another-body", "status": "healthy"}).encode()

    class Opener:
        def open(self, request, timeout):
            assert request.get_header("Authorization") == "Bearer " + "A" * 48
            assert timeout == 2
            return Response()

    monkeypatch.setattr(board.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ValueError, match="board_health_identity_or_status_mismatch"):
        deployment.health()


def test_port_probe_uses_address_reuse_without_port_sharing(monkeypatch):
    calls = []

    class Probe:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def setsockopt(self, *args):
            calls.append(("setsockopt", *args))

        def bind(self, address):
            calls.append(("bind", address))

    monkeypatch.setattr(board.socket, "socket", lambda *args: Probe())
    board.require_port_available()
    assert calls == [
        ("setsockopt", socket.SOL_SOCKET, socket.SO_REUSEADDR, 1),
        ("bind", ("127.0.0.1", board.PORT)),
    ]


@pytest.mark.skipif(sys.platform != "linux", reason="SO_REUSEADDR semantics differ on Windows")
def test_linux_probe_reuses_time_wait_but_rejects_live_listener(monkeypatch):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.listen()
    client = socket.create_connection(("127.0.0.1", port), timeout=2)
    accepted, _ = listener.accept()
    accepted.shutdown(socket.SHUT_WR)
    assert client.recv(1) == b""
    client.close()
    accepted.close()
    listener.close()
    monkeypatch.setattr(board, "PORT", port)
    # The old startup probe failed here because TIME_WAIT owns the local port.
    with socket.socket() as old_probe, pytest.raises(OSError):
        old_probe.bind(("127.0.0.1", port))
    board.require_port_available()
    with socket.socket() as live:
        live.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        live.bind(("127.0.0.1", port))
        live.listen()
        with pytest.raises(ValueError, match="board_port_already_in_use"):
            board.require_port_available()


@pytest.mark.skipif(os.name != "nt", reason="Windows ACL and PowerShell replacement semantics")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
def test_windows_private_file_replace_keeps_only_owner_and_system(tmp_path, shell):
    executable = shutil.which(shell)
    if not executable:
        pytest.skip(f"{shell} is unavailable")
    code = r"""
$ErrorActionPreference = 'Stop'
$module = Import-Module $env:BOARD_TEST_MODULE -Force -PassThru
& $module {
    param($root)
    [void][IO.Directory]::CreateDirectory($root)
    Set-BoardPrivateAcl -Path $root -Directory
    $path = Join-Path $root 'test.token'
    Write-BoardAtomicText -Path $path -Text 'fixture-value-one' -Private
    Write-BoardAtomicText -Path $path -Text 'fixture-value-two' -Private
    $info = New-Object IO.FileInfo($path)
    $acl = if ($PSVersionTable.PSVersion.Major -ge 6) {
        [IO.FileSystemAclExtensions]::GetAccessControl($info, [Security.AccessControl.AccessControlSections]::Access)
    } else { $info.GetAccessControl([Security.AccessControl.AccessControlSections]::Access) }
    $sids = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object { $_.IdentityReference.Value })
    $current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value
    [pscustomobject]@{
        contents = [IO.File]::ReadAllText($path)
        protected = $acl.AreAccessRulesProtected
        owner_and_system_only = ($sids.Count -eq 2 -and $sids -contains $current -and $sids -contains 'S-1-5-18')
        leftover_files = @(Get-ChildItem -LiteralPath $root -Force).Count
    } | ConvertTo-Json -Compress
} $env:BOARD_TEST_SCRATCH
"""
    env = os.environ | {
        "BOARD_TEST_MODULE": str(MODULE_PATH.with_name("Board.psm1")),
        "BOARD_TEST_SCRATCH": str(tmp_path / "private"),
    }
    # A PowerShell 7 parent can pass its incompatible built-in module paths to
    # Windows PowerShell 5. Let each tested shell initialize its native defaults.
    env.pop("PSModulePath", None)
    result = subprocess.run([executable, "-NoProfile", "-NonInteractive", "-Command", code],
                            env=env, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stderr
    assert not result.stderr
    assert json.loads(result.stdout) == {
        "contents": "fixture-value-two", "protected": True,
        "owner_and_system_only": True, "leftover_files": 1,
    }
