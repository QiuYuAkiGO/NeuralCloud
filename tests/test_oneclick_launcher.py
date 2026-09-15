from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "tools/launcher/OneClick.psm1"
ENGINES = list(dict.fromkeys(filter(None, (shutil.which("pwsh"), shutil.which("powershell.exe")))))
pytestmark = pytest.mark.skipif(not ENGINES, reason="Windows PowerShell unavailable")


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def configuration(root):
    root.mkdir(parents=True, exist_ok=True)
    (root / "manifest.json").write_text('{"control_mode":"development_debug"}')
    (root / "homeostasis.json").write_text('{}')
    profile = {
        "mode": "demo", "development": True, "state_root": "isolated",
        "manifest_path": "manifest.json", "profile_path": "homeostasis.json",
        "embodiment_provider": "none", "mistyai_base_url": "http://127.0.0.1:8787",
        "mistyai_token_file": None,
        "ports": {"gui": 18890, "runtime": 18870, "cognitive": 18871, "homeostasis": 18872},
        "bindings": {}, "mistyai": {"enabled": False, "source_path": "../MistyAI", "distro": "Ubuntu-24.04"},
    }
    payload = {"schema_version": 1, "profile": "local", "open_browser": False,
               "prepare_environment": True, "profiles": {"local": profile}}
    save(root, payload)
    return payload


def save(root, payload):
    (root / "launch.json").write_text(json.dumps(payload), encoding="utf-8")


def invoke(tmp_path, body, engine=None, environment=None):
    runner = tmp_path / "runner.ps1"
    runner.write_text(
        "$ErrorActionPreference='Stop'\n"
        f"$module=Import-Module {literal(MODULE)} -Force -PassThru\n"
        "$result = & $module {\n" + body + "\n}\n"
        "$result | ConvertTo-Json -Depth 12 -Compress\n", encoding="utf-8-sig")
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("NC_")}
    env.update(environment or {})
    result = subprocess.run([engine or ENGINES[0], "-NoProfile", "-NonInteractive", "-File", str(runner)],
                            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1])


def read_command(root):
    return f"Read-OneClickConfiguration -Root {literal(root)} -ConfigPath 'launch.json' -Profile ''"


@pytest.mark.parametrize("engine", ENGINES)
def test_configuration_and_fingerprint_work_in_both_powershell_engines(tmp_path, engine):
    root = tmp_path / "project with spaces O'Connor"
    configuration(root)
    result = invoke(tmp_path, f"$s={read_command(root)}; @{{settings=$s;fingerprint=(Get-OneClickFingerprint $s)}}", engine)
    assert result["settings"]["state_root"] == str(root / "isolated")
    assert result["settings"]["ports"]["gui"] == 18890
    again = invoke(tmp_path, f"$s={read_command(root)}; @{{fingerprint=(Get-OneClickFingerprint $s)}}", ENGINES[-1])
    assert result["fingerprint"] == again["fingerprint"]


@pytest.mark.parametrize("change", [
    lambda c: c.update(schema_version=True),
    lambda c: c.update(open_browser="false"),
    lambda c: c["profiles"]["local"].update(development="false"),
    lambda c: c["profiles"]["local"]["ports"].update(gui=18870),
    lambda c: c["profiles"]["local"]["ports"].update(gui=True),
    lambda c: c["profiles"]["local"].update(state_root="data"),
    lambda c: c["profiles"]["local"].update(state_root="data/"),
    lambda c: c["profiles"]["local"].update(state_root="."),
    lambda c: c["profiles"]["local"].update(state_root="data/cognitive"),
    lambda c: c["profiles"]["local"].update(state_root=".."),
    lambda c: c["profiles"]["local"].update(mode="LIVE"),
    lambda c: c["profiles"]["local"].update(startup_timeout_seconds=0),
    lambda c: c["profiles"]["local"].update(mistyai_base_url="http://secret:password@localhost:8787"),
    lambda c: c["profiles"]["local"]["mistyai"].update(enabled=True),
    lambda c: c["profiles"]["local"]["bindings"].update(NC_CONTROL_TOKEN="private-test-value"),
])
def test_invalid_configuration_fails_before_any_external_command(tmp_path, change):
    root = tmp_path / "project"
    payload = configuration(root)
    change(payload)
    save(root, payload)
    result = invoke(tmp_path, f"""
function Invoke-LauncherExternalCommand {{ throw 'external_command_must_not_run' }}
try {{ {read_command(root)} | Out-Null; @{{accepted=$true}} }}
catch {{ @{{accepted=$false;reason=$_.Exception.Message}} }}
""")
    assert not result["accepted"]
    assert "private-test-value" not in result["reason"]
    assert "secret:password" not in result["reason"]
    assert "external_command_must_not_run" not in result["reason"]


def test_shared_bindings_expand_paths_and_preserve_saved_model_mode(tmp_path):
    root = tmp_path / "project"
    payload = configuration(root)
    (root / "manifest.json").write_text('{"control_mode":"study_runtime"}')
    (root / "model.json").write_text('{"mode":"live","api_key":"must-not-print"}')
    selected = payload["profiles"]["local"]
    selected.update(development=False, state_root=None, mode="configured", bindings={
        "NC_GUI_CONFIG": "model.json", "NC_RUNTIME_STATE_DIR": "%ONECLICK_TEST_DATA%/runtime",
        "NC_QQ_INPUT_TOKEN_BUDGET": 96000, "NC_QQ_MEMORY_RECALL_MAX_CHARACTERS": 16000,
    })
    save(root, payload)
    result = invoke(tmp_path, read_command(root), environment={"ONECLICK_TEST_DATA": str(tmp_path / "private")})
    assert result["mode"] == "configured"
    assert result["bindings"]["NC_RUNTIME_STATE_DIR"] == str(tmp_path / "private/runtime")
    assert result["bindings"]["NC_QQ_INPUT_TOKEN_BUDGET"] == "96000"
    assert "must-not-print" not in json.dumps(result)


def test_development_clears_inherited_shared_credentials(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    result = invoke(tmp_path, f"$s={read_command(root)}; Get-OneClickEnvironment $s", environment={
        "NC_QQ_SESSION_CONFIG": "unexpected-shared-path", "NC_CONTROL_TOKEN": "unexpected-token"})
    assert result["NC_QQ_SESSION_CONFIG"] is None
    assert result["NC_CONTROL_TOKEN"] is None
    assert result["NC_IGNORE_SHARED_LAUNCH_DEFAULTS"] == "1"
    assert result["NC_STARTUP_TIMEOUT_SECONDS"] == "300"


def test_recovered_pause_is_reported_without_automatic_resume(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    result = invoke(tmp_path, f"""
$s={read_command(root)}
function Get-OneClickHealth {{ return @{{running=$true}} }}
function Invoke-RestMethod {{
    param($Uri, $TimeoutSec)
    return @{{runtime=@{{lifecycle='PAUSED';health=@{{cognitive_healthy=$true;homeostasis_healthy=$true}}}}}}
}}
Assert-OneClickReady $s
@{{lifecycle=$s.runtime_lifecycle}}
""")
    assert result["lifecycle"] == "PAUSED"


@pytest.mark.parametrize("key,value", [("NC_QQ_INPUT_TOKEN_BUDGET", 8191),
                                     ("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", 0),
                                     ("NC_QQ_INPUT_TOKEN_BUDGET", "96000")])
def test_shared_budget_validation_matches_service_minimums(tmp_path, key, value):
    root = tmp_path / "project"
    payload = configuration(root)
    (root / "manifest.json").write_text('{"control_mode":"study_runtime"}')
    payload["profiles"]["local"].update(development=False, state_root=None, bindings={key: value})
    save(root, payload)
    result = invoke(tmp_path, f"try {{ {read_command(root)} }} catch {{ @{{reason=$_.Exception.Message}} }}")
    assert key in result["reason"]


def test_repeated_up_reuses_existing_instance_without_start_or_stop(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    result = invoke(tmp_path, f"""
function Get-OneClickHealth {{ return @{{running=$true}} }}
function Test-OneClickConfigurationMatch {{ return $true }}
function Assert-OneClickReady {{}}
function Invoke-OneClickChild {{ throw 'must_not_start_or_stop' }}
function Initialize-OneClickPython {{ throw 'must_not_change_dependencies' }}
Invoke-OneClickLaunch -Root {literal(root)} -ConfigPath launch.json -Command up -NoOpen
""")
    assert result["status"] == "running"


def test_changed_configuration_requires_explicit_restart_and_preserves_processes(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    result = invoke(tmp_path, f"""
function Get-OneClickHealth {{ return @{{running=$true}} }}
function Test-OneClickConfigurationMatch {{ return $false }}
function Invoke-OneClickChild {{ throw 'must_not_start_or_stop' }}
try {{ Invoke-OneClickLaunch -Root {literal(root)} -ConfigPath launch.json -Command up -NoOpen }}
catch {{ @{{reason=$_.Exception.Message}} }}
""")
    assert "restart" in result["reason"]
    assert "must_not_start_or_stop" not in result["reason"]


def test_cold_start_forwards_configuration_and_never_copies_shared_model_file(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    result = invoke(tmp_path, f"""
function Get-OneClickHealth {{ return @{{running=$false}} }}
function Initialize-OneClickPython {{}}
function Assert-OneClickReady {{}}
function Write-OneClickStamp {{}}
$script:recorded=$null
function Invoke-OneClickChild {{
    param($Root,$Script,$Arguments,$Environment)
    $script:recorded=@{{script=$Script;arguments=$Arguments;environment=$Environment}}
}}
Invoke-OneClickLaunch -Root {literal(root)} -ConfigPath launch.json -Command up -NoOpen | Out-Null
$script:recorded
""")
    assert result["script"] == "start.ps1"
    assert "-IgnoreSharedLaunchDefaults" in result["arguments"]
    assert "-Development" in result["arguments"]
    args = result["arguments"]
    assert args[args.index("-StateRoot") + 1] == str(root / "isolated")
    assert json.loads((root / "isolated/gui_settings.json").read_text()) == {"mode": "demo"}


def test_down_uses_selected_isolated_registry_and_never_starts(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    result = invoke(tmp_path, f"""
$script:recorded=$null
function Invoke-OneClickChild {{
    param($Root,$Script,$Arguments,$Environment)
    $script:recorded=@{{script=$Script;arguments=$Arguments}}
}}
function Get-OneClickHealth {{ throw 'must_not_start' }}
Invoke-OneClickLaunch -Root {literal(root)} -ConfigPath launch.json -Command down -NoOpen | Out-Null
$script:recorded
""")
    assert result == {"script": "stop.ps1", "arguments": ["-StateRoot", str(root / "isolated")]}


def test_child_failure_with_empty_output_preserves_exit_code(tmp_path):
    result = invoke(tmp_path, f"""
function Invoke-LauncherExternalCommand {{ return @{{exit_code=17;stdout=$null;stderr=$null}} }}
try {{ Invoke-OneClickChild -Root {literal(tmp_path)} -Script start.ps1 -Arguments @() }}
catch {{ @{{reason=$_.Exception.Message}} }}
""")
    assert "code 17" in result["reason"]


def test_managed_up_cannot_stamp_changed_bindings_as_applied(tmp_path):
    root = tmp_path / "project"
    payload = configuration(root)
    runtime_dir = root / "presets/runtime"
    homeostasis_dir = root / "presets/homeostasis"
    runtime_dir.mkdir(parents=True)
    homeostasis_dir.mkdir()
    (runtime_dir / "shared-misty-v1.json").write_text('{"control_mode":"study_runtime"}')
    (homeostasis_dir / "qq-social-drive-v1.json").write_text('{}')
    selected = payload["profiles"]["local"]
    selected.update(development=False, state_root=None, embodiment_provider="auto",
                    manifest_path="presets/runtime/shared-misty-v1.json",
                    profile_path="presets/homeostasis/qq-social-drive-v1.json",
                    ports={"gui": 8790, "runtime": 8770, "cognitive": 8771, "homeostasis": 8772})
    selected["mistyai"]["enabled"] = True
    save(root, payload)
    result = invoke(tmp_path, f"""
function Get-OneClickHealth {{ return @{{running=$true}} }}
function Test-OneClickConfigurationMatch {{ return $false }}
function Write-OneClickStamp {{ throw 'must_not_stamp_old_instance' }}
function Invoke-OneClickChild {{ throw 'must_not_change_old_instance' }}
try {{ Invoke-OneClickLaunch -Root {literal(root)} -ConfigPath launch.json -Command up -NoOpen }}
catch {{ @{{reason=$_.Exception.Message}} }}
""")
    assert "restart" in result["reason"]
    assert "must_not" not in result["reason"]


def test_stamp_detects_registry_replacement_and_manifest_changes(tmp_path):
    root = tmp_path / "project"
    configuration(root)
    (root / "isolated").mkdir()
    registry = root / "isolated/.neuralcloud.pids.json"
    registry.write_text('{"synthetic":1}')
    result = invoke(tmp_path, f"""
$s={read_command(root)}
Write-OneClickStamp $s
$before=Test-OneClickConfigurationMatch $s
[IO.File]::WriteAllText($s.registry_path, '{{"synthetic":2}}')
$after=Test-OneClickConfigurationMatch $s
@{{before=$before;after=$after}}
""")
    assert result == {"before": True, "after": False}


@pytest.mark.parametrize("engine", ENGINES)
def test_entrypoint_resolves_default_config_from_script_location(tmp_path, engine):
    root = tmp_path / "space project"
    root.mkdir()
    shutil.copy2(ROOT / "launch.ps1", root / "launch.ps1")
    module_dir = root / "tools/launcher"
    module_dir.mkdir(parents=True)
    (module_dir / "OneClick.psm1").write_text("""
function Invoke-OneClickLaunch {
    param($Root,$ConfigPath,$Profile,$Command,$NoOpen)
    if ($ConfigPath -ne (Join-Path $Root 'launch.json')) { throw 'wrong_default_config' }
    return @{status='checked';url='';config_path=$ConfigPath;profile='shared'}
}
Export-ModuleMember -Function Invoke-OneClickLaunch
""")
    result = subprocess.run([engine, "-NoProfile", "-File", str(root / "launch.ps1"), "check"],
                            cwd=tmp_path, capture_output=True, text=True, timeout=20)
    assert result.returncode == 0, result.stdout + result.stderr
    assert str(root / "launch.json") in result.stdout
