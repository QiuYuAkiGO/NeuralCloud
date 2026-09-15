from __future__ import annotations

import json

import pytest

from tests.test_shared_launch_bindings import _config
from tests.test_start_script import POWERSHELL, _run_isolated_start

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


@pytest.mark.parametrize("mode", ["demo", "live", "ollama"])
@pytest.mark.parametrize("source", ["default", "binding", "environment"])
def test_normal_without_mode_reuses_the_shared_model_configuration(tmp_path, mode, source):
    if source == "default":
        path = tmp_path / "data/gui_settings.json"
    else:
        path = tmp_path / "private-model.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"mode": mode, "api_key": "fixture-private-value"}))
    environment = {}
    if source == "binding":
        _config(tmp_path, {"NC_GUI_CONFIG": str(path)})
    elif source == "environment":
        environment["NC_GUI_CONFIG"] = str(path)
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert launches and all(item["model_mode"] == mode for item in launches)
    assert "fixture-private-value" not in result.stdout + result.stderr


def test_explicit_mode_wins_without_reading_a_missing_config(tmp_path):
    result, launches, _ = _run_isolated_start(tmp_path, "-Mode", "ollama", development=False,
        environment={"NC_GUI_CONFIG": str(tmp_path / "missing.json")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(item["model_mode"] == "ollama" for item in launches)


def test_development_without_mode_stays_demo(tmp_path):
    path = tmp_path / "live.json"
    path.write_text(json.dumps({"mode": "live"}))
    result, launches, _ = _run_isolated_start(tmp_path, saved_configuration="default",
        environment={"NC_GUI_CONFIG": str(path)})
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(item["model_mode"] == "demo" for item in launches)


@pytest.mark.parametrize("mode", [None, "debug", "LIVE", 1])
def test_invalid_saved_mode_fails_without_falling_back_to_mock(tmp_path, mode):
    path = tmp_path / "invalid.json"
    path.write_text(json.dumps({"mode": mode, "api_key": "fixture-private-value"}))
    result, launches, _ = _run_isolated_start(tmp_path, development=False,
        environment={"NC_GUI_CONFIG": str(path)})
    assert result.returncode == 1 and not launches
    assert "must declare demo, live, or ollama" in result.stdout
    assert "fixture-private-value" not in result.stdout + result.stderr
