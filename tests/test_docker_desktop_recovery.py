"""Exercise the Windows launcher against isolated temp folders and mocked Docker I/O."""

from __future__ import annotations

import base64
import ctypes
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
$script:KeepAliveStarts = 0
$script:KeepAliveStops = 0
$script:KeepAliveEvents = @()
function script:Get-DDPaths {
    $local = Join-Path $script:Root 'local'
    @{ Local=$local; Run=(Join-Path $local 'Docker\run'); Secrets=(Join-Path $local 'docker-secrets-engine');
       Logs=(Join-Path $local 'Docker\log\host'); Journal=(Join-Path $script:Root 'journal'); Docker='mock'; Desktop='mock' }
}
function script:Invoke-DDProbe { @{Healthy=$false} }
$script:OriginalWslProbe = (Get-Command Invoke-DDWslProbe).ScriptBlock
$script:OriginalBackup = (Get-Command Backup-DDRuntime).ScriptBlock
$script:OriginalStartKeepAlive = (Get-Command Start-DDWslKeepAlive).ScriptBlock
$script:OriginalStopKeepAlive = (Get-Command Stop-DDWslKeepAlive).ScriptBlock
function script:Invoke-DDWslProbe { throw 'Unexpected real WSL probe attempt' }
function script:Start-DDWslKeepAlive {
    $script:KeepAliveStarts++
    $script:KeepAliveEvents += 'keepalive_start'
    @{mock=$true}
}
function script:Stop-DDWslKeepAlive {
    param($KeepAlive)
    if ($null -ne $KeepAlive) {
        $script:KeepAliveStops++
        $script:KeepAliveEvents += 'keepalive_stop'
    }
}
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


def test_wsl_integration_waits_for_delayed_readiness_after_engine_start(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        $script:WslChecks = 0
        function script:Start-DDHidden { $script:Starts++ }
        function script:Invoke-DDProbe { @{Healthy=($script:Starts -gt 0)} }
        function script:Invoke-DDWslProbe {
            $script:WslChecks++
            @{Healthy=($script:WslChecks -ge 2)}
        }
        $result = Start-DockerDesktopSafe -WslDistro 'Ubuntu 24.04' -TimeoutSeconds 2
        @{result=$result; starts=$script:Starts; wsl_checks=$script:WslChecks}
    """)
    assert report["result"]["status"] == "healthy"
    assert report["result"]["integration_status"] == "ready"
    assert report["result"]["wsl_distro"] == "Ubuntu 24.04"
    assert report["starts"] == 1
    assert report["wsl_checks"] == 2
    assert not backups(tmp_path)


def test_failed_wsl_integration_never_restarts_or_rotates_again(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        $script:WslChecks = 0
        function script:Start-DDHidden { $script:Starts++ }
        function script:Invoke-DDProbe { @{Healthy=($script:Starts -gt 0)} }
        function script:Invoke-DDWslProbe { $script:WslChecks++; @{Healthy=$false} }
        $result = Start-DockerDesktopSafe -RefreshRuntimeOnColdStart -WslDistro 'Ubuntu-24.04' -TimeoutSeconds 1
        @{result=$result; starts=$script:Starts; wsl_checks=$script:WslChecks; keepalive_stops=$script:KeepAliveStops}
    """)
    assert report["result"]["status"] == "integration_not_ready"
    assert report["result"]["exit_code"] == 30
    assert report["starts"] == 1
    assert report["wsl_checks"] >= 2
    assert len(backups(tmp_path)) == 2
    assert report["keepalive_stops"] == 1


@pytest.mark.parametrize("existing_start", [False, True])
def test_already_healthy_and_existing_start_success_also_check_wsl(tmp_path: Path, existing_start: bool) -> None:
    setup = (
        "function script:Get-DDProcesses { @([pscustomobject]@{ProcessName='Docker Desktop'}) }; "
        "function script:Wait-DDState { @{State='healthy'; Error=$null} }"
        if existing_start else "function script:Invoke-DDProbe { @{Healthy=$true} }"
    )
    report = run_mock(tmp_path, setup + """
        $script:WslChecks = 0
        function script:Invoke-DDWslProbe { $script:WslChecks++; @{Healthy=$true} }
        $result = Start-DockerDesktopSafe -RefreshRuntimeOnColdStart -WslDistro 'Ubuntu-24.04' -TimeoutSeconds 1
        @{result=$result; starts=$script:Starts; wsl_checks=$script:WslChecks}
    """)
    assert report["result"]["integration_status"] == "ready"
    assert report["wsl_checks"] == 1
    assert report["starts"] == 0
    assert not backups(tmp_path)
    assert report["result"]["journal"] is None


@pytest.mark.parametrize(("milliseconds", "duration", "kill_after"), [(5000, "3.500s", "0.500s"), (1000, "0.700s", "0.100s")])
def test_wsl_probe_preserves_distro_as_one_argument_without_a_shell(tmp_path: Path, milliseconds: int, duration: str, kill_after: str) -> None:
    report = run_mock(tmp_path, r"""
        function script:Invoke-DDTimedProcess {
            param($Executable, $Arguments, $TimeoutMilliseconds)
            @{executable=$Executable; arguments=$Arguments; timeout=$TimeoutMilliseconds}
        }
        $paths.Wsl = 'mock-wsl'
        & $script:OriginalWslProbe $paths 'Ubuntu "test" --shutdown \' PROBE_MS
    """.replace("PROBE_MS", str(milliseconds)))
    parse = ctypes.windll.shell32.CommandLineToArgvW
    parse.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_int)]
    parse.restype = ctypes.POINTER(ctypes.c_wchar_p)
    count = ctypes.c_int()
    pointer = parse("probe.exe " + report["arguments"], ctypes.byref(count))
    assert pointer
    try:
        arguments = [pointer[index] for index in range(1, count.value)]
    finally:
        ctypes.windll.kernel32.LocalFree(ctypes.cast(pointer, ctypes.c_void_p))
    assert arguments[:5] == ["--distribution", 'Ubuntu "test" --shutdown \\', "--exec", "/usr/bin/timeout", "--kill-after=" + kill_after]
    assert arguments[5] == duration
    assert arguments[-6:] == ["/usr/bin/docker", "--host", "unix:///var/run/docker.sock", "info", "--format", "{{.ServerVersion}}"]
    assert report["timeout"] == milliseconds
    # WSL treats unnecessarily quoted initial option tokens as a Linux command.
    # A correct C-runtime argv round-trip alone cannot catch this WSL-specific behavior.
    assert report["arguments"].startswith("--distribution ")
    assert " --exec /usr/bin/timeout " in report["arguments"]
    assert '"--distribution"' not in report["arguments"]
    assert '"/usr/bin/docker"' not in report["arguments"]


@pytest.mark.parametrize("flag", ["-CheckOnly", "-WhatIf"])
def test_read_only_healthy_engine_does_not_wake_wsl(tmp_path: Path, flag: str) -> None:
    report = run_mock(tmp_path, """
        function script:Invoke-DDProbe { @{Healthy=$true} }
        function script:Invoke-DDWslProbe { throw 'Read-only mode must not invoke WSL' }
    """ + f"Start-DockerDesktopSafe {flag} -WslDistro 'Ubuntu-24.04'")
    assert report["status"] == "integration_not_checked"
    assert report["exit_code"] == 10
    assert report["journal"] is None
    assert not backups(tmp_path)


def test_cold_wsl_keepalive_precedes_backup_and_start_and_is_released(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        function script:Backup-DDRuntime {
            param($Paths, $Evidence)
            $script:KeepAliveEvents += 'backup'
            & $script:OriginalBackup $Paths $Evidence
        }
        function script:Start-DDHidden {
            $script:KeepAliveEvents += 'desktop_start'
            $script:Starts++
        }
        function script:Invoke-DDProbe { @{Healthy=($script:Starts -gt 0)} }
        function script:Invoke-DDWslProbe { @{Healthy=$true} }
        $result = Start-DockerDesktopSafe -RefreshRuntimeOnColdStart -WslDistro 'Ubuntu-24.04' -TimeoutSeconds 1
        @{result=$result; events=$script:KeepAliveEvents; starts=$script:KeepAliveStarts; stops=$script:KeepAliveStops}
    """)
    assert report["result"]["status"] == "healthy"
    assert report["events"] == ["keepalive_start", "backup", "desktop_start", "keepalive_stop"]
    assert report["starts"] == report["stops"] == 1


@pytest.mark.parametrize("mode", ["healthy", "existing", "check_only", "what_if"])
def test_non_cold_paths_do_not_create_wsl_keepalive(tmp_path: Path, mode: str) -> None:
    setup = {
        "healthy": "function script:Invoke-DDProbe { @{Healthy=$true} }",
        "existing": "function script:Get-DDProcesses { @([pscustomobject]@{ProcessName='Docker Desktop'}) }; function script:Wait-DDState { @{State='healthy'; Error=$null} }",
        "check_only": "",
        "what_if": "",
    }[mode]
    flag = {"check_only": "-CheckOnly", "what_if": "-WhatIf"}.get(mode, "")
    report = run_mock(tmp_path, setup + """
        function script:Invoke-DDWslProbe { @{Healthy=$true} }
    """ + f"$result = Start-DockerDesktopSafe -WslDistro 'Ubuntu-24.04' {flag}; " + """
        @{result=$result; starts=$script:KeepAliveStarts; stops=$script:KeepAliveStops}
    """)
    assert report["starts"] == report["stops"] == 0
    assert not backups(tmp_path)


def test_wsl_prewarm_failure_does_not_start_or_modify_runtime(tmp_path: Path) -> None:
    report = run_mock(tmp_path, """
        function script:Start-DDWslKeepAlive { throw 'WSL readiness marker timed out' }
        $result = Start-DockerDesktopSafe -RefreshRuntimeOnColdStart -WslDistro 'Ubuntu-24.04' -TimeoutSeconds 1
        @{result=$result; starts=$script:Starts}
    """)
    assert report["result"]["exit_code"] == 20
    assert "marker timed out" in report["result"]["message"]
    assert report["starts"] == 0
    assert not backups(tmp_path)
    assert (tmp_path / "local/Docker/run/sentinel").read_text() == "run-data"


@pytest.mark.parametrize("echo_marker", [True, False])
def test_keepalive_marker_and_cleanup_with_owned_fake_process(tmp_path: Path, echo_marker: bool) -> None:
    # Replace only wsl.exe with an owned PowerShell child; exercise real stdin/ready/cleanup code.
    command = (
        "[Console]::WriteLine([Console]::ReadLine()); [Console]::In.ReadToEnd() | Out-Null"
        if echo_marker else "Start-Sleep -Seconds 30"
    )
    report = run_mock(tmp_path, r"""
        $script:OwnStopCalls = 0
        $script:OwnedPid = 0
        function script:New-DDWslKeepAliveProcess {
            $encoded = [Convert]::ToBase64String([Text.Encoding]::Unicode.GetBytes('CHILD_COMMAND'))
            $process = [Diagnostics.Process]::new()
            $process.StartInfo = [Diagnostics.ProcessStartInfo]@{
                FileName=(Get-Process -Id $PID).Path
                Arguments=('-NoProfile -NonInteractive -EncodedCommand ' + $encoded)
                UseShellExecute=$false; CreateNoWindow=$true
                RedirectStandardInput=$true; RedirectStandardOutput=$true; RedirectStandardError=$true
            }
            return $process
        }
        function script:Stop-DDWslKeepAlive {
            param($holder)
            $script:OwnedPid = $holder.Process.Id
            $script:OwnStopCalls++
            & $script:OriginalStopKeepAlive $holder
        }
        $outcome = 'ready'
        $errorText = $null
        try {
            $holder = & $script:OriginalStartKeepAlive (Get-DDPaths) 'mock-distro' 3000 30
            Stop-DDWslKeepAlive $holder
        } catch {
            $outcome = 'failed'
            $errorText = $_.Exception.Message
        }
        @{outcome=$outcome; error=$errorText; stop_calls=$script:OwnStopCalls;
            process_alive=($null -ne (Get-Process -Id $script:OwnedPid -ErrorAction SilentlyContinue))}
    """.replace("CHILD_COMMAND", command))
    assert report["outcome"] == ("ready" if echo_marker else "failed")
    assert report["stop_calls"] == 1
    assert report["process_alive"] is False
    if not echo_marker:
        assert "timed out" in report["error"]
