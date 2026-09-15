from __future__ import annotations

import json

import pytest

from tests.test_start_script import POWERSHELL, _run_isolated_start

pytestmark = pytest.mark.skipif(POWERSHELL is None, reason="PowerShell unavailable")


def _config(tmp_path, payload):
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    path = data / "shared-misty-launch.json"
    path.write_text(json.dumps(payload))
    return path


def test_normal_restart_paths_come_from_persisted_binding_without_moving_history(tmp_path):
    private = tmp_path / "private"
    private.mkdir()
    (private / "model.json").write_text(json.dumps({"mode": "live"}))
    old = tmp_path / "data/runtime"
    old.mkdir(parents=True)
    journal = old / "old-journal.sqlite3"
    journal.write_bytes(b"retained-old-development-journal")
    canonical = private / "shared-runtime"
    config = _config(tmp_path, {
        "NC_RUNTIME_STATE_DIR": str(canonical),
        "NC_HOMEOSTASIS_STATE_DIR": str(private / "retained-domain"),
        "NC_GUI_CONFIG": str(private / "model.json"),
        "NC_GUI_PRESETS": str(tmp_path / "data/presets"),
        "NC_QQ_SESSION_CONFIG": str(private / "session.json"),
        "NC_QQ_WATCHER_CONFIG": str(private / "watcher.json"),
        "NC_QQ_DIRECT_CONFIG": str(private / "direct.json"),
        "NC_SHARED_MEMORY_DB": str(private / "shared-memory.sqlite3"),
        "NC_CONTROL_TOKEN_FILE": str(private / "control.token"),
        "NC_SERVICE_TOKEN_FILE": str(private / "service.token"),
    })
    before = config.read_bytes()
    result, launches, _ = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 0, result.stdout + result.stderr
    assert config.read_bytes() == before
    assert journal.read_bytes() == b"retained-old-development-journal"
    for launch in launches:
        assert launch["runtime_state_dir"] == str(canonical)
        assert launch["homeostasis_state_dir"] == str(private / "retained-domain")
        assert launch["cognitive_state_path"] == str(tmp_path / "data/cognitive/state.json")
        assert launch["gui_config"] == str(private / "model.json")
        assert launch["qq_session_config"] == str(private / "session.json")
        assert launch["qq_watcher_config"] == str(private / "watcher.json")
        assert launch["qq_direct_config"] == str(private / "direct.json")
        assert launch["shared_memory_db"] == str(private / "shared-memory.sqlite3")
    registry = json.loads((tmp_path / ".neuralcloud.pids.json").read_text(encoding="utf-8-sig"))
    assert registry["runtime_state_dir"] == str(canonical)
    assert (canonical / "active-start.json").is_file()
    assert not (canonical / journal.name).exists()


def test_explicit_parameter_and_environment_override_saved_bindings(tmp_path):
    _config(tmp_path, {"NC_HOMEOSTASIS_STATE_DIR": str(tmp_path / "saved-domain"),
                       "NC_QQ_SESSION_CONFIG": str(tmp_path / "saved-session.json")})
    chosen = tmp_path / "explicit-domain"
    result, launches, _ = _run_isolated_start(tmp_path, "-HomeostasisStateDir", str(chosen),
        development=False, environment={"NC_HOMEOSTASIS_STATE_DIR": str(tmp_path / "environment-domain"),
                                       "NC_QQ_SESSION_CONFIG": str(tmp_path / "environment-session.json")})
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(x["homeostasis_state_dir"] == str(chosen) for x in launches)
    assert all(x["qq_session_config"] == str(tmp_path / "environment-session.json") for x in launches)


def test_isolated_development_never_reads_normal_launch_bindings(tmp_path):
    _config(tmp_path, {"NC_CONTROL_TOKEN": "not-a-path-and-must-not-be-loaded"})
    state = tmp_path / "isolated-development"
    result, launches, _ = _run_isolated_start(tmp_path, "-StateRoot", str(state), saved_configuration="default")
    assert result.returncode == 0, result.stdout + result.stderr
    assert all(x["runtime_state_dir"] == str(state / "runtime") for x in launches)
    assert all(not x["qq_session_config"] for x in launches)


@pytest.mark.parametrize("payload", [{"NC_CONTROL_TOKEN": "secret-value"}, {"NC_GUI_CONFIG": "relative.json"}, {"NC_GUI_CONFIG": ["array"]}])
def test_unapproved_launch_defaults_fail_before_any_process(tmp_path, payload):
    _config(tmp_path, payload)
    result, launches, _ = _run_isolated_start(tmp_path, development=False)
    assert result.returncode == 1 and not launches
    assert "approved absolute path bindings" in result.stdout
    assert "secret-value" not in result.stdout + result.stderr
