"""Windows launcher tests use fake monitors and private temporary files only."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "tools/qq/Start-QQAcceptance.ps1"
POWERSHELL = shutil.which("powershell.exe")
pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="Windows PowerShell required")


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@pytest.fixture
def launcher(tmp_path):
    local = tmp_path / "Local App Data"
    private = local / "NeuralCloud" / "QQDesktop"
    private.mkdir(parents=True)
    config = private / "acceptance [test].json"
    paths = {name: private / filename for name, filename in {
        "run_dir": "new formal run", "watcher_status_path": "watcher.status.json", "trace_file": "trace.jsonl",
        "ledger_db": "ledger.sqlite3", "memory_db": "memory.sqlite3"}.items()}
    for name in ("ledger_db", "memory_db"):
        paths[name].write_text("synthetic-only-not-read-by-launcher")
    values = {name: str(path) for name, path in paths.items()}
    values.update(worker_identity={"pid": 1234, "started_at": "2026-09-11T00:00:00Z"}, build_hash="synthetic-build-123456")
    config.write_text(json.dumps(values))

    def invoke(body, *, timeout=15):
        runner = tmp_path / "runner.ps1"
        runner.write_text(f"""
$ErrorActionPreference='Stop'
$env:LOCALAPPDATA={literal(local)}
. {literal(SCRIPT)} -ConfigPath {literal(config)} -TimeoutSeconds 2
$script:Config={literal(config)}
$script:RunDirectory={literal(paths['run_dir'])}
$script:Private={literal(private)}
$script:Starts=0
$script:Python='C:\\synthetic venv\\python.exe'
$script:BasePython='C:\\synthetic base\\python.exe'
$script:OriginalStart=(Get-Command Start-QQAProcess).ScriptBlock
function Get-QQWInterpreterPaths {{ return @($script:Python,$script:BasePython) }}
function New-QQALauncherMutex {{ return New-Object Threading.Mutex($false, ('Local\\QQAcceptanceTest-'+[Guid]::NewGuid().ToString('N'))) }}
function Stop-Process {{ throw 'must_not_stop_anything' }}
function Get-QQAProcesses {{ if ($script:Starts -gt 0) {{ New-TestProcess }} }}
function New-TestProcess {{
    [pscustomobject]@{{ProcessId=424242; ExecutablePath=$script:BasePython;
        Arguments=@($script:Python,'-m','harness.qq.acceptance','start','--config',$script:Snapshot)}}
}}
function Write-TestManifest {{
    [void][IO.Directory]::CreateDirectory($script:RunDirectory)
    $cfg=Read-QQAConfiguration $script:Config
    $manifest=@{{schema_version='qq-acceptance-manifest-v1'; duration_seconds=28800; monitor_pid=424242;
        started_at=[DateTimeOffset]::UtcNow.ToString('o'); config=$cfg.Value}}
    [IO.File]::WriteAllText((Join-Path $script:RunDirectory 'manifest.json'),($manifest | ConvertTo-Json -Depth 6))
    [IO.File]::WriteAllText((Join-Path $script:RunDirectory 'samples.jsonl'), '{{"elapsed_seconds":0,"reasons":[]}}'+[Environment]::NewLine)
}}
function Start-QQAProcess {{
    param($PythonPath,$SnapshotPath,$StdoutPath,$StderrPath)
    $script:Starts++
    $script:Snapshot=$SnapshotPath
    $script:Spawn=@{{python=$PythonPath; snapshot=$SnapshotPath; stdout=$StdoutPath; stderr=$StderrPath}}
    Write-TestManifest
    return [pscustomobject]@{{Id=424241; HasExited=$false}}
}}
$script:Status=@{{service='qq-desktop-watcher-v1'; pid=1234; started_at='2026-09-11T00:00:00Z';
    build_hash='synthetic-build-123456'; running=$true; automatic_screen_capture=$true; automatic_send=$true;
    heartbeat_at=[DateTimeOffset]::UtcNow.ToString('o')}}
[IO.File]::WriteAllText({literal(paths['watcher_status_path'])},($script:Status | ConvertTo-Json))
{body}
""", encoding="utf-8-sig")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(runner)],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=ROOT)
        assert result.returncode == 0, result.stderr
        assert "synthetic-only-not-read" not in result.stdout + result.stderr
        return json.loads(result.stdout)

    return invoke, config, paths


def test_hidden_launch_freezes_config_and_repeat_is_idempotent(launcher):
    invoke, config, paths = launcher
    result = invoke("""
$first=Invoke-QQAcceptanceStart $script:Config
$second=Invoke-QQAcceptanceStart $script:Config
@{first=$first; second=$second; starts=$script:Starts; spawn=$script:Spawn;
  snapshot=(Read-QQWJson $script:Snapshot)} | ConvertTo-Json -Depth 8 -Compress
""")
    assert result["first"]["status"] == "started" and result["first"]["monitor_pid"] == 424242
    assert result["second"]["status"] == "already_running" and result["starts"] == 1
    assert result["first"]["fully_automatic_e2e_pass"] is False
    assert result["spawn"]["snapshot"] != str(config)
    assert result["snapshot"]["run_dir"] == str(paths["run_dir"])
    assert Path(result["spawn"]["snapshot"]).parent == Path(result["first"]["stderr_path"]).parent
    assert (Path(result["spawn"]["snapshot"]).parent / "launch.json").is_file()


@pytest.mark.parametrize("mutation", [
    "$script:Status.pid=9999", "$script:Status.build_hash='other-build'",
    "$script:Status.started_at='2026-09-11T00:01:00Z'", "$script:Status.running='true'",
    "$script:Status.automatic_send=$false", "$script:Status.heartbeat_at=[DateTimeOffset]::UtcNow.AddMinutes(-2).ToString('o')",
])
def test_fixed_worker_mismatch_never_launches(launcher, mutation):
    invoke, _, paths = launcher
    result = invoke(f"""
{mutation}
[IO.File]::WriteAllText({literal(paths['watcher_status_path'])},($script:Status | ConvertTo-Json))
$result=Invoke-QQAcceptanceStart $script:Config
@{{result=$result; starts=$script:Starts}} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20 and result["starts"] == 0


@pytest.mark.parametrize("mutation", [
    "$cfg.run_dir='relative-path'", "$cfg.run_dir=$env:LOCALAPPDATA",
    "$cfg.run_dir=$cfg.ledger_db", "$cfg.memory_db=$cfg.ledger_db",
    "$cfg.worker_identity.pid=$true", "$cfg.worker_identity.started_at='2026-09-11T00:00:00'",
    "$cfg | Add-Member NoteProperty sample_interval_seconds 16", "$cfg | Add-Member NoteProperty sample_interval_seconds $true",
    "$cfg | Add-Member NoteProperty duration_seconds 1", "$cfg | Add-Member NoteProperty token 'DO_NOT_OUTPUT_PRIVATE_KEY'",
])
def test_invalid_paths_parameters_and_extra_secrets_are_rejected(launcher, mutation):
    invoke, _, _ = launcher
    result = invoke(f"""
$cfg=Read-QQWJson $script:Config
{mutation}
[IO.File]::WriteAllText($script:Config,($cfg | ConvertTo-Json -Depth 5))
$result=Invoke-QQAcceptanceStart $script:Config
@{{result=$result; starts=$script:Starts}} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["result"]["exit_code"] == 20 and result["starts"] == 0
    assert "DO_NOT_OUTPUT_PRIVATE_KEY" not in json.dumps(result)


def test_prior_run_and_foreign_process_never_restart(launcher):
    invoke, _, _ = launcher
    result = invoke("""
Write-TestManifest
$old=Invoke-QQAcceptanceStart $script:Config
$script:Snapshot=$script:Config
function Get-QQAProcesses { $p=New-TestProcess; $p.ExecutablePath='C:\\other\\python.exe'; return $p }
$foreign=Invoke-QQAcceptanceStart $script:Config
@{old=$old; foreign=$foreign; starts=$script:Starts} | ConvertTo-Json -Depth 5 -Compress
""")
    assert result["old"]["reason"] == "run_directory_already_exists"
    assert result["foreign"]["reason"] == "another_acceptance_monitor_exists" and result["starts"] == 0


def test_native_start_arguments_hidden_and_exact_without_shell_expansion(launcher):
    invoke, _, _ = launcher
    result = invoke("""
function Start-Process {
    param($FilePath,$ArgumentList,$WorkingDirectory,$WindowStyle,[switch]$PassThru,$RedirectStandardOutput,$RedirectStandardError,$ErrorAction)
    return @{file=$FilePath; arguments=@(Split-QQWCommandLine ('python.exe '+$ArgumentList)); hidden=$WindowStyle;
             stdout=$RedirectStandardOutput; stderr=$RedirectStandardError}
}
& $script:OriginalStart $script:Python 'C:\\private path\\a`$b[0].json' 'C:\\private\\out' 'C:\\private\\err' | ConvertTo-Json -Compress
""")
    assert result["hidden"] == "Hidden"
    assert result["arguments"] == ["python.exe", "-m", "harness.qq.acceptance", "start", "--config", "C:\\private path\\a`$b[0].json"]


@pytest.mark.parametrize("exited", [True, False])
def test_startup_exit_and_timeout_preserve_evidence_without_respawn(launcher, exited):
    invoke, _, _ = launcher
    result = invoke("""
function Start-QQAProcess { param($PythonPath,$SnapshotPath,$StdoutPath,$StderrPath)
    $script:Starts++; return [pscustomobject]@{Id=424241;HasExited=EXITED} }
function Get-QQAProcesses { }
$result=Invoke-QQAcceptanceStart $script:Config -TimeoutSeconds 0.05
@{result=$result; starts=$script:Starts} | ConvertTo-Json -Depth 5 -Compress
""".replace("EXITED", "$true" if exited else "$false"))
    assert result["starts"] == 1
    assert result["result"]["reason"] == ("monitor_exited_before_readiness" if exited else "monitor_readiness_timeout_no_restart")


def test_busy_launcher_lock_never_probes_or_starts_process(launcher):
    invoke, _, _ = launcher
    value = invoke("""
function New-QQALauncherMutex {
    $value=New-Object PSObject
    $value | Add-Member ScriptMethod WaitOne { param($milliseconds) return $false }
    $value | Add-Member ScriptMethod Dispose { }
    return $value
}
function Get-QQAProcesses { throw 'must_not_probe_while_locked' }
$result=Invoke-QQAcceptanceStart $script:Config
@{result=$result; starts=$script:Starts} | ConvertTo-Json -Depth 5 -Compress
""")
    assert value["result"]["status"] == "launch_in_progress" and value["starts"] == 0


def test_reparse_parent_is_rejected_before_monitor_launch(launcher):
    invoke, _, _ = launcher
    value = invoke("""
$target=Join-Path $script:Private 'real-source'
[void][IO.Directory]::CreateDirectory($target)
[IO.File]::WriteAllText((Join-Path $target 'memory.sqlite3'),'synthetic')
$link=Join-Path $script:Private 'reparse-source'
[void](New-Item -ItemType Junction -Path $link -Target $target -ErrorAction Stop)
$cfg=Read-QQWJson $script:Config
$cfg.memory_db=Join-Path $link 'memory.sqlite3'
[IO.File]::WriteAllText($script:Config,($cfg | ConvertTo-Json -Depth 5))
$result=Invoke-QQAcceptanceStart $script:Config
@{result=$result; starts=$script:Starts} | ConvertTo-Json -Depth 5 -Compress
""")
    assert value["result"]["exit_code"] == 20 and value["starts"] == 0
