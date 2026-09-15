"""Web configuration bindings are visible to the Runtime child only."""

import json

import pytest

from tests.test_start_script import POWERSHELL, _run_isolated_start

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


@pytest.mark.parametrize("source", ["environment", "saved"])
def test_web_config_reaches_only_runtime(tmp_path, source):
    path = tmp_path / "private/web/config.json"
    environment = {}
    if source == "saved":
        (tmp_path / "data").mkdir()
        (tmp_path / "data/shared-misty-launch.json").write_text(json.dumps({"NC_WEB_CONFIG": str(path)}))
    else:
        environment["NC_WEB_CONFIG"] = str(path)
    result, launches, _ = _run_isolated_start(tmp_path, development=False, environment=environment)
    assert result.returncode == 0, result.stdout + result.stderr
    assert [launch["web_config"] for launch in launches] == ["", "", str(path), ""]


def test_absent_binding_does_not_get_invented(tmp_path):
    result, launches, _ = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(not launch["web_config"] for launch in launches)


def test_isolated_development_does_not_inherit_shared_web_binding(tmp_path):
    result, launches, _ = _run_isolated_start(tmp_path, "-StateRoot", str(tmp_path / "isolated"),
        environment={"NC_WEB_CONFIG": str(tmp_path / "private/shared-web.json")}, saved_configuration="default")
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(not launch["web_config"] for launch in launches)
