from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
POWERSHELL_ENGINES = list(dict.fromkeys(filter(None, [POWERSHELL, shutil.which("powershell.exe")])))
LOCAL_TOKEN = "8fd04a283e5149fc963a5bd2c753fc70"
EXPLICIT_TOKEN = "4f0528dd448243549128af62a86f1f61"


def _run_isolated_start(
    tmp_path: Path,
    *arguments: str,
    saved_configuration: str = "active",
    environment: dict[str, str] | None = None,
    launcher_root_matches: bool = True,
    launcher_distro: str = "MistyAI-Configured",
    local_token: str = LOCAL_TOKEN,
    token_read_error: bool = False,
    powershell: str | None = POWERSHELL,
    development: bool = True,
) -> tuple[subprocess.CompletedProcess[str], list[dict[str, str]], list[str]]:
    """Exercise startup with fake processes, endpoints, and local credential I/O."""
    shutil.copy2(ROOT / "start.ps1", tmp_path / "start.ps1")
    shutil.copytree(ROOT / "presets", tmp_path / "presets")
    python_path = tmp_path / ".venv" / "Scripts" / "python.exe"
    python_path.parent.mkdir(parents=True)
    python_path.touch()
    runtime_dir = tmp_path / "data" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    manifest = tmp_path / "presets" / "runtime" / "grounded-mistyai-v1.json"
    if saved_configuration != "default":
        (runtime_dir / f"{saved_configuration}-start.json").write_text(
            json.dumps(
                {
                    "manifest_path": str(manifest),
                    "profile_path": str(tmp_path / "presets" / "homeostasis" / "functional-proxy-v1.json"),
                    "manifest_ref": "grounded-mistyai-v1",
                    "profile_ref": "functional-proxy-v1",
                }
            ),
            encoding="utf-8",
        )
    fixture = {
        "state": {
            "neuralcloud_root": str(tmp_path if launcher_root_matches else tmp_path / "other-checkout"),
            "mistyai_distro": launcher_distro,
        },
        "token": local_token,
        "token_read_error": token_read_error,
    }
    (tmp_path / "launcher-fixture.json").write_text(json.dumps(fixture), encoding="utf-8")
    module_dir = tmp_path / "tools" / "launcher"
    module_dir.mkdir(parents=True)
    module_source = (ROOT / "tools" / "launcher" / "Launcher.psm1").read_text(encoding="utf-8")
    # Keep the real module and its private-function boundary, replacing every
    # credential/state operation so the test cannot read the user's WSL or state.
    (module_dir / "Launcher.psm1").write_text(
        module_source
        + r"""
$script:FixtureRoot = [System.IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..\..'))
function Get-LauncherDataPaths {
    return [pscustomobject]@{ state = (Join-Path $script:FixtureRoot 'launcher-fixture.json') }
}
function Read-LauncherState {
    param([string]$Path)
    return (Get-Content -Raw -LiteralPath $Path | ConvertFrom-Json).state
}
function Read-MistyAIServiceToken {
    param([string]$Distro)
    Add-Content -LiteralPath (Join-Path $script:FixtureRoot 'token-reads.txt') -Value $Distro
    $fixture = Get-Content -Raw -LiteralPath (Join-Path $script:FixtureRoot 'launcher-fixture.json') | ConvertFrom-Json
    if ($fixture.token_read_error) { throw "Private backend failure: $($fixture.token)" }
    return [string]$fixture.token
}
function Invoke-LauncherWslCommand { throw 'A startup test must never access WSL.' }
""",
        encoding="utf-8",
    )
    runner = tmp_path / "run-start.ps1"
    runner.write_text(
        r"""
$ErrorActionPreference = 'Stop'
$global:StartTestRoot = $PSScriptRoot
$global:StartTestProcessId = 800000
function Get-NetTCPConnection { return $null }
function netstat { return $null }
function Get-Process { return $null }
function Stop-Process { throw 'A startup test must never stop a real process.' }
function Start-Process {
    [CmdletBinding()]
    param($FilePath, $ArgumentList, $WorkingDirectory, $RedirectStandardOutput,
          $RedirectStandardError, $WindowStyle, [switch]$PassThru)
    [pscustomobject]@{
        module = [string]$ArgumentList[1]
        model_mode = [string]$env:NC_MODE
        token = [string]$env:NC_MISTYAI_TOKEN
        token_file = [string]$env:NC_MISTYAI_TOKEN_FILE
        base_url = [string]$env:NC_MISTYAI_BASE_URL
        voice_config = [string]$env:NC_VOICE_CONFIG
        web_config = [string]$env:NC_WEB_CONFIG
        provider = [string]$env:NC_EMBODIMENT_PROVIDER
        runtime_state_dir = [string]$env:NC_RUNTIME_STATE_DIR
        cognitive_state_path = [string]$env:NC_COGNITIVE_STATE_PATH
        homeostasis_state_dir = [string]$env:NC_HOMEOSTASIS_STATE_DIR
        gui_config = [string]$env:NC_GUI_CONFIG
        gui_presets = [string]$env:NC_GUI_PRESETS
        gui_port = [string]$env:NC_GUI_PORT
        control_mode = [string]$env:NC_CONTROL_MODE
        isolated_development = [string]$env:NC_ISOLATED_DEVELOPMENT
        profile_path = [string]$env:NC_HOMEOSTASIS_PROFILE_PATH
        qq_session_config = [string]$env:NC_QQ_SESSION_CONFIG
        qq_watcher_config = [string]$env:NC_QQ_WATCHER_CONFIG
        qq_direct_config = [string]$env:NC_QQ_DIRECT_CONFIG
        shared_memory_db = [string]$env:NC_SHARED_MEMORY_DB
        control_token_file = [string]$env:NC_CONTROL_TOKEN_FILE
        service_token_file = [string]$env:NC_SERVICE_TOKEN_FILE
        control_token_sha256 = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($env:NC_CONTROL_TOKEN))).Replace('-', '').ToLowerInvariant()
        service_token_sha256 = [BitConverter]::ToString([Security.Cryptography.SHA256]::Create().ComputeHash([Text.Encoding]::UTF8.GetBytes($env:NC_SERVICE_TOKEN))).Replace('-', '').ToLowerInvariant()
    } | ConvertTo-Json -Compress | Add-Content -LiteralPath (Join-Path $global:StartTestRoot 'launches.jsonl')
    $global:StartTestProcessId += 1
    return [pscustomobject]@{ Id = $global:StartTestProcessId; StartTime = (Get-Date) }
}
function Invoke-RestMethod {
    [CmdletBinding()]
    param($Uri, $Headers, $TimeoutSec, $Method, $ContentType, $Body)
    $manifest = Get-Content -Raw -LiteralPath $env:NC_RUNTIME_MANIFEST_PATH | ConvertFrom-Json
    return [pscustomobject]@{
        healthy = $true
        runtime = [pscustomobject]@{}
        lifecycle = 'RUNNING'
        embodiment = [pscustomobject]@{
            state = 'available'
            provider_id = $manifest.embodiment.body_provider
            body_instance_id = $manifest.embodiment.body_instance_id
            negotiated = [pscustomobject]@{ missing_required = @(); incompatible_required = @() }
            activated_capabilities = @($manifest.embodiment.capability_requirements | ForEach-Object { $_.capability })
        }
    }
}
& (Join-Path $PSScriptRoot 'start.ps1') @args
exit $LASTEXITCODE
""".lstrip(),
        encoding="utf-8",
    )
    env = {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("NC_MISTYAI_") and key.upper() not in {
            "NC_EMBODIMENT_PROVIDER", "NC_CONTROL_TOKEN_FILE", "NC_SERVICE_TOKEN_FILE",
            "NC_HOMEOSTASIS_STATE_DIR",
            "NC_RUNTIME_STATE_DIR", "NC_GUI_CONFIG", "NC_GUI_PRESETS",
            "NC_QQ_SESSION_CONFIG", "NC_QQ_WATCHER_CONFIG", "NC_SHARED_MEMORY_DB", "NC_VOICE_CONFIG",
        }
    }
    env.update(environment or {})
    result = subprocess.run(
        [str(powershell), "-NoProfile", "-NonInteractive", "-File", str(runner),
         *(["-Development"] if development else []), *arguments],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=20,
        check=False,
    )
    launches_path = tmp_path / "launches.jsonl"
    launches = (
        [json.loads(line) for line in launches_path.read_text(encoding="utf-8-sig").splitlines()]
        if launches_path.exists()
        else []
    )
    reads_path = tmp_path / "token-reads.txt"
    token_reads = reads_path.read_text(encoding="utf-8-sig").splitlines() if reads_path.exists() else []
    return result, launches, token_reads


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
@pytest.mark.parametrize("saved_configuration", ["active", "pending"])
def test_no_argument_start_reuses_local_credentials_for_saved_mistyai_configuration(
    tmp_path: Path, saved_configuration: str, powershell: str
) -> None:
    result, launches, token_reads = _run_isolated_start(
        tmp_path, saved_configuration=saved_configuration, powershell=powershell
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert token_reads == ["MistyAI-Configured"]
    assert [launch["module"] for launch in launches] == [
        "harness.cognitive.service",
        "harness.homeostasis.service",
        "harness.runtime.service",
        "gui.runtime_server",
    ]
    assert [launch["token"] for launch in launches] == ["", "", LOCAL_TOKEN, ""]
    assert launches[2]["base_url"] == "http://127.0.0.1:8787"
    assert not (tmp_path / "data" / "runtime" / "pending-start.json").exists()
    assert LOCAL_TOKEN not in result.stdout + result.stderr
    assert LOCAL_TOKEN not in (tmp_path / ".neuralcloud.pids.json").read_text(encoding="utf-8-sig")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
def test_no_argument_default_start_does_not_require_mistyai_credentials(tmp_path: Path, powershell: str) -> None:
    result, launches, token_reads = _run_isolated_start(
        tmp_path, saved_configuration="default", powershell=powershell, development=False
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert len(launches) == 4
    assert not token_reads
    assert all(not launch["token"] and not launch["token_file"] for launch in launches)
    assert all(launch["gui_port"] == "8790" for launch in launches)
    assert all(launch["control_mode"] == "study_runtime" for launch in launches)
    assert all(launch["isolated_development"] == "0" for launch in launches)
    assert all(Path(launch["profile_path"]).name == "qq-social-drive-v1.json" for launch in launches)


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    ("launcher_root_matches", "launcher_distro"), [(False, "Other-Checkout-Distro"), (True, "")]
)
def test_local_token_lookup_defaults_to_supported_distro_without_matching_saved_distro(
    tmp_path: Path, launcher_root_matches: bool, launcher_distro: str
) -> None:
    result, _, token_reads = _run_isolated_start(
        tmp_path,
        environment={"NC_MISTYAI_BASE_URL": "http://localhost:8787/"},
        launcher_root_matches=launcher_root_matches,
        launcher_distro=launcher_distro,
    )

    assert result.returncode == 0, result.stderr or result.stdout
    assert token_reads == ["Ubuntu-24.04"]


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("credential_source", ["token", "file_argument", "file_environment"])
def test_explicit_mistyai_credentials_take_precedence_over_local_lookup(
    tmp_path: Path, credential_source: str
) -> None:
    token_file = tmp_path / "explicit.token"
    token_file.write_text(EXPLICIT_TOKEN + "\n", encoding="utf-8")
    arguments = ("-MistyAITokenFile", str(token_file)) if credential_source == "file_argument" else ()
    environment = {"NC_MISTYAI_TOKEN": EXPLICIT_TOKEN if credential_source == "token" else "placeholder"}
    if credential_source == "file_environment":
        environment["NC_MISTYAI_TOKEN_FILE"] = str(token_file)
    if credential_source == "file_argument":
        environment["NC_MISTYAI_TOKEN_FILE"] = str(tmp_path / "missing-lower-priority.token")
    result, launches, token_reads = _run_isolated_start(tmp_path, *arguments, environment=environment)

    assert result.returncode == 0, result.stderr or result.stdout
    assert not token_reads
    runtime = launches[2]
    assert runtime["token"] == (EXPLICIT_TOKEN if credential_source == "token" else "")
    assert runtime["token_file"] == (str(token_file) if credential_source != "token" else "")
    assert EXPLICIT_TOKEN not in result.stdout + result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("credential_source", ["token", "file"])
def test_invalid_explicit_mistyai_credentials_are_not_replaced_by_local_credentials(
    tmp_path: Path, credential_source: str
) -> None:
    environment = {"NC_MISTYAI_TOKEN": "placeholder"}
    if credential_source == "file":
        environment = {"NC_MISTYAI_TOKEN_FILE": str(tmp_path / "missing.token")}
    result, launches, token_reads = _run_isolated_start(tmp_path, environment=environment)

    assert result.returncode == 1
    assert "Configuration error:" in result.stdout
    assert not launches
    assert not token_reads


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    "base_url", ["https://mistyai.example", "http://127.0.0.1:9999", "https://localhost:8787"]
)
def test_local_credentials_are_not_automatically_used_for_other_mistyai_origins(
    tmp_path: Path, base_url: str
) -> None:
    result, launches, token_reads = _run_isolated_start(
        tmp_path, environment={"NC_MISTYAI_BASE_URL": base_url}
    )

    assert result.returncode == 1
    assert "Configuration error:" in result.stdout
    assert not launches
    assert not token_reads


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(("local_token", "token_read_error"), [(LOCAL_TOKEN, True), ("placeholder", False)])
def test_unavailable_or_invalid_local_credentials_fail_before_launch_without_leaking_token(
    tmp_path: Path, local_token: str, token_read_error: bool
) -> None:
    result, launches, token_reads = _run_isolated_start(
        tmp_path, local_token=local_token, token_read_error=token_read_error
    )

    assert result.returncode == 1
    assert "Configuration error:" in result.stdout
    assert not launches
    assert token_reads == ["MistyAI-Configured"]
    assert LOCAL_TOKEN not in result.stdout + result.stderr
    assert "Private backend failure" not in result.stdout + result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    ("provider", "expected_error"),
    [
        ("none", "required embodiment manifest cannot use provider none"),
        ("simulator", "required embodiment provider mismatch"),
    ],
)
def test_required_misty_manifest_rejects_provider_override_before_launch(
    provider: str,
    expected_error: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("NC_MISTYAI_TOKEN", None)
    environment.pop("NC_MISTYAI_TOKEN_FILE", None)
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "start.ps1"),
            "-Development",
            "-ManifestPath",
            str(ROOT / "presets" / "runtime" / "grounded-mistyai-v1.json"),
            "-EmbodimentProvider",
            provider,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1
    assert expected_error in result.stdout.lower()
    assert "Starting " not in result.stdout


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_acceptance_state_is_isolated_and_tokens_are_not_printed(tmp_path):
    source = tmp_path / "settings-source.json"
    source.write_text(json.dumps({"api_key": "fixture-local-model-key", "model": "fixture"}))
    state = tmp_path / "acceptance state"
    result, launches, _ = _run_isolated_start(
        tmp_path, "-StateRoot", str(state), saved_configuration="default",
        environment={"NC_GUI_CONFIG": str(source)},
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    for launch in launches:
        assert Path(launch["runtime_state_dir"]) == state / "runtime"
        assert Path(launch["cognitive_state_path"]) == state / "cognitive" / "state.json"
        assert Path(launch["homeostasis_state_dir"]) == state / "homeostasis"
        assert Path(launch["gui_config"]) == state / "gui_settings.json"
        assert Path(launch["gui_presets"]) == state / "presets"
    assert (state / "gui_settings.json").read_bytes() == source.read_bytes()
    assert not (tmp_path / "data" / "runtime" / "active-start.json").exists()
    assert (state / "runtime" / "active-start.json").exists()
    registry = json.loads((state / ".neuralcloud.pids.json").read_text(encoding="utf-8-sig"))
    assert not (tmp_path / ".neuralcloud.pids.json").exists()
    assert all(launch["isolated_development"] == "1" for launch in launches)
    for name in ("control", "service"):
        secret_file = Path(registry[f"{name}_token_file"])
        token = secret_file.read_text()
        assert len(token) >= 32 and token not in result.stdout + result.stderr
    assert "fixture-local-model-key" not in result.stdout + result.stderr


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_normal_start_cannot_create_independent_state(tmp_path):
    result, launches, _ = _run_isolated_start(
        tmp_path, "-StateRoot", str(tmp_path / "independent"), development=False,
    )
    assert result.returncode == 1
    assert "requires -Development" in result.stdout
    assert not launches
    assert not (tmp_path / "independent").exists()


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_normal_start_rejects_explicit_development_manifest(tmp_path):
    result, launches, _ = _run_isolated_start(
        tmp_path, "-ManifestPath", str(tmp_path / "presets/runtime/development-demo.json"),
        development=False,
    )
    assert result.returncode == 1
    assert "require -Development" in result.stdout
    assert not launches


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
def test_normal_start_does_not_load_legacy_saved_development_manifest(tmp_path):
    result, launches, reads = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert launches and all(launch["control_mode"] == "study_runtime" for launch in launches)
    assert not reads


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize("explicit", [True, False])
def test_normal_shared_start_preserves_canonical_state_and_channel_bindings(tmp_path, explicit):
    canonical = tmp_path / "private" / "canonical-domain"
    canonical.mkdir(parents=True)
    sentinel = canonical / "state.json"
    sentinel.write_text('{"synthetic_existing_state":true}')
    bindings = {
        "NC_QQ_SESSION_CONFIG": str(tmp_path / "session.json"),
        "NC_QQ_WATCHER_CONFIG": str(tmp_path / "watcher.json"),
        "NC_SHARED_MEMORY_DB": str(tmp_path / "shared.sqlite3"),
        "NC_HOMEOSTASIS_STATE_DIR": str(canonical if not explicit else tmp_path / "ignored"),
    }
    result, launches, _ = _run_isolated_start(
        tmp_path, *(["-HomeostasisStateDir", str(canonical)] if explicit else []),
        development=False, environment=bindings,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert sentinel.read_text() == '{"synthetic_existing_state":true}'
    assert not (tmp_path / "data/homeostasis").exists()
    for launch in launches:
        assert Path(launch["homeostasis_state_dir"]) == canonical
        assert launch["qq_session_config"] == bindings["NC_QQ_SESSION_CONFIG"]
        assert launch["qq_watcher_config"] == bindings["NC_QQ_WATCHER_CONFIG"]
        assert launch["shared_memory_db"] == bindings["NC_SHARED_MEMORY_DB"]
        assert launch["control_mode"] == "study_runtime"
