"""The voice credential configuration belongs to the Runtime child only."""

import json

import pytest

from tests.test_start_script import POWERSHELL, _run_isolated_start

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


@pytest.mark.parametrize("source", ["environment", "saved"])
def test_voice_binding_survives_launch_without_reaching_other_domains(tmp_path, source):
    path = tmp_path / "private" / "voice-client.json"
    environment = {}
    if source == "saved":
        (tmp_path / "data").mkdir()
        (tmp_path / "data/shared-misty-launch.json").write_text(json.dumps({"NC_VOICE_CONFIG": str(path)}))
    else:
        environment["NC_VOICE_CONFIG"] = str(path)
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [launch["voice_config"] for launch in launches] == ["", "", str(path), ""]


def test_no_voice_configuration_does_not_invent_one(tmp_path):
    result, launches, _ = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(not launch["voice_config"] for launch in launches)
