from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.test_start_script import POWERSHELL, POWERSHELL_ENGINES, _run_isolated_start


pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


def _run(tmp_path, monkeypatch, *args, probe="", record_timeout=False, **kwargs):
    """Run the real entrypoint; replace only fixture OS/process boundaries."""
    original_write = Path.write_text

    def write(path, content, *write_args, **write_kwargs):
        if path.name == "run-start.ps1":
            content = content.replace(
                "        model_mode = [string]$env:NC_MODE",
                "        input_budget = [string]$env:NC_QQ_INPUT_TOKEN_BUDGET\n"
                "        recall_characters = [string]$env:NC_QQ_MEMORY_RECALL_MAX_CHARACTERS\n"
                "        model_mode = [string]$env:NC_MODE",
            ).replace("& (Join-Path $PSScriptRoot 'start.ps1') @args", probe + "\n& (Join-Path $PSScriptRoot 'start.ps1') @args")
            if record_timeout:
                # Observe the real Wait-Endpoint caller at its mocked HTTP
                # boundary, without replacing timeout parsing or readiness logic.
                signature = "    param($Uri, $Headers, $TimeoutSec, $Method, $ContentType, $Body)\n"
                assert signature in content
                content = content.replace(signature, signature + r'''
    [pscustomobject]@{
        url = [string]$Uri
        seconds = [int](Get-Variable -Name Seconds -Scope 1 -ValueOnly)
    } | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $global:StartTestRoot 'readiness-timeouts.jsonl')
''')
        return original_write(path, content, *write_args, **write_kwargs)

    monkeypatch.setattr(Path, "write_text", write)
    return _run_isolated_start(tmp_path, *args, development=False, **kwargs)


def _bindings(tmp_path, payload):
    path = tmp_path / "data/shared-misty-launch.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _registry(tmp_path, count=4):
    modules = {
        "cognitive": "harness.cognitive.service",
        "homeostasis": "harness.homeostasis.service",
        "runtime": "harness.runtime.service",
        "gui": "gui.runtime_server",
    }
    python = str(tmp_path / ".venv/Scripts/python.exe")
    return {
        "schema_version": "neuralcloud-processes-v2",
        "root": str(tmp_path),
        "python_path": python,
        "services": [
            {
                "name": name,
                "module": module,
                "pid": 900001 + index,
                "port": 19970 + index,
                "root": str(tmp_path),
                "python_path": python,
                "process_start_time_utc": "2026-09-15T00:00:00.0000000Z",
            }
            for index, (name, module) in enumerate(list(modules.items())[:count])
        ],
    }


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("values", [(96000, 16000), ("96000", "16000"), (8192, 1)])
def test_shared_integer_budgets_reach_every_service(tmp_path, monkeypatch, powershell, values):
    config = _bindings(tmp_path, dict(zip(
        ("NC_QQ_INPUT_TOKEN_BUDGET", "NC_QQ_MEMORY_RECALL_MAX_CHARACTERS"), values,
    )))
    before = config.read_bytes()
    result, launches, _ = _run(tmp_path, monkeypatch, powershell=powershell)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    assert all(launch["input_budget"] == str(values[0]) for launch in launches)
    assert all(launch["recall_characters"] == str(values[1]) for launch in launches)
    assert config.read_bytes() == before


@pytest.mark.parametrize("name,value", [
    ("NC_QQ_INPUT_TOKEN_BUDGET", 8191),
    ("NC_QQ_INPUT_TOKEN_BUDGET", True),
    ("NC_QQ_INPUT_TOKEN_BUDGET", [96000]),
    ("NC_QQ_INPUT_TOKEN_BUDGET", "96000\nprivate-invalid-value"),
    ("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", 0),
    ("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", -1),
    ("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", 1.5),
    ("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", None),
    ("NC_UNAPPROVED", "private-invalid-value"),
])
def test_invalid_budgets_fail_before_start_without_exposing_values(tmp_path, monkeypatch, name, value):
    _bindings(tmp_path, {name: value})
    result, launches, _ = _run(tmp_path, monkeypatch)
    assert result.returncode == 1 and not launches
    assert "Configuration error" in result.stdout
    assert "private-invalid-value" not in result.stdout + result.stderr


def test_selected_binding_file_replaces_old_defaults(tmp_path, monkeypatch):
    _bindings(tmp_path, {"NC_UNAPPROVED": "private-invalid-value"})
    custom = tmp_path / "custom.json"
    custom.write_text(json.dumps({"NC_QQ_INPUT_TOKEN_BUDGET": 96000}))
    result, launches, _ = _run(tmp_path, monkeypatch, "-SharedLaunchConfigPath", "custom.json")
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(launch["input_budget"] == "96000" for launch in launches)


def test_missing_selected_binding_file_fails_without_start(tmp_path, monkeypatch):
    result, launches, _ = _run(tmp_path, monkeypatch, "-SharedLaunchConfigPath", "missing.json")
    assert result.returncode == 1 and not launches
    assert "Configuration error" in result.stdout


def test_ignore_defaults_accepts_environment_from_unified_launcher(tmp_path, monkeypatch):
    config = _bindings(tmp_path, {"NC_UNAPPROVED": "private-invalid-value"})
    before = config.read_bytes()
    result, launches, _ = _run(tmp_path, monkeypatch, "-IgnoreSharedLaunchDefaults", environment={
        "NC_QQ_INPUT_TOKEN_BUDGET": "96000", "NC_QQ_MEMORY_RECALL_MAX_CHARACTERS": "16000",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(launch["input_budget"] == "96000" and launch["recall_characters"] == "16000" for launch in launches)
    assert config.read_bytes() == before


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("bad_configuration", ["invalid_json", "unapproved_binding"])
def test_ignore_defaults_environment_flag_skips_bad_old_file(
    tmp_path, monkeypatch, powershell, bad_configuration,
):
    config = _bindings(tmp_path, {"NC_UNAPPROVED": "private-invalid-value"})
    if bad_configuration == "invalid_json":
        config.write_text('{"unfinished":', encoding="utf-8")
    before = config.read_bytes()
    result, launches, _ = _run(tmp_path, monkeypatch, powershell=powershell, environment={
        "NC_IGNORE_SHARED_LAUNCH_DEFAULTS": "1",
        "NC_QQ_INPUT_TOKEN_BUDGET": "96000",
        "NC_QQ_MEMORY_RECALL_MAX_CHARACTERS": "16000",
    })
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    assert all(launch["input_budget"] == "96000" and launch["recall_characters"] == "16000" for launch in launches)
    assert config.read_bytes() == before


def _assert_readiness_timeout(tmp_path, seconds):
    calls = [json.loads(line) for line in
             (tmp_path / "readiness-timeouts.jsonl").read_text(encoding="utf-8-sig").splitlines()]
    assert [entry["url"] for entry in calls] == [
        "http://127.0.0.1:8771/v1/health",
        "http://127.0.0.1:8772/v1/health",
        "http://127.0.0.1:8770/v1/status",
        "http://127.0.0.1:8790/api/status",
    ]
    assert [entry["seconds"] for entry in calls] == [seconds] * 4


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("configured,expected", [("", 180), ("10", 10), ("600", 600), ("1800", 1800)])
def test_startup_timeout_environment_reaches_all_readiness_gates(
    tmp_path, monkeypatch, powershell, configured, expected,
):
    result, launches, _ = _run(tmp_path, monkeypatch, powershell=powershell,
                              record_timeout=True,
                              environment={"NC_STARTUP_TIMEOUT_SECONDS": configured})
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    _assert_readiness_timeout(tmp_path, expected)


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("configured", ["0", "9", "1801", "600.5", "2147483648", "private-invalid-timeout"])
def test_invalid_startup_timeout_environment_fails_before_any_process(
    tmp_path, monkeypatch, powershell, configured,
):
    result, launches, token_reads = _run(tmp_path, monkeypatch, powershell=powershell,
                                       record_timeout=True,
                                       environment={"NC_STARTUP_TIMEOUT_SECONDS": configured})
    assert result.returncode == 1
    assert not launches and not token_reads
    assert "startup timeout must be an integer from 10 to 1800 seconds" in result.stdout
    assert "private-invalid-timeout" not in result.stdout + result.stderr
    assert not (tmp_path / "readiness-timeouts.jsonl").exists()


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("configured", ["900", "private-invalid-timeout"])
def test_explicit_startup_timeout_overrides_valid_or_invalid_environment(
    tmp_path, monkeypatch, powershell, configured,
):
    result, launches, _ = _run(tmp_path, monkeypatch, "-StartupTimeoutSeconds", "600",
                              powershell=powershell, record_timeout=True,
                              environment={"NC_STARTUP_TIMEOUT_SECONDS": configured})
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    _assert_readiness_timeout(tmp_path, 600)


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("explicit", ["9", "1801"])
def test_invalid_explicit_startup_timeout_cannot_fall_back_to_valid_environment(
    tmp_path, monkeypatch, powershell, explicit,
):
    result, launches, token_reads = _run(tmp_path, monkeypatch, "-StartupTimeoutSeconds", explicit,
                                       powershell=powershell, record_timeout=True,
                                       environment={"NC_STARTUP_TIMEOUT_SECONDS": "600"})
    assert result.returncode != 0
    assert not launches and not token_reads
    assert "StartupTimeoutSeconds" in result.stdout + result.stderr
    assert not (tmp_path / "readiness-timeouts.jsonl").exists()


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("count", [1, 4])
def test_dead_valid_registry_recovers_without_stopping_any_process(tmp_path, monkeypatch, count, powershell):
    path = tmp_path / ".neuralcloud.pids.json"
    path.write_text(json.dumps(_registry(tmp_path, count)))
    if count == 4:
        (tmp_path / ".gui.pid").write_text("900004\n")
    result, launches, _ = _run(tmp_path, monkeypatch, powershell=powershell)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    assert "confirmed stale" in result.stdout
    assert not (tmp_path / ".gui.pid").exists()
    assert json.loads(path.read_text(encoding="utf-8-sig"))["services"][0]["pid"] == 800001


@pytest.mark.parametrize("fault", [
    "schema", "root", "empty_services", "services_object", "unknown_service", "module",
    "entry_root", "entry_python", "timestamp", "pid", "port", "duplicate_pid", "duplicate_port", "legacy",
])
def test_malformed_registry_is_retained_without_start_or_stop(tmp_path, monkeypatch, fault):
    record = _registry(tmp_path)
    if fault == "schema":
        record["schema_version"] = "unknown"
    elif fault == "root":
        record["root"] = str(tmp_path / "foreign")
    elif fault == "empty_services":
        record["services"] = []
    elif fault == "services_object":
        record["services"] = record["services"][0]
    elif fault == "unknown_service":
        record["services"][0]["name"] = "foreign"
    elif fault == "module":
        record["services"][0]["module"] = "foreign.service"
    elif fault == "entry_root":
        record["services"][0]["root"] = str(tmp_path / "foreign")
    elif fault == "entry_python":
        record["services"][0]["python_path"] = str(tmp_path / "foreign/python.exe")
    elif fault == "timestamp":
        record["services"][0]["process_start_time_utc"] = "unknown"
    elif fault == "pid":
        record["services"][0]["pid"] = -1
    elif fault == "port":
        record["services"][0]["port"] = 65536
    elif fault == "duplicate_pid":
        record["services"][1]["pid"] = record["services"][0]["pid"]
    elif fault == "duplicate_port":
        record["services"][1]["port"] = record["services"][0]["port"]
    elif fault == "legacy":
        (tmp_path / ".gui.pid").write_text("unidentified")
    path = tmp_path / ".neuralcloud.pids.json"
    path.write_text(json.dumps(record))
    before = path.read_bytes()
    result, launches, _ = _run(tmp_path, monkeypatch)
    assert result.returncode == 1 and not launches
    assert "No owner was stopped" in result.stdout
    assert path.read_bytes() == before


@pytest.mark.parametrize("probe", [
    "function Get-Process { param($Id) if ($Id -eq 900001) { return [pscustomobject]@{ Id = $Id } } }",
    "function Get-Process { throw 'Cannot determine process ownership' }",
    "function Get-NetTCPConnection { param($LocalPort) if ($LocalPort -eq 19970) { return [pscustomobject]@{ OwningProcess = 901000 } } }",
    "function Get-NetTCPConnection { param($LocalPort) if ($LocalPort -eq 8790) { return [pscustomobject]@{ OwningProcess = 901000 } } }",
])
def test_live_unknown_pid_or_any_registered_or_requested_port_blocks_recovery(tmp_path, monkeypatch, probe):
    path = tmp_path / ".neuralcloud.pids.json"
    path.write_text(json.dumps(_registry(tmp_path)))
    before = path.read_bytes()
    result, launches, _ = _run(tmp_path, monkeypatch, probe=probe)
    assert result.returncode == 1 and not launches
    assert path.read_bytes() == before
    assert "No owner was stopped" in result.stdout


@pytest.mark.parametrize("foreign_owner", [False, True])
def test_failed_start_cleans_owned_venv_parent_and_socket_child_only(tmp_path, monkeypatch, foreign_owner):
    probe = r'''
$global:MockProcesses = @{}
$global:MockStarts = 0
function Start-Process {
    param($FilePath, $ArgumentList)
    $global:MockStarts += 1
    if ($global:MockStarts -gt 1) { throw 'synthetic later service failure' }
    $stamp = (Get-Date).ToUniversalTime()
    $global:MockProcesses[810001] = [pscustomobject]@{
        Id = 810001; StartTime = $stamp; Name = 'python.exe'; ExecutablePath = $FilePath
        CommandLine = ('"' + $FilePath + '" -m ' + $ArgumentList[1]); ParentProcessId = 0
    }
    $global:MockProcesses[810002] = [pscustomobject]@{
        Id = 810002; StartTime = $stamp.AddMilliseconds(100); Name = 'python.exe'; ExecutablePath = 'C:\base\python.exe'
        CommandLine = ('C:\base\python.exe -m ' + $ArgumentList[1]); ParentProcessId = CHILD_PARENT
    }
    return $global:MockProcesses[810001]
}
function Get-Process { param($Id) return $global:MockProcesses[[int]$Id] }
function Get-CimInstance {
    param($ClassName, $Filter)
    if ($Filter -match '^ProcessId=(\d+)$') { return $global:MockProcesses[[int]$Matches[1]] }
    throw 'unexpected process inspection'
}
function Get-NetTCPConnection {
    param($LocalPort)
    if ($LocalPort -eq 8771 -and $global:MockProcesses.ContainsKey(810002)) {
        return [pscustomobject]@{ OwningProcess = 810002 }
    }
}
function Stop-Process {
    param($Id)
    Add-Content -LiteralPath (Join-Path $global:StartTestRoot 'stopped.txt') -Value $Id
    $global:MockProcesses.Remove([int]$Id)
}
function Start-Sleep {}
'''.replace("CHILD_PARENT", "1234" if foreign_owner else "810001")
    # Unidentified live ownership waits for ten seconds in the real cleanup.
    result, _, _ = _run(tmp_path, monkeypatch, probe=probe)
    assert result.returncode == 1
    assert "synthetic later service failure" in result.stdout
    stopped = tmp_path / "stopped.txt"
    if foreign_owner:
        assert not stopped.exists()
        assert (tmp_path / ".neuralcloud.pids.json").exists()
    else:
        assert stopped.read_text(encoding="utf-8-sig").splitlines() == ["810002", "810001"]
        assert not (tmp_path / ".neuralcloud.pids.json").exists()
