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
    with pytest.raises(ValueError, match="ports"):
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


def test_live_model_configuration_reaches_local_mock_endpoint(tmp_path, monkeypatch):
    """Actual TCP and existing Cognitive host/client; no paid API or production service."""
    import asyncio
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import threading
    from harness.cognitive.service import CognitiveServiceHost

    captured = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            payload = json.dumps({"data": [{"id": "local-mock"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append((self.path, self.headers.get("Authorization"), body))
            payload = json.dumps({"choices": [{"message": {"role": "assistant", "content": "mock reply"},
                                               "finish_reason": "stop"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    root = Path(__file__).resolve().parents[1]
    state = tmp_path / "standalone-data"
    state.mkdir()
    config_path = state / "gui_settings.json"
    config_path.write_text(json.dumps({"mode": "live", "base_url": f"http://127.0.0.1:{server.server_port}/v1",
                                       "model": "local-mock", "api_key": "mock-only-key"}))
    (tmp_path / "standalone.json").write_text(json.dumps({**bootstrap.DEFAULT, "mode": "live"}))
    config = bootstrap.read_config(tmp_path)
    env = bootstrap.environment(tmp_path, config["ports"], config["mode"])
    env["NC_COGNITIVE_MANIFEST_PATH"] = str(root / "presets/runtime/development-demo.json")
    env["NC_RUNTIME_MANIFEST_PATH"] = env["NC_COGNITIVE_MANIFEST_PATH"]
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    host = None
    try:
        host = CognitiveServiceHost(config_path=config_path, presets_dir=state / "presets", state_path=state / "cognitive/state.json")
        assert host.chat.mode == "live"
        assert host.chat.llm.complete_chat([{"role": "user", "content": "test"}]) == "mock reply"
        path, authorization, body = captured[-1]
        assert path == "/v1/chat/completions"
        assert authorization == "Bearer mock-only-key"
        assert body["model"] == "local-mock"
    finally:
        if host:
            asyncio.run(host.close())
        server.shutdown()
        server.server_close()
        thread.join()


def test_missing_live_key_has_actionable_error(tmp_path):
    (tmp_path / "standalone.json").write_text(json.dumps({**bootstrap.DEFAULT, "mode": "live"}))
    with pytest.raises(ValueError, match="gui_settings.json"):
        bootstrap.read_config(tmp_path)
