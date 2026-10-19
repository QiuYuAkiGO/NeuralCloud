"""Focused bootstrap contracts; actual service/OS smoke runs separately on the ZIP."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from tools.release import bootstrap


def test_existing_config_is_preserved_and_invalid_mode_fails(tmp_path):
    path = tmp_path / "standalone.json"
    path.write_text('{"mode":"live","ports":{}}')
    with pytest.raises(ValueError, match="mode=demo"):
        bootstrap.read_config(tmp_path)
    assert json.loads(path.read_text())["mode"] == "live"


def test_demo_isolated_from_ambient_production(tmp_path, monkeypatch):
    monkeypatch.setenv("NC_MISTYAI_TOKEN", "do-not-inherit")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", "private.json")
    monkeypatch.setenv("NC_MODE", "live")
    values = bootstrap.environment(tmp_path, bootstrap.DEFAULT["ports"])
    assert "NC_MISTYAI_TOKEN" not in values
    assert "NC_QQ_DIRECT_CONFIG" not in values
    assert values["NC_MODE"] == "demo"
    assert values["NC_SERVICE_TOKEN"] != values["NC_CONTROL_TOKEN"]
    assert Path(values["NC_RUNTIME_STATE_DIR"]).is_relative_to(tmp_path)


def test_lock_rejects_second_process_and_releases(tmp_path):
    script = "from pathlib import Path; from tools.release.bootstrap import launch_lock; "
    script += f"ctx=launch_lock(Path({str(tmp_path)!r})); ctx.__enter__()"
    with bootstrap.launch_lock(tmp_path):
        result = subprocess.run([sys.executable, "-c", script], capture_output=True)
        assert result.returncode != 0
        assert b"already running" in result.stderr
    with bootstrap.launch_lock(tmp_path):
        pass


@pytest.mark.parametrize("ports", [{}, {**bootstrap.DEFAULT["ports"], "gui": 80},
                                    {**bootstrap.DEFAULT["ports"], "gui": 18870}])
def test_bad_ports_fail_before_install(tmp_path, ports):
    (tmp_path / "standalone.json").write_text(json.dumps({"mode": "demo", "ports": ports}))
    with pytest.raises(ValueError, match="ports"):
        bootstrap.read_config(tmp_path)
