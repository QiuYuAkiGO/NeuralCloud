"""Exercise the Windows launcher against isolated temp folders and mocked Docker I/O."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import pytest

REPO = Path(__file__).resolve().parents[1]
PWSH = shutil.which("pwsh")
pytestmark = pytest.mark.skipif(os.name != "nt" or not PWSH, reason="requires Windows PowerShell")

COMMON = r"""
$script:Root = $env:TEST_DOCKER_ROOT
$script:Starts = 0
function script:Get-DDPaths {
    $local = Join-Path $script:Root 'local'
    @{ Local=$local; Run=(Join-Path $local 'Docker\run'); Secrets=(Join-Path $local 'docker-secrets-engine');
       Logs=(Join-Path $local 'Docker\log\host'); Journal=(Join-Path $script:Root 'journal'); Docker='mock'; Desktop='mock' }
}
function script:Invoke-DDProbe { @{Healthy=$false} }
function script:Get-DDProcesses { @() }
function script:Start-DDHidden { throw 'Unexpected real startup attempt' }
$paths = Get-DDPaths
foreach ($path in @($paths.Run, $paths.Secrets, $paths.Logs)) { [IO.Directory]::CreateDirectory($path) | Out-Null }
[IO.File]::WriteAllText((Join-Path $paths.Run 'sentinel'), 'run-data')
[IO.File]::WriteAllText((Join-Path $paths.Secrets 'sentinel'), 'secret-runtime-data')
function script:Add-FailureLog {
    param([string]$Socket, [string]$Stamp = [DateTimeOffset]::UtcNow.ToString('o'))
    $line = "[$Stamp] backend cancelling with error: rename $Socket $Socket.stale`: The file cannot be accessed by the system."
    [IO.File]::AppendAllText((Join-Path (Get-DDPaths).Logs 'com.docker.backend.exe.log'), $line + "`n")
}
"""


def run_mock(tmp_path: Path, body: str) -> dict:
    module = str(REPO / "tools/docker/DockerDesktop.psm1").replace("'", "''")
    script = (
        f"$ErrorActionPreference='Stop'; Import-Module '{module}' -Force; "
        "& (Get-Module DockerDesktop) {\n" + COMMON + "\n" + body + "\n} | ConvertTo-Json -Depth 10 -Compress"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode()
    completed = subprocess.run(
        [PWSH, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        env={**os.environ, "TEST_DOCKER_ROOT": str(tmp_path)},
        capture_output=True, text=True, timeout=20, check=True,
    )
    return json.loads(completed.stdout)


def backups(tmp_path: Path) -> list[Path]:
    return list(tmp_path.rglob("*.misty-backup-*"))


def test_healthy_engine_ignores_explicit_refresh(tmp_path: Path) -> None:
    report = run_mock(tmp_path, "function script:Invoke-DDProbe { @{Healthy=$true} }; Start-DockerDesktopSafe -RefreshRuntimeOnColdStart")
    assert report["status"] == "healthy"
    assert not backups(tmp_path)
    assert not (tmp_path / "journal").exists()


@pytest.mark.parametrize("flag", ["-CheckOnly", "-WhatIf"])
def test_read_only_modes_never_start_or_refresh(tmp_path: Path, flag: str) -> None:
    report = run_mock(tmp_path, f"Start-DockerDesktopSafe {flag} -RefreshRuntimeOnColdStart")
    assert report["exit_code"] == 10
    assert not backups(tmp_path)
    assert not (tmp_path / "journal").exists()


def test_existing_processes_wait_without_touching_runtime(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        function script:Get-DDProcesses { @([pscustomobject]@{ProcessName='Docker Desktop'}) }
        function script:Wait-DDState { @{State='timeout'; Error=$null} }
        Start-DockerDesktopSafe -RefreshRuntimeOnColdStart -TimeoutSeconds 1
    """)
    assert report["exit_code"] == 20
    assert "Existing Docker processes" in report["message"]
    assert not backups(tmp_path)


def test_cold_refresh_preserves_both_directories_and_has_one_isolation_budget(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        function script:Start-DDHidden {
            $script:Starts++
            Add-FailureLog (Join-Path (Get-DDPaths).Run 'sailor-ingest.sock')
        }
        $result = Start-DockerDesktopSafe -RefreshRuntimeOnColdStart -TimeoutSeconds 1
        @{result=$result; starts=$script:Starts}
    """)
    assert report["starts"] == 1
    assert report["result"]["exit_code"] == 20
    assert len(backups(tmp_path)) == 2
    assert sorted((path / "sentinel").read_text() for path in backups(tmp_path)) == ["run-data", "secret-runtime-data"]
    journal = json.loads(Path(report["result"]["journal"]).read_text())
    assert journal["status"] == "backed_up_and_recreated"


def test_new_exact_socket_error_repairs_both_roots_then_starts_once_more(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        function script:Start-DDHidden {
            $script:Starts++
            Add-FailureLog (Join-Path (Get-DDPaths).Secrets 'engine.sock')
        }
        function script:Invoke-DDProbe { @{Healthy=($script:Starts -ge 2)} }
        $result = Start-DockerDesktopSafe -TimeoutSeconds 1
        @{result=$result; starts=$script:Starts}
    """)
    assert report["result"]["status"] == "healthy"
    assert report["starts"] == 2
    assert len(backups(tmp_path)) == 2


@pytest.mark.parametrize("mode", ["old", "unrelated", "exists_only"])
def test_old_or_unrelated_errors_and_socket_existence_do_not_authorize_repair(tmp_path: Path, mode: str) -> None:
    setup = {
        "old": "Add-FailureLog (Join-Path $paths.Run 'sailor-ingest.sock') '2020-01-01T00:00:00Z'",
        "unrelated": "",
        "exists_only": "[IO.File]::WriteAllText((Join-Path $paths.Run 'sailor-ingest.sock'), 'socket')",
    }[mode]
    start = "Add-FailureLog (Join-Path $script:Root 'other/engine.sock')" if mode == "unrelated" else "$script:Starts++"
    report = run_mock(tmp_path, setup + f"; function script:Start-DDHidden {{ {start} }}; Start-DockerDesktopSafe -TimeoutSeconds 1")
    assert report["exit_code"] == 20
    assert not backups(tmp_path)


def test_second_root_junction_rejected_before_first_root_moves(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        $outside = Join-Path $script:Root 'outside'
        [IO.Directory]::CreateDirectory($outside) | Out-Null
        Move-Item -LiteralPath $paths.Secrets -Destination (Join-Path $script:Root 'original-secrets')
        New-Item -ItemType Junction -Path $paths.Secrets -Target $outside | Out-Null
        Start-DockerDesktopSafe -RefreshRuntimeOnColdStart
    """)
    assert report["exit_code"] == 20
    assert "reparse" in report["message"]
    assert not backups(tmp_path)
    assert (tmp_path / "local/Docker/run/sentinel").read_text() == "run-data"


def test_probe_timeout_kills_only_its_own_process(tmp_path: Path) -> None:
    before = time.monotonic()
    report = run_mock(tmp_path, """
        Invoke-DDTimedProcess (Get-Process -Id $PID).Path '-NoProfile -NonInteractive -Command "Start-Sleep -Seconds 30"' 200
    """)
    assert report["TimedOut"] is True
    assert report["Healthy"] is False
    assert time.monotonic() - before < 8
