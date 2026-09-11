"""Native PS5.1 checks using synthetic private files and simulated workers only.

These tests never start a watcher, access QQ, read a real token or call a model.
"""
from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
START = ROOT / "tools" / "qq" / "Start-QQWatcher.ps1"
STOP = ROOT / "tools" / "qq" / "Stop-QQWatcher.ps1"
POWERSHELL = shutil.which("powershell.exe")
PWSH = shutil.which("pwsh.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="native Windows PowerShell is required")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def watcher_case(tmp_path):
    local = tmp_path / "Local App Data"
    private = local / "NeuralCloud" / "QQDesktop"
    private.mkdir(parents=True)
    config = private / "nondefault watcher.json"
    values = {
        "session_config_path": str(private / "session.json"),
        "state_db": str(private / "watcher.sqlite3"),
        "status_file": str(private / "watcher.status.json"),
        "control_file": str(private / "watcher.control.json"),
        "body_token_file": str(private / "body.token"),
        "agent_token_file": str(private / "agent.token"),
        "body_url": "http://127.0.0.1:28788", "agent_url": "http://127.0.0.1:28766",
        "expected_instance_id": "synthetic-instance", "expected_revision": 2,
        "group_id": "10001", "self_member_id": "20001", "vision_model": "synthetic-vision",
        "poll_seconds": 15, "cooldown_seconds": 60, "automatic_send": True,
    }
    for field in ("session_config_path", "body_token_file", "agent_token_file"):
        Path(values[field]).write_text("synthetic-secret-do-not-read", encoding="utf-8")
    config.write_text(json.dumps(values), encoding="utf-8")

    def invoke(body, *, stop=False, timeout=15, shell=None):
        runner = tmp_path / "runner.ps1"
        runner.write_text(f"""
$ErrorActionPreference = 'Stop'
$env:LOCALAPPDATA = {ps_literal(local)}
. {ps_literal(STOP if stop else START)} -ConfigPath {ps_literal(config)} -TimeoutSeconds 17
$script:PassedConfig = $ConfigPath
$script:PassedTimeout = $TimeoutSeconds
$script:Config = {ps_literal(config)}
$script:StatusFile = {ps_literal(values['status_file'])}
$script:ControlFile = {ps_literal(values['control_file'])}
$script:Database = {ps_literal(values['state_db'])}
$script:Python = 'C:\\synthetic venv\\python.exe'
$script:BasePython = 'C:\\synthetic base\\python.exe'
$script:Started = [DateTime]::UtcNow.AddSeconds(-2)
$script:Starts = 0
$script:Stops = 0
$script:Probes = 0
$script:TestMutex = 'Local\\NeuralCloudQQWatcherTest-' + [Guid]::NewGuid().ToString('N')
function New-QQWLauncherMutex {{ return New-Object Threading.Mutex($false, $script:TestMutex) }}
function Get-QQWInterpreterPaths {{ return @($script:Python, $script:BasePython) }}
function Stop-Process {{ $script:Stops++; throw 'must not stop a process' }}
function Start-Sleep {{ param($Milliseconds) }}
function New-TestProcess {{
    return [pscustomobject]@{{ProcessId=434343; ExecutablePath=$script:BasePython;
        CreatedAt=$script:Started.AddSeconds(-1);
        Arguments=@($script:Python,'-m','harness.qq.watcher','--config',$script:Config)}}
}}
function Get-QQWProcesses {{ $script:Probes++; return (New-TestProcess) }}
function New-TestStatus {{
    return [pscustomobject]@{{service='qq-desktop-watcher-v1'; pid=434343;
        started_at=$script:Started.ToString('o'); heartbeat_at=[DateTime]::UtcNow.ToString('o');
        running=$true; automatic_screen_capture=$true; automatic_send=$true; state='watching';
        config_path=$script:Config; state_db=$script:Database; group_id='10001'; self_member_id='20001'}}
}}
function Write-TestStatus {{
    param($Value)
    [IO.File]::WriteAllText($script:StatusFile, ($Value | ConvertTo-Json -Compress))
}}
function Start-QQWProcess {{
    param($PythonPath, $ConfigPath, $StdoutPath, $StderrPath)
    $script:Starts++
    $script:Spawn = @{{python=$PythonPath; config=$ConfigPath; stdout=$StdoutPath; stderr=$StderrPath}}
    return [pscustomobject]@{{Id=424242}}
}}
Write-TestStatus (New-TestStatus)
{body}
""", encoding="utf-8-sig")
        completed = subprocess.run(
            [shell or POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
            cwd=ROOT, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout,
        )
        assert completed.returncode == 0, completed.stderr
        assert "synthetic-secret" not in completed.stdout + completed.stderr
        return json.loads(completed.stdout)

    return invoke, private, config, values


def test_existing_matching_worker_is_idempotent_and_ps51(watcher_case):
    invoke, private, config, _ = watcher_case
    value = invoke("""
$before = (Get-Location).Path
$result = Invoke-QQWatcherStart $script:Config
@{result=$result; starts=$script:Starts; stops=$script:Stops; ps=$PSVersionTable.PSVersion.Major;
 unchanged=((Get-Location).Path -eq $before)} | ConvertTo-Json -Compress
""")
    assert value["result"]["status"] == "already_running"
    assert value["result"]["exit_code"] == 0
    assert value["starts"] == value["stops"] == 0
    assert value["ps"] == 5 and value["unchanged"]
    assert not (private / "logs").exists()
    assert config.exists()


@pytest.mark.parametrize("mutation", [
    "$status.heartbeat_at = [DateTime]::UtcNow.AddMinutes(-5).ToString('o')",
    "$status.heartbeat_at = [DateTime]::UtcNow.AddMinutes(5).ToString('o')",
    "$status.started_at = $script:Started.AddMinutes(-2).ToString('o')",
    "$status.heartbeat_at = '2026-09-11T10:00:00'",
    "$status.pid = 99999",
    "$status.group_id = '10002'",
    "$status.self_member_id = '20002'",
    "$status.config_path = 'C:\\other\\watcher.json'",
    "$status.state_db = 'C:\\other\\watcher.sqlite3'",
    "$status.service = 'other-service'",
    "$status.automatic_send = $false",
    "$status.running = 'true'",
    "$status.state = 'reading_initial_baseline'",
])
def test_stale_mismatched_or_unready_status_does_not_launch_duplicate(watcher_case, mutation):
    invoke, _, _, _ = watcher_case
    value = invoke(f"""
$status = New-TestStatus
{mutation}
Write-TestStatus $status
$result = Invoke-QQWatcherStart $script:Config
@{{result=$result; starts=$script:Starts; stops=$script:Stops}} | ConvertTo-Json -Compress
""")
    assert value["result"]["exit_code"] == 20
    assert value["result"]["reason"] == "existing_watcher_has_no_matching_fresh_running_status"
    assert value["starts"] == value["stops"] == 0


@pytest.mark.parametrize("mutation", [
    "$p.ExecutablePath = 'C:\\other\\python.exe'",
    "$p.Arguments[4] = 'C:\\other\\watcher.json'",
    "$p.Arguments += '--once'",
])
def test_process_identity_mismatch_never_restarts_or_kills(watcher_case, mutation):
    invoke, _, _, _ = watcher_case
    value = invoke(f"""
function Get-QQWProcesses {{ $p = New-TestProcess; {mutation}; return $p }}
$result = Invoke-QQWatcherStart $script:Config
@{{result=$result; starts=$script:Starts; stops=$script:Stops}} | ConvertTo-Json -Compress
""")
    assert value["result"]["exit_code"] == 20
    assert value["starts"] == value["stops"] == 0


def test_cold_start_accepts_verified_child_pid_and_waits_for_readiness(watcher_case):
    invoke, private, config, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses {
 $script:Probes++
 if ($script:Starts -eq 0) { return @() }
 $status = New-TestStatus
 if ($script:Probes -lt 3) { $status.state='starting'; $status.automatic_send=$false }
 Write-TestStatus $status
 return (New-TestProcess)
}
$result = Invoke-QQWatcherStart $script:Config -TimeoutSeconds 3
@{result=$result; starts=$script:Starts; probes=$script:Probes; spawn=$script:Spawn;
 control=(Read-QQWJson $script:ControlFile)} | ConvertTo-Json -Depth 5 -Compress
""")
    assert value["result"]["status"] == "started"
    assert value["result"]["launch_pid"] == 424242
    assert value["starts"] == 1 and value["probes"] >= 3
    assert Path(value["spawn"]["config"]) == config
    assert Path(value["spawn"]["stdout"]).parent == private / "logs"
    assert value["spawn"]["stdout"] != value["spawn"]["stderr"]
    assert value["control"] == {"paused": False}


def test_start_timeout_requests_pause_and_never_kills(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses { return @() }
$clock = [Diagnostics.Stopwatch]::StartNew()
$result = Invoke-QQWatcherStart $script:Config -TimeoutSeconds 0.7
@{result=$result; starts=$script:Starts; stops=$script:Stops; elapsed=$clock.Elapsed.TotalSeconds;
 control=(Read-QQWJson $script:ControlFile)} | ConvertTo-Json -Depth 5 -Compress
""")
    assert value["result"]["reason"] == "startup_deadline_reached_pause_requested", value
    assert value["starts"] == 1 and value["stops"] == 0
    assert value["elapsed"] < 2
    assert value["control"] == {"paused": True}


def test_expired_preflight_does_not_start_a_worker(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses { [Threading.Thread]::Sleep(100); return @() }
$result = Invoke-QQWatcherStart $script:Config -TimeoutSeconds 0.05
@{result=$result; starts=$script:Starts} | ConvertTo-Json -Compress
""")
    assert value["result"]["reason"] == "startup_deadline_reached_before_launch"
    assert value["starts"] == 0


@pytest.mark.parametrize("field,value", [
    ("automatic_send", "true"), ("group_id", 10001), ("poll_seconds", 0),
    ("cooldown_seconds", 1), ("body_url", "http://example.invalid:8788"),
    ("control_file", "C:\\outside\\control.json"),
    ("initial_baseline_observation", "C:\\outside\\baseline.json"),
])
def test_invalid_private_config_never_probes_or_starts(watcher_case, field, value):
    invoke, _, config, values = watcher_case
    values[field] = value
    config.write_text(json.dumps(values), encoding="utf-8")
    result = invoke("""
$result = Invoke-QQWatcherStart $script:Config
@{result=$result; starts=$script:Starts; probes=$script:Probes} | ConvertTo-Json -Compress
""")
    assert result["result"]["exit_code"] == 20
    assert result["starts"] == result["probes"] == 0


def test_optional_private_baseline_validated_and_not_read(watcher_case):
    invoke, private, config, values = watcher_case
    baseline = private / "baseline.json"
    baseline.write_text("synthetic-secret-not-json", encoding="utf-8")
    values["initial_baseline_observation"] = str(baseline)
    config.write_text(json.dumps(values), encoding="utf-8")
    result = invoke("(Invoke-QQWatcherStart $script:Config) | ConvertTo-Json -Compress")
    assert result["status"] == "already_running"


@pytest.mark.parametrize("field,value", [
    ("accessibility_enabled", "true"), ("accessibility_enabled", 1),
    ("observation_interval_seconds", "1"), ("observation_interval_seconds", 0.1),
    ("observation_interval_seconds", 2.1), ("turn_deadline_seconds", True),
    ("turn_deadline_seconds", 61), ("trace_file", "relative.jsonl"),
    ("trace_file", "C:\\outside\\trace.jsonl"), ("trace_file", 123),
])
def test_observation_trace_configuration_rejected_before_process_probe(watcher_case, field, value):
    invoke, _, config, values = watcher_case
    values[field] = value
    config.write_text(json.dumps(values), encoding="utf-8")
    result = invoke("@{result=(Invoke-QQWatcherStart $script:Config); probes=$script:Probes; starts=$script:Starts} | ConvertTo-Json -Compress")
    assert result["result"]["exit_code"] == 20 and result["probes"] == result["starts"] == 0


def test_optional_observation_defaults_and_explicit_trace_match_runtime(watcher_case):
    invoke, private, config, values = watcher_case
    result = invoke("Read-QQWConfiguration $script:Config | ConvertTo-Json -Compress")
    assert Path(result["TraceFile"]) == private / "watcher-trace.jsonl"
    assert result["AccessibilityEnabled"] is False
    trace = private / "private-trace.jsonl"
    trace.write_text("synthetic-secret-not-json", encoding="utf-8")
    values.update(trace_file=str(trace), accessibility_enabled=True, observation_interval_seconds=1, turn_deadline_seconds=60)
    config.write_text(json.dumps(values), encoding="utf-8")
    result = invoke("Read-QQWConfiguration $script:Config | ConvertTo-Json -Compress")
    assert Path(result["TraceFile"]) == trace and result["AccessibilityEnabled"] is True


@pytest.mark.parametrize("target", ["body_token_file", "agent_token_file", "state_db", "status_file", "control_file", "session_config_path"])
def test_trace_never_overlaps_tokens_configuration_or_outputs(watcher_case, target):
    invoke, _, config, values = watcher_case
    values["trace_file"] = values[target]
    config.write_text(json.dumps(values), encoding="utf-8")
    result = invoke("(Invoke-QQWatcherStart $script:Config) | ConvertTo-Json -Compress")
    assert result["exit_code"] == 20


def test_reparse_path_and_overlapping_output_rejected(watcher_case):
    invoke, _, config, values = watcher_case
    result = invoke("""
function Get-Item {
 param($LiteralPath, [switch]$Force, $ErrorAction)
 if ($LiteralPath.EndsWith('NeuralCloud')) { return [pscustomobject]@{Attributes=[IO.FileAttributes]::ReparsePoint} }
 return Microsoft.PowerShell.Management\\Get-Item -LiteralPath $LiteralPath -Force
}
@{result=(Invoke-QQWatcherStart $script:Config); starts=$script:Starts} | ConvertTo-Json -Compress
""")
    assert result["result"]["exit_code"] == 20 and result["starts"] == 0
    values["control_file"] = values["body_token_file"]
    config.write_text(json.dumps(values), encoding="utf-8")
    result = invoke("(Invoke-QQWatcherStart $script:Config) | ConvertTo-Json -Compress")
    assert result["exit_code"] == 20


def test_pause_waits_for_verified_acknowledgement_and_preserves_script_arguments(watcher_case):
    invoke, _, config, _ = watcher_case
    value = invoke("""
$status = New-TestStatus
$status.running=$false; $status.state='paused'; $status.automatic_send=$false; $status.automatic_screen_capture=$false
Write-TestStatus $status
$result = Invoke-QQWatcherStop $script:Config
@{result=$result; control=(Read-QQWJson $script:ControlFile); stops=$script:Stops;
 config=$script:PassedConfig; timeout=$script:PassedTimeout} | ConvertTo-Json -Depth 4 -Compress
""", stop=True)
    assert value["result"]["status"] == "paused"
    assert value["result"]["exit_code"] == 0 and value["stops"] == 0
    assert value["control"] == {"paused": True}
    assert Path(value["config"]) == config and value["timeout"] == 17


def test_pause_observes_process_exit_without_killing(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses { $script:Probes++; if ($script:Probes -eq 1) { return (New-TestProcess) }; return @() }
$result = Invoke-QQWatcherStop $script:Config -TimeoutSeconds 2
@{result=$result; stops=$script:Stops; probes=$script:Probes} | ConvertTo-Json -Compress
""", stop=True)
    assert value["result"]["status"] == "stopped"
    assert value["stops"] == 0 and value["probes"] == 2


def test_pause_timeout_is_not_reported_as_stopped(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
$result = Invoke-QQWatcherStop $script:Config -TimeoutSeconds 0.3
@{result=$result; stops=$script:Stops; control=(Read-QQWJson $script:ControlFile)} | ConvertTo-Json -Depth 4 -Compress
""", stop=True)
    assert value["result"]["status"] == "pause_requested"
    assert value["result"]["exit_code"] == 10 and value["stops"] == 0
    assert value["control"] == {"paused": True}


def test_unknown_executable_is_neither_killed_nor_reported_stopped(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses { $p = New-TestProcess; $p.ExecutablePath='C:\\other\\python.exe'; return $p }
$result = Invoke-QQWatcherStop $script:Config
@{result=$result; stops=$script:Stops} | ConvertTo-Json -Compress
""", stop=True)
    assert value["result"]["reason"] == "matching_config_has_unverified_process_identity"
    assert value["result"]["exit_code"] == 10 and value["stops"] == 0


@pytest.mark.parametrize("arguments", [
    "@($script:Python,'-m','harness.qq.watcher','--config',$script:Config,'--once')",
    "@($script:Python,'-m','harness.qq.watcher','--once',('--config='+$script:Config))",
])
def test_manual_once_worker_is_not_reported_stopped(watcher_case, arguments):
    invoke, _, _, _ = watcher_case
    value = invoke(f"""
function Get-QQWProcesses {{ $p=New-TestProcess; $p.Arguments={arguments}; return $p }}
$result=Invoke-QQWatcherStop $script:Config
@{{result=$result; stops=$script:Stops; control=(Read-QQWJson $script:ControlFile)}} | ConvertTo-Json -Depth 4 -Compress
""", stop=True)
    assert value["result"]["status"] == "pause_requested"
    assert value["result"]["exit_code"] == 10 and value["stops"] == 0
    assert value["control"] == {"paused": True}


def test_unrelated_config_process_is_left_alone_by_stop(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses { $p=New-TestProcess; $p.Arguments[4]='C:\\other\\watcher.json'; return $p }
$result=Invoke-QQWatcherStop $script:Config
@{result=$result; stops=$script:Stops} | ConvertTo-Json -Compress
""", stop=True)
    assert value["result"]["status"] == "stopped" and value["stops"] == 0


def test_native_argv_roundtrip_hidden_launch_and_narrow_process_inventory(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke(f"""
. {ps_literal(START)}
function Start-Process {{
 param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru,
       $RedirectStandardOutput,$RedirectStandardError,$ErrorAction)
 return [pscustomobject]@{{file=$FilePath; args=$ArgumentList; cwd=$WorkingDirectory; style=$WindowStyle}}
}}
function Get-CimInstance {{
 param($ClassName,$Filter,$OperationTimeoutSec,$ErrorAction)
 $script:Filter=$Filter; $script:ProbeTimeout=$OperationTimeoutSec
 return [pscustomobject]@{{ProcessId=434343;ExecutablePath=$script:BasePython;CreationDate=$script:Started;
  CommandLine=((ConvertTo-QQWArgument $script:Python)+' '+$script:Launched.args)}}
}}
$script:Launched=Start-QQWProcess $script:Python $script:Config 'C:\\out.log' 'C:\\err.log'
$found=@(Get-QQWProcesses)
@{{launch=$script:Launched; argv=$found[0].Arguments; filter=$script:Filter; timeout=$script:ProbeTimeout}} |
 ConvertTo-Json -Depth 5 -Compress
""")
    assert value["launch"]["style"] == "Hidden"
    assert value["argv"][1:4] == ["-m", "harness.qq.watcher", "--config"]
    assert Path(value["argv"][4]) == watcher_case[2]
    assert value["timeout"] == 2
    assert "CommandLine LIKE '%harness.qq.watcher%'" in value["filter"]


def test_broken_process_query_does_not_claim_readiness_or_stop(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses { throw 'synthetic inaccessible CIM' }
$start=Invoke-QQWatcherStart $script:Config
$stop=Invoke-QQWatcherStop $script:Config
@{start=$start; stop=$stop; starts=$script:Starts; stops=$script:Stops} | ConvertTo-Json -Depth 5 -Compress
""", stop=True)
    assert value["start"]["exit_code"] == value["stop"]["exit_code"] == 20
    assert value["starts"] == value["stops"] == 0


def test_pause_is_written_even_when_start_holds_launcher_mutex(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function New-QQWLauncherMutex {
 $mutex = [pscustomobject]@{}
 $mutex | Add-Member ScriptMethod WaitOne { param($Timeout) return $false }
 $mutex | Add-Member ScriptMethod Dispose { }
 $mutex | Add-Member ScriptMethod ReleaseMutex { throw 'not owned' }
 return $mutex
}
function Get-QQWProcesses {
 if ((Read-QQWJson $script:ControlFile).paused -ne $true) { throw 'pause must precede process probing' }
 return @()
}
$result=Invoke-QQWatcherStop $script:Config -TimeoutSeconds 0.3
@{result=$result; stops=$script:Stops; control=(Read-QQWJson $script:ControlFile)} | ConvertTo-Json -Depth 4 -Compress
""", stop=True)
    assert value["result"]["status"] == "pause_requested"
    assert value["result"]["exit_code"] == 10 and value["stops"] == 0
    assert value["control"] == {"paused": True}


def test_pause_precedes_interpreter_failure(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWInterpreterPaths { throw 'synthetic interpreter unavailable' }
$result=Invoke-QQWatcherStop $script:Config
@{result=$result; stops=$script:Stops; control=(Read-QQWJson $script:ControlFile)} | ConvertTo-Json -Depth 4 -Compress
""", stop=True)
    assert value["result"]["exit_code"] == 20 and value["stops"] == 0
    assert value["control"] == {"paused": True}


def test_start_wait_honors_pause_even_when_ready_status_arrives(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
function Get-QQWProcesses {
 if ($script:Starts -eq 0) { return @() }
 Set-QQWPaused (Read-QQWConfiguration $script:Config) $true
 return (New-TestProcess)
}
$result=Invoke-QQWatcherStart $script:Config -TimeoutSeconds 3
@{result=$result; starts=$script:Starts; stops=$script:Stops; control=(Read-QQWJson $script:ControlFile)} |
 ConvertTo-Json -Depth 4 -Compress
""")
    assert value["result"]["status"] == "pause_requested"
    assert value["result"]["reason"] == "startup_cancelled_by_pause_request"
    assert value["starts"] == 1 and value["stops"] == 0
    assert value["control"] == {"paused": True}


@pytest.mark.skipif(PWSH is None, reason="PowerShell 7 is required")
def test_pwsh_json_dates_preserve_timezone_and_recognize_live_worker(watcher_case):
    invoke, _, _, _ = watcher_case
    value = invoke("""
$status=Read-QQWJson $script:StatusFile
$result=Invoke-QQWatcherStart $script:Config
@{result=$result; ps=$PSVersionTable.PSVersion.Major; starts=$script:Starts; stops=$script:Stops;
 timestamp_type=$status.heartbeat_at.GetType().FullName; timestamp=[string]$status.heartbeat_at} |
 ConvertTo-Json -Depth 4 -Compress
""", shell=PWSH)
    assert value["ps"] == 7
    assert value["result"]["status"] == "already_running"
    assert value["starts"] == value["stops"] == 0
    assert value["timestamp_type"] == "System.String" and value["timestamp"].endswith("Z")


@pytest.mark.parametrize("has_timezone", [True, False])
def test_older_core_typed_dates_keep_timezone_requirement(watcher_case, has_timezone):
    invoke, _, _, _ = watcher_case
    assignment = (
        "$status.started_at=$script:Started; $status.heartbeat_at=[DateTime]::UtcNow"
        if has_timezone else
        "$status.started_at=[DateTime]::SpecifyKind($script:Started,[DateTimeKind]::Unspecified); "
        "$status.heartbeat_at=[DateTime]::SpecifyKind([DateTime]::UtcNow,[DateTimeKind]::Unspecified)"
    )
    value = invoke(f"""
$configuration=Read-QQWConfiguration $script:Config
$status=New-TestStatus
{assignment}
@{{match=(Test-QQWStatus $status @(Get-QQWProcesses) $configuration @(Get-QQWInterpreterPaths))}} |
 ConvertTo-Json -Compress
""")
    assert value["match"] is has_timezone
