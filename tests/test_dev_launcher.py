from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
DEV_SCRIPT = ROOT / "dev.ps1"
LAUNCHER_MODULE = ROOT / "tools" / "launcher" / "Launcher.psm1"
START_SCRIPT = ROOT / "start.ps1"
STOP_SCRIPT = ROOT / "stop.ps1"
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")
WINDOWS_POWERSHELL = shutil.which("powershell.exe")


pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_script(
    script: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
    timeout: float = 20,
) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    if environment:
        env.update(environment)
    return subprocess.run(
        [
            str(POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(script),
            *arguments,
        ],
        cwd=script.parent,
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _invoke_module_json(tmp_path: Path, body: str, *, environment: dict[str, str] | None = None):
    runner = tmp_path / "invoke-launcher-module.ps1"
    runner.write_text(
        "\n".join(
            [
                '$ErrorActionPreference = "Stop"',
                f"$launcher = Import-Module -Name {_ps_literal(LAUNCHER_MODULE)} -Force -PassThru",
                "$result = & $launcher {",
                body,
                "}",
                "$result | ConvertTo-Json -Depth 12 -Compress",
            ]
        ),
        encoding="utf-8",
    )
    completed = _run_script(runner, environment=environment)
    assert completed.returncode == 0, completed.stderr or completed.stdout
    output_lines = [line for line in completed.stdout.splitlines() if line.strip()]
    assert output_lines, "PowerShell module invocation returned no JSON"
    return json.loads(output_lines[-1])


def _copy_entrypoint(tmp_path: Path) -> Path:
    copied = tmp_path / "dev.ps1"
    shutil.copy2(DEV_SCRIPT, copied)
    return copied


def _install_fake_module(tmp_path: Path, implementation: str) -> None:
    module_dir = tmp_path / "tools" / "launcher"
    module_dir.mkdir(parents=True)
    (module_dir / "Launcher.psm1").write_text(implementation, encoding="utf-8")


def _json_stdout(completed: subprocess.CompletedProcess[str]) -> dict[str, object]:
    output = completed.stdout.strip()
    assert output, completed.stderr or "launcher returned no output"
    json_start = output.find("{")
    json_end = output.rfind("}")
    assert json_start >= 0 and json_end >= json_start, output
    return json.loads(output[json_start : json_end + 1])


def _write_mocked_doctor_runner(tmp_path: Path) -> tuple[Path, Path, Path]:
    neural_root = tmp_path / "neuralcloud"
    mistyai_root = tmp_path / "mistyai"
    (neural_root / "presets" / "runtime").mkdir(parents=True)
    (neural_root / "start.ps1").write_text("# test fixture\n", encoding="utf-8")
    (neural_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (neural_root / "presets" / "runtime" / "grounded-mistyai-v1.json").write_text("{}\n", encoding="utf-8")
    (mistyai_root / "deploy").mkdir(parents=True)
    (mistyai_root / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (mistyai_root / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (mistyai_root / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")

    runner = tmp_path / "mocked-doctor.ps1"
    runner.write_text(
        r"""
param(
    [Parameter(Mandatory = $true)][string]$ModulePath,
    [Parameter(Mandatory = $true)][string]$NeuralRoot,
    [Parameter(Mandatory = $true)][string]$MistyRoot,
    [switch]$JsonMode
)
$ErrorActionPreference = 'Stop'
Import-Module -Name $ModulePath -Force
$invoker = {
    param($request)
    $file = [System.IO.Path]::GetFileName([string]$request.FilePath).ToLowerInvariant()
    $arguments = @($request.ArgumentList)
    $joined = $arguments -join ' '
    $stdout = ''

    if ($file -eq 'git.exe') {
        $stdout = 'git version 2.50.0'
    } elseif ($file -in @('py.exe', 'python.exe', 'python')) {
        if ($joined -match '(?:^| )-m uv(?: |$)') { $stdout = 'uv 0.12.9' } else { $stdout = '3.12.9' }
    } elseif ($file -eq 'docker.exe') {
        if ($arguments -contains 'info') { $stdout = 'linux' } else { $stdout = 'Docker Compose version v2.39.0' }
    } elseif ($file -eq 'wsl.exe') {
        if ($arguments.Count -ge 2 -and $arguments[0] -eq '--list' -and $arguments[1] -eq '--quiet') {
            $stdout = "Ubuntu-24.04`n"
        } elseif ($arguments.Count -ge 2 -and $arguments[0] -eq '--list' -and $arguments[1] -eq '--verbose') {
            $stdout = "  NAME            STATE           VERSION`n* Ubuntu-24.04    Running         2`n"
        } elseif ($arguments -contains '--status') {
            $stdout = 'Default Version: 2'
        } elseif ($arguments -contains '-c') {
            $bashScript = [string]$arguments[-1]
            if ($bashScript -match 'python_path=') {
                $stdout = @'
ubuntu=ubuntu:24.04
systemd=yes
python_path=/usr/bin/python3.12
uv_path=/usr/local/bin/uv
docker_path=/usr/bin/docker
python=Python 3.12.9
uv=uv 0.12.9
docker=linux
compose=Docker Compose version v2.39.0
'@
            } elseif ($bashScript -match 'test -x /opt/mistyai/.venv/bin/mistyctl') {
                $stdout = "deployment=no`nconfig=no`nunsafe_paths=no`nunits=missing`nbody_env=missing`nbody_token=missing`nagent_token_file=missing`nnovnc_password=missing`ninstance_dir=missing`n"
            } elseif ($bashScript -match 'classify\(\)') {
                $stdout = "current=absent`nprevious=absent`ncurrent_fingerprint=`ncurrent_image=`ncurrent_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
            }
        }
    }
    return [pscustomobject]@{ ExitCode = 0; StdOut = $stdout; StdErr = '' }
}
$invoke = @{
    Command = 'doctor'
    Root = $NeuralRoot
    MistyAIPath = $MistyRoot
    Distro = 'Ubuntu-24.04'
    Mode = 'demo'
    NonInteractive = $true
    NoOpen = $true
    Json = [bool]$JsonMode
    Service = 'all'
    CommandInvoker = $invoker
}
$exitCode = Invoke-NeuralCloudLauncher @invoke
exit $exitCode
""".lstrip(),
        encoding="utf-8",
    )
    return runner, neural_root, mistyai_root


def test_new_launcher_check_uses_stable_machine_readable_shape(tmp_path: Path) -> None:
    check = _invoke_module_json(
        tmp_path,
        """
        New-LauncherCheck `
            -Id "windows.python" `
            -Current "3.11.9" `
            -Required "3.12.x" `
            -Status "missing" `
            -Remediation "Install Python 3.12" `
            -RestartRequired `
            -Category "windows"
        """,
    )

    assert check == {
        "id": "windows.python",
        "category": "windows",
        "current": "3.11.9",
        "required": "3.12.x",
        "status": "missing",
        "remediation": "Install Python 3.12",
        "restart_required": True,
    }


@pytest.mark.parametrize(
    ("current", "required", "expected"),
    [
        ("3.12.0", "3.12.0", True),
        ("3.12.9", "3.12.0", True),
        ("3.11.9", "3.12.0", False),
        ("0.12.8", "0.12.9", False),
    ],
)
def test_launcher_version_comparison(
    tmp_path: Path,
    current: str,
    required: str,
    expected: bool,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        f"Test-LauncherVersion -Current {_ps_literal(current)} -Required {_ps_literal(required)}",
    )

    assert result is expected


def test_doctor_distinguishes_configuration_conflicts_from_missing_dependencies(
    tmp_path: Path,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        r"""
        function Get-LauncherPrerequisiteChecks {
            param(
                [string]$Root, [string]$MistyAIPath, [string]$Distro,
                [scriptblock]$CommandInvoker, [switch]$IncludeRuntimePorts
            )
            return @($script:DoctorPrerequisiteChecks)
        }
        function Get-MistyAIInstallationChecks {
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return @($script:DoctorInstallationChecks)
        }
        function Invoke-DoctorCase {
            param([object[]]$PrerequisiteChecks, [object[]]$InstallationChecks = @())
            $script:DoctorPrerequisiteChecks = @($PrerequisiteChecks)
            $script:DoctorInstallationChecks = @($InstallationChecks)
            return Invoke-LauncherDoctorCommand `
                -Root 'C:\NeuralCloud' `
                -MistyAIPath 'C:\MistyAI' `
                -Distro 'Ubuntu-24.04'
        }

        $wslReady = New-LauncherCheck `
            -Id 'windows.wsl_distro' -Category 'windows' -Current 'Ubuntu-24.04' `
            -Required 'Ubuntu-24.04' -Status 'pass'
        $ubuntuReady = New-LauncherCheck `
            -Id 'wsl.ubuntu' -Category 'wsl' -Current 'ubuntu:24.04' `
            -Required 'ubuntu:24.04' -Status 'pass'
        $systemdReady = New-LauncherCheck `
            -Id 'wsl.systemd' -Category 'wsl' -Current 'running' `
            -Required 'systemd enabled' -Status 'pass'
        $repositoryConflict = New-LauncherCheck `
            -Id 'repo.mistyai' -Category 'repository' -Current 'missing' `
            -Required 'MistyAI repository' -Status 'blocked'
        $runtimeConflict = New-LauncherCheck `
            -Id 'runtime.port.8787' -Category 'runtime' -Current 'PID=1234' `
            -Required 'free or launcher-owned' -Status 'blocked'
        $deploymentConflict = New-LauncherCheck `
            -Id 'mistyai.deployment_ownership' -Category 'mistyai' -Current 'unmanaged' `
            -Required 'launcher-managed or absent' -Status 'blocked'
        $ordinaryMissing = New-LauncherCheck `
            -Id 'windows.git' -Category 'windows' -Current 'missing' `
            -Required 'Git' -Status 'missing'

        [pscustomobject]@{
            repository = Invoke-DoctorCase -PrerequisiteChecks @($wslReady, $repositoryConflict)
            runtime = Invoke-DoctorCase -PrerequisiteChecks @($wslReady, $runtimeConflict)
            deployment = Invoke-DoctorCase `
                -PrerequisiteChecks @($wslReady, $ubuntuReady, $systemdReady) `
                -InstallationChecks @($deploymentConflict)
            missing = Invoke-DoctorCase -PrerequisiteChecks @($ordinaryMissing)
        }
        """,
    )

    assert result["repository"]["exit_code"] == 40
    assert result["runtime"]["exit_code"] == 40
    assert result["deployment"]["exit_code"] == 40
    assert result["missing"]["exit_code"] == 10
    assert result["repository"]["status"] == "blocked"
    assert result["runtime"]["status"] == "blocked"
    assert result["deployment"]["status"] == "blocked"
    assert result["missing"]["status"] == "action_required"


@pytest.mark.parametrize(
    ("relative_path", "expected"),
    [
        (".env", True),
        ("config/.env.local", True),
        ("secrets/body-service.token", True),
        ("certificates/client.pem", True),
        ("certificates/client.key", True),
        ("certificates/client.ppk", True),
        (".npmrc", True),
        (".pypirc", True),
        (".netrc", True),
        (".docker/config.json", True),
        ("gcloud/application_default_credentials.json", True),
        (".kube/config", True),
        ("clusters/kubeconfig", True),
        (".git-credentials", True),
        ("terraform/production.tfstate", True),
        ("terraform/production.tfstate.backup", True),
        (".env.example", False),
        ("docs/token-rotation.md", False),
        ("src/tokenizer.py", False),
    ],
)
def test_sensitive_workspace_path_classification(
    tmp_path: Path,
    relative_path: str,
    expected: bool,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        f"Test-SensitiveRelativePath -Path {_ps_literal(relative_path)}",
    )

    assert result is expected


def test_launcher_text_redaction_removes_explicit_and_bearer_secrets(tmp_path: Path) -> None:
    explicit_secret = "launcher-test-explicit-secret-314159"
    bearer_secret = "launcher-test-bearer-secret-271828"
    redacted = _invoke_module_json(
        tmp_path,
        """
        Protect-LauncherText `
            -Text ("token={0}; Authorization: Bearer {1}" -f $env:NC_TEST_EXPLICIT_SECRET, $env:NC_TEST_BEARER_SECRET) `
            -Secrets @($env:NC_TEST_EXPLICIT_SECRET)
        """,
        environment={
            "NC_TEST_EXPLICIT_SECRET": explicit_secret,
            "NC_TEST_BEARER_SECRET": bearer_secret,
        },
    )

    assert explicit_secret not in redacted
    assert bearer_secret not in redacted
    assert "redact" in redacted.lower()


def test_launcher_text_redaction_handles_quoted_credentials(tmp_path: Path) -> None:
    single_quoted_secret = "launcher-test-single-quoted-secret-141421"
    double_quoted_secret = "launcher-test-double-quoted-secret-173205"
    redacted = _invoke_module_json(
        tmp_path,
        "Protect-LauncherText -Text $env:NC_TEST_QUOTED_CREDENTIALS",
        environment={
            "NC_TEST_QUOTED_CREDENTIALS": (
                f"NC_MISTYAI_TOKEN='{single_quoted_secret}'; "
                f'Authorization: Bearer "{double_quoted_secret}"'
            )
        },
    )

    assert single_quoted_secret not in redacted
    assert double_quoted_secret not in redacted
    assert redacted.lower().count("redacted") == 2


def test_private_key_marker_is_detected_beyond_the_first_read_buffer(tmp_path: Path) -> None:
    candidate = tmp_path / "innocent-looking.txt"
    candidate.write_text(
        ("x" * 9000) + "\n-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n",
        encoding="utf-8",
    )

    result = _invoke_module_json(
        tmp_path,
        f"Test-LauncherPrivateKeyContent -Path {_ps_literal(candidate)}",
    )

    assert result is True


@pytest.mark.parametrize(
    ("relative_path", "content"),
    [
        ("keys/misty-body.ppk", "not otherwise recognizable as a private key\n"),
        ("notes/putty-v2.txt", "PuTTY-User-Key-File-2: ssh-rsa\nEncryption: none\n"),
        ("notes/putty-v3.txt", "PuTTY-User-Key-File-3: ssh-ed25519\nEncryption: none\n"),
    ],
)
def test_workspace_snapshot_rejects_ppk_paths_and_putty_private_key_headers(
    tmp_path: Path,
    relative_path: str,
    content: str,
) -> None:
    workspace = tmp_path / "sensitive-workspace"
    candidate = workspace / Path(relative_path)
    candidate.parent.mkdir(parents=True)
    candidate.write_text(content, encoding="utf-8")
    staging = tmp_path / "sensitive-staging"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:gitCalls = 0
        try {{
            New-MistyAIWorkspaceSnapshot `
                -Root {_ps_literal(workspace)} `
                -StagingDirectory {_ps_literal(staging)} `
                -CommandInvoker {{
                    param($request)
                    $script:gitCalls++
                    return [pscustomobject]@{{
                        exit_code = 0
                        stdout = ({_ps_literal(relative_path)} + [char]0)
                        stderr = ''
                    }}
                }} | Out-Null
        }} catch {{
            [pscustomobject]@{{
                exit_code = Get-LauncherFailureCode -ErrorRecord $_ -Default 20
                error = $_.Exception.Message
                git_calls = $script:gitCalls
                staging_exists = Test-Path -LiteralPath {_ps_literal(staging)}
            }}
        }}
        """,
    )

    assert result["exit_code"] == 40
    assert relative_path in result["error"]
    assert result["git_calls"] == 1
    assert result["staging_exists"] is False


def test_unverified_wsl_port_owner_is_not_treated_as_mistyai(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        f"""
        $owner = [pscustomobject]@{{
            pid = 1234
            process_name = 'wslrelay'
            command_line = 'wslrelay --port 8787'
        }}
        [pscustomobject]@{{
            unverified = Test-LauncherKnownPortOwner -Owner $owner -Port 8787 -Root {_ps_literal(tmp_path)}
            verified = Test-LauncherKnownPortOwner -Owner $owner -Port 8787 -Root {_ps_literal(tmp_path)} -KnownMistyPorts @(8787)
        }}
        """,
    )

    assert result == {"unverified": False, "verified": True}


def test_mistyai_compose_probe_classifies_any_running_incomplete_set_as_partial(
    tmp_path: Path,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        """
        $script:probeScript = ''
        $state = Get-MistyAIServiceState `
            -Distro 'Ubuntu-24.04' `
            -CommandInvoker {
                param($request)
                $script:probeScript = [string]$request.ArgumentList[-1]
                # Simulate one running container whose health is not sufficient
                # for the complete three-service healthy set.
                return [pscustomobject]@{
                    exit_code = 0
                    stdout = "mistyai-body.service=inactive`nmistyai-body.service-enabled=disabled`nmistyai-operator.service=inactive`nmistyai-operator.service-enabled=disabled`ncompose=partial`n"
                    stderr = ''
                }
            }
        [pscustomobject]@{
            state = $state
            probe_script = $script:probeScript
        }
        """,
    )

    assert result["state"] == {
        "body": "inactive",
        "operator": "inactive",
        "body_enabled": "disabled",
        "operator_enabled": "disabled",
        "compose": "partial",
        "running": False,
    }
    probe_script = result["probe_script"]
    assert "container_count=0" in probe_script
    assert "running_count=0" in probe_script
    assert "healthy_count=0" in probe_script
    assert (
        'test "$container_count" -eq 3 && test "$running_count" -eq 3 '
        '&& test "$healthy_count" -eq 3'
    ) in probe_script
    assert 'elif test "$running_count" -gt 0; then' in probe_script
    assert "compose_state=partial" in probe_script


def test_stop_mistyai_without_owned_components_is_a_noop(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        """
        Stop-MistyAI -Distro 'Ubuntu-24.04' -CommandInvoker {
            throw 'external command runner must not be invoked'
        }
        """,
    )

    assert result["success"] is True
    assert result["skipped"] is True
    assert result["reason"] == "not_owned"


def test_stop_mistyai_fails_closed_when_mistyctl_is_missing(tmp_path: Path) -> None:
    fingerprint = "a" * 64
    image_id = "sha256:" + ("b" * 64)
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:composeStopScript = ''
        $stopped = Stop-MistyAI `
            -Distro 'Ubuntu-24.04' `
            -StopCompose `
            -ExpectedFingerprint '{fingerprint}' `
            -CommandInvoker {{
                param($request)
                $bash = [string]$request.ArgumentList[-1]
                if ($bash -match 'classify /opt/mistyai current') {{
                    return [pscustomobject]@{{
                        exit_code = 0
                        stdout = "current=managed`nprevious=absent`ncurrent_fingerprint={fingerprint}`ncurrent_image={image_id}`ncurrent_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
                        stderr = ''
                    }}
                }}
                if ($bash -match 'env down --json') {{
                    $script:composeStopScript = $bash
                    return [pscustomobject]@{{
                        exit_code = 68
                        stdout = ''
                        stderr = 'managed MistyAI mistyctl is unavailable'
                    }}
                }}
                return [pscustomobject]@{{ exit_code = 0; stdout = ''; stderr = '' }}
            }}
        [pscustomobject]@{{
            stopped = $stopped
            compose_stop_script = $script:composeStopScript
        }}
        """,
    )

    stopped = result["stopped"]
    compose_script = result["compose_stop_script"]
    assert stopped["success"] is False
    assert "mistyctl" in stopped["environment_error"].lower()
    assert "if ! test -x /opt/mistyai/.venv/bin/mistyctl" in compose_script
    assert "refusing to forget Compose ownership" in compose_script
    assert "exit 68" in compose_script
    assert not re.search(
        r"if\s+test\s+-x\s+[^;]+;\s*then\s+[^;]+env\s+down[^;]+;\s*fi",
        compose_script,
    )


def test_stop_mistyai_fails_when_compose_containers_remain(tmp_path: Path) -> None:
    fingerprint = "c" * 64
    image_id = "sha256:" + ("d" * 64)
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:composeStopScript = ''
        $stopped = Stop-MistyAI `
            -Distro 'Ubuntu-24.04' `
            -StopCompose `
            -ExpectedFingerprint '{fingerprint}' `
            -CommandInvoker {{
                param($request)
                $bash = [string]$request.ArgumentList[-1]
                if ($bash -match 'classify /opt/mistyai current') {{
                    return [pscustomobject]@{{
                        exit_code = 0
                        stdout = "current=managed`nprevious=absent`ncurrent_fingerprint={fingerprint}`ncurrent_image={image_id}`ncurrent_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
                        stderr = ''
                    }}
                }}
                if ($bash -match 'env down --json') {{
                    $script:composeStopScript = $bash
                    return [pscustomobject]@{{
                        exit_code = 69
                        stdout = ''
                        stderr = 'MistyAI Compose service still has containers after down: agent'
                    }}
                }}
                return [pscustomobject]@{{ exit_code = 0; stdout = ''; stderr = '' }}
            }}
        [pscustomobject]@{{
            stopped = $stopped
            compose_stop_script = $script:composeStopScript
        }}
        """,
    )

    stopped = result["stopped"]
    compose_script = result["compose_stop_script"]
    assert stopped["success"] is False
    assert "still has containers" in stopped["environment_error"].lower()
    assert "for service in desktop agent loopback-gateway" in compose_script
    assert re.search(
        r'compose\s+-f\s+/opt/mistyai/deploy/compose\.yaml\s+ps\s+-aq\s+"\$service"',
        compose_script,
    )
    assert "exit 69" in compose_script


def test_down_with_fresh_state_does_not_invoke_external_commands(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{ state = {_ps_literal(state_path)} }}
        Invoke-LauncherDownCommand `
            -Root {_ps_literal(tmp_path)} `
            -Distro 'Ubuntu-24.04' `
            -State $state `
            -Paths $paths `
            -CommandInvoker {{ throw 'external command runner must not be invoked' }}
        """,
    )

    assert result["status"] == "stopped"
    assert result["neuralcloud"]["reason"] == "not_owned"
    assert result["mistyai"]["reason"] == "not_owned"


def test_down_does_not_touch_mistyai_when_neuralcloud_stop_fails(tmp_path: Path) -> None:
    state_path = tmp_path / "state.json"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $state = Get-LauncherDefaultState
        $state.neuralcloud_root = [System.IO.Path]::GetFullPath({_ps_literal(tmp_path)})
        $state.mistyai_distro = 'Ubuntu-24.04'
        $state.owned.neuralcloud = $true
        $state.owned.mistyai.body = $true
        $paths = [pscustomobject]@{{ state = {_ps_literal(state_path)} }}
        $script:invocations = 0
        try {{
            Invoke-LauncherDownCommand `
                -Root {_ps_literal(tmp_path)} `
                -Distro 'Ubuntu-24.04' `
                -State $state `
                -Paths $paths `
                -CommandInvoker {{
                    param($request)
                    $script:invocations++
                    [pscustomobject]@{{ exit_code = 1; stdout = ''; stderr = 'stop failed' }}
                }} | Out-Null
        }} catch {{
            [pscustomobject]@{{
                invocations = $script:invocations
                exit_code = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            }}
        }}
        """,
    )

    assert result == {"invocations": 1, "exit_code": 30}


@pytest.mark.parametrize("command", ["install", "up"])
@pytest.mark.parametrize(
    ("body", "operator", "compose", "body_enabled", "operator_enabled"),
    [
        ("activating", "inactive", "stopped", "disabled", "disabled"),
        ("unknown", "inactive", "stopped", "disabled", "disabled"),
        ("inactive", "deactivating", "stopped", "disabled", "disabled"),
        ("inactive", "unknown", "stopped", "disabled", "disabled"),
        ("inactive", "inactive", "partial", "disabled", "disabled"),
        ("inactive", "inactive", "unhealthy", "disabled", "disabled"),
        ("inactive", "inactive", "unknown", "disabled", "disabled"),
        ("inactive", "inactive", "stopped", "unknown", "disabled"),
        ("inactive", "inactive", "stopped", "disabled", "enabling"),
    ],
)
def test_install_and_up_reject_unstable_mistyai_initial_state_before_deploy_or_start(
    tmp_path: Path,
    command: str,
    body: str,
    operator: str,
    compose: str,
    body_enabled: str,
    operator_enabled: str,
) -> None:
    neuralcloud_root = tmp_path / f"neuralcloud-{command}"
    mistyai_root = tmp_path / f"mistyai-{command}"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / f"{command}-unstable-state.json"
    staging = tmp_path / f"{command}-unstable-staging"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:deployCalls = 0
        $script:initializeCalls = 0
        $script:startCalls = 0

        function Assert-LauncherPrerequisites {{
            param(
                [string]$Root, [string]$MistyAIPath, [string]$Distro,
                [switch]$IncludeRuntimePorts, [switch]$NonInteractive,
                [switch]$Json, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            param([string]$Root, [object]$State, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{ changed = $false; fingerprint = 'neural-stability-test' }}
        }}
        function Get-MistyAIServiceState {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{
                body = '{body}'
                operator = '{operator}'
                body_enabled = '{body_enabled}'
                operator_enabled = '{operator_enabled}'
                compose = '{compose}'
                running = $false
            }}
        }}
        function Deploy-MistyAIWorkspace {{
            param(
                [string]$MistyAIPath, [string]$Distro, [object]$State,
                [string]$StagingDirectory, [scriptblock]$CommandInvoker
            )
            $script:deployCalls++
            return [pscustomobject]@{{
                changed = $false
                fingerprint = ('7' * 64 -join '')
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{
            param(
                [string]$Distro, [bool]$DeploymentChanged,
                [switch]$StartEnvironment, [switch]$PreserveBody,
                [switch]$PreserveOperator, [switch]$PreserveCompose,
                [scriptblock]$CommandInvoker
            )
            $script:initializeCalls++
        }}
        function Read-MistyAIServiceToken {{ return 'stable-state-test-token-1234567890' }}
        function Wait-MistyAIReady {{ return [pscustomobject]@{{ body_instance_id = 'm0' }} }}
        function Start-NeuralCloud {{
            $script:startCalls++
            return [pscustomobject]@{{ output = ''; manifest = 'grounded-mistyai-v1.json' }}
        }}
        function Wait-NeuralCloudReady {{
            return [pscustomobject]@{{ runtime = [pscustomobject]@{{ lifecycle = 'RUNNING' }} }}
        }}
        function Test-NeuralCloudRegistryHealth {{
            return [pscustomobject]@{{ running = $true; services = @() }}
        }}

        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{
            state = {_ps_literal(state_path)}
            staging = {_ps_literal(staging)}
        }}
        $failureCode = 0
        $failureMessage = ''
        try {{
            if ('{command}' -eq 'install') {{
                Invoke-LauncherInstallCommand `
                    -Root {_ps_literal(neuralcloud_root)} `
                    -MistyAIPath {_ps_literal(mistyai_root)} `
                    -Distro 'Ubuntu-24.04' `
                    -State $state `
                    -Paths $paths `
                    -NonInteractive `
                    -CommandInvoker {{ throw 'external command runner must not be invoked' }} | Out-Null
            }} else {{
                Invoke-LauncherUpCommand `
                    -Root {_ps_literal(neuralcloud_root)} `
                    -MistyAIPath {_ps_literal(mistyai_root)} `
                    -Distro 'Ubuntu-24.04' `
                    -Mode 'demo' `
                    -State $state `
                    -Paths $paths `
                    -NoOpen `
                    -CommandInvoker {{ throw 'external command runner must not be invoked' }} | Out-Null
            }}
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            deploy_calls = $script:deployCalls
            initialize_calls = $script:initializeCalls
            start_calls = $script:startCalls
            final_state = Get-Content -Raw -LiteralPath {_ps_literal(state_path)} | ConvertFrom-Json
        }}
        """,
    )

    assert result["failure_code"] == 20
    assert "partial or transitional" in result["failure_message"].lower()
    assert result["deploy_calls"] == 0
    assert result["initialize_calls"] == 0
    assert result["start_calls"] == 0
    assert result["final_state"]["owned"]["mistyai"] == {
        "body": False,
        "operator": False,
        "compose": False,
    }


def test_install_persists_conservative_ownership_then_restores_prior_ownership(
    tmp_path: Path,
) -> None:
    neuralcloud_root = tmp_path / "neuralcloud-install"
    mistyai_root = tmp_path / "mistyai-install"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / "install-state.json"
    staging = tmp_path / "install-staging"
    fingerprint = "f" * 64
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:installStatePath = {_ps_literal(state_path)}
        $script:beforeInitialize = $null

        function Assert-LauncherPrerequisites {{
            param(
                [string]$Root, [string]$MistyAIPath, [string]$Distro,
                [switch]$IncludeRuntimePorts, [switch]$NonInteractive,
                [switch]$Json, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            param([string]$Root, [object]$State, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{ changed = $false; fingerprint = 'neural-install-test' }}
        }}
        function Get-MistyAIServiceState {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{
                body = 'active'
                operator = 'inactive'
                body_enabled = 'enabled'
                operator_enabled = 'disabled'
                compose = 'stopped'
                running = $false
            }}
        }}
        function Deploy-MistyAIWorkspace {{
            param(
                [string]$MistyAIPath, [string]$Distro, [object]$State,
                [string]$StagingDirectory, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{
                changed = $true
                fingerprint = '{fingerprint}'
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{
            param(
                [string]$Distro, [bool]$DeploymentChanged,
                [switch]$StartEnvironment, [switch]$PreserveBody,
                [switch]$PreserveOperator, [switch]$PreserveCompose,
                [scriptblock]$CommandInvoker
            )
            $script:beforeInitialize = Get-Content -Raw -LiteralPath $script:installStatePath | ConvertFrom-Json
            return [pscustomobject]@{{ units_installed = $true }}
        }}

        $state = Get-LauncherDefaultState
        $state.mistyai_source_path = [System.IO.Path]::GetFullPath({_ps_literal(mistyai_root)})
        $state.mistyai_distro = 'Ubuntu-24.04'
        $state.owned.mistyai.body = $true
        $paths = [pscustomobject]@{{
            state = $script:installStatePath
            staging = {_ps_literal(staging)}
        }}
        $installed = Invoke-LauncherInstallCommand `
            -Root {_ps_literal(neuralcloud_root)} `
            -MistyAIPath {_ps_literal(mistyai_root)} `
            -Distro 'Ubuntu-24.04' `
            -State $state `
            -Paths $paths `
            -NonInteractive `
            -CommandInvoker {{ throw 'external command runner must not be invoked' }}
        [pscustomobject]@{{
            installed = $installed
            before_initialize = $script:beforeInitialize
            final = Get-Content -Raw -LiteralPath $script:installStatePath | ConvertFrom-Json
        }}
        """,
    )

    assert result["installed"]["status"] == "installed"
    before_initialize = result["before_initialize"]
    assert before_initialize["install_phase"] == "configuring_mistyai"
    assert before_initialize["mistyai_deployment_fingerprint"] == fingerprint
    assert before_initialize["owned"]["mistyai"] == {
        "body": True,
        "operator": True,
        "compose": False,
    }
    assert before_initialize["mistyai_unit_enablement_owned"] == {
        "body": False,
        "operator": True,
    }

    final = result["final"]
    assert final["install_phase"] == "ready"
    assert final["owned"]["neuralcloud"] is False
    assert final["owned"]["mistyai"] == {
        "body": True,
        "operator": False,
        "compose": False,
    }
    assert final["mistyai_unit_enablement_owned"] == {
        "body": False,
        "operator": False,
    }


def test_install_failure_keeps_pending_ownership_when_state_cannot_be_confirmed(
    tmp_path: Path,
) -> None:
    neuralcloud_root = tmp_path / "neuralcloud-install-failure"
    mistyai_root = tmp_path / "mistyai-install-failure"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / "install-failure-state.json"
    staging = tmp_path / "install-failure-staging"
    fingerprint = "8" * 64
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:serviceStateCalls = 0

        function Assert-LauncherPrerequisites {{
            param(
                [string]$Root, [string]$MistyAIPath, [string]$Distro,
                [switch]$IncludeRuntimePorts, [switch]$NonInteractive,
                [switch]$Json, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            param([string]$Root, [object]$State, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{ changed = $false; fingerprint = 'neural-install-test' }}
        }}
        function Get-MistyAIServiceState {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            $script:serviceStateCalls++
            if ($script:serviceStateCalls -gt 1) {{
                throw 'service state probe unavailable after install failure'
            }}
            return [pscustomobject]@{{
                body = 'inactive'
                operator = 'inactive'
                body_enabled = 'disabled'
                operator_enabled = 'disabled'
                compose = 'stopped'
                running = $false
            }}
        }}
        function Deploy-MistyAIWorkspace {{
            param(
                [string]$MistyAIPath, [string]$Distro, [object]$State,
                [string]$StagingDirectory, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{
                changed = $false
                fingerprint = '{fingerprint}'
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{
            param(
                [string]$Distro, [bool]$DeploymentChanged,
                [switch]$StartEnvironment, [switch]$PreserveBody,
                [switch]$PreserveOperator, [switch]$PreserveCompose,
                [scriptblock]$CommandInvoker
            )
            throw 'simulated service install failure'
        }}

        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{
            state = {_ps_literal(state_path)}
            staging = {_ps_literal(staging)}
        }}
        $failureCode = 0
        try {{
            Invoke-LauncherInstallCommand `
                -Root {_ps_literal(neuralcloud_root)} `
                -MistyAIPath {_ps_literal(mistyai_root)} `
                -Distro 'Ubuntu-24.04' `
                -State $state `
                -Paths $paths `
                -NonInteractive `
                -CommandInvoker {{ throw 'external command runner must not be invoked' }} | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            service_state_calls = $script:serviceStateCalls
            final = Get-Content -Raw -LiteralPath {_ps_literal(state_path)} | ConvertFrom-Json
        }}
        """,
    )

    assert result["failure_code"] == 40
    assert result["service_state_calls"] == 2
    final = result["final"]
    assert final["install_phase"] == "recovery_required"
    assert final["mistyai_deployment_fingerprint"] == fingerprint
    assert final["owned"]["mistyai"] == {
        "body": True,
        "operator": True,
        "compose": False,
    }
    assert final["mistyai_unit_enablement_owned"] == {
        "body": True,
        "operator": True,
    }


def test_up_persists_pending_ownership_before_external_starts(tmp_path: Path) -> None:
    neuralcloud_root = tmp_path / "neuralcloud"
    mistyai_root = tmp_path / "mistyai"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / "launcher-state.json"
    staging = tmp_path / "staging"
    fingerprint = "e" * 64
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:statePath = {_ps_literal(state_path)}
        $script:beforeMisty = $null
        $script:beforeNeural = $null

        function Assert-LauncherPrerequisites {{
            param(
                [string]$Root, [string]$MistyAIPath, [string]$Distro,
                [switch]$IncludeRuntimePorts, [switch]$NonInteractive,
                [switch]$Json, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Get-MistyAIServiceState {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{
                body = 'inactive'
                operator = 'inactive'
                body_enabled = 'disabled'
                operator_enabled = 'disabled'
                compose = 'stopped'
                running = $false
            }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            param([string]$Root, [object]$State, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{ changed = $false; fingerprint = 'neural-test' }}
        }}
        function Deploy-MistyAIWorkspace {{
            param(
                [string]$MistyAIPath, [string]$Distro, [object]$State,
                [string]$StagingDirectory, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{
                changed = $false
                fingerprint = '{fingerprint}'
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{
            param(
                [string]$Distro, [bool]$DeploymentChanged,
                [switch]$StartEnvironment, [switch]$PreserveBody,
                [switch]$PreserveOperator, [switch]$PreserveCompose,
                [scriptblock]$CommandInvoker
            )
            $script:beforeMisty = Get-Content -Raw -LiteralPath $script:statePath | ConvertFrom-Json
        }}
        function Read-MistyAIServiceToken {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return 'pending-ownership-test-token-123456'
        }}
        function Wait-MistyAIReady {{
            param(
                [string]$Distro, [string]$Token, [string]$ManifestPath,
                [scriptblock]$CommandInvoker, [int]$TimeoutSeconds
            )
            return [pscustomobject]@{{ body_instance_id = 'm0' }}
        }}
        function Start-NeuralCloud {{
            param(
                [string]$Root, [string]$Mode, [string]$Token,
                [scriptblock]$CommandInvoker
            )
            $script:beforeNeural = Get-Content -Raw -LiteralPath $script:statePath | ConvertFrom-Json
            throw 'simulated interruption at NeuralCloud start'
        }}
        function Stop-NeuralCloud {{
            param([string]$Root, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{ success = $true; output = '' }}
        }}
        function Stop-MistyAI {{
            param(
                [string]$Distro, [scriptblock]$CommandInvoker,
                [switch]$StopBody, [switch]$StopOperator,
                [switch]$DisableBody, [switch]$DisableOperator,
                [switch]$StopCompose, [string]$ExpectedFingerprint
            )
            return [pscustomobject]@{{
                success = $true
                service_error = ''
                environment_error = ''
            }}
        }}

        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{
            state = $script:statePath
            staging = {_ps_literal(staging)}
        }}
        $caught = ''
        try {{
            Invoke-LauncherUpCommand `
                -Root {_ps_literal(neuralcloud_root)} `
                -MistyAIPath {_ps_literal(mistyai_root)} `
                -Distro 'Ubuntu-24.04' `
                -Mode 'demo' `
                -State $state `
                -Paths $paths `
                -NoOpen `
                -CommandInvoker {{ throw 'external command runner must not be invoked' }} | Out-Null
        }} catch {{
            $caught = $_.Exception.Message
        }}
        [pscustomobject]@{{
            caught = $caught
            before_misty = $script:beforeMisty
            before_neural = $script:beforeNeural
        }}
        """,
    )

    assert "simulated interruption" in result["caught"]
    before_misty = result["before_misty"]
    assert before_misty["install_phase"] == "starting_mistyai"
    assert before_misty["mistyai_deployment_fingerprint"] == fingerprint
    assert before_misty["owned"]["neuralcloud"] is False
    assert before_misty["owned"]["mistyai"] == {
        "body": True,
        "operator": True,
        "compose": True,
    }
    assert before_misty["mistyai_unit_enablement_owned"] == {
        "body": True,
        "operator": True,
    }

    before_neural = result["before_neural"]
    assert before_neural["install_phase"] == "starting_neuralcloud"
    assert before_neural["mistyai_deployment_fingerprint"] == fingerprint
    assert before_neural["owned"]["neuralcloud"] is True
    assert before_neural["owned"]["mistyai"] == {
        "body": True,
        "operator": True,
        "compose": True,
    }
    assert before_neural["mistyai_unit_enablement_owned"] == {
        "body": True,
        "operator": True,
    }


def test_up_treats_pid_registry_health_as_a_hard_final_gate(tmp_path: Path) -> None:
    neuralcloud_root = tmp_path / "neuralcloud-registry-gate"
    mistyai_root = tmp_path / "mistyai-registry-gate"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / "registry-gate-state.json"
    staging = tmp_path / "registry-gate-staging"
    fingerprint = "9" * 64
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:registryChecks = 0
        $script:neuralStarts = 0
        $script:neuralStops = 0
        $script:mistyStops = 0

        function Assert-LauncherPrerequisites {{
            param(
                [string]$Root, [string]$MistyAIPath, [string]$Distro,
                [switch]$IncludeRuntimePorts, [switch]$NonInteractive,
                [switch]$Json, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Get-MistyAIServiceState {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{
                body = 'inactive'
                operator = 'inactive'
                body_enabled = 'disabled'
                operator_enabled = 'disabled'
                compose = 'stopped'
                running = $false
            }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            param([string]$Root, [object]$State, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{ changed = $false; fingerprint = 'neural-gate-test' }}
        }}
        function Deploy-MistyAIWorkspace {{
            param(
                [string]$MistyAIPath, [string]$Distro, [object]$State,
                [string]$StagingDirectory, [scriptblock]$CommandInvoker
            )
            return [pscustomobject]@{{
                changed = $false
                fingerprint = '{fingerprint}'
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{
            param(
                [string]$Distro, [bool]$DeploymentChanged,
                [switch]$StartEnvironment, [switch]$PreserveBody,
                [switch]$PreserveOperator, [switch]$PreserveCompose,
                [scriptblock]$CommandInvoker
            )
        }}
        function Read-MistyAIServiceToken {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return 'registry-gate-token-1234567890'
        }}
        function Wait-MistyAIReady {{
            param(
                [string]$Distro, [string]$Token, [string]$ManifestPath,
                [scriptblock]$CommandInvoker, [int]$TimeoutSeconds
            )
            return [pscustomobject]@{{ body_instance_id = 'm0' }}
        }}
        function Start-NeuralCloud {{
            param(
                [string]$Root, [string]$Mode, [string]$Token,
                [scriptblock]$CommandInvoker
            )
            $script:neuralStarts++
            return [pscustomobject]@{{ output = ''; manifest = 'grounded-mistyai-v1.json' }}
        }}
        function Wait-NeuralCloudReady {{
            param([string]$ManifestPath, [int]$TimeoutSeconds)
            return [pscustomobject]@{{
                runtime = [pscustomobject]@{{ lifecycle = 'RUNNING' }}
            }}
        }}
        function Test-NeuralCloudRegistryHealth {{
            param([string]$Root)
            $script:registryChecks++
            return [pscustomobject]@{{
                running = $false
                services = @([pscustomobject]@{{
                    name = 'runtime'
                    running = $true
                    identity_matches = $false
                }})
            }}
        }}
        function Stop-NeuralCloud {{
            param([string]$Root, [scriptblock]$CommandInvoker)
            $script:neuralStops++
            return [pscustomobject]@{{ success = $true; output = '' }}
        }}
        function Stop-MistyAI {{
            param(
                [string]$Distro, [scriptblock]$CommandInvoker,
                [switch]$StopBody, [switch]$StopOperator,
                [switch]$DisableBody, [switch]$DisableOperator,
                [switch]$StopCompose, [string]$ExpectedFingerprint
            )
            $script:mistyStops++
            return [pscustomobject]@{{
                success = $true
                service_error = ''
                environment_error = ''
            }}
        }}

        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{
            state = {_ps_literal(state_path)}
            staging = {_ps_literal(staging)}
        }}
        $failureCode = 0
        $failureMessage = ''
        try {{
            Invoke-LauncherUpCommand `
                -Root {_ps_literal(neuralcloud_root)} `
                -MistyAIPath {_ps_literal(mistyai_root)} `
                -Distro 'Ubuntu-24.04' `
                -Mode 'demo' `
                -State $state `
                -Paths $paths `
                -NoOpen `
                -CommandInvoker {{ throw 'external command runner must not be invoked' }} | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            registry_checks = $script:registryChecks
            neural_starts = $script:neuralStarts
            neural_stops = $script:neuralStops
            misty_stops = $script:mistyStops
            final_state = Get-Content -Raw -LiteralPath {_ps_literal(state_path)} | ConvertFrom-Json
        }}
        """,
    )

    assert result["failure_code"] == 30
    assert "pid registry" in result["failure_message"].lower()
    assert result["registry_checks"] == 1
    assert result["neural_starts"] == 1
    assert result["neural_stops"] == 1
    assert result["misty_stops"] == 1
    assert result["final_state"]["install_phase"] == "failed"
    assert result["final_state"]["owned"]["neuralcloud"] is False
    assert result["final_state"]["owned"]["mistyai"] == {
        "body": False,
        "operator": False,
        "compose": False,
    }
    assert result["final_state"]["mistyai_unit_enablement_owned"] == {
        "body": False,
        "operator": False,
    }


def test_start_process_registry_v2_records_full_service_identity(tmp_path: Path) -> None:
    runtime_root = tmp_path / "runtime-root"
    runtime_root.mkdir()
    result = _invoke_module_json(
        tmp_path,
        rf"""
        $tokens = $null
        $parseErrors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {_ps_literal(START_SCRIPT)}, [ref]$tokens, [ref]$parseErrors
        )
        if ($parseErrors.Count -ne 0) {{ throw 'start.ps1 did not parse' }}
        foreach ($functionName in @('Write-ProcessRegistry', 'Start-NeuralService')) {{
            $definition = $ast.Find({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                    $node.Name -eq $functionName
            }}, $true)
            if ($null -eq $definition) {{ throw "missing function: $functionName" }}
            Invoke-Expression $definition.Extent.Text
        }}

        $Root = [System.IO.Path]::GetFullPath({_ps_literal(runtime_root)})
        $RegistryPath = Join-Path $Root '.neuralcloud.pids.json'
        $LogDir = Join-Path $Root 'logs'
        [void](New-Item -ItemType Directory -Path $LogDir -Force)
        $python = Join-Path $Root '.venv\Scripts\python.exe'
        $ManifestPath = Join-Path $Root 'manifest.json'
        $ProfilePath = Join-Path $Root 'profile.json'
        $launchStartedAt = '2026-09-04T01:02:03.0000000Z'
        $started = New-Object System.Collections.Generic.List[object]
        $script:fakeProcessStart = [datetime]'2026-09-04T01:02:04Z'

        function Start-Process {{
            param(
                [string]$FilePath, [object[]]$ArgumentList,
                [string]$WorkingDirectory, [string]$RedirectStandardOutput,
                [string]$RedirectStandardError, [string]$WindowStyle,
                [switch]$PassThru
            )
            return [pscustomobject]@{{ Id = 4321; StartTime = $script:fakeProcessStart }}
        }}

        $entry = Start-NeuralService `
            -Name 'runtime' `
            -Module 'harness.runtime.service' `
            -Port 19070 `
            -Python $python
        [pscustomobject]@{{
            entry = $entry
            registry = Get-Content -Raw -LiteralPath $RegistryPath | ConvertFrom-Json
        }}
        """,
    )

    registry = result["registry"]
    entry = result["entry"]
    assert registry["schema_version"] == "neuralcloud-processes-v2"
    assert Path(registry["root"]) == runtime_root.resolve()
    assert Path(registry["python_path"]) == (
        runtime_root / ".venv" / "Scripts" / "python.exe"
    ).resolve()
    assert registry["started_at"].startswith("2026-09-04T01:02:03")
    assert len(registry["services"]) == 1
    assert entry["name"] == "runtime"
    assert entry["pid"] == 4321
    assert entry["module"] == "harness.runtime.service"
    assert entry["port"] == 19070
    assert Path(entry["root"]) == runtime_root.resolve()
    assert Path(entry["python_path"]) == (
        runtime_root / ".venv" / "Scripts" / "python.exe"
    ).resolve()
    assert entry["process_start_time_utc"].startswith("2026-09-04T01:02:04")
    assert entry["started_at"]
    assert entry["out_log"].endswith("runtime.out.log")
    assert entry["err_log"].endswith("runtime.err.log")
    registry_entry = registry["services"][0]
    for identity_field in ("name", "pid", "module", "port", "root", "python_path"):
        assert registry_entry[identity_field] == entry[identity_field]
    assert registry_entry["process_start_time_utc"].startswith("2026-09-04T01:02:04")
    assert registry_entry["started_at"]


def test_start_failure_cleanup_waits_for_identity_matched_processes_before_registry_removal() -> None:
    source = START_SCRIPT.read_text(encoding="utf-8-sig")
    cleanup_start = source.index("function Stop-StartedProcesses")
    cleanup_end = source.index("\n$python =", cleanup_start + 1)
    cleanup = source[cleanup_start:cleanup_end]

    assert "process_start_time_utc" in cleanup
    assert "$live.StartTime.ToUniversalTime()" in cleanup
    assert "Get-CimInstance Win32_Process" in cleanup
    assert "$entry.python_path" in cleanup
    assert "$processInfo.ExecutablePath" in cleanup
    assert "$entry.root" in cleanup
    assert "$expectedRoot = [System.IO.Path]::GetFullPath($Root)" in cleanup
    assert "$processInfo.Name -match \"^python(w)?\\.exe$\"" in cleanup
    assert "$commandLine -match [regex]::Escape([string]$entry.module)" in cleanup
    assert "$portOwner = Get-PortOwner -Port ([int]$entry.port)" in cleanup
    assert "[int]$portOwner -eq [int]$entry.pid" in cleanup
    assert "if (-not $identityMatches)" in cleanup
    assert cleanup.index("if (-not $identityMatches)") < cleanup.index("Stop-Process")
    assert "$deadline = (Get-Date).AddSeconds(10)" in cleanup
    assert "Start-Sleep -Milliseconds 200" in cleanup
    assert "return $identityMatched -and $remaining.Count -eq 0" in cleanup

    catch_start = source.index("} catch {")
    catch_block = source[catch_start:]
    assert "$cleanupSucceeded = Stop-StartedProcesses" in catch_block
    assert "if ($cleanupSucceeded -and (Test-Path -LiteralPath $RegistryPath))" in catch_block
    assert "Remove-Item -LiteralPath $RegistryPath -Force" in catch_block
    assert "elseif (-not $cleanupSucceeded)" in catch_block
    assert "PID registry was retained for stop.ps1" in catch_block


def test_start_failure_cleanup_stops_only_full_process_identity_match(tmp_path: Path) -> None:
    runtime_root = tmp_path / "cleanup-identity-root"
    runtime_root.mkdir()
    result = _invoke_module_json(
        tmp_path,
        rf"""
        $tokens = $null
        $parseErrors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {_ps_literal(START_SCRIPT)}, [ref]$tokens, [ref]$parseErrors
        )
        if ($parseErrors.Count -ne 0) {{ throw 'start.ps1 did not parse' }}
        $definition = $ast.Find({{
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                $node.Name -eq 'Stop-StartedProcesses'
        }}, $true)
        Invoke-Expression $definition.Extent.Text

        $script:actualStart = [datetime]'2026-09-04T02:03:04Z'
        function Get-Process {{
            param([int]$Id, $ErrorAction)
            if ($script:stopped) {{ return $null }}
            return [pscustomobject]@{{ Id = $Id; StartTime = $script:actualStart }}
        }}
        function Get-CimInstance {{
            param([string]$ClassName, [string]$Filter, $ErrorAction)
            return [pscustomobject]@{{
                Name = 'python.exe'
                ExecutablePath = $script:actualPython
                CommandLine = ('"{{0}}" -m {{1}}' -f $script:actualPython, $script:commandModule)
            }}
        }}
        function Get-PortOwner {{ param([int]$Port); return $script:portOwner }}
        function Stop-Process {{
            param([int]$Id, [switch]$Force, $ErrorAction)
            $script:stopped = $true
        }}
        function Start-Sleep {{ param([int]$Milliseconds) }}
        function Get-Date {{
            $script:clock = $script:clock.AddSeconds(11)
            return $script:clock
        }}
        function Invoke-CleanupCase {{
            param(
                [string]$EntryRoot,
                [string]$EntryPython,
                [string]$ActualPython,
                [string]$EntryModule,
                [string]$CommandModule,
                [int]$PortOwner
            )
            $Root = [System.IO.Path]::GetFullPath({_ps_literal(runtime_root)})
            $python = Join-Path $Root '.venv\Scripts\python.exe'
            $script:actualPython = $ActualPython
            $script:commandModule = $CommandModule
            $script:portOwner = $PortOwner
            $script:stopped = $false
            $script:clock = [datetime]'2026-09-04T00:00:00Z'
            $started = New-Object System.Collections.Generic.List[object]
            [void]$started.Add([pscustomobject]@{{
                pid = 4321
                port = 19070
                module = $EntryModule
                root = $EntryRoot
                python_path = $EntryPython
                process_start_time_utc = $script:actualStart.ToUniversalTime().ToString('o')
            }})
            $success = Stop-StartedProcesses
            return [pscustomobject]@{{ success = $success; stopped = $script:stopped }}
        }}

        $root = [System.IO.Path]::GetFullPath({_ps_literal(runtime_root)})
        $pythonPath = Join-Path $root '.venv\Scripts\python.exe'
        [pscustomobject]@{{
            valid = Invoke-CleanupCase $root $pythonPath $pythonPath 'harness.runtime.service' 'harness.runtime.service' 4321
            python_mismatch = Invoke-CleanupCase $root $pythonPath (Join-Path $root 'other-python.exe') 'harness.runtime.service' 'harness.runtime.service' 4321
            module_mismatch = Invoke-CleanupCase $root $pythonPath $pythonPath 'harness.runtime.service' 'other.module' 4321
            root_mismatch = Invoke-CleanupCase (Join-Path $root 'other-root') $pythonPath $pythonPath 'harness.runtime.service' 'harness.runtime.service' 4321
            port_owner_mismatch = Invoke-CleanupCase $root $pythonPath $pythonPath 'harness.runtime.service' 'harness.runtime.service' 9999
        }}
        """,
    )

    assert result["valid"] == {"success": True, "stopped": True}
    for mismatch in (
        "python_mismatch",
        "module_mismatch",
        "root_mismatch",
        "port_owner_mismatch",
    ):
        assert result[mismatch] == {"success": False, "stopped": False}


def test_stop_process_identity_accepts_custom_port_and_rejects_identity_mismatches(
    tmp_path: Path,
) -> None:
    runtime_root = tmp_path / "stop-runtime-root"
    runtime_root.mkdir()
    result = _invoke_module_json(
        tmp_path,
        rf"""
        $tokens = $null
        $parseErrors = $null
        $ast = [System.Management.Automation.Language.Parser]::ParseFile(
            {_ps_literal(STOP_SCRIPT)}, [ref]$tokens, [ref]$parseErrors
        )
        if ($parseErrors.Count -ne 0) {{ throw 'stop.ps1 did not parse' }}
        foreach ($functionName in @('Test-PortOwnerChild', 'Test-ProcessIdentity')) {{
            $definition = $ast.Find({{
                param($node)
                $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                    $node.Name -eq $functionName
            }}, $true)
            if ($null -eq $definition) {{ throw "missing $functionName" }}
            Invoke-Expression $definition.Extent.Text
        }}

        $Root = [System.IO.Path]::GetFullPath({_ps_literal(runtime_root)})
        $script:expectedPython = Join-Path $Root '.venv\Scripts\python.exe'
        $script:actualPython = $script:expectedPython
        $script:actualStart = [datetime]'2026-09-04T02:03:04Z'
        $script:commandModule = 'harness.runtime.service'
        $script:portOwner = 4321
        $script:lastPortChecked = 0

        function Get-CimInstance {{
            param([string]$ClassName, [string]$Filter, $ErrorAction)
            $requestedPid = [int]([regex]::Match($Filter, '\d+').Value)
            return [pscustomobject]@{{
                Name = 'python.exe'
                ExecutablePath = $script:actualPython
                CommandLine = ('"{{0}}" -m {{1}}' -f $script:actualPython, $script:commandModule)
                ParentProcessId = if ($requestedPid -eq 9876) {{ 4321 }} else {{ 0 }}
            }}
        }}
        function Get-Process {{
            param([int]$Id, $ErrorAction)
            return [pscustomobject]@{{ Id = $Id; StartTime = $script:actualStart }}
        }}
        function Get-PortOwner {{
            param([int]$Port)
            $script:lastPortChecked = $Port
            return $script:portOwner
        }}
        function Copy-TestEntry {{
            param([object]$Entry)
            return $Entry | ConvertTo-Json -Depth 5 | ConvertFrom-Json
        }}

        $base = [pscustomobject]@{{
            name = 'runtime'
            pid = 4321
            module = 'harness.runtime.service'
            port = 19070
            root = $Root
            python_path = $script:expectedPython
            process_start_time_utc = $script:actualStart.ToUniversalTime().ToString('o')
        }}
        $valid = Test-ProcessIdentity $base

        $rootMismatch = Copy-TestEntry $base
        $rootMismatch.root = Join-Path $Root 'other-checkout'
        $pythonMismatch = Copy-TestEntry $base
        $pythonMismatch.python_path = Join-Path $Root 'other-python.exe'
        $startMismatch = Copy-TestEntry $base
        $startMismatch.process_start_time_utc = $script:actualStart.AddMinutes(5).ToString('o')
        $moduleMismatch = Copy-TestEntry $base
        $moduleMismatch.module = 'harness.cognitive.service'
        $lowPortMismatch = Copy-TestEntry $base
        $lowPortMismatch.port = 0
        $highPortMismatch = Copy-TestEntry $base
        $highPortMismatch.port = 70000

        $script:portOwner = 9999
        $portOwnerMismatch = Test-ProcessIdentity $base
        $script:portOwner = 9876
        $childPortOwner = Test-ProcessIdentity $base
        $script:portOwner = 4321
        $script:commandModule = 'other.module'
        $commandLineMismatch = Test-ProcessIdentity $base

        [pscustomobject]@{{
            valid = $valid
            root_mismatch = Test-ProcessIdentity $rootMismatch
            python_mismatch = Test-ProcessIdentity $pythonMismatch
            start_mismatch = Test-ProcessIdentity $startMismatch
            module_mismatch = Test-ProcessIdentity $moduleMismatch
            low_port_mismatch = Test-ProcessIdentity $lowPortMismatch
            high_port_mismatch = Test-ProcessIdentity $highPortMismatch
            port_owner_mismatch = $portOwnerMismatch
            child_port_owner = $childPortOwner
            command_line_mismatch = $commandLineMismatch
            custom_port_checked = $script:lastPortChecked
        }}
        """,
    )

    assert result == {
        "valid": True,
        "root_mismatch": False,
        "python_mismatch": False,
        "start_mismatch": False,
        "module_mismatch": False,
        "low_port_mismatch": False,
        "high_port_mismatch": False,
        "port_owner_mismatch": False,
        "child_port_owner": True,
        "command_line_mismatch": False,
        "custom_port_checked": 19070,
    }


def test_workspace_fingerprint_is_order_independent_and_content_sensitive(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "a.txt").write_text("alpha\n", encoding="utf-8")
    (workspace / "b.txt").write_text("beta\n", encoding="utf-8")

    first = _invoke_module_json(
        tmp_path,
        f"Get-WorkspaceFingerprint -Root {_ps_literal(workspace)} -Files @('b.txt', 'a.txt')",
    )
    reordered = _invoke_module_json(
        tmp_path,
        f"Get-WorkspaceFingerprint -Root {_ps_literal(workspace)} -Files @('a.txt', 'b.txt')",
    )
    (workspace / "b.txt").write_text("changed\n", encoding="utf-8")
    changed = _invoke_module_json(
        tmp_path,
        f"Get-WorkspaceFingerprint -Root {_ps_literal(workspace)} -Files @('a.txt', 'b.txt')",
    )

    assert isinstance(first, str)
    assert len(first) == 64
    assert reordered == first
    assert changed != first


def test_deployment_verifies_snapshot_archive_hash_before_wsl_extraction() -> None:
    source = LAUNCHER_MODULE.read_text(encoding="utf-8-sig")
    snapshot_hash = source.index(
        "$archiveSha256 = (Get-FileHash -LiteralPath $archivePath -Algorithm SHA256)"
    )
    snapshot_result = source.index("archive_sha256 = $archiveSha256", snapshot_hash)
    prepare_start = source.index("archive_sha256=__ARCHIVE_SHA256__", snapshot_result)
    hash_read = source.index('actual_archive_sha256=$(sha256sum -- "$archive"', prepare_start)
    hash_compare = source.index(
        'test "$actual_archive_sha256" = "$archive_sha256"',
        hash_read,
    )
    extraction = source.index('python3.12 - "$archive" "$stage"', hash_compare)
    replacement = source.index(
        "Replace('__ARCHIVE_SHA256__', (Quote-LauncherBashLiteral $snapshot.archive_sha256))",
        extraction,
    )

    assert snapshot_hash < snapshot_result < prepare_start
    assert prepare_start < hash_read < hash_compare < extraction < replacement


@pytest.mark.skipif(shutil.which("git") is None, reason="Git is unavailable")
def test_workspace_enumeration_includes_dirty_and_untracked_but_not_ignored_or_cache(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    subprocess.run(["git", "init", "--quiet", str(workspace)], check=True)
    (workspace / ".gitignore").write_text("ignored.txt\n", encoding="utf-8")
    (workspace / "tracked.txt").write_text("original\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(workspace), "add", ".gitignore", "tracked.txt"],
        check=True,
    )
    (workspace / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    (workspace / "untracked.txt").write_text("included\n", encoding="utf-8")
    (workspace / "ignored.txt").write_text("ignored\n", encoding="utf-8")
    cache = workspace / "nested" / "__pycache__"
    cache.mkdir(parents=True)
    (cache / "module.pyc").write_bytes(b"cache")

    files = _invoke_module_json(
        tmp_path,
        f"@((Get-LauncherWorkspaceFiles -Root {_ps_literal(workspace)}))",
    )

    assert files == [".gitignore", "tracked.txt", "untracked.txt"]


def test_deployment_refuses_unmanaged_opt_path_before_building_an_image(tmp_path: Path) -> None:
    workspace = tmp_path / "mistyai"
    (workspace / "deploy").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='test'\n", encoding="utf-8")
    (workspace / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (workspace / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    staging = tmp_path / "staging"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $state = Get-LauncherDefaultState
        $script:wslScripts = New-Object System.Collections.Generic.List[string]
        try {{
            Deploy-MistyAIWorkspace `
                -MistyAIPath {_ps_literal(workspace)} `
                -Distro 'Ubuntu-24.04' `
                -State $state `
                -StagingDirectory {_ps_literal(staging)} `
                -CommandInvoker {{
                    param($request)
                    if ($request.FilePath -eq 'git.exe') {{
                        $files = @('deploy/compose.yaml', 'pyproject.toml', 'uv.lock')
                        return [pscustomobject]@{{ exit_code = 0; stdout = (($files -join [char]0) + [char]0); stderr = '' }}
                    }}
                    [void]$script:wslScripts.Add(($request.ArgumentList -join ' '))
                    return [pscustomobject]@{{ exit_code = 0; stdout = "current=unmanaged`nprevious=absent`n"; stderr = '' }}
                }} | Out-Null
        }} catch {{
            [pscustomobject]@{{
                exit_code = Get-LauncherFailureCode -ErrorRecord $_ -Default 20
                error = $_.Exception.Message
                wsl_calls = $script:wslScripts.Count
                attempted_image_build = [bool](@($script:wslScripts | Where-Object {{ $_ -match 'image build' }}).Count)
            }}
        }}
        """,
    )

    assert result["exit_code"] == 40
    assert "unmanaged" in result["error"].lower()
    assert result["wsl_calls"] == 1
    assert result["attempted_image_build"] is False


def test_failed_candidate_build_does_not_mutate_m0_or_swap_directories(tmp_path: Path) -> None:
    workspace = tmp_path / "mistyai-candidate"
    (workspace / "deploy").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='candidate-test'\n", encoding="utf-8")
    (workspace / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (workspace / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    staging = tmp_path / "candidate-staging"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $state = Get-LauncherDefaultState
        $script:prepareScript = ''
        $script:swapSeen = $false
        try {{
            Deploy-MistyAIWorkspace `
                -MistyAIPath {_ps_literal(workspace)} `
                -Distro 'Ubuntu-24.04' `
                -State $state `
                -StagingDirectory {_ps_literal(staging)} `
                -CommandInvoker {{
                    param($request)
                    if ($request.FilePath -eq 'git.exe') {{
                        $files = @('deploy/compose.yaml', 'pyproject.toml', 'uv.lock')
                        return [pscustomobject]@{{ exit_code = 0; stdout = (($files -join [char]0) + [char]0); stderr = '' }}
                    }}
                    $bash = [string]$request.ArgumentList[-1]
                    if ($bash -match 'classify /opt/mistyai current') {{
                        return [pscustomobject]@{{
                            exit_code = 0
                            stdout = "current=absent`nprevious=absent`ncurrent_fingerprint=`ncurrent_image=`ncurrent_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
                            stderr = ''
                        }}
                    }}
                    if ($bash.Trim() -eq 'command -v uv') {{
                        return [pscustomobject]@{{ exit_code = 0; stdout = '/usr/local/bin/uv'; stderr = '' }}
                    }}
                    if ($bash -match 'trap restore_old EXIT') {{
                        $script:swapSeen = $true
                        return [pscustomobject]@{{ exit_code = 99; stdout = ''; stderr = 'unexpected swap' }}
                    }}
                    if ($bash -match 'candidate_tag=') {{
                        $script:prepareScript = $bash
                        return [pscustomobject]@{{ exit_code = 77; stdout = ''; stderr = 'candidate build failed' }}
                    }}
                    return [pscustomobject]@{{ exit_code = 98; stdout = ''; stderr = 'unexpected command' }}
                }} | Out-Null
        }} catch {{
            [pscustomobject]@{{
                exit_code = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
                prepare_script = $script:prepareScript
                swap_seen = $script:swapSeen
            }}
        }}
        """,
    )

    prepare_script = result["prepare_script"]
    assert result["exit_code"] == 20
    assert result["swap_seen"] is False
    assert re.search(
        r"mistyai-desktop:launcher-[0-9a-f]{16}-[0-9a-f]{32}",
        prepare_script,
    )
    assert "image: %s" in prepare_script
    assert "build --pull desktop" in prepare_script
    assert not re.search(r"mistyctl[\"']?\s+image\s+build", prepare_script)
    assert not re.search(
        r"image\s+tag[^\r\n]+mistyai-desktop:m0",
        prepare_script,
    )
    assert 'mv -- "$stage" /opt/mistyai' not in prepare_script


def test_deployment_swap_stops_units_strictly_before_tagging_or_moving(tmp_path: Path) -> None:
    workspace = tmp_path / "mistyai-swap"
    (workspace / "deploy").mkdir(parents=True)
    (workspace / "pyproject.toml").write_text("[project]\nname='swap-test'\n", encoding="utf-8")
    (workspace / "uv.lock").write_text("version = 1\n", encoding="utf-8")
    (workspace / "deploy" / "compose.yaml").write_text("services: {}\n", encoding="utf-8")
    staging = tmp_path / "swap-staging"
    old_fingerprint = "1" * 64
    old_image = "sha256:" + ("2" * 64)
    candidate_image = "sha256:" + ("3" * 64)
    result = _invoke_module_json(
        tmp_path,
        f"""
        $state = Get-LauncherDefaultState
        $state.mistyai_deployment_fingerprint = '{old_fingerprint}'
        $state.mistyai_source_path = [System.IO.Path]::GetFullPath({_ps_literal(workspace)})
        $state.mistyai_distro = 'Ubuntu-24.04'
        $script:swapScript = ''
        try {{
            Deploy-MistyAIWorkspace `
                -MistyAIPath {_ps_literal(workspace)} `
                -Distro 'Ubuntu-24.04' `
                -State $state `
                -StagingDirectory {_ps_literal(staging)} `
                -CommandInvoker {{
                    param($request)
                    if ($request.FilePath -eq 'git.exe') {{
                        $files = @('deploy/compose.yaml', 'pyproject.toml', 'uv.lock')
                        return [pscustomobject]@{{ exit_code = 0; stdout = (($files -join [char]0) + [char]0); stderr = '' }}
                    }}
                    $bash = [string]$request.ArgumentList[-1]
                    if ($bash -match 'classify /opt/mistyai current') {{
                        return [pscustomobject]@{{
                            exit_code = 0
                            stdout = "current=managed`nprevious=absent`ncurrent_fingerprint={old_fingerprint}`ncurrent_image={old_image}`ncurrent_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
                            stderr = ''
                        }}
                    }}
                    if ($bash.Trim() -eq 'command -v uv') {{
                        return [pscustomobject]@{{ exit_code = 0; stdout = '/usr/local/bin/uv'; stderr = '' }}
                    }}
                    if ($bash -match 'trap restore_old EXIT') {{
                        $script:swapScript = $bash
                        return [pscustomobject]@{{ exit_code = 71; stdout = ''; stderr = 'Failed to stop mistyai-body.service' }}
                    }}
                    if ($bash -match 'candidate_tag=') {{
                        return [pscustomobject]@{{
                            exit_code = 0
                            stdout = "previous_image={old_image}`ncandidate_image={candidate_image}`n"
                            stderr = ''
                        }}
                    }}
                    return [pscustomobject]@{{ exit_code = 98; stdout = ''; stderr = 'unexpected command' }}
                }} | Out-Null
        }} catch {{
            [pscustomobject]@{{
                exit_code = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
                swap_script = $script:swapScript
            }}
        }}
        """,
    )

    assert result["exit_code"] == 20
    swap_script = result["swap_script"]
    transaction = swap_script[swap_script.index("trap restore_old EXIT") :]
    operator_stop = transaction.index("systemctl stop mistyai-operator.service")
    operator_check = transaction.index("mistyai-operator.service did not stop")
    body_stop = transaction.index("systemctl stop mistyai-body.service")
    body_check = transaction.index("mistyai-body.service did not stop")
    canonical_tag = transaction.index('image tag "$candidate_image" mistyai-desktop:m0')
    first_move = transaction.index("mv --", canonical_tag)
    inactive_identity_guard = transaction.index(
        'if ! verify_mistyai_unit_owned_or_absent "$unit"; then'
    )
    first_active_probe = transaction.index(
        "systemctl is-active --quiet mistyai-body.service"
    )
    assert operator_stop < operator_check < body_stop < body_check < canonical_tag < first_move
    assert inactive_identity_guard < first_active_probe < canonical_tag
    assert "refusing deployment swap because $unit has an unknown identity" in transaction
    assert "exit 65" in transaction[inactive_identity_guard:canonical_tag]
    assert not re.search(r"systemctl stop[^\r\n]*\|\| true", transaction)


def test_restore_checks_inactive_units_and_rechecks_before_overwriting_unit_files(
    tmp_path: Path,
) -> None:
    image_id = "sha256:" + ("4" * 64)
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:restoreScript = ''
        $restored = Restore-MistyAIDeployment `
            -Distro 'Ubuntu-24.04' `
            -PreviousImageId '{image_id}' `
            -CommandInvoker {{
                param($request)
                $script:restoreScript = [string]$request.ArgumentList[-1]
                return [pscustomobject]@{{
                    exit_code = 65
                    stdout = ''
                    stderr = 'inactive unit identity could not be proven'
                }}
            }}
        [pscustomobject]@{{ restored = $restored; script = $script:restoreScript }}
        """,
    )

    assert result["restored"] is False
    restore_script = result["script"]
    initial_guard = restore_script.index(
        'if ! verify_mistyai_unit_owned_or_absent "$unit"; then'
    )
    active_probe = restore_script.index(
        "systemctl is-active --quiet mistyai-body.service",
        initial_guard,
    )
    assert initial_guard < active_probe
    assert "refusing rollback because $unit has an unknown identity" in restore_script

    main = restore_script[restore_script.index("trap restore_failed_layout EXIT") :]
    current_move = main.index('mv -- /opt/mistyai "$failed"')
    previous_move = main.index("mv -- /opt/mistyai.previous /opt/mistyai")
    overwrite_recheck = main.index(
        "for unit in mistyai-body.service mistyai-operator.service; do",
        previous_move,
    )
    first_install = main.index(
        "install -m 0644 /opt/mistyai/deploy/systemd/mistyai-body.service",
        overwrite_recheck,
    )
    assert current_move < previous_move < overwrite_recheck < first_install
    recheck_block = main[overwrite_recheck:first_install]
    assert 'if test -e "$unit_file" || test -L "$unit_file"; then' in recheck_block
    assert 'verify_mistyai_unit_identity "$unit" /opt/mistyai' in recheck_block
    assert 'verify_mistyai_unit_identity "$unit" "$failed"' in recheck_block
    assert 'elif ! verify_mistyai_unit_absent "$unit"; then' in recheck_block
    assert "cannot prove $unit is absent during rollback" in recheck_block


def test_windows_path_conversion_preserves_spaces_without_shell_escaping(tmp_path: Path) -> None:
    converted = _invoke_module_json(
        tmp_path,
        r"ConvertTo-WslPath -Path 'C:\Projects\Misty AI\source' -Distro 'Ubuntu-24.04'",
    )

    assert converted == "/mnt/c/Projects/Misty AI/source"


def test_wsl_command_uses_discrete_arguments_and_an_injected_runner(tmp_path: Path) -> None:
    capture_path = tmp_path / "wsl-request.json"
    captured = _invoke_module_json(
        tmp_path,
        r"""
        $response = Invoke-LauncherWslCommand `
            -Distro 'Ubuntu-24.04' `
            -User 'root' `
            -WorkingDirectory "/opt/misty ai's" `
            -Script "printf '%s\n' 'hello world'" `
            -SensitiveValues @('test-sensitive-value') `
            -CommandInvoker {
                param($request)
                $request | ConvertTo-Json -Depth 8 -Compress |
                    Set-Content -LiteralPath $env:NC_TEST_CAPTURE -Encoding UTF8
                return [pscustomobject]@{ ExitCode = 0; StdOut = 'ok'; StdErr = '' }
            }
        [pscustomobject]@{
            response = $response
            request = Get-Content -LiteralPath $env:NC_TEST_CAPTURE -Raw | ConvertFrom-Json
        }
        """,
        environment={"NC_TEST_CAPTURE": str(capture_path)},
    )

    assert captured["response"] == {"exit_code": 0, "stdout": "ok", "stderr": ""}
    request = captured["request"]
    assert request["FilePath"] == "wsl.exe"
    arguments = request["ArgumentList"]
    assert arguments[:4] == ["--distribution", "Ubuntu-24.04", "--user", "root"]
    assert arguments[4:9] == ["--exec", "bash", "--noprofile", "--norc", "-c"]
    assert len(arguments) == 10
    assert "/opt/misty ai" in arguments[-1]
    assert "hello world" in arguments[-1]
    assert request["SensitiveValues"] == ["test-sensitive-value"]


def test_json_doctor_suppresses_wsl_root_progress_and_keeps_stdout_valid_json(tmp_path: Path) -> None:
    runner, neural_root, mistyai_root = _write_mocked_doctor_runner(tmp_path)

    completed = _run_script(
        runner,
        "-ModulePath",
        str(LAUNCHER_MODULE),
        "-NeuralRoot",
        str(neural_root),
        "-MistyRoot",
        str(mistyai_root),
        "-JsonMode",
    )

    assert "[privileged]" not in completed.stdout
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "neuralcloud-launcher-result-v1"
    assert payload["command"] == "doctor"
    assert completed.returncode == payload["exit_code"]


def test_human_doctor_reports_wsl_root_operations_without_command_or_secret_text(tmp_path: Path) -> None:
    runner, neural_root, mistyai_root = _write_mocked_doctor_runner(tmp_path)

    completed = _run_script(
        runner,
        "-ModulePath",
        str(LAUNCHER_MODULE),
        "-NeuralRoot",
        str(neural_root),
        "-MistyRoot",
        str(mistyai_root),
    )

    assert "[privileged] WSL root (Ubuntu-24.04):" in completed.stdout
    assert "check Ubuntu, systemd, Python, uv, and Docker integration" in completed.stdout
    assert "python_path=$(command -v" not in completed.stdout
    assert "Authorization: Bearer" not in completed.stdout


@pytest.mark.skipif(WINDOWS_POWERSHELL is None, reason="Windows PowerShell 5.1 is unavailable")
def test_windows_powershell_51_imports_module_and_runs_mocked_doctor(tmp_path: Path) -> None:
    runner, neural_root, mistyai_root = _write_mocked_doctor_runner(tmp_path)

    completed = subprocess.run(
        [
            str(WINDOWS_POWERSHELL),
            "-NoLogo",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(runner),
            "-ModulePath",
            str(LAUNCHER_MODULE),
            "-NeuralRoot",
            str(neural_root),
            "-MistyRoot",
            str(mistyai_root),
            "-JsonMode",
        ],
        cwd=runner.parent,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )

    assert "ParserError" not in completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["schema_version"] == "neuralcloud-launcher-result-v1"
    assert payload["command"] == "doctor"
    assert completed.returncode == payload["exit_code"]


def test_dev_entrypoint_forwards_all_public_parameters_without_starting_services(tmp_path: Path) -> None:
    entrypoint = _copy_entrypoint(tmp_path)
    capture_path = tmp_path / "capture.json"
    mistyai_path = tmp_path / "Misty AI working tree"
    _install_fake_module(
        tmp_path,
        r"""
        function Invoke-NeuralCloudLauncher {
            [CmdletBinding()]
            param(
                [string]$Command,
                [string]$Root,
                [string]$MistyAIPath,
                [string]$Distro,
                [string]$Mode,
                [switch]$NonInteractive,
                [switch]$NoOpen,
                [switch]$Json,
                [string]$Service
            )
            [pscustomobject]@{
                command = $Command
                root = $Root
                mistyai_path = $MistyAIPath
                distro = $Distro
                mode = $Mode
                non_interactive = [bool]$NonInteractive
                no_open = [bool]$NoOpen
                json = [bool]$Json
                service = $Service
            } | ConvertTo-Json -Compress | Set-Content -LiteralPath $env:NC_TEST_CAPTURE -Encoding UTF8
            return 0
        }
        Export-ModuleMember -Function Invoke-NeuralCloudLauncher
        """,
    )

    completed = _run_script(
        entrypoint,
        "logs",
        "-MistyAIPath",
        str(mistyai_path),
        "-Distro",
        "Ubuntu-Test",
        "-Mode",
        "ollama",
        "-NonInteractive",
        "-NoOpen",
        "-Json",
        "-Service",
        "agent",
        environment={"NC_TEST_CAPTURE": str(capture_path)},
    )

    assert completed.returncode == 0, completed.stderr or completed.stdout
    captured = json.loads(capture_path.read_text(encoding="utf-8-sig"))
    assert captured == {
        "command": "logs",
        "root": str(tmp_path),
        "mistyai_path": str(mistyai_path),
        "distro": "Ubuntu-Test",
        "mode": "ollama",
        "non_interactive": True,
        "no_open": True,
        "json": True,
        "service": "agent",
    }


@pytest.mark.parametrize("command", ["up", "down", "restart", "status", "doctor", "install", "logs"])
def test_dev_entrypoint_accepts_every_documented_command(tmp_path: Path, command: str) -> None:
    entrypoint = _copy_entrypoint(tmp_path)
    capture_path = tmp_path / f"{command}.txt"
    _install_fake_module(
        tmp_path,
        r"""
        function Invoke-NeuralCloudLauncher {
            param(
                [string]$Command, [string]$Root, [string]$MistyAIPath,
                [string]$Distro, [string]$Mode, [switch]$NonInteractive,
                [switch]$NoOpen, [switch]$Json, [string]$Service
            )
            Set-Content -LiteralPath $env:NC_TEST_CAPTURE -Value $Command -Encoding UTF8
            return 0
        }
        Export-ModuleMember -Function Invoke-NeuralCloudLauncher
        """,
    )

    completed = _run_script(entrypoint, command, environment={"NC_TEST_CAPTURE": str(capture_path)})

    assert completed.returncode == 0, completed.stderr or completed.stdout
    assert capture_path.read_text(encoding="utf-8-sig").strip() == command


@pytest.mark.parametrize(
    "arguments",
    [
        ["launch"],
        ["up", "-Mode", "unsupported"],
    ],
)
def test_dev_entrypoint_rejects_invalid_values_before_module_invocation(
    tmp_path: Path,
    arguments: list[str],
) -> None:
    entrypoint = _copy_entrypoint(tmp_path)
    marker = tmp_path / "invoked.txt"
    _install_fake_module(
        tmp_path,
        r"""
        function Invoke-NeuralCloudLauncher {
            param(
                [string]$Command, [string]$Root, [string]$MistyAIPath,
                [string]$Distro, [string]$Mode, [switch]$NonInteractive,
                [switch]$NoOpen, [switch]$Json, [string]$Service
            )
            Set-Content -LiteralPath $env:NC_TEST_MARKER -Value "invoked"
            return 0
        }
        Export-ModuleMember -Function Invoke-NeuralCloudLauncher
        """,
    )

    completed = _run_script(entrypoint, *arguments, environment={"NC_TEST_MARKER": str(marker)})

    assert completed.returncode != 0
    assert not marker.exists()


def test_dev_entrypoint_reports_missing_module_as_json_configuration_failure(tmp_path: Path) -> None:
    entrypoint = _copy_entrypoint(tmp_path)

    completed = _run_script(entrypoint, "doctor", "-Json")

    assert completed.returncode == 40
    payload = _json_stdout(completed)
    assert payload["schema_version"] == "neuralcloud-launcher-result-v1"
    assert payload["command"] == "doctor"
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 40
    assert "module is missing" in str(payload["error"]).lower()


@pytest.mark.parametrize("exit_code", [0, 10, 20, 30, 40, 50])
def test_dev_entrypoint_preserves_launcher_exit_codes(tmp_path: Path, exit_code: int) -> None:
    entrypoint = _copy_entrypoint(tmp_path)
    _install_fake_module(
        tmp_path,
        r"""
        function Invoke-NeuralCloudLauncher {
            param(
                [string]$Command, [string]$Root, [string]$MistyAIPath,
                [string]$Distro, [string]$Mode, [switch]$NonInteractive,
                [switch]$NoOpen, [switch]$Json, [string]$Service
            )
            return [int]$env:NC_TEST_EXIT_CODE
        }
        Export-ModuleMember -Function Invoke-NeuralCloudLauncher
        """,
    )

    completed = _run_script(entrypoint, "status", environment={"NC_TEST_EXIT_CODE": str(exit_code)})

    assert completed.returncode == exit_code


def test_dev_entrypoint_redacts_unhandled_errors_in_json_mode(tmp_path: Path) -> None:
    entrypoint = _copy_entrypoint(tmp_path)
    secret = "launcher-entrypoint-secret-161803"
    _install_fake_module(
        tmp_path,
        r"""
        function Protect-LauncherText {
            param([string]$Text)
            return $Text.Replace($env:NC_TEST_SECRET, "<redacted>")
        }
        function Invoke-NeuralCloudLauncher {
            param(
                [string]$Command, [string]$Root, [string]$MistyAIPath,
                [string]$Distro, [string]$Mode, [switch]$NonInteractive,
                [switch]$NoOpen, [switch]$Json, [string]$Service
            )
            throw "Authorization: Bearer $env:NC_TEST_SECRET"
        }
        Export-ModuleMember -Function Invoke-NeuralCloudLauncher, Protect-LauncherText
        """,
    )

    completed = _run_script(entrypoint, "up", "-Json", environment={"NC_TEST_SECRET": secret})

    assert completed.returncode == 40
    assert secret not in completed.stdout
    assert secret not in completed.stderr
    payload = _json_stdout(completed)
    assert payload["exit_code"] == 40
    assert "<redacted>" in str(payload["error"])


@pytest.mark.parametrize("mismatched_unit", ["body", "operator"])
def test_initialize_refuses_same_named_unit_with_wrong_execstart_before_starting(
    tmp_path: Path,
    mismatched_unit: str,
) -> None:
    body_status = "mismatch" if mismatched_unit == "body" else "valid"
    operator_status = "mismatch" if mismatched_unit == "operator" else "valid"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:calls = New-Object System.Collections.Generic.List[string]
        $script:probe = ''
        $probeStdout = "mistyai-body.service={body_status}`n" +
            "mistyai-body.service-enabled=enabled`n" +
            "mistyai-operator.service={operator_status}`n" +
            "mistyai-operator.service-enabled=enabled`n" +
            "secrets=valid`n"
        $invoker = {{
            param($request)
            $effectiveScript = [string]$request.ArgumentList[-1]
            [void]$script:calls.Add($effectiveScript)
            if ($effectiveScript.Contains('probe_unit()')) {{
                $script:probe = $effectiveScript
                return [pscustomobject]@{{
                    exit_code = 0
                    stdout = $probeStdout
                    stderr = ''
                }}
            }}
            return [pscustomobject]@{{ exit_code = 0; stdout = ''; stderr = '' }}
        }}

        $failureCode = 0
        $failureMessage = ''
        try {{
            Initialize-MistyAIConfiguration `
                -Distro 'Ubuntu-24.04' `
                -DeploymentChanged:$false `
                -StartEnvironment `
                -CommandInvoker $invoker | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 20
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            probe = $script:probe
            calls = @($script:calls)
        }}
        """,
    )

    assert result["failure_code"] == 40
    assert "unexpected file, execstart, or enablement state" in result["failure_message"].lower()
    assert "refusing to overwrite or start" in result["failure_message"].lower()

    probe = result["probe"]
    assert "systemctl show \"$unit\" -p ExecStart --value" in probe
    assert 'grep -Fq "path=$expected_exec ;"' in probe
    assert "mistyai-body.service) expected_exec=/opt/mistyai/.venv/bin/mistyai-body" in probe
    assert "mistyai-operator.service) expected_exec=/opt/mistyai/.venv/bin/mistyai-operator-api" in probe
    assert "probe_unit mistyai-body.service" in probe
    assert "probe_unit mistyai-operator.service" in probe
    assert 'elif verify_mistyai_unit_absent "$unit"; then' in probe
    assert "printf '%s=missing" in probe
    assert "printf '%s=mismatch" in probe

    calls = result["calls"]
    assert len(calls) == 3
    assert "refusing symbolic-link configuration or data path" in calls[0]
    assert "mistyctl config init" in calls[1]
    assert calls[2] == probe
    assert not any("mistyctl env up" in call for call in calls)
    assert not any("mistyctl service install" in call for call in calls)
    assert not any("systemctl restart" in call for call in calls)


@pytest.mark.parametrize(
    (
        "body_unit",
        "body_enabled",
        "operator_unit",
        "operator_enabled",
        "expected_body_action",
        "expected_operator_action",
    ),
    [
        (
            "valid",
            "enabled",
            "valid",
            "disabled",
            "systemctl enable mistyai-body.service",
            "systemctl disable mistyai-operator.service",
        ),
        (
            "missing",
            "missing",
            "missing",
            "missing",
            "systemctl disable mistyai-body.service",
            "systemctl disable mistyai-operator.service",
        ),
    ],
)
def test_initialize_restores_original_enablement_and_verifies_service_install(
    tmp_path: Path,
    body_unit: str,
    body_enabled: str,
    operator_unit: str,
    operator_enabled: str,
    expected_body_action: str,
    expected_operator_action: str,
) -> None:
    expected_body_enabled = "enabled" if body_enabled == "enabled" else "disabled"
    expected_operator_enabled = "enabled" if operator_enabled == "enabled" else "disabled"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:calls = New-Object System.Collections.Generic.List[string]
        $script:probeCalls = 0
        $initialProbe = "mistyai-body.service={body_unit}`n" +
            "mistyai-body.service-enabled={body_enabled}`n" +
            "mistyai-operator.service={operator_unit}`n" +
            "mistyai-operator.service-enabled={operator_enabled}`n" +
            "secrets=repair`n"
        $postProbe = "mistyai-body.service=valid`n" +
            "mistyai-body.service-enabled={expected_body_enabled}`n" +
            "mistyai-operator.service=valid`n" +
            "mistyai-operator.service-enabled={expected_operator_enabled}`n" +
            "secrets=valid`n"
        $invoker = {{
            param($request)
            $effectiveScript = [string]$request.ArgumentList[-1]
            [void]$script:calls.Add($effectiveScript)
            if ($effectiveScript.Contains('probe_unit()')) {{
                $script:probeCalls++
                $stdout = if ($script:probeCalls -eq 1) {{ $initialProbe }} else {{ $postProbe }}
                return [pscustomobject]@{{ exit_code = 0; stdout = $stdout; stderr = '' }}
            }}
            return [pscustomobject]@{{ exit_code = 0; stdout = ''; stderr = '' }}
        }}

        $initialized = Initialize-MistyAIConfiguration `
            -Distro 'Ubuntu-24.04' `
            -DeploymentChanged:$false `
            -CommandInvoker $invoker
        [pscustomobject]@{{
            initialized = $initialized
            probe_calls = $script:probeCalls
            calls = @($script:calls)
        }}
        """,
    )

    assert result["initialized"] == {
        "units_installed": True,
        "enablement_restored": True,
    }
    assert result["probe_calls"] == 2
    calls = result["calls"]
    probe_indices = [index for index, call in enumerate(calls) if "probe_unit()" in call]
    assert len(probe_indices) == 2
    install_index = next(index for index, call in enumerate(calls) if "mistyctl service install" in call)
    restore_index = next(
        index
        for index, call in enumerate(calls)
        if "systemctl stop mistyai-operator.service" in call
    )
    assert probe_indices[0] < install_index < restore_index < probe_indices[1]
    restore_script = calls[restore_index]
    assert expected_body_action in restore_script
    assert expected_operator_action in restore_script
    assert "if systemctl is-enabled --quiet mistyai-body.service; then exit 72; fi" in restore_script or (
        "systemctl is-enabled --quiet mistyai-body.service" in restore_script
    )
    assert "systemctl is-enabled --quiet mistyai-operator.service" in restore_script


@pytest.mark.parametrize(
    ("post_body_unit", "post_body_enabled", "post_secrets"),
    [
        ("mismatch", "disabled", "valid"),
        ("valid", "enabled", "valid"),
        ("valid", "disabled", "repair"),
    ],
)
def test_initialize_treats_post_install_identity_enablement_and_secrets_as_hard_gate(
    tmp_path: Path,
    post_body_unit: str,
    post_body_enabled: str,
    post_secrets: str,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:probeCalls = 0
        $script:serviceInstallCalls = 0
        $initialProbe = "mistyai-body.service=missing`n" +
            "mistyai-body.service-enabled=missing`n" +
            "mistyai-operator.service=missing`n" +
            "mistyai-operator.service-enabled=missing`n" +
            "secrets=repair`n"
        $postProbe = "mistyai-body.service={post_body_unit}`n" +
            "mistyai-body.service-enabled={post_body_enabled}`n" +
            "mistyai-operator.service=valid`n" +
            "mistyai-operator.service-enabled=disabled`n" +
            "secrets={post_secrets}`n"
        $invoker = {{
            param($request)
            $effectiveScript = [string]$request.ArgumentList[-1]
            if ($effectiveScript.Contains('probe_unit()')) {{
                $script:probeCalls++
                $stdout = if ($script:probeCalls -eq 1) {{ $initialProbe }} else {{ $postProbe }}
                return [pscustomobject]@{{ exit_code = 0; stdout = $stdout; stderr = '' }}
            }}
            if ($effectiveScript.Contains('mistyctl service install')) {{
                $script:serviceInstallCalls++
            }}
            return [pscustomobject]@{{ exit_code = 0; stdout = ''; stderr = '' }}
        }}

        $failureCode = 0
        $failureMessage = ''
        try {{
            Initialize-MistyAIConfiguration `
                -Distro 'Ubuntu-24.04' `
                -DeploymentChanged:$false `
                -CommandInvoker $invoker | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            probe_calls = $script:probeCalls
            service_install_calls = $script:serviceInstallCalls
        }}
        """,
    )

    assert result["failure_code"] == 20
    assert "did not leave the expected unit identity" in result["failure_message"].lower()
    assert result["probe_calls"] == 2
    assert result["service_install_calls"] == 1


def test_initialize_reuses_fully_healthy_misty_components_without_restart(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        """
        $script:calls = New-Object System.Collections.Generic.List[string]
        $probeStdout = "mistyai-body.service=valid`n" +
            "mistyai-body.service-enabled=enabled`n" +
            "mistyai-operator.service=valid`n" +
            "mistyai-operator.service-enabled=enabled`n" +
            "secrets=valid`n"
        $invoker = {
            param($request)
            $effectiveScript = [string]$request.ArgumentList[-1]
            [void]$script:calls.Add($effectiveScript)
            $stdout = if ($effectiveScript.Contains('probe_unit()')) { $probeStdout } else { '' }
            return [pscustomobject]@{ exit_code = 0; stdout = $stdout; stderr = '' }
        }

        $initialized = Initialize-MistyAIConfiguration `
            -Distro 'Ubuntu-24.04' `
            -DeploymentChanged:$false `
            -StartEnvironment `
            -PreserveBody `
            -PreserveOperator `
            -PreserveCompose `
            -CommandInvoker $invoker
        [pscustomobject]@{
            initialized = $initialized
            calls = @($script:calls)
        }
        """,
    )

    assert result["initialized"] == {
        "units_installed": False,
        "enablement_restored": True,
    }
    calls = result["calls"]
    assert sum("probe_unit()" in call for call in calls) == 1
    assert not any("mistyctl env up" in call for call in calls)
    assert not any("mistyctl service install" in call for call in calls)
    assert not any("systemctl start" in call for call in calls)
    assert not any("systemctl restart" in call for call in calls)


def test_initialize_repairs_secret_permissions_without_direct_unit_restart(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        """
        $script:calls = New-Object System.Collections.Generic.List[string]
        $script:probeCalls = 0
        $initialProbe = "mistyai-body.service=valid`n" +
            "mistyai-body.service-enabled=enabled`n" +
            "mistyai-operator.service=valid`n" +
            "mistyai-operator.service-enabled=enabled`n" +
            "secrets=repair`n"
        $postProbe = $initialProbe.Replace('secrets=repair', 'secrets=valid')
        $invoker = {
            param($request)
            $effectiveScript = [string]$request.ArgumentList[-1]
            [void]$script:calls.Add($effectiveScript)
            if ($effectiveScript.Contains('probe_unit()')) {
                $script:probeCalls++
                $stdout = if ($script:probeCalls -eq 1) { $initialProbe } else { $postProbe }
                return [pscustomobject]@{ exit_code = 0; stdout = $stdout; stderr = '' }
            }
            return [pscustomobject]@{ exit_code = 0; stdout = ''; stderr = '' }
        }

        Initialize-MistyAIConfiguration `
            -Distro 'Ubuntu-24.04' `
            -DeploymentChanged:$false `
            -StartEnvironment `
            -PreserveBody `
            -PreserveOperator `
            -PreserveCompose `
            -CommandInvoker $invoker | Out-Null
        [pscustomobject]@{
            probe_calls = $script:probeCalls
            calls = @($script:calls)
            probe = @($script:calls | Where-Object { $_.Contains('probe_unit()') })[0]
        }
        """,
    )

    assert result["probe_calls"] == 2
    calls = result["calls"]
    assert sum("mistyctl service install" in call for call in calls) == 1
    assert not any("mistyctl env up" in call for call in calls)
    assert not any("systemctl start" in call for call in calls)
    assert not any("systemctl restart" in call for call in calls)

    probe = result["probe"]
    assert "test -f /etc/mistyai/body-service.env && ! test -L" in probe
    assert "640:root:mistyai-svc" in probe
    assert "test -f /etc/mistyai/body-service.token && ! test -L" in probe
    assert "640:root:misty-operator" in probe
    assert "test -f /etc/mistyai/instances/misty-body-01/agent.token && ! test -L" in probe
    assert "400:2002:0" in probe
    assert "test -f /etc/mistyai/instances/misty-body-01/novnc.password && ! test -L" in probe
    assert "440:2001:misty-operator" in probe
    assert "test -d /etc/mistyai/instances/misty-body-01 && ! test -L" in probe
    assert "2770:0:misty-operator" in probe


@pytest.mark.parametrize(
    ("invalid_name", "invalid_value"),
    [
        ("body_env", "600:root:mistyai-svc"),
        ("body_token", "640:root:root"),
        ("agent_token_file", "400:2002:2002"),
        ("novnc_password", "440:2001:root"),
        ("instance_dir", "0770:0:misty-operator"),
    ],
)
def test_doctor_blocks_every_inexact_mistyai_secret_permission(
    tmp_path: Path,
    invalid_name: str,
    invalid_value: str,
) -> None:
    permissions = {
        "body_env": "640:root:mistyai-svc",
        "body_token": "640:root:misty-operator",
        "agent_token_file": "400:2002:0",
        "novnc_password": "440:2001:misty-operator",
        "instance_dir": "2770:0:misty-operator",
    }
    permissions[invalid_name] = invalid_value
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:installationProbe = ''
        $installationOutput = @'
deployment=yes
config=yes
unsafe_paths=no
units=valid
body_env={permissions['body_env']}
body_token={permissions['body_token']}
agent_token_file={permissions['agent_token_file']}
novnc_password={permissions['novnc_password']}
instance_dir={permissions['instance_dir']}
'@
        $ownershipOutput = "current=managed`nprevious=absent`n" +
            "current_fingerprint=$('a' * 64)`ncurrent_image=sha256:$('b' * 64)`n" +
            "current_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
        $checks = Get-MistyAIInstallationChecks `
            -Distro 'Ubuntu-24.04' `
            -CommandInvoker {{
                param($request)
                $bash = [string]$request.ArgumentList[-1]
                if ($bash.Contains('print_file_stat()')) {{
                    $script:installationProbe = $bash
                    return [pscustomobject]@{{ exit_code = 0; stdout = $installationOutput; stderr = '' }}
                }}
                if ($bash.Contains('classify()')) {{
                    return [pscustomobject]@{{ exit_code = 0; stdout = $ownershipOutput; stderr = '' }}
                }}
                throw "unexpected WSL probe: $bash"
            }}
        [pscustomobject]@{{
            permission_check = @($checks | Where-Object id -eq 'mistyai.token_permissions')[0]
            probe = $script:installationProbe
        }}
        """,
    )

    permission_check = result["permission_check"]
    assert permission_check["status"] == "blocked"
    assert f"{invalid_name}={invalid_value}" in permission_check["current"]
    assert "exact non-symlink permissions" in permission_check["required"]

    probe = result["probe"]
    assert "print_file_stat body_env '%a:%U:%G' /etc/mistyai/body-service.env" in probe
    assert "print_file_stat body_token '%a:%U:%G' /etc/mistyai/body-service.token" in probe
    assert "print_file_stat agent_token_file '%a:%u:%g'" in probe
    assert "/etc/mistyai/instances/misty-body-01/agent.token" in probe
    assert "print_file_stat novnc_password '%a:%u:%G'" in probe
    assert "/etc/mistyai/instances/misty-body-01/novnc.password" in probe
    assert "test -f \"$path\" && ! test -L \"$path\"" in probe
    assert "test -d /etc/mistyai/instances/misty-body-01 && ! test -L" in probe


def test_down_passes_enablement_ownership_to_disable_and_clears_it_on_success(
    tmp_path: Path,
) -> None:
    state_path = tmp_path / "down-enablement-state.json"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:stopArguments = $null
        function Stop-MistyAI {{
            param(
                [string]$Distro, [scriptblock]$CommandInvoker,
                [switch]$StopBody, [switch]$StopOperator,
                [switch]$DisableBody, [switch]$DisableOperator,
                [switch]$StopCompose, [string]$ExpectedFingerprint
            )
            $script:stopArguments = [pscustomobject]@{{
                stop_body = [bool]$StopBody
                stop_operator = [bool]$StopOperator
                disable_body = [bool]$DisableBody
                disable_operator = [bool]$DisableOperator
                stop_compose = [bool]$StopCompose
                fingerprint = $ExpectedFingerprint
            }}
            return [pscustomobject]@{{
                success = $true
                service_error = ''
                environment_error = ''
            }}
        }}

        $state = Get-LauncherDefaultState
        $state.mistyai_distro = 'Ubuntu-24.04'
        $state.mistyai_deployment_fingerprint = ('c' * 64 -join '')
        $state.mistyai_unit_enablement_owned.body = $true
        $state.mistyai_unit_enablement_owned.operator = $true
        $paths = [pscustomobject]@{{ state = {_ps_literal(state_path)} }}
        $down = Invoke-LauncherDownCommand `
            -Root {_ps_literal(tmp_path)} `
            -Distro 'Ubuntu-24.04' `
            -State $state `
            -Paths $paths `
            -CommandInvoker {{ throw 'external command runner must not be invoked' }}
        [pscustomobject]@{{
            down = $down
            stop_arguments = $script:stopArguments
            final = Get-Content -Raw -LiteralPath {_ps_literal(state_path)} | ConvertFrom-Json
        }}
        """,
    )

    assert result["down"]["status"] == "stopped"
    assert result["stop_arguments"] == {
        "stop_body": False,
        "stop_operator": False,
        "disable_body": True,
        "disable_operator": True,
        "stop_compose": False,
        "fingerprint": "c" * 64,
    }
    assert result["final"]["mistyai_unit_enablement_owned"] == {
        "body": False,
        "operator": False,
    }


def test_status_is_partial_when_any_neuralcloud_child_process_remains(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        f"""
        function Test-NeuralCloudRegistryHealth {{
            param([string]$Root)
            return [pscustomobject]@{{
                running = $false
                services = @(
                    [pscustomobject]@{{ name = 'cognitive'; running = $false }},
                    [pscustomobject]@{{ name = 'runtime'; running = $true }}
                )
            }}
        }}
        function Get-MistyAIServiceState {{
            param([string]$Distro, [scriptblock]$CommandInvoker)
            return [pscustomobject]@{{
                body = 'inactive'
                operator = 'inactive'
                body_enabled = 'disabled'
                operator_enabled = 'disabled'
                compose = 'stopped'
                running = $false
            }}
        }}
        Invoke-LauncherStatusCommand `
            -Root {_ps_literal(tmp_path)} `
            -Distro 'Ubuntu-24.04' `
            -CommandInvoker {{ throw 'external command runner must not be invoked' }}
        """,
    )

    assert result["status"] == "partial"
    assert result["exit_code"] == 0
    assert result["neuralcloud"]["running"] is False
    assert any(service["running"] for service in result["neuralcloud"]["services"])


@pytest.mark.parametrize(
    ("arguments", "expected_command"),
    [
        (["launch", "-Json"], "launch"),
        (["status", "-Mode", "unsupported", "-Json"], "status"),
    ],
)
def test_dev_entrypoint_reports_invalid_command_and_mode_as_structured_json(
    tmp_path: Path,
    arguments: list[str],
    expected_command: str,
) -> None:
    entrypoint = _copy_entrypoint(tmp_path)

    completed = _run_script(entrypoint, *arguments)

    assert completed.returncode == 40
    payload = _json_stdout(completed)
    assert payload["schema_version"] == "neuralcloud-launcher-result-v1"
    assert payload["command"] == expected_command
    assert payload["status"] == "failed"
    assert payload["exit_code"] == 40
    assert "unknown" in str(payload["error"]).lower()


def test_up_passes_initial_mistyai_health_as_preserve_flags(tmp_path: Path) -> None:
    neuralcloud_root = tmp_path / "neuralcloud-preserve"
    mistyai_root = tmp_path / "mistyai-preserve"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / "preserve-state.json"
    staging = tmp_path / "preserve-staging"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:preserve = $null
        function Assert-LauncherPrerequisites {{
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            return [pscustomobject]@{{ changed = $false; fingerprint = 'neural-preserve-test' }}
        }}
        function Get-MistyAIServiceState {{
            return [pscustomobject]@{{
                body = 'active'
                operator = 'active'
                body_enabled = 'enabled'
                operator_enabled = 'enabled'
                compose = 'running'
                running = $true
            }}
        }}
        function Deploy-MistyAIWorkspace {{
            return [pscustomobject]@{{
                changed = $false
                fingerprint = ('d' * 64 -join '')
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{
            param(
                [string]$Distro, [bool]$DeploymentChanged,
                [switch]$StartEnvironment, [switch]$PreserveBody,
                [switch]$PreserveOperator, [switch]$PreserveCompose,
                [scriptblock]$CommandInvoker
            )
            $script:preserve = [pscustomobject]@{{
                start_environment = [bool]$StartEnvironment
                body = [bool]$PreserveBody
                operator = [bool]$PreserveOperator
                compose = [bool]$PreserveCompose
            }}
            throw 'stop after preserve flags are captured'
        }}

        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{
            state = {_ps_literal(state_path)}
            staging = {_ps_literal(staging)}
        }}
        $caught = ''
        try {{
            Invoke-LauncherUpCommand `
                -Root {_ps_literal(neuralcloud_root)} `
                -MistyAIPath {_ps_literal(mistyai_root)} `
                -Distro 'Ubuntu-24.04' `
                -Mode 'demo' `
                -State $state `
                -Paths $paths `
                -NoOpen | Out-Null
        }} catch {{
            $caught = $_.Exception.Message
        }}
        [pscustomobject]@{{ caught = $caught; preserve = $script:preserve }}
        """,
    )

    assert "preserve flags" in result["caught"]
    assert result["preserve"] == {
        "start_environment": True,
        "body": True,
        "operator": True,
        "compose": True,
    }


def test_mistyai_unit_identity_rejects_dropins_and_requires_exact_managed_unit(
    tmp_path: Path,
) -> None:
    helper = _invoke_module_json(
        tmp_path,
        "Get-MistyAIUnitIdentityBash",
    )

    assert 'unit_file="/etc/systemd/system/$unit"' in helper
    assert 'expected_file="$deployment_root/deploy/systemd/$unit"' in helper
    assert 'test -f "$unit_file" && ! test -L "$unit_file"' in helper
    assert 'test -f "$expected_file" && ! test -L "$expected_file"' in helper
    assert 'cmp -s -- "$unit_file" "$expected_file"' in helper
    assert '-p FragmentPath --value' in helper
    assert '= "$unit_file"' in helper
    assert 'test -z "$(systemctl show "$unit" -p DropInPaths --value' in helper
    assert '-p ExecStart --value' in helper
    assert 'grep -Fq "path=$expected_exec ;"' in helper
    assert '! test -e "$unit_file" && ! test -L "$unit_file"' in helper
    assert 'load_state=$(systemctl show "$unit" -p LoadState --value 2>/dev/null) || return 1' in helper
    assert 'test "$load_state" = not-found' in helper
    assert '! systemctl is-active --quiet "$unit"' in helper
    assert '! systemctl is-enabled --quiet "$unit"' in helper


def test_doctor_treats_unprovable_absence_as_mismatch_not_missing(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        """
        $script:installationProbe = ''
        $installationOutput = @'
deployment=yes
config=yes
unsafe_paths=no
units=mismatch
body_env=640:root:mistyai-svc
body_token=640:root:misty-operator
agent_token_file=400:2002:0
novnc_password=440:2001:misty-operator
instance_dir=2770:0:misty-operator
'@
        $ownershipOutput = "current=managed`nprevious=absent`n" +
            "current_fingerprint=$('a' * 64)`ncurrent_image=sha256:$('b' * 64)`n" +
            "current_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
        $checks = Get-MistyAIInstallationChecks `
            -Distro 'Ubuntu-24.04' `
            -CommandInvoker {
                param($request)
                $bash = [string]$request.ArgumentList[-1]
                if ($bash.Contains('print_file_stat()')) {
                    $script:installationProbe = $bash
                    return [pscustomobject]@{ exit_code = 0; stdout = $installationOutput; stderr = '' }
                }
                return [pscustomobject]@{ exit_code = 0; stdout = $ownershipOutput; stderr = '' }
            }
        [pscustomobject]@{
            units = @($checks | Where-Object id -eq 'mistyai.systemd_units')[0]
            probe = $script:installationProbe
        }
        """,
    )

    assert result["units"]["current"] == "mismatch"
    assert result["units"]["status"] == "blocked"
    probe = result["probe"]
    assert 'if ! systemctl cat "$unit"' in probe
    assert "if verify_mistyai_unit_absent \"$unit\"; then printf 'missing'; else printf 'mismatch'; fi" in probe
    assert "verify_mistyai_unit_absent()" in probe


def test_doctor_blocks_symbolic_link_configuration_or_data_paths(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        r"""
        function Get-LauncherPrerequisiteChecks {
            return @(
                (New-LauncherCheck `
                    -Id 'windows.wsl_distro' `
                    -Category 'windows' `
                    -Current 'Ubuntu-24.04' `
                    -Required 'Ubuntu-24.04' `
                    -Status 'pass'),
                (New-LauncherCheck `
                    -Id 'wsl.ubuntu' `
                    -Category 'wsl' `
                    -Current 'ubuntu:24.04' `
                    -Required 'ubuntu:24.04' `
                    -Status 'pass'),
                (New-LauncherCheck `
                    -Id 'wsl.systemd' `
                    -Category 'wsl' `
                    -Current 'running' `
                    -Required 'systemd enabled' `
                    -Status 'pass')
            )
        }
        $script:installationProbe = ''
        $installationOutput = @'
deployment=yes
config=yes
unsafe_paths=yes
units=valid
body_env=640:root:mistyai-svc
body_token=640:root:misty-operator
agent_token_file=400:2002:0
novnc_password=440:2001:misty-operator
instance_dir=2770:0:misty-operator
'@
        $ownershipOutput = "current=managed`nprevious=absent`n" +
            "current_fingerprint=$('a' * 64)`ncurrent_image=sha256:$('b' * 64)`n" +
            "current_previous_image=`nprevious_fingerprint=`nprevious_image=`nprevious_previous_image=`n"
        $doctor = Invoke-LauncherDoctorCommand `
            -Root 'C:\NeuralCloud' `
            -MistyAIPath 'C:\MistyAI' `
            -Distro 'Ubuntu-24.04' `
            -CommandInvoker {
                param($request)
                $bash = [string]$request.ArgumentList[-1]
                if ($bash.Contains('unsafe_paths=no')) {
                    $script:installationProbe = $bash
                    return [pscustomobject]@{ exit_code = 0; stdout = $installationOutput; stderr = '' }
                }
                return [pscustomobject]@{ exit_code = 0; stdout = $ownershipOutput; stderr = '' }
            }
        [pscustomobject]@{
            doctor = $doctor
            path_check = @($doctor.checks | Where-Object id -eq 'mistyai.path_safety')[0]
            probe = $script:installationProbe
        }
        """,
    )

    assert result["doctor"]["exit_code"] == 40
    assert result["doctor"]["status"] == "blocked"
    assert result["path_check"]["status"] == "blocked"
    assert result["path_check"]["current"] == "unsafe or unreadable path detected"
    probe = result["probe"]
    for path in (
        "/etc/mistyai",
        "/etc/mistyai/body-service.token",
        "/etc/mistyai/instances/misty-body-01",
        "/var/lib/mistyai",
        "/var/lib/mistyai/evidence",
        "/run/mistyai",
    ):
        assert path in probe
    assert 'if test -L "$path"; then unsafe_paths=yes; fi' in probe


def test_doctor_skips_installation_probe_until_systemd_is_ready(tmp_path: Path) -> None:
    result = _invoke_module_json(
        tmp_path,
        r"""
        $script:installationProbeCalls = 0
        function Get-LauncherPrerequisiteChecks {
            return @(
                (New-LauncherCheck -Id 'windows.wsl_distro' -Category 'windows' `
                    -Current 'Ubuntu-24.04' -Required 'Ubuntu-24.04' -Status 'pass'),
                (New-LauncherCheck -Id 'wsl.ubuntu' -Category 'wsl' `
                    -Current 'ubuntu:24.04' -Required 'ubuntu:24.04' -Status 'pass'),
                (New-LauncherCheck -Id 'wsl.systemd' -Category 'wsl' `
                    -Current 'not running' -Required 'systemd enabled' -Status 'action_required')
            )
        }
        function Get-MistyAIInstallationChecks {
            $script:installationProbeCalls++
            throw 'installation probe must not run before systemd is ready'
        }

        $doctor = Invoke-LauncherDoctorCommand `
            -Root 'C:\NeuralCloud' `
            -MistyAIPath 'C:\MistyAI' `
            -Distro 'Ubuntu-24.04'
        [pscustomobject]@{
            doctor = $doctor
            installation_probe_calls = $script:installationProbeCalls
        }
        """,
    )

    assert result["doctor"]["status"] == "action_required"
    assert result["doctor"]["exit_code"] == 10
    assert result["installation_probe_calls"] == 0


def test_initialize_treats_previous_deployment_unit_as_stale_and_reinstalls(
    tmp_path: Path,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        """
        $script:probeCalls = 0
        $script:serviceInstallCalls = 0
        $initialProbe = "mistyai-body.service=stale`n" +
            "mistyai-body.service-enabled=disabled`n" +
            "mistyai-operator.service=valid`n" +
            "mistyai-operator.service-enabled=disabled`n" +
            "secrets=valid`n"
        $postProbe = $initialProbe.Replace('mistyai-body.service=stale', 'mistyai-body.service=valid')
        $invoker = {
            param($request)
            $effectiveScript = [string]$request.ArgumentList[-1]
            if ($effectiveScript.Contains('probe_unit()')) {
                $script:probeCalls++
                $stdout = if ($script:probeCalls -eq 1) { $initialProbe } else { $postProbe }
                return [pscustomobject]@{ exit_code = 0; stdout = $stdout; stderr = '' }
            }
            if ($effectiveScript.Contains('mistyctl service install')) {
                $script:serviceInstallCalls++
            }
            return [pscustomobject]@{ exit_code = 0; stdout = ''; stderr = '' }
        }

        $initialized = Initialize-MistyAIConfiguration `
            -Distro 'Ubuntu-24.04' `
            -DeploymentChanged:$false `
            -CommandInvoker $invoker
        [pscustomobject]@{
            initialized = $initialized
            probe_calls = $script:probeCalls
            service_install_calls = $script:serviceInstallCalls
        }
        """,
    )

    assert result["initialized"] == {
        "units_installed": True,
        "enablement_restored": True,
    }
    assert result["service_install_calls"] == 1
    assert result["probe_calls"] == 2


@pytest.mark.parametrize(
    ("health_schema", "agency_state", "manual_takeover", "expected_reason"),
    [
        ("body-contract-v1", "TERMINAL", False, "terminal or unavailable"),
        ("body-contract-v1", "AVAILABLE", True, "manual takeover is active"),
        ("body-contract-v0", "AVAILABLE", False, "contract identity is incompatible"),
    ],
)
def test_wait_mistyai_ready_rejects_terminal_manual_takeover_and_bad_contract(
    tmp_path: Path,
    health_schema: str,
    agency_state: str,
    manual_takeover: bool,
    expected_reason: str,
) -> None:
    manifest = tmp_path / "grounded-manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "embodiment": {
                    "body_instance_id": "misty-body-01",
                    "capability_requirements": [],
                }
            }
        ),
        encoding="utf-8",
    )
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:health = [pscustomobject]@{{
            schema_version = '{health_schema}'
            status = 'healthy'
            agency_state = '{agency_state}'
            manual_takeover_active = ${str(manual_takeover).lower()}
            body_instance_id = 'misty-body-01'
        }}
        $script:capabilities = [pscustomobject]@{{
            schema_version = 'body-contract-v1'
            contract_versions = @('body-contract-v1')
            body_instance_id = 'misty-body-01'
            extensions = [pscustomobject]@{{}}
        }}
        function Get-MistyAIServiceState {{
            return [pscustomobject]@{{
                body = 'active'
                operator = 'active'
                body_enabled = 'enabled'
                operator_enabled = 'enabled'
                compose = 'running'
                running = $true
            }}
        }}
        function Invoke-RestMethod {{
            param([string]$Method, [string]$Uri, [hashtable]$Headers, [int]$TimeoutSec)
            if ($Uri.EndsWith('/v1/health')) {{ return $script:health }}
            if ($Uri.EndsWith('/v1/capabilities')) {{ return $script:capabilities }}
            throw "unexpected URI: $Uri"
        }}
        function Start-Sleep {{ param([int]$Milliseconds) }}

        $failureCode = 0
        $failureMessage = ''
        try {{
            Wait-MistyAIReady `
                -Distro 'Ubuntu-24.04' `
                -Token 'readiness-gate-token-123456' `
                -ManifestPath {_ps_literal(manifest)} `
                -TimeoutSeconds 0 | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{ failure_code = $failureCode; failure_message = $failureMessage }}
        """,
    )

    assert result["failure_code"] == 20
    assert expected_reason in result["failure_message"].lower()


def test_up_does_not_start_neuralcloud_after_mistyai_readiness_gate_failure(
    tmp_path: Path,
) -> None:
    neuralcloud_root = tmp_path / "neuralcloud-misty-gate"
    mistyai_root = tmp_path / "mistyai-misty-gate"
    neuralcloud_root.mkdir()
    mistyai_root.mkdir()
    state_path = tmp_path / "misty-gate-state.json"
    staging = tmp_path / "misty-gate-staging"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:neuralStarts = 0
        function Assert-LauncherPrerequisites {{
            return [pscustomobject]@{{ checks = @(); actions = @() }}
        }}
        function Initialize-NeuralCloudEnvironment {{
            return [pscustomobject]@{{ changed = $false; fingerprint = 'misty-gate-neural' }}
        }}
        function Get-MistyAIServiceState {{
            return [pscustomobject]@{{
                body = 'active'
                operator = 'active'
                body_enabled = 'enabled'
                operator_enabled = 'enabled'
                compose = 'running'
                running = $true
            }}
        }}
        function Deploy-MistyAIWorkspace {{
            return [pscustomobject]@{{
                changed = $false
                fingerprint = ('e' * 64 -join '')
                previous_available = $false
                previous_image_id = ''
            }}
        }}
        function Initialize-MistyAIConfiguration {{}}
        function Read-MistyAIServiceToken {{ return 'misty-gate-token-123456789' }}
        function Wait-MistyAIReady {{
            Throw-LauncherFailure `
                -Message 'MistyAI did not become ready: manual takeover is active' `
                -ExitCode 20
        }}
        function Start-NeuralCloud {{
            $script:neuralStarts++
            return [pscustomobject]@{{}}
        }}

        $state = Get-LauncherDefaultState
        $paths = [pscustomobject]@{{
            state = {_ps_literal(state_path)}
            staging = {_ps_literal(staging)}
        }}
        $failureCode = 0
        $failureMessage = ''
        try {{
            Invoke-LauncherUpCommand `
                -Root {_ps_literal(neuralcloud_root)} `
                -MistyAIPath {_ps_literal(mistyai_root)} `
                -Distro 'Ubuntu-24.04' `
                -Mode 'demo' `
                -State $state `
                -Paths $paths `
                -NoOpen | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            neural_starts = $script:neuralStarts
        }}
        """,
    )

    assert result["failure_code"] == 20
    assert "manual takeover" in result["failure_message"].lower()
    assert result["neural_starts"] == 0


def test_human_launcher_result_prints_deployment_fingerprint_and_service_pids(
    tmp_path: Path,
) -> None:
    fingerprint = "f" * 64
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:hostLines = New-Object System.Collections.Generic.List[string]
        function Write-Host {{
            param([Parameter(ValueFromRemainingArguments = $true)][object[]]$Object)
            [void]$script:hostLines.Add(($Object -join ' '))
        }}
        $launcherResult = [pscustomobject]@{{
            command = 'up'
            status = 'running'
            deployment_fingerprint = '{fingerprint}'
            neuralcloud = [pscustomobject]@{{
                services = @(
                    [pscustomobject]@{{ name = 'cognitive'; pid = 4101 }},
                    [pscustomobject]@{{ name = 'homeostasis'; pid = 4102 }},
                    [pscustomobject]@{{ name = 'runtime'; pid = 4103 }},
                    [pscustomobject]@{{ name = 'gui'; pid = 4104 }}
                )
            }}
        }}
        Write-LauncherResult -Result $launcherResult
        [pscustomobject]@{{ lines = @($script:hostLines) }}
        """,
    )

    output = "\n".join(result["lines"])
    assert f"Deployment fingerprint: {fingerprint}" in output
    assert "NeuralCloud PIDs:" in output
    for name, pid in (
        ("cognitive", 4101),
        ("homeostasis", 4102),
        ("runtime", 4103),
        ("gui", 4104),
    ):
        assert f"{name}={pid}" in output


def test_logs_wsl_failure_uses_misty_exit_code_and_redacts_error(tmp_path: Path) -> None:
    secret = "launcher-logs-secret-123456789"
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:calls = 0
        $failureCode = 0
        $failureMessage = ''
        try {{
            Invoke-LauncherLogsCommand `
                -Root {_ps_literal(tmp_path)} `
                -Distro 'Ubuntu-24.04' `
                -Service 'body' `
                -CommandInvoker {{
                    param($request)
                    $script:calls++
                    $bash = [string]$request.ArgumentList[-1]
                    if ($bash.Contains('cat -- "$token"')) {{
                        return [pscustomobject]@{{
                            exit_code = 0
                            stdout = '{secret}'
                            stderr = ''
                        }}
                    }}
                    return [pscustomobject]@{{
                        exit_code = 7
                        stdout = 'partial log contained {secret}'
                        stderr = 'Authorization: Bearer {secret}'
                    }}
                }} | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            calls = $script:calls
        }}
        """,
    )

    assert result["failure_code"] == 20
    assert result["calls"] == 2
    assert "unable to read body mistyai logs" in result["failure_message"].lower()
    assert secret not in result["failure_message"]
    assert "redacted" in result["failure_message"].lower()


@pytest.mark.parametrize("service", ["all", "mistyai"])
def test_combined_mistyai_logs_fail_fast_when_mistyctl_fails(
    tmp_path: Path,
    service: str,
) -> None:
    result = _invoke_module_json(
        tmp_path,
        f"""
        $script:calls = 0
        $script:logsScript = ''
        $failureCode = 0
        $failureMessage = ''
        try {{
            Invoke-LauncherLogsCommand `
                -Root {_ps_literal(tmp_path)} `
                -Distro 'Ubuntu-24.04' `
                -Service '{service}' `
                -CommandInvoker {{
                    param($request)
                    $script:calls++
                    $bash = [string]$request.ArgumentList[-1]
                    if ($bash.Contains('cat -- "$token"')) {{
                        return [pscustomobject]@{{
                            exit_code = 0
                            stdout = 'combined-logs-token-123456789'
                            stderr = ''
                        }}
                    }}
                    $script:logsScript = $bash
                    return [pscustomobject]@{{
                        exit_code = 9
                        stdout = ''
                        stderr = 'mistyctl env logs failed'
                    }}
                }} | Out-Null
        }} catch {{
            $failureCode = Get-LauncherFailureCode -ErrorRecord $_ -Default 40
            $failureMessage = $_.Exception.Message
        }}
        [pscustomobject]@{{
            failure_code = $failureCode
            failure_message = $failureMessage
            calls = $script:calls
            logs_script = $script:logsScript
        }}
        """,
    )

    assert result["failure_code"] == 20
    assert result["calls"] == 2
    assert f"unable to read {service} mistyai logs" in result["failure_message"].lower()
    assert "mistyctl env logs failed" in result["failure_message"].lower()

    script = result["logs_script"]
    assert script.startswith("set -euo pipefail;")
    executable_check = script.index("test -x /opt/mistyai/.venv/bin/mistyctl")
    working_directory = script.index("cd /opt/mistyai")
    mistyctl_logs = script.index(".venv/bin/mistyctl env logs --tail 200")
    journal_logs = script.index("journalctl -u mistyai-body.service")
    assert executable_check < working_directory < mistyctl_logs < journal_logs
    assert "if test -x /opt/mistyai/.venv/bin/mistyctl" not in script
    assert "|| true" not in script
