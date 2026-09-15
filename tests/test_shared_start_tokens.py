from __future__ import annotations

import hashlib
import json

import pytest

from tests.test_start_script import POWERSHELL, POWERSHELL_ENGINES, _run_isolated_start


pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
def test_private_shared_tokens_are_reused_by_every_process_without_disclosure(tmp_path, powershell):
    private = tmp_path / "private"
    private.mkdir()
    values = {"control": "bf" * 32, "service": "c9" * 32}
    env = {}
    for name, value in values.items():
        path = private / f"{name}.token"
        path.write_text(value)
        env[f"NC_{name.upper()}_TOKEN_FILE"] = str(path)
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment=env, powershell=powershell)
    assert result.returncode == 0, result.stdout + result.stderr
    assert len(launches) == 4
    registry_text = (tmp_path / ".neuralcloud.pids.json").read_text(encoding="utf-8-sig")
    registry = json.loads(registry_text)
    for name, value in values.items():
        path = private / f"{name}.token"
        assert path.read_text() == value
        assert registry[f"{name}_token_file"] == str(path)
        expected_hash = hashlib.sha256(value.encode()).hexdigest()
        assert all(launch[f"{name}_token_sha256"] == expected_hash for launch in launches)
        assert value not in result.stdout + result.stderr + registry_text


@pytest.mark.parametrize("powershell", POWERSHELL_ENGINES)
def test_new_private_token_files_are_atomic_and_persisted(tmp_path, powershell):
    private = tmp_path / "private"
    private.mkdir()
    env = {f"NC_{name.upper()}_TOKEN_FILE": str(private / f"{name}.token") for name in ("control", "service")}
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment=env, powershell=powershell)
    assert result.returncode == 0, result.stdout + result.stderr
    values = [(private / f"{name}.token").read_text() for name in ("control", "service")]
    assert all(len(value) == 64 for value in values) and values[0] != values[1]
    assert not list(private.glob("*.tmp"))
    for name, value in zip(("control", "service"), values, strict=True):
        assert launches[0][f"{name}_token_sha256"] == hashlib.sha256(value.encode()).hexdigest()
        assert value not in result.stdout + result.stderr


@pytest.mark.parametrize("bad", ["short", "placeholder-" + "x" * 40, "x" * 33 + " space"])
def test_invalid_existing_token_is_preserved_and_blocks_start(tmp_path, bad):
    path = tmp_path / "control.token"
    path.write_text(bad)
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment={"NC_CONTROL_TOKEN_FILE": str(path)})
    assert result.returncode == 1 and not launches
    assert path.read_text() == bad
    assert bad not in result.stdout + result.stderr
    assert "Shared token files" in result.stdout


@pytest.mark.parametrize("candidate", ["relative.token", "missing-parent"])
def test_untrusted_or_missing_token_parent_is_not_created(tmp_path, candidate):
    token = candidate if candidate == "relative.token" else str(tmp_path / "missing" / "control.token")
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment={"NC_CONTROL_TOKEN_FILE": token})
    assert result.returncode == 1 and not launches
    assert not (tmp_path / "missing").exists()


def test_control_and_service_credentials_must_be_distinct(tmp_path):
    path = tmp_path / "same.token"
    path.write_text("b3" * 32)
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment={
        "NC_CONTROL_TOKEN_FILE": str(path), "NC_SERVICE_TOKEN_FILE": str(path),
    })
    assert result.returncode == 1 and not launches
    assert path.read_text() == "b3" * 32


def test_token_acl_is_private_to_current_user_and_system(tmp_path):
    from tests.test_dev_launcher import _ps_literal, _run_script

    result, _, _ = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 0
    path = tmp_path / "data/runtime/control.token"
    script = tmp_path / "inspect-acl.ps1"
    script.write_text(f"""
Import-Module (Join-Path $PSHOME 'Modules\\Microsoft.PowerShell.Security')
$acl = Get-Acl -LiteralPath {_ps_literal(path)}
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]))
@{{ protected = $acl.AreAccessRulesProtected; current = [Security.Principal.WindowsIdentity]::GetCurrent().User.Value;
    allowed = @($rules | Where-Object {{ $_.AccessControlType -eq 'Allow' }} | ForEach-Object {{ $_.IdentityReference.Value }})
}} | ConvertTo-Json -Compress
""", encoding="utf-8")
    output = _run_script(script)
    assert output.returncode == 0, output.stderr
    acl = json.loads(output.stdout)
    assert acl["protected"] is True
    assert set(acl["allowed"]) == {acl["current"], "S-1-5-18"}
