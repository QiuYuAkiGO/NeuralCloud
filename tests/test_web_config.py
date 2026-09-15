"""Configuration publication must not hot-change the web body's credentials or policy."""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from harness.research.manifest import load_experiment_manifest
from harness.runtime.web_config import WebConfigStore, WebConfigurationError, WebSettings

ROOT = Path(__file__).resolve().parents[1]
KEY = "tvly-synthetic-secret-one"
AUTH = {"enabled": True, "conversation_enabled": True, "autonomous_enabled": True}


def store(path, **kwargs):
    return WebConfigStore(path, environ={}, **kwargs)


def test_unconfigured_store_is_readonly_and_preserves_manifest(tmp_path):
    config = store(tmp_path / "runtime")
    manifest = load_experiment_manifest(ROOT / "presets/runtime/shared-misty-v1.json")
    assert config.load_settings() is None
    assert config.load_api_key() == ""
    assert config.apply_manifest(manifest) is manifest
    assert config.snapshot()["saved_revision"] == 0
    assert not (tmp_path / "runtime").exists()


def test_revision_save_is_redacted_and_only_applied_on_reconstruction(tmp_path):
    config = store(tmp_path)
    saved = config.stage({"base_revision": 0, "api_key": KEY, "authorization": AUTH})
    assert saved["saved_revision"] == 1 and saved["active_revision"] == 0
    assert saved["restart_required"] and saved["api_key_configured"]
    assert KEY not in json.dumps(saved)
    assert config.load_settings() is None and not config.load_api_key()
    restarted = store(tmp_path)
    assert restarted.load_api_key() == KEY
    assert restarted.load_settings() == WebSettings()
    assert not restarted.snapshot()["restart_required"]
    changed = restarted.stage({"base_revision": 1, "settings": {"max_results": 3}, "api_key": "tvly-synthetic-secret-two"})
    assert changed["saved_revision"] == 2 and changed["active_revision"] == 1
    assert restarted.load_api_key() == KEY
    assert restarted.load_settings().max_results == 5
    assert store(tmp_path).load_settings().max_results == 3
    for path in (tmp_path / "web").rglob("*.json"):
        assert KEY not in path.read_text()


def test_keep_clear_and_test_do_not_publish_a_revision(tmp_path):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    settings, key = config.test_configuration({"settings": {"max_results": 1}, "api_key": None})
    assert key == KEY and settings.max_results == 1
    assert config.snapshot()["saved_revision"] == 1
    config.stage({"base_revision": 1, "api_key": None})
    assert store(tmp_path).load_api_key() == KEY
    config.stage({"base_revision": 2, "clear_api_key": True})
    assert not store(tmp_path).load_api_key()


def test_authorization_overlay_preserves_global_switch_and_unrelated_capabilities(tmp_path):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY, "authorization": AUTH})
    base = load_experiment_manifest(ROOT / "presets/runtime/shared-misty-v1.json")
    payload = base.model_dump(mode="json")
    payload["autonomous_capabilities"]["enabled"] = False
    base = type(base).model_validate(payload)
    changed = store(tmp_path).apply_manifest(base)
    assert changed.web_access.enabled and changed.web_access.conversation_enabled
    assert not changed.autonomous_capabilities.enabled
    assert changed.autonomous_capabilities.owners["web"] == "shared"
    assert set(base.autonomous_capabilities.allowed_capabilities) <= set(changed.autonomous_capabilities.allowed_capabilities)
    assert changed.revision == base.revision + 1
    assert changed == store(tmp_path).apply_manifest(base)
    assert config.apply_manifest(base) is base
    config.stage({"base_revision": 1, "authorization": {"enabled": False, "conversation_enabled": False, "autonomous_enabled": False}})
    disabled = store(tmp_path).apply_manifest(base)
    assert disabled.autonomous_capabilities.owners["web"] == "off"
    assert "web.search.v1" not in disabled.autonomous_capabilities.allowed_capabilities
    assert not disabled.web_access.enabled


def test_connection_only_bundle_does_not_modify_manifest(tmp_path):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    base = load_experiment_manifest(ROOT / "presets/runtime/shared-misty-v1.json")
    assert store(tmp_path).apply_manifest(base) is base


@pytest.mark.parametrize("payload", [
    {"api_key": "short"}, {"settings": {"base_url": "http://api.tavily.com"}},
    {"settings": {"base_url": "https://secret@api.tavily.com"}},
    {"settings": {"base_url": "https://api.tavily.com/?secret=bad"}},
    {"settings": {"daily_requests": 10, "autonomous_daily_requests": 11}},
    {"settings": {"max_results": True}}, {"settings": {"timeout_seconds": float("nan")}},
    {"settings": {"api_key": KEY}}, {"authorization": AUTH},
])
def test_invalid_configuration_never_writes_or_echoes_input(tmp_path, payload):
    config = store(tmp_path)
    with pytest.raises(WebConfigurationError) as captured:
        config.stage({"base_revision": 0, **payload})
    assert KEY not in str(captured.value)
    assert "secret@" not in str(captured.value)
    assert not (tmp_path / "web").exists()


def test_revision_conflict_cannot_overwrite_saved_state(tmp_path):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    before = config.path.read_bytes()
    with pytest.raises(WebConfigurationError, match="revision_conflict"):
        config.stage({"base_revision": 0, "api_key": "tvly-another-synthetic-key"})
    assert config.path.read_bytes() == before


def test_custom_index_and_unsafe_revision_references(tmp_path):
    index = tmp_path / "private" / "index.json"
    config = WebConfigStore(tmp_path / "runtime", environ={"NC_WEB_CONFIG": str(index)})
    config.stage({"base_revision": 0, "api_key": KEY})
    assert index.is_file() and not (tmp_path / "runtime").exists()
    data = json.loads(index.read_text())
    data["revision_file"] = "../other.json"
    index.write_text(json.dumps(data))
    broken = WebConfigStore(tmp_path, config_path=index, environ={})
    assert broken.load_settings() is None
    assert broken.snapshot()["load_error"] == "web_config_invalid"
    assert broken.stage({"base_revision": 0, "api_key": KEY})["saved_revision"] == 2
    assert WebConfigStore(tmp_path, config_path=index, environ={}).load_api_key() == KEY


def test_disabled_body_never_reads_key_and_missing_key_does_not_break_startup(tmp_path, monkeypatch):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    (tmp_path / "web/keys/web-r1.key").unlink()
    restarted = store(tmp_path)
    assert restarted.load_settings() == WebSettings()
    assert restarted.load_error is None
    assert restarted.snapshot()["active_revision"] == 1
    assert restarted.load_api_key() == ""
    assert restarted.load_error == "web_api_key_unavailable"
    assert restarted.snapshot()["active_api_key_configured"] is False


def test_saved_index_corruption_does_not_hide_active_revision(tmp_path):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    restarted = store(tmp_path)
    config.path.write_text("invalid json")
    snapshot = restarted.snapshot()
    assert snapshot["active_revision"] == 1
    assert snapshot["load_error"] == "web_config_invalid"
    assert restarted.load_api_key() == KEY


def test_failed_publication_preserves_active_index_and_future_save_recovers(tmp_path, monkeypatch):
    import harness.runtime.web_config as module
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    original = module.os.replace

    def failed(*args):
        raise OSError("synthetic lock")

    monkeypatch.setattr(module.os, "replace", failed)
    with pytest.raises(WebConfigurationError, match="write_failed"):
        config.stage({"base_revision": 1, "api_key": "tvly-new-synthetic-key"})
    assert store(tmp_path).load_api_key() == KEY
    monkeypatch.setattr(module.os, "replace", original)
    result = config.stage({"base_revision": 1, "api_key": "tvly-new-synthetic-key"})
    assert result["saved_revision"] == 3
    assert store(tmp_path).load_api_key() == "tvly-new-synthetic-key"
    assert not list((tmp_path / "web").rglob("*.tmp"))


@pytest.mark.skipif(os.name == "nt", reason="Windows uses a private DACL instead of POSIX mode bits")
def test_secret_modes_are_private(tmp_path):
    config = store(tmp_path)
    config.stage({"base_revision": 0, "api_key": KEY})
    assert (tmp_path / "web").stat().st_mode & 0o777 == 0o700
    assert (tmp_path / "web/keys/web-r1.key").stat().st_mode & 0o777 == 0o600


def test_private_permissions_are_installed_before_writing_secret_bytes(tmp_path, monkeypatch):
    import harness.runtime.web_config as module
    original = module._private_permissions
    observed = []

    def check_empty(path, *, directory=False):
        if not directory:
            observed.append(path.name)
            assert path.read_bytes() == b""
        original(path, directory=directory)

    monkeypatch.setattr(module, "_private_permissions", check_empty)
    store(tmp_path).stage({"base_revision": 0, "api_key": KEY})
    assert len(observed) == 3


@pytest.mark.skipif(os.name != "nt", reason="Windows DACL verification")
def test_windows_secret_acl_is_private_to_owner_and_system(tmp_path):
    powershell = shutil.which("powershell") or shutil.which("pwsh")
    if not powershell:
        pytest.skip("PowerShell unavailable")
    store(tmp_path).stage({"base_revision": 0, "api_key": KEY})
    script = "$ErrorActionPreference='Stop'; Import-Module (Join-Path $PSHOME 'Modules/Microsoft.PowerShell.Security') -ErrorAction Stop; $acl=Get-Acl -LiteralPath $env:NC_WEB_TEST_ACL_PATH; [pscustomobject]@{protected=$acl.AreAccessRulesProtected;identities=@($acl.Access | ForEach-Object {$_.IdentityReference.Translate([Security.Principal.SecurityIdentifier]).Value})} | ConvertTo-Json -Compress"
    environment = dict(os.environ, NC_WEB_TEST_ACL_PATH=str(tmp_path / "web/keys/web-r1.key"))
    result = subprocess.run([powershell, "-NoProfile", "-NonInteractive", "-Command", script], env=environment,
                            capture_output=True, text=True, timeout=15, check=True)
    acl = json.loads(result.stdout)
    assert acl["protected"] is True
    assert set(acl["identities"]) == {"S-1-3-4", "S-1-5-18"}
