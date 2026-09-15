from __future__ import annotations

import json

import pytest

from tests.test_dev_launcher import POWERSHELL, _invoke_module_json, _ps_literal
from tests.test_start_script import _run_isolated_start


pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


def test_normal_duplicate_start_does_not_stop_owner_or_replace_registry(tmp_path):
    registry = tmp_path / ".neuralcloud.pids.json"
    registry.write_text('{"synthetic_owned_registry":true}')
    before = registry.read_bytes()
    result, launches, _ = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 1
    assert "No owner was stopped" in result.stdout
    assert not launches
    assert registry.read_bytes() == before


@pytest.mark.parametrize("available", [True, False])
def test_readiness_uses_manifest_provider_and_gui_8790(tmp_path, available):
    manifest = tmp_path / "manifest.json"
    manifest.write_text(json.dumps({"embodiment": {
        "mode": "off" if available else "grounded",
        "body_provider": "none" if available else "mistyai",
        "capability_requirements": [],
    }}))
    result = _invoke_module_json(tmp_path, f"""
function Start-Sleep {{}}
function Invoke-RestMethod {{
    param($Method, $Uri, $TimeoutSec)
    if ($Uri -ne 'http://127.0.0.1:8790/api/status') {{ throw 'Wrong GUI port' }}
    return [pscustomobject]@{{ runtime = [pscustomobject]@{{
        lifecycle = 'RUNNING'
        embodiment = [pscustomobject]@{{ state = 'disabled'; provider_id = 'none' }}
    }} }}
}}
try {{
    $status = Wait-NeuralCloudReady -ManifestPath {_ps_literal(manifest)} -TimeoutSeconds 0
    return @{{ ready = $true; lifecycle = $status.runtime.lifecycle }}
}} catch {{ return @{{ ready = $false; reason = $_.Exception.Message }} }}
""")
    assert result["ready"] is available
    if not available:
        assert "Embodiment state is not available" in result["reason"]


@pytest.mark.parametrize("mode", [None, "demo", "live", "ollama"])
def test_start_launcher_keeps_auto_provider_and_new_default_manifest(tmp_path, mode):
    mode_argument = f"-Mode '{mode}'" if mode else ""
    result = _invoke_module_json(tmp_path, f"""
$script:Recorded = $null
function Get-LauncherPowerShellExecutable {{ return 'fixture-powershell' }}
function Invoke-LauncherExternalCommand {{
    param($FilePath, $ArgumentList, $WorkingDirectory, $Environment, $SensitiveValues, $CommandInvoker)
    $script:Recorded = @($ArgumentList)
    return [pscustomobject]@{{ exit_code = 0; stdout = ''; stderr = '' }}
}}
$result = Start-NeuralCloud -Root {_ps_literal(tmp_path)} {mode_argument} -Token 'synthetic-token'
return @{{ args = $script:Recorded; manifest = $result.manifest }}
""")
    arguments = result["args"]
    assert arguments[arguments.index("-EmbodimentProvider") + 1] == "auto"
    assert result["manifest"].endswith("shared-misty-v1.json")
    if mode:
        assert arguments[arguments.index("-Mode") + 1] == mode
    else:
        assert "-Mode" not in arguments


def test_default_dev_entrypoint_does_not_invent_a_model_override(tmp_path):
    from tests.test_dev_launcher import _copy_entrypoint, _install_fake_module, _run_script

    entrypoint = _copy_entrypoint(tmp_path)
    _install_fake_module(tmp_path, """
function Invoke-NeuralCloudLauncher {
    param($Command, $Root, $MistyAIPath, $Distro, $Mode, $NonInteractive, $NoOpen, $Json, $Service)
    if ($Mode) { throw 'Default entrypoint invented a model override' }
    return 0
}
Export-ModuleMember -Function Invoke-NeuralCloudLauncher
""")
    result = _run_script(entrypoint, "up", "-NoOpen", "-NonInteractive")
    assert result.returncode == 0, result.stdout + result.stderr
