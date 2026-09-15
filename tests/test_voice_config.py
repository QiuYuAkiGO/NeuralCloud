"""Voice registration requires an explicit profile without a GPU dependency."""

from __future__ import annotations

import json

import pytest

from harness.runtime.voice_config import VoiceSettings


def config(tmp_path, **updates):
    path = tmp_path / "voice.json"
    path.write_text(json.dumps({"token_file": str(tmp_path / "api.token"),
                               "profile_id": "test-misty", "profile_revision": 1, **updates}), encoding="utf-8")
    return path


def test_no_binding_disables_voice_without_reading_credentials():
    assert VoiceSettings.from_environment({}) is None


def test_explicit_profile_and_revision_are_preserved(tmp_path):
    path = config(tmp_path, profile_revision=4)
    settings = VoiceSettings.from_environment({"NC_VOICE_CONFIG": str(path)})
    assert settings.scope.profile_id == "test-misty" and settings.scope.profile_revision == 4
    assert settings.base_url == "http://127.0.0.1:8794"
    assert settings.timeout_seconds == 5
    assert not settings.token_file.exists()  # Parsing does not create or load secrets.


@pytest.mark.parametrize("updates", [
    {"profile_id": ""}, {"profile_revision": 0}, {"profile_revision": True},
    {"profile_id": "../another"}, {"token_file": "relative.token"},
    {"enabled": "true"}, {"mode": "unrestricted"},
    {"timeout_seconds": float("nan")}, {"timeout_seconds": 61},
    {"base_url": "http://private.example:8794"}, {"base_url": "http://127.0.0.1:8794/voice"},
    {"base_url": "http://user:private-secret@127.0.0.1:8794"},
    {"base_url": "http://127.0.0.1:99999"}, {"token": "private-secret"},
])
def test_invalid_configuration_is_rejected_without_echoing_contents(tmp_path, updates):
    with pytest.raises(ValueError) as error:
        VoiceSettings.from_environment({"NC_VOICE_CONFIG": str(config(tmp_path, **updates))})
    assert str(error.value) == "voice_launch_configuration_invalid"
    assert "private-secret" not in str(error.value)


def test_no_default_profile_is_fabricated(tmp_path):
    path = tmp_path / "voice.json"
    path.write_text(json.dumps({"token_file": str(tmp_path / "api.token")}))
    with pytest.raises(ValueError, match="voice_launch_configuration_invalid"):
        VoiceSettings.from_environment({"NC_VOICE_CONFIG": str(path)})


@pytest.mark.parametrize("value", ["missing.json", "relative/voice.json"])
def test_configuration_requires_existing_absolute_file(value):
    with pytest.raises(ValueError, match="voice_launch_configuration_invalid"):
        VoiceSettings.from_environment({"NC_VOICE_CONFIG": value})


def test_oversized_configuration_is_rejected(tmp_path):
    path = tmp_path / "voice.json"
    path.write_text(" " * 65537)
    with pytest.raises(ValueError, match="voice_launch_configuration_invalid"):
        VoiceSettings.from_environment({"NC_VOICE_CONFIG": str(path)})
