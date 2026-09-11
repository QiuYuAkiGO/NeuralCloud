"""Native PS5.1 launcher checks with synthetic private files and mocked effects.

No real QQ session, Docker/WSL operation, HTTP endpoint, or model is invoked.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools" / "qq" / "Start-QQSession.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="native Windows PowerShell is required")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def launch_case(tmp_path):
    local = tmp_path / "Local App Data"
    private = local / "NeuralCloud" / "QQDesktop"
    private.mkdir(parents=True)
    config = private / "session.json"
    token = private / "session.token"
    database = private / "memory.sqlite3"
    token.write_text("synthetic-secret-" + "x" * 32, encoding="utf-8")
    database.write_text("synthetic launcher fixture; not opened as SQLite", encoding="utf-8")
    config.write_text(json.dumps({
        "token_file": str(token), "memory_db": str(database), "allowed_group_ids": ["10001", "10002"],
        "self_member_id": "20001", "decision_enabled": True,
    }), encoding="utf-8")

    def invoke(body, *, timeout=10):
        runner = tmp_path / "case.ps1"
        source = f"""
$ErrorActionPreference = 'Stop'
$env:LOCALAPPDATA = {ps_literal(local)}
. {ps_literal(SCRIPT)}
$script:Config = {ps_literal(config)}
$script:Starts = 0
$script:HealthCalls = 0
$script:Stops = 0
function Stop-Process {{ $script:Stops++; throw 'must not stop a process' }}
function Start-QQSProcess {{
    param($PythonPath, $ConfigPath, $Port, $StdoutPath, $StderrPath)
    $script:Starts++
    $script:Spawn = @{{python=$PythonPath; config=$ConfigPath; port=$Port; stdout=$StdoutPath; stderr=$StderrPath}}
    return [pscustomobject]@{{Id=424242; HasExited=$false}}
}}
function Get-QQSListenerPid {{ param($Port) return 434343 }}
function Test-QQSPortListening {{ param($Port) return $false }}
function Start-Sleep {{ param($Milliseconds) }}
function New-TestHealth {{
    return [pscustomobject]@{{
        service='qq-memory-session-v1'; status='healthy'; persistent_memory=$true
        memory_db={ps_literal(database)}; allowed_group_ids=@('10002','10001'); self_member_id='20001'
        autonomous_decision_enabled=$true; automatic_send=$false; automatic_screen_capture=$false
        standard_runtime_started_by_this_service=$false; started_at='2026-09-11T00:00:00+00:00'
    }}
}}
function Get-QQSHealth {{ param($Port, $Token, $TimeoutMilliseconds) $script:HealthCalls++; return (New-TestHealth) }}
{body}
"""
        runner.write_text(source, encoding="utf-8-sig")
        result = subprocess.run(
            [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
            cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        assert result.returncode == 0, result.stderr
        assert "synthetic-secret" not in result.stdout + result.stderr
        return json.loads(result.stdout)

    return invoke, private, config, token


def test_native_ps51_existing_matching_service_is_idempotent(launch_case):
    invoke, private, _, _ = launch_case
    result = invoke("""
$before = (Get-Location).Path
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{result=$result; starts=$script:Starts; stops=$script:Stops; ps=$PSVersionTable.PSVersion.Major;
 location_unchanged=((Get-Location).Path -eq $before)} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["ps"] == 5
    assert result["result"]["status"] == "already_running"
    assert result["result"]["exit_code"] == 0
    assert result["result"]["listener_pid"] == 434343
    assert result["starts"] == result["stops"] == 0
    assert result["location_unchanged"]
    assert not (private / "logs").exists()


@pytest.mark.parametrize("change", [
    "$health.memory_db = 'C:\\different\\memory.sqlite3'",
    "$health.self_member_id = '99999'",
    "$health.allowed_group_ids = @('10001')",
    "$health.autonomous_decision_enabled = $false",
    "$health.automatic_send = 'true'",
    "$health.service = 'other-service'",
    "$health.status = 'unhealthy'",
])
def test_occupied_port_never_restarts_a_different_or_misconfigured_service(launch_case, change):
    invoke, _, _, _ = launch_case
    result = invoke(f"""
function Get-QQSHealth {{ param($Port, $Token, $TimeoutMilliseconds) $health = New-TestHealth; {change}; return $health }}
function Test-QQSPortListening {{ param($Port) return $true }}
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{{result=$result; starts=$script:Starts; stops=$script:Stops}} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20
    assert result["result"]["reason"] == "port_occupied_by_unverified_or_different_service"
    assert result["starts"] == result["stops"] == 0


def test_unauthenticated_occupant_is_not_stopped(launch_case):
    invoke, _, _, _ = launch_case
    result = invoke("""
function Get-QQSHealth { param($Port, $Token, $TimeoutMilliseconds) return $null }
function Test-QQSPortListening { param($Port) return $true }
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{result=$result; starts=$script:Starts; stops=$script:Stops} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20
    assert result["starts"] == result["stops"] == 0


def test_cold_launch_waits_for_authenticated_matching_health_once(launch_case):
    invoke, private, config, _ = launch_case
    result = invoke("""
function Get-QQSHealth {
    param($Port, $Token, $TimeoutMilliseconds)
    $script:HealthCalls++
    if ($script:HealthCalls -lt 3) { return $null }
    return (New-TestHealth)
}
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{result=$result; starts=$script:Starts; health_calls=$script:HealthCalls; spawn=$script:Spawn} |
 ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["status"] == "started"
    assert result["result"]["launch_pid"] == 424242
    assert result["starts"] == 1 and result["health_calls"] == 3
    assert Path(result["spawn"]["config"]) == config
    assert Path(result["spawn"]["python"]) == ROOT / ".venv" / "Scripts" / "python.exe"
    assert Path(result["spawn"]["stdout"]).parent == private / "logs"
    assert result["spawn"]["stdout"] != result["spawn"]["stderr"]


def test_unhealthy_launch_has_bounded_deadline_without_stopping_processes(launch_case):
    invoke, _, _, _ = launch_case
    result = invoke("""
function Get-QQSHealth { param($Port, $Token, $TimeoutMilliseconds) return $null }
$timer = [Diagnostics.Stopwatch]::StartNew()
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789 -TimeoutSeconds 0.5
@{result=$result; starts=$script:Starts; stops=$script:Stops; elapsed=$timer.Elapsed.TotalSeconds} |
 ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20
    assert result["result"]["reason"] == "authenticated_health_deadline_reached"
    assert result["starts"] == 1 and result["stops"] == 0
    assert result["elapsed"] < 2


def test_invalid_private_references_and_token_never_launch(launch_case):
    invoke, _, config, token = launch_case
    token.write_text("synthetic-short", encoding="utf-8")
    result = invoke("""
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{result=$result; starts=$script:Starts; health_calls=$script:HealthCalls} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["reason"] == "configuration_or_launch_error"
    assert result["starts"] == result["health_calls"] == 0
    token.write_text("synthetic-secret-" + "x" * 32, encoding="utf-8")
    data = json.loads(config.read_text(encoding="utf-8"))
    data["memory_db"] = str(ROOT / "README.md")
    config.write_text(json.dumps(data), encoding="utf-8")
    result = invoke("""
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{result=$result; starts=$script:Starts; health_calls=$script:HealthCalls} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20
    assert result["starts"] == result["health_calls"] == 0


def test_reparse_component_is_rejected_before_network_or_launch(launch_case):
    invoke, _, _, _ = launch_case
    result = invoke("""
function Get-Item {
 param($LiteralPath, [switch]$Force, $ErrorAction)
 if ($LiteralPath.EndsWith('NeuralCloud')) { return [pscustomobject]@{Attributes=[IO.FileAttributes]::ReparsePoint} }
 return Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath -Force
}
$result = Invoke-QQSessionStart -ConfigPath $script:Config -Port 28789
@{result=$result; starts=$script:Starts; health_calls=$script:HealthCalls} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20
    assert result["starts"] == result["health_calls"] == 0


def test_start_process_hidden_quoted_and_does_not_change_parent_environment(tmp_path):
    runner = tmp_path / "hidden.ps1"
    runner.write_text(f"""
$ErrorActionPreference = 'Stop'
. {ps_literal(SCRIPT)}
$before = (Get-Location).Path
$env:QQ_LAUNCHER_SYNTHETIC = 'unchanged'
function Start-Process {{
 param($FilePath, $ArgumentList, $WorkingDirectory, $WindowStyle, [switch]$PassThru,
       $RedirectStandardOutput, $RedirectStandardError, $ErrorAction)
 return [pscustomobject]@{{file=$FilePath; argv=$ArgumentList; cwd=$WorkingDirectory; style=$WindowStyle;
                         stdout=$RedirectStandardOutput; stderr=$RedirectStandardError}}
}}
$result = Start-QQSProcess -PythonPath 'C:\\repo with spaces\\python.exe' -ConfigPath 'C:\\private space\\session.json' `
 -Port 28789 -StdoutPath 'C:\\private\\out.log' -StderrPath 'C:\\private\\err.log'
@{{result=$result; location_unchanged=((Get-Location).Path -eq $before); env_unchanged=($env:QQ_LAUNCHER_SYNTHETIC -eq 'unchanged')}} |
 ConvertTo-Json -Depth 5 -Compress
""", encoding="utf-8-sig")
    result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(runner)],
                            cwd=ROOT, capture_output=True, encoding="utf-8", timeout=10)
    assert result.returncode == 0, result.stderr
    data = json.loads(result.stdout)
    assert data["result"]["style"] == "Hidden"
    assert data["result"]["cwd"] == str(ROOT)
    assert '"C:\\private space\\session.json"' in data["result"]["argv"]
    assert '"--port" "28789"' in data["result"]["argv"]
    assert "Bearer" not in data["result"]["argv"]
    assert data["location_unchanged"] and data["env_unchanged"]
