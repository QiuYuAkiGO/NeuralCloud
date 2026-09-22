"""Configuration publication must not hot-change the server body's credentials or policy."""

import json
from pathlib import Path

import asyncssh
import pytest

from harness.research.manifest import load_experiment_manifest
from harness.runtime.server_config import ServerConfigStore, ServerConfigurationError

ROOT = Path(__file__).resolve().parents[1]
AUTH = {"enabled": True, "autonomous_enabled": True}


def store(path, **kwargs):
    return ServerConfigStore(path, environ={}, **kwargs)


def _keys():
    key = asyncssh.generate_private_key("ssh-ed25519")
    private = key.export_private_key("openssh")
    public = key.export_public_key("openssh")
    host = asyncssh.generate_private_key("ssh-ed25519").export_public_key("openssh")
    if isinstance(private, bytes):
        private = private.decode()
    if isinstance(public, bytes):
        public = public.decode()
    if isinstance(host, bytes):
        host = host.decode()
    return private, public, host


def test_unconfigured_store_is_readonly_and_preserves_manifest(tmp_path):
    config = store(tmp_path / "runtime")
    manifest = load_experiment_manifest(ROOT / "presets/runtime/shared-misty-v1.json")
    assert config.load_settings() is None
    assert config.load_private_key() == ""
    assert config.apply_manifest(manifest) is manifest
    assert "server_access" not in json.loads(manifest.canonical_json())
    assert "server" not in manifest.autonomous_capabilities.owners


def test_revision_save_is_redacted_and_only_applied_on_reconstruction(tmp_path):
    private, _, host = _keys()
    config = store(tmp_path)
    saved = config.stage({"base_revision": 0, "private_key": private,
                          "settings": {"host": "127.0.0.1", "host_key": host}, "authorization": AUTH})
    assert saved["saved_revision"] == 1 and saved["active_revision"] == 0
    assert saved["restart_required"] and saved["private_key_configured"]
    assert private.strip() not in json.dumps(saved)
    assert config.load_settings() is None and not config.load_private_key()
    restarted = store(tmp_path)
    assert restarted.load_private_key().strip() == private.strip()
    assert restarted.load_settings().host == "127.0.0.1"
    changed = restarted.stage({"base_revision": 1, "settings": {"host": "127.0.0.1", "port": 2222, "host_key": host}})
    assert changed["saved_revision"] == 2 and changed["active_revision"] == 1
    assert restarted.load_settings().port == 22
    assert store(tmp_path).load_settings().port == 2222
    for path in (tmp_path / "server").rglob("*.json"):
        assert "BEGIN OPENSSH PRIVATE KEY" not in path.read_text()


def test_authorization_overlay_preserves_global_switch(tmp_path):
    private, _, host = _keys()
    config = store(tmp_path)
    config.stage({"base_revision": 0, "private_key": private,
                  "settings": {"host_key": host}, "authorization": AUTH})
    base = load_experiment_manifest(ROOT / "presets/runtime/shared-misty-v1.json")
    payload = base.model_dump(mode="json")
    payload["autonomous_capabilities"]["enabled"] = False
    base = type(base).model_validate(payload)
    changed = store(tmp_path).apply_manifest(base)
    assert changed.server_access.enabled
    assert not changed.autonomous_capabilities.enabled
    assert changed.autonomous_capabilities.owners["server"] == "shared"
    assert "server.observe.v1" in changed.autonomous_capabilities.allowed_capabilities
    config.stage({"base_revision": 1, "authorization": {"enabled": False, "autonomous_enabled": False}})
    disabled = store(tmp_path).apply_manifest(base)
    assert disabled.autonomous_capabilities.owners["server"] == "off"
    assert "server.observe.v1" not in disabled.autonomous_capabilities.allowed_capabilities
    assert not disabled.server_access.enabled


def test_generate_key_is_consumed_on_save(tmp_path):
    _, _, host = _keys()
    config = store(tmp_path)
    generated = config.generate_key()
    assert generated["public_key"].startswith("ssh-ed25519")
    saved = config.stage({"base_revision": 0, "use_generated_key": True,
                          "settings": {"host_key": host}, "authorization": AUTH})
    assert saved["private_key_configured"]
    assert store(tmp_path).load_private_key()


def test_enabled_requires_key_and_host_key(tmp_path):
    private, _, _ = _keys()
    config = store(tmp_path)
    with pytest.raises(ServerConfigurationError, match="server_enabled_requires_key_and_host_key"):
        config.stage({"base_revision": 0, "private_key": private, "authorization": AUTH})


def test_revision_conflict_cannot_overwrite_saved_state(tmp_path):
    private, _, host = _keys()
    config = store(tmp_path)
    config.stage({"base_revision": 0, "private_key": private, "settings": {"host_key": host}})
    before = config.path.read_bytes()
    with pytest.raises(ServerConfigurationError, match="revision_conflict"):
        config.stage({"base_revision": 0, "private_key": private, "settings": {"host_key": host, "port": 2222}})
    assert config.path.read_bytes() == before


@pytest.mark.parametrize("payload", [
    {"settings": {"host": "bad host"}},
    {"settings": {"host_key": "not-a-key"}},
    {"settings": {"daily_commands": 10, "autonomous_daily_commands": 11}},
    {"authorization": AUTH},
])
def test_invalid_configuration_never_writes(tmp_path, payload):
    config = store(tmp_path)
    with pytest.raises(ServerConfigurationError):
        config.stage({"base_revision": 0, **payload})
    assert not (tmp_path / "server").exists() or not list((tmp_path / "server").rglob("*.key"))
