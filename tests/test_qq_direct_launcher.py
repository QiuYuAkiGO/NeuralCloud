"""QQDirect launcher checks use synthetic files and never spawn/stop a daemon."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="native Windows PowerShell required")


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def direct_case(tmp_path):
    local = tmp_path / "Local App Data"
    private = local / "NeuralCloud" / "QQDirect"
    private.mkdir(parents=True)
    config = private / "custom direct.json"
    values = {key: str(private / name) for key, name in {
        "state_db": "direct.sqlite3", "status_file": "status.json", "control_file": "control.json",
        "token_file": "onebot.token", "evidence_dir": "evidence", "image_cache_dir": "images",
    }.items()}
    values.update(transport="onebot", self_member_id="20001", enabled=False)
    config.write_text(json.dumps(values), encoding="utf-8")

    def invoke(body, *, stop=False):
        script = ROOT / "tools" / "qq" / ("Stop-QQDirect.ps1" if stop else "Start-QQDirect.ps1")
        runner = tmp_path / "check.ps1"
        runner.write_text(f"""
$ErrorActionPreference='Stop'
$env:LOCALAPPDATA={literal(local)}
. {literal(script)} -ConfigPath {literal(config)} -TimeoutSeconds 17 -CheckOnly
$script:Configuration=Read-QQDConfiguration {literal(config)}
$script:Interpreters=@('C:\\fake venv\\python.exe', 'C:\\fake base\\python.exe')
$script:Source='qq-direct:' + ('a' * 64)
$script:Created=[datetime]::UtcNow.AddSeconds(-3)
$script:Process=[pscustomobject]@{{ProcessId=123456;ParentProcessId=999;ExecutablePath=$script:Interpreters[1]
 CreatedAt=$script:Created;Arguments=@($script:Interpreters[0],'-m','harness.qq.direct','--config',{literal(config)})}}
$script:Registry=@{{service='qq-direct-launch-v1';repository_root=$script:QQDRepositoryRoot;config_path={literal(config)}
 source_hash=$script:Source;launch_pid=123456;processes=@(@{{pid=123456;parent_pid=999;executable=$script:Interpreters[1];created_at=$script:Created.ToString('o')}})}}
$script:Registry=$script:Registry | ConvertTo-Json -Depth 8 | ConvertFrom-Json
$script:Status=@{{service='qq-direct-onebot-v1';source_hash=$script:Source;config_path={literal(config)}
 repository_root=$script:QQDRepositoryRoot;state_db=$script:Configuration.StateDatabase;self_member_id='20001'
 running=$true;pid=123456;started_at=$script:Created.AddSeconds(1).ToString('o');heartbeat_at=[datetime]::UtcNow.ToString('o')}}
$script:Starts=0;$script:Stops=0
function Get-QQWInterpreterPaths {{ return $script:Interpreters }}
function Get-QQDProcesses {{ return @() }}
function Start-Process {{ $script:Starts++; throw 'real_start_forbidden' }}
function Stop-Process {{ $script:Stops++; throw 'real_stop_forbidden' }}
{body}
""", encoding="utf-8-sig")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
            capture_output=True, text=True, timeout=20)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout.strip().splitlines()[-1])

    return invoke, private


def test_start_check_only_preserves_arguments_and_does_not_write_control(direct_case):
    invoke, private = direct_case
    result = invoke("""
$r=Invoke-QQDirectStart -ConfigPath $ConfigPath -TimeoutSeconds $TimeoutSeconds -CheckOnly
@{state=$r.state;timeout=$TimeoutSeconds;config_ok=($ConfigPath -like '*custom direct.json');starts=$script:Starts;stops=$script:Stops} | ConvertTo-Json -Compress
""")
    assert result == {"state": "checked", "timeout": 17, "config_ok": True, "starts": 0, "stops": 0}
    assert not (private / "control.json").exists()


def test_stop_check_only_never_writes_control_or_stops_process(direct_case):
    invoke, private = direct_case
    result = invoke("""
$r=Invoke-QQDirectStop -ConfigPath $ConfigPath -CheckOnly
@{state=$r.state;starts=$script:Starts;stops=$script:Stops} | ConvertTo-Json -Compress
""", stop=True)
    assert result == {"state": "checked", "starts": 0, "stops": 0}
    assert not (private / "control.json").exists()


def test_start_control_preserves_pause_and_stop_requests_both_flags(direct_case):
    invoke, private = direct_case
    result = invoke("""
Set-QQDControl $script:Configuration $false
$a=Read-QQWJson $script:Configuration.ControlFile
Write-QQDJson $script:Configuration.ControlFile @{paused=$false;stop=$true}
Set-QQDControl $script:Configuration $false
$b=Read-QQWJson $script:Configuration.ControlFile
Set-QQDControl $script:Configuration $true
$c=Read-QQWJson $script:Configuration.ControlFile
@{initial_paused=$a.paused;preserved_unpaused=$b.paused;reset_stop=$b.stop;stopped_paused=$c.paused;stopped=$c.stop} | ConvertTo-Json -Compress
""")
    assert result == {"initial_paused": True, "preserved_unpaused": False, "reset_stop": False,
        "stopped_paused": True, "stopped": True}
    assert json.loads((private / "control.json").read_text()) == {"paused": True, "stop": True}


def test_registry_rejects_pid_reuse_and_foreign_module(direct_case):
    invoke, _ = direct_case
    result = invoke("""
$a=Test-QQDRegistry ([pscustomobject]$script:Registry) $script:Configuration @($script:Process) $script:Interpreters
$script:Process.CreatedAt=$script:Created.AddSeconds(1)
$b=Test-QQDRegistry ([pscustomobject]$script:Registry) $script:Configuration @($script:Process) $script:Interpreters
$script:Process.CreatedAt=$script:Created
$script:Process.Arguments[2]='harness.qq.watcher'
$c=Test-QQDProcess $script:Process $script:Configuration $script:Interpreters
@{valid=$a;reused_pid=$b;foreign_module=$c} | ConvertTo-Json -Compress
""")
    assert result == {"valid": True, "reused_pid": False, "foreign_module": False}


def test_status_binds_current_source_and_repository_root(direct_case):
    invoke, _ = direct_case
    result = invoke("""
$a=Test-QQDStatus ([pscustomobject]$script:Status) $script:Configuration @($script:Process) $script:Source
$b=Test-QQDStatus ([pscustomobject]$script:Status) $script:Configuration @($script:Process) ('qq-direct:' + ('b' * 64))
$script:Status.repository_root='C:\\foreign checkout'
$c=Test-QQDStatus ([pscustomobject]$script:Status) $script:Configuration @($script:Process) $script:Source
@{valid=$a;stale_source=$b;foreign_root=$c} | ConvertTo-Json -Compress
""")
    assert result == {"valid": True, "stale_source": False, "foreign_root": False}


def test_native_source_hash_matches_python_source_definition(direct_case):
    invoke, _ = direct_case
    expected = "qq-direct:" + hashlib.sha256(b"".join(path.read_bytes()
        for path in sorted((ROOT / "harness" / "qq").glob("*.py")))).hexdigest()
    assert invoke("@{hash=(Get-QQDSourceHash)} | ConvertTo-Json -Compress")["hash"] == expected


def test_source_hash_uses_ordinal_filename_order_including_underscores(direct_case):
    invoke, private = direct_case
    checkout = private / "synthetic checkout"
    source = checkout / "harness" / "qq"
    source.mkdir(parents=True)
    # Different contents make a cultural direct_config.py/direct.py reversal visible.
    for name, content in {
        "direct_config.py": b"config",
        "direct.py": b"actor",
        "__init__.py": b"init",
        "direct_images.py": b"images",
        "z.py": b"last",
    }.items():
        (source / name).write_bytes(content)
    expected = "qq-direct:" + hashlib.sha256(b"initactorconfigimageslast").hexdigest()
    result = invoke(f"$script:QQDRepositoryRoot={literal(checkout)}; @{{hash=(Get-QQDSourceHash)}} | ConvertTo-Json -Compress")
    assert result["hash"] == expected


def test_repeated_start_requires_saved_process_and_fresh_source(direct_case):
    invoke, private = direct_case
    result = invoke("""
Write-QQDJson $script:Configuration.Registry $script:Registry
Write-QQDJson $script:Configuration.StatusFile $script:Status
function Get-QQDProcesses { return $script:Process }
function Get-QQDSourceHash { return $script:Source }
$a=Invoke-QQDirectStart -ConfigPath $ConfigPath
$script:Source='qq-direct:' + ('b' * 64)
$b=Invoke-QQDirectStart -ConfigPath $ConfigPath
@{existing=$a.state;stale=$b.state;starts=$script:Starts;stops=$script:Stops} | ConvertTo-Json -Compress
""")
    assert result == {"existing": "already_running", "stale": "failed", "starts": 0, "stops": 0}
    assert not (private / "control.json").exists()


def test_stop_refuses_recycled_pid_before_any_control_mutation(direct_case):
    invoke, private = direct_case
    result = invoke("""
Write-QQDJson $script:Configuration.Registry $script:Registry
$script:Process.CreatedAt=$script:Created.AddSeconds(1)
function Get-QQDProcesses { return $script:Process }
$r=Invoke-QQDirectStop -ConfigPath $ConfigPath
@{state=$r.state;stops=$script:Stops} | ConvertTo-Json -Compress
""", stop=True)
    assert result == {"state": "failed", "stops": 0}
    assert not (private / "control.json").exists()
