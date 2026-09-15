from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from gui import runtime_server, server
from harness.runtime.ownership import DevelopmentModeRequired


def test_default_gui_is_facade_and_chat_reaches_existing_runtime(monkeypatch):
    calls = []

    def forbidden(**kwargs):
        raise AssertionError("Normal GUI must not construct a cognition owner")

    def handle(request):
        calls.append((request.method, request.url.path))
        if request.url.path == "/v1/messages":
            return httpx.Response(200, json={"event_id": "shared-event"})
        if request.url.path == "/v1/snapshot":
            return httpx.Response(200, json={
                "manifest": {"control_mode": "study_runtime"},
                "runtime": {}, "cognitive_ui": {
                    "shared_mind": {"owner_id": "misty", "memory_records": 1},
                    "shared_memory": {"entries": [{"record_id": "r1", "channel": "qq", "text": "synthetic"}]},
                },
            })
        return httpx.Response(403, json={"detail": "development_mode_required"})

    original_init = runtime_server.RuntimeGateway.__init__

    def gateway_init(self, base_url, token):
        original_init(self, base_url, token)
        self.client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handle))

    async def no_stream(self):
        pass

    monkeypatch.setattr(server, "HarnessChatService", forbidden)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", gateway_init)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", no_stream)
    with TestClient(server.create_app(runtime_url="http://runtime.test", control_token="test")) as client:
        status = client.get("/api/status").json()
        assert status["authority"] == {"instance_mode": "shared", "development": False}
        assert status["shared_memory"]["entries"][0]["channel"] == "qq"
        assert client.post("/api/chat", json={"text": "hello"}).status_code == 200
        assert client.post("/api/reset").status_code == 403
    assert ("POST", "/v1/messages") in calls
    assert ("POST", "/v1/cognitive/control") in calls


def test_independent_gui_requires_both_explicit_entry_and_authority(tmp_path):
    with pytest.raises(ValueError, match="independent_gui_state_requires_development"):
        server.create_app(config_path=tmp_path / "gui.json", control_mode="development_debug")
    with pytest.raises(DevelopmentModeRequired):
        server.create_app(development=True, control_mode="study_runtime", config_path=tmp_path / "gui.json")
    with pytest.raises(ValueError, match="explicit_config_path"):
        server.create_app(development=True, control_mode="development_debug")


def test_gui_default_port_does_not_claim_qq_agent():
    assert server.PORT == runtime_server.PORT == 8790


def test_shared_memory_panel_escapes_text_and_keeps_source_identity():
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    script = r"""
const fs = require('fs');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('function renderSharedMemory(');
const end = source.indexOf('\nfunction ', start + 1);
const panel = {innerHTML: ''};
const $ = () => panel;
const esc = value => String(value ?? '').replace(/[&<>"']/g, x => ({'&':'&amp;', '<':'&lt;', '>':'&gt;', '"':'&quot;', "'":'&#39;'}[x]));
eval(source.slice(start, end));
renderSharedMemory({shared_mind:{owner_id:'misty', mode:'shared',memory_records:12},shared_memory:{entries:[{record_id:'local-record',channel:'qq',session_id:'synthetic-session',author_principal:'scoped-author',display_name:'<script>bad</script>',text:'<img onerror=bad>',occurred_at:'synthetic-time',source:{original_record_id:'original-record',original_sha256:'original-digest'}}]}});
console.log(JSON.stringify(panel.innerHTML));
"""
    result = subprocess.run([node, "-e", script, str(Path(server.STATIC_DIR) / "app.js")], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
    html = json.loads(result.stdout)
    assert "共享记忆 · misty" in html
    assert "qq" in html and "scoped-author" in html and "original-record" in html
    assert "&lt;img onerror=bad&gt;" in html and "<img" not in html
    assert "<script>" not in html
