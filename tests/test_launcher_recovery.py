"""Recovery tests use mocks, except isolated, short-lived output-inheritance probes."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools" / "launcher" / "Launcher.psm1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


def literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def invoke(tmp_path: Path, body: str, *, shell: str | None = POWERSHELL):
    runner = tmp_path / "mock-launcher.ps1"
    runner.write_text(
        f'$ErrorActionPreference = "Stop"\n'
        f'$module = Import-Module {literal(MODULE)} -Force -PassThru\n'
        f'$result = & $module {{\n{body}\n}}\n'
        '$result | ConvertTo-Json -Depth 15 -Compress\n',
        encoding="utf-8",
    )
    result = subprocess.run(
        [str(shell), "-NoLogo", "-NoProfile", "-NonInteractive", "-File", str(runner)],
        capture_output=True, text=True, timeout=30, check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout
    return json.loads(result.stdout.strip().splitlines()[-1])


@pytest.mark.parametrize("working_directory", ["", "/tmp/a directory"])
def test_wsl_scripts_normalize_crlf_and_lone_cr(tmp_path: Path, working_directory: str):
    result = invoke(tmp_path, f"""
        $script:captured = $null
        Invoke-LauncherWslCommand -Distro 'Ubuntu-24.04' `
            -Script "set -euo pipefail`r`necho one`recho two`n" `
            -WorkingDirectory {literal(working_directory)} -CommandInvoker {{
                param($request)
                $script:captured = $request
                return @{{ exit_code = 0; stdout = ''; stderr = '' }}
            }} | Out-Null
        $script:captured
    """)
    script = result["ArgumentList"][-1]
    assert "\r" not in script
    assert script.endswith("set -euo pipefail\necho one\necho two\n")
    if working_directory:
        assert script.startswith("cd -- '/tmp/a directory' && ")


def test_registry_health_supports_custom_registry_and_ports(tmp_path: Path):
    registry = tmp_path / "isolated" / ".neuralcloud.pids.json"
    registry.parent.mkdir()
    result = invoke(tmp_path, f"""
        $script:testRoot = {literal(tmp_path)}
        $script:pythonPath = Join-Path $script:testRoot '.venv\\Scripts\\python.exe'
        $script:entries = @()
        $modules = @{{ cognitive = 'harness.cognitive.service'; homeostasis = 'harness.homeostasis.service'; runtime = 'harness.runtime.service'; gui = 'gui.runtime_server' }}
        $ports = @{{ cognitive = 19771; homeostasis = 19772; runtime = 19770; gui = 19790 }}
        $index = 0
        foreach ($name in @('cognitive', 'homeostasis', 'runtime', 'gui')) {{
            $index++
            $script:entries += [pscustomobject]@{{ name = $name; pid = 12000 + $index; port = $ports[$name]; module = $modules[$name]; root = $script:testRoot; python_path = $script:pythonPath; process_start_time_utc = '2026-09-15T01:00:00Z' }}
        }}
        @{{ services = $script:entries }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath {literal(registry)}
        function Get-Process {{
            param($Id, $ErrorAction)
            [pscustomobject]@{{ ProcessName = 'python'; Path = $script:pythonPath; StartTime = [datetime]'2026-09-15T01:00:00Z' }}
        }}
        function Get-LauncherProcessCommandLine {{
            param($ProcessId)
            $entry = $script:entries | Where-Object {{ $_.pid -eq $ProcessId }}
            return $script:pythonPath + ' -m ' + $entry.module
        }}
        function Get-LauncherPortOwner {{
            param($Port)
            $entry = $script:entries | Where-Object {{ $_.port -eq $Port }}
            return [pscustomobject]@{{ pid = $entry.pid }}
        }}
        [pscustomobject]@{{
            custom = Test-NeuralCloudRegistryHealth -Root $script:testRoot -RegistryPath {literal(registry)} -Ports $ports
            default_ports = Test-NeuralCloudRegistryHealth -Root $script:testRoot -RegistryPath {literal(registry)}
            default_path = Test-NeuralCloudRegistryHealth -Root $script:testRoot
        }}
    """)
    assert result["custom"]["running"] is True
    assert len(result["custom"]["services"]) == 4
    assert result["default_ports"]["running"] is False
    assert result["default_path"]["running"] is False


@pytest.mark.parametrize("scenario", ["matching", "manifest_mismatch", "mode_mismatch", "partial"])
def test_up_reuses_or_preserves_existing_instance_before_mutations(tmp_path: Path, scenario: str):
    manifest = tmp_path / "presets" / "runtime" / "shared-misty-v1.json"
    registry = {"manifest_path": str(manifest if scenario != "manifest_mismatch" else tmp_path / "other.json"),
                "services": [{"name": "runtime", "pid": 12345}]}
    (tmp_path / ".neuralcloud.pids.json").write_text(json.dumps(registry), encoding="utf-8")
    result = invoke(tmp_path, f"""
        function Assert-LauncherPrerequisites {{ return [pscustomobject]@{{ checks = @(); actions = @() }} }}
        function Test-NeuralCloudRegistryHealth {{ return [pscustomobject]@{{ running = ${str(scenario != 'partial').lower()}; services = @([pscustomobject]@{{ running = $true }}) }} }}
        function Wait-NeuralCloudReady {{ return [pscustomobject]@{{ mode = '{'live' if scenario == 'mode_mismatch' else 'demo'}'; runtime = [pscustomobject]@{{ lifecycle = 'RUNNING' }} }} }}
        function Get-MistyAIServiceState {{ throw 'must not inspect or change deployment before reuse' }}
        function Initialize-NeuralCloudEnvironment {{ throw 'must not mutate dependencies' }}
        function Start-NeuralCloud {{ throw 'must not start existing instance' }}
        function Stop-NeuralCloud {{ throw 'must not stop existing instance' }}
        $state = Get-LauncherDefaultState
        $state.owned.neuralcloud = $false
        $paths = [pscustomobject]@{{ state = {literal(tmp_path / 'state.json')}; staging = {literal(tmp_path / 'staging')} }}
        $payload = $null; $message = ''; $code = 0
        try {{
            $payload = Invoke-LauncherUpCommand -Root {literal(tmp_path)} -MistyAIPath {literal(tmp_path / 'mistyai')} -Distro 'Ubuntu-24.04' -Mode demo -State $state -Paths $paths -NoOpen -CommandInvoker {{ throw 'external call forbidden' }}
        }} catch {{ $message = $_.Exception.Message; $code = Get-LauncherFailureCode -ErrorRecord $_ -Default 99 }}
        [pscustomobject]@{{ payload = $payload; message = $message; code = $code; owned = $state.owned.neuralcloud }}
    """)
    assert result["owned"] is False
    if scenario == "matching":
        assert result["payload"]["reused"] is True
        assert result["payload"]["status"] == "running"
    else:
        assert result["code"] == 40
        assert "preserved" in result["message"]


def test_rejected_start_does_not_stop_registry_appearing_during_start(tmp_path: Path):
    result = invoke(tmp_path, f"""
        function Assert-LauncherPrerequisites {{ return [pscustomobject]@{{ checks = @(); actions = @() }} }}
        function Get-MistyAIServiceState {{ return [pscustomobject]@{{ body = 'active'; operator = 'active'; body_enabled = 'enabled'; operator_enabled = 'enabled'; compose = 'running'; running = $true }} }}
        function Initialize-NeuralCloudEnvironment {{ return [pscustomobject]@{{ changed = $false }} }}
        function Deploy-MistyAIWorkspace {{ return [pscustomobject]@{{ changed = $false; fingerprint = 'test'; previous_available = $false }} }}
        function Initialize-MistyAIConfiguration {{}}
        function Read-MistyAIServiceToken {{ return 'synthetic-token' }}
        function Wait-MistyAIReady {{ return [pscustomobject]@{{ body_instance_id = 'test' }} }}
        $script:stops = 0
        function Start-NeuralCloud {{
            @{{ services = @(@{{ name = 'runtime'; pid = 43210; root = {literal(tmp_path)}; process_start_time_utc = [datetime]::UtcNow.ToString('o') }}) }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath {literal(tmp_path / '.neuralcloud.pids.json')}
            throw 'Shared registry already exists; rejected by lower launcher'
        }}
        function Stop-NeuralCloud {{ $script:stops++; throw 'must not stop someone else' }}
        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{ state = {literal(tmp_path / 'state.json')}; staging = {literal(tmp_path / 'staging')} }}
        $message = ''
        try {{ Invoke-LauncherUpCommand -Root {literal(tmp_path)} -MistyAIPath {literal(tmp_path / 'mistyai')} -Distro 'Ubuntu-24.04' -State $state -Paths $paths -NoOpen -CommandInvoker {{ throw 'external call forbidden' }} | Out-Null }} catch {{ $message = $_.Exception.Message }}
        [pscustomobject]@{{ message = $message; stops = $script:stops; owned = $state.owned.neuralcloud }}
    """)
    assert "rejected by lower launcher" in result["message"]
    assert result["stops"] == 0
    assert result["owned"] is False
    assert (tmp_path / ".neuralcloud.pids.json").exists()


def test_rollback_passes_only_new_processes_to_identity_checked_stop(tmp_path: Path):
    result = invoke(tmp_path, f"""
        $old = [pscustomobject]@{{ name = 'runtime'; pid = 12345; root = {literal(tmp_path)}; process_start_time_utc = '2026-01-01T00:00:00Z' }}
        $new = [pscustomobject]@{{ name = 'gui'; pid = 54321; root = {literal(tmp_path)}; process_start_time_utc = [datetime]::UtcNow.ToString('o') }}
        $unknown = [pscustomobject]@{{ name = 'homeostasis'; pid = 98765; root = {literal(tmp_path)}; process_start_time_utc = '' }}
        @{{ schema_version = 'neuralcloud-processes-v2'; services = @($old, $new, $unknown) }} | ConvertTo-Json -Depth 6 | Set-Content -LiteralPath {literal(tmp_path / '.neuralcloud.pids.json')}
        $script:stopped = @()
        function Stop-NeuralCloud {{
            param($Root, $StateRoot, $CommandInvoker)
            $script:stopped = @((Get-Content -LiteralPath (Join-Path $StateRoot '.neuralcloud.pids.json') -Raw | ConvertFrom-Json).services)
            return [pscustomobject]@{{ success = $true; output = '' }}
        }}
        $cleanup = Stop-LauncherNewNeuralCloudProcesses -Root {literal(tmp_path)} -BeforeEntries @($old) -StartedAt ([datetime]::UtcNow.AddSeconds(-5))
        [pscustomobject]@{{ cleanup = $cleanup; stopped = $script:stopped; remaining = @(Get-NeuralCloudRegistryEntries -Root {literal(tmp_path)}) }}
    """)
    assert result["cleanup"]["success"] is True
    assert [entry["pid"] for entry in result["stopped"]] == [54321]
    assert [entry["pid"] for entry in result["remaining"]] == [12345, 98765]


@pytest.mark.parametrize("auto_start", [True, False])
def test_prerequisites_optionally_start_docker_before_install(tmp_path: Path, auto_start: bool):
    result = invoke(tmp_path, f"""
        $script:dockerStarts = 0
        function Get-LauncherPrerequisiteChecks {{
            New-LauncherCheck -Id 'windows.docker_desktop' -Current 'installed' -Required 'running' -Status $(if ($script:dockerStarts) {{ 'pass' }} else {{ 'action_required' }})
        }}
        function Start-LauncherDockerDesktop {{
            param($Root, $Distro, $TimeoutSeconds, $CommandInvoker)
            $script:dockerStarts++
            return [pscustomobject]@{{ id = 'windows.docker_desktop'; status = 'started'; detail = 'mock' }}
        }}
        function Invoke-LauncherInstallPrerequisites {{ return [pscustomobject]@{{ actions = @(); restart_required = $false }} }}
        $payload = $null; $message = ''
        try {{ $payload = Assert-LauncherPrerequisites -Root {literal(tmp_path)} -MistyAIPath {literal(tmp_path / 'mistyai')} -Distro 'Ubuntu-24.04' -AutoStartDocker:${str(auto_start).lower()} -Json -CommandInvoker {{ throw 'external call forbidden' }} }} catch {{ $message = $_.Exception.Message }}
        [pscustomobject]@{{ payload = $payload; message = $message; starts = $script:dockerStarts }}
    """)
    assert result["starts"] == int(auto_start)
    if auto_start:
        assert result["payload"]["actions"][0]["status"] == "started"
    else:
        assert "Prerequisites are not ready" in result["message"]


def test_docker_start_helper_uses_bounded_safe_background_entrypoint(tmp_path: Path):
    helper = tmp_path / "tools" / "docker" / "Start-DockerDesktop.ps1"
    helper.parent.mkdir(parents=True)
    helper.write_text("# test placeholder: never executed\n", encoding="utf-8")
    result = invoke(tmp_path, f"""
        $script:request = $null
        $action = Start-LauncherDockerDesktop -Root {literal(tmp_path)} -Distro 'Ubuntu-24.04' -TimeoutSeconds 15 -CommandInvoker {{
            param($request)
            $script:request = $request
            return @{{ exit_code = 0; stdout = '{{"message":"mock engine ready"}}'; stderr = '' }}
        }}
        [pscustomobject]@{{ action = $action; request = $script:request }}
    """)
    assert result["action"]["status"] == "started"
    assert result["request"]["TimeoutSeconds"] == 145
    arguments = result["request"]["ArgumentList"]
    assert str(helper) in arguments
    assert "-WslDistro" in arguments and "Ubuntu-24.04" in arguments
    assert "-RefreshRuntimeOnColdStart" not in arguments
    assert not any("shutdown" in argument.lower() or "winget" in argument.lower() for argument in arguments)


def test_doctor_never_starts_docker(tmp_path: Path):
    result = invoke(tmp_path, f"""
        function Get-LauncherPrerequisiteChecks {{
            New-LauncherCheck -Id 'windows.docker_desktop' -Current 'installed' -Required 'running' -Status 'action_required'
        }}
        function Start-LauncherDockerDesktop {{ throw 'doctor must remain read-only' }}
        Invoke-LauncherDoctorCommand -Root {literal(tmp_path)} -MistyAIPath {literal(tmp_path / 'mistyai')} -Distro 'Ubuntu-24.04' -CommandInvoker {{ throw 'external call forbidden' }}
    """)
    assert result["exit_code"] == 10


def test_status_never_starts_docker(tmp_path: Path):
    result = invoke(tmp_path, f"""
        function Test-NeuralCloudRegistryHealth {{ return [pscustomobject]@{{ running = $false; services = @() }} }}
        function Get-MistyAIServiceState {{ return [pscustomobject]@{{ body = 'inactive'; operator = 'inactive'; compose = 'stopped'; running = $false }} }}
        function Start-LauncherDockerDesktop {{ throw 'status must remain read-only' }}
        Invoke-LauncherStatusCommand -Root {literal(tmp_path)} -Distro 'Ubuntu-24.04' -CommandInvoker {{ throw 'external call forbidden' }}
    """)
    assert result["status"] == "stopped"
    assert result["exit_code"] == 0


@pytest.fixture(params=["pwsh", "powershell.exe"])
def output_shell(request):
    executable = shutil.which(request.param)
    if executable is None:
        pytest.skip(f"{request.param} is unavailable")
    return executable


@pytest.mark.skipif(os.name != "nt", reason="Windows process handle inheritance")
def test_file_backed_start_returns_while_own_background_child_is_alive(tmp_path: Path, output_shell: str):
    """The child deliberately inherits both output handles; EOF must not be required."""
    parent = tmp_path / "parent.py"
    child_pid_path = tmp_path / "child.pid"
    base_python = Path(sys._base_executable)
    marker = "neuralcloud-output-test-" + uuid.uuid4().hex
    parent.write_text(
        "import os, subprocess, sys\n"
        "from pathlib import Path\n"
        f"child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(25)', {marker!r}], close_fds=False)\n"
        f"Path({str(child_pid_path)!r}).write_text(str(child.pid))\n"
        "print('parent-ready', flush=True)\n"
        "print('parent-stderr', file=sys.stderr, flush=True)\n"
        "print(os.environ.get('NEURALCLOUD_OUTPUT_TEST', '<missing>'), flush=True)\n"
        "print(os.environ.get('NEURALCLOUD_OUTPUT_ABSENT', '<missing>'), flush=True)\n",
        encoding="utf-8",
    )
    try:
        result = invoke(tmp_path, f"""
            [Environment]::SetEnvironmentVariable('NEURALCLOUD_OUTPUT_TEST', 'original-value', 'Process')
            [Environment]::SetEnvironmentVariable('NEURALCLOUD_OUTPUT_ABSENT', $null, 'Process')
            $watch = [Diagnostics.Stopwatch]::StartNew()
            $probe = Invoke-LauncherExternalCommand -FilePath {literal(base_python)} -ArgumentList @({literal(parent)}) `
                -FileBackedOutput -TimeoutSeconds 5 -Environment @{{ NEURALCLOUD_OUTPUT_TEST = 'child-value'; NEURALCLOUD_OUTPUT_ABSENT = 'temporary-value' }}
            $elapsed = $watch.Elapsed.TotalSeconds
            $testChild = [int](Get-Content -LiteralPath {literal(child_pid_path)})
            $result = [pscustomobject]@{{
                probe = $probe; elapsed = $elapsed
                child_alive = $null -ne (Get-Process -Id $testChild -ErrorAction SilentlyContinue)
                restored = [Environment]::GetEnvironmentVariable('NEURALCLOUD_OUTPUT_TEST', 'Process')
                restored_absent = [Environment]::GetEnvironmentVariable('NEURALCLOUD_OUTPUT_ABSENT', 'Process')
            }}
            # The test deliberately inherits every handle, including the test
            # runner's own capture pipes. Stop our marked child after recording
            # that the helper returned while it was alive, before runner exit.
            $identity = Get-CimInstance Win32_Process -Filter "ProcessId=$testChild"
            if ($identity -and $identity.CommandLine.Contains({literal(marker)})) {{
                Stop-Process -Id $testChild -Force -ErrorAction SilentlyContinue
            }}
            $result
        """, shell=output_shell)
        assert result["probe"]["exit_code"] == 0, result["probe"]
        assert "parent-ready" in result["probe"]["stdout"]
        assert "child-value" in result["probe"]["stdout"]
        assert "temporary-value" in result["probe"]["stdout"]
        assert "parent-stderr" in result["probe"]["stderr"]
        assert result["elapsed"] < 5
        assert result["child_alive"] is True
        assert result["restored"] == "original-value"
        assert result["restored_absent"] in (None, "")
    finally:
        # Clean up only this test's child, even when a regression times out the runner.
        if child_pid_path.exists():
            child_pid = int(child_pid_path.read_text())
            cleanup = (
                f"$child = Get-CimInstance Win32_Process -Filter 'ProcessId={child_pid}'; "
                f"if ($child -and $child.CommandLine.Contains({literal(marker)})) "
                f"{{ Stop-Process -Id {child_pid} -Force -ErrorAction SilentlyContinue }}"
            )
            subprocess.run([str(POWERSHELL), "-NoProfile", "-Command", cleanup],
                           capture_output=True, timeout=10, check=False)


@pytest.mark.skipif(os.name != "nt", reason="Windows file-backed process output")
@pytest.mark.parametrize("exit_code", [0, 7])
def test_file_backed_output_preserves_exit_code(tmp_path: Path, exit_code: int, output_shell: str):
    program = tmp_path / "exit.py"
    program.write_text(f"print('exit probe', flush=True)\nraise SystemExit({exit_code})\n", encoding="utf-8")
    result = invoke(tmp_path, f"""
        Invoke-LauncherExternalCommand -FilePath {literal(Path(sys._base_executable))} -ArgumentList @({literal(program)}) -FileBackedOutput -TimeoutSeconds 5
    """, shell=output_shell)
    assert result["exit_code"] == exit_code
    assert "exit probe" in result["stdout"]


def test_neural_start_requests_file_backed_output_with_mock_compatibility(tmp_path: Path):
    result = invoke(tmp_path, f"""
        $script:captured = $null
        $start = Start-NeuralCloud -Root {literal(tmp_path)} -Mode demo -Token 'synthetic-token' -CommandInvoker {{
            param($request)
            $script:captured = $request
            return @{{ exit_code = 0; stdout = 'ready'; stderr = '' }}
        }}
        [pscustomobject]@{{ start = $start; request = $script:captured }}
    """)
    assert result["request"]["FileBackedOutput"] is True
    assert result["start"]["output"] == "ready"
