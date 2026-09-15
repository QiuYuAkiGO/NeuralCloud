"""Shared GUI/control routes use private credentials without exposing or activating them."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from gui import runtime_server, server
from harness.runtime.web_config import WebConfigStore
from harness.runtime.web_routes import add_web_routes


@pytest.fixture
def web_gui(monkeypatch):
    requests = []
    config = {"active_revision": 0, "saved_revision": 0, "settings": {}, "api_key_configured": False}

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/web/config":
            return httpx.Response(200, json=config)
        if request.url.path == "/v1/web/test":
            return httpx.Response(200, json={"status": "partial", "checks": [], "activated": False})
        return httpx.Response(200, json={"enabled": False, "usage": {"requests": 0}})

    def gateway_init(self, base_url, token):
        self.base_url = base_url
        self.headers = {"Authorization": f"Bearer {token}"}
        self.client = httpx.AsyncClient(base_url=base_url, headers=self.headers, transport=httpx.MockTransport(handle))
        self.stream_task = self.monitor_stream_task = None

    async def no_stream(self):
        pass

    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", gateway_init)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", no_stream)
    with TestClient(runtime_server.create_app(runtime_url="http://runtime.test", control_token="private-control-fixture")) as client:
        yield client, requests


def test_page_and_configuration_use_authenticated_facade(web_gui):
    client, requests = web_gui
    page = client.get("/web")
    assert page.status_code == 200 and "联网搜索" in page.text
    assert "/static/web.js" in page.text and "/static/web.css" in page.text
    assert 'href="/web"' in client.get("/").text
    config = client.get("/api/web/config")
    status = client.get("/api/web/status")
    assert config.status_code == status.status_code == 200
    assert [request.url.path for request in requests] == ["/v1/web/config", "/v1/web/status"]
    assert all(request.headers["authorization"] == "Bearer private-control-fixture" for request in requests)
    assert "private-control-fixture" not in page.text + config.text + status.text


def test_save_and_test_preserve_payload_without_exposing_secret(web_gui):
    client, requests = web_gui
    payload = {"base_revision": 0, "api_key": "tvly-synthetic-secret", "settings": {"max_results": 2}}
    for method, path in [("PUT", "/api/web/config"), ("POST", "/api/web/test")]:
        result = client.request(method, path, json=payload, headers={"Origin": "http://testserver"})
        assert result.status_code == 200
        assert payload["api_key"] not in result.text
        assert json.loads(requests[-1].content) == payload


@pytest.mark.parametrize("headers", [{"Origin": "https://unrelated.test"}, {"Sec-Fetch-Site": "cross-site"}])
def test_mutations_reject_cross_origin_without_touching_runtime(web_gui, headers):
    client, requests = web_gui
    assert client.put("/api/web/config", json={}, headers=headers).status_code == 403
    assert client.post("/api/web/test", json={}, headers=headers).status_code == 403
    assert not requests


def test_legacy_gui_has_no_web_execution_or_configuration_routes(tmp_path):
    app = server.create_app(development=True, control_mode="development_debug", config_path=tmp_path / "legacy.json")
    assert not any(getattr(route, "path", "").startswith(("/web", "/api/web")) for route in app.routes)


@pytest.fixture
def web_control(tmp_path):
    store = WebConfigStore(tmp_path, environ={})
    calls = []

    async def connection_test(settings, key):
        calls.append((settings, key))
        if key == "tvly-failing-secret":
            raise RuntimeError("Provider echoed tvly-failing-secret")
        return {"status": "partial", "checks": [], "activated": False}

    async def status():
        return {"enabled": False}

    async def auth(authorization: str | None = Header(default=None)):
        if authorization != "Bearer private-control-fixture":
            raise HTTPException(status_code=401, detail="invalid_control_token")

    supervisor = SimpleNamespace(web_config_store=store, web=SimpleNamespace(test=connection_test, status=status))
    router = APIRouter(dependencies=[Depends(auth)])
    add_web_routes(router, supervisor)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, store, calls


def test_runtime_endpoints_all_require_bearer(web_control):
    client, _, calls = web_control
    for method, path in [("GET", "/v1/web/config"), ("PUT", "/v1/web/config"), ("POST", "/v1/web/test"), ("GET", "/v1/web/status")]:
        assert client.request(method, path, json={}).status_code == 401
    assert not calls


def test_control_save_and_test_are_redacted_and_do_not_activate(web_control):
    client, store, calls = web_control
    headers = {"Authorization": "Bearer private-control-fixture"}
    payload = {"base_revision": 0, "api_key": "tvly-synthetic-secret"}
    result = client.put("/v1/web/config", json=payload, headers=headers)
    assert result.status_code == 200 and result.json()["restart_required"]
    assert payload["api_key"] not in result.text
    assert store.load_settings() is None
    assert client.put("/v1/web/config", json=payload, headers=headers).status_code == 409
    test = client.post("/v1/web/test", json={}, headers=headers)
    assert test.status_code == 200 and test.json()["activated"] is False
    assert calls[0][1] == payload["api_key"]
    assert store.snapshot()["saved_revision"] == 1


def test_control_validation_and_provider_errors_never_echo_secrets(web_control):
    client, _, _ = web_control
    headers = {"Authorization": "Bearer private-control-fixture"}
    result = client.put("/v1/web/config", json={"base_revision": 0, "settings": {"api_key": "tvly-secret-in-wrong-field"}}, headers=headers)
    assert result.status_code == 422 and "tvly-secret" not in result.text
    result = client.post("/v1/web/test", json={"api_key": "tvly-failing-secret"}, headers=headers)
    assert result.status_code == 503 and "tvly-failing-secret" not in result.text
    result = client.put("/v1/web/config", content='{"api_key":"private-json-secret",invalid}', headers=headers)
    assert result.status_code == 422 and "private-json-secret" not in result.text


def test_gui_status_and_partial_results_render_as_text():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node unavailable")
    source = Path(runtime_server.STATIC_DIR) / "web.js"
    script = r"""
const fs = require('fs');
const fields = new Map();
global.document = {getElementById(id) { if (!fields.has(id)) fields.set(id, {value:'',textContent:'',checked:false,placeholder:''}); return fields.get(id); }};
const source = fs.readFileSync(process.argv[1], 'utf8');
eval(source.slice(0,source.indexOf('$("clear_api_key").addEventListener')) + `
const config={active_revision:0,saved_revision:0,settings:{base_url:'https://api.tavily.com'},authorization:null};
const status={enabled:true,body:{effective_capabilities:['web.search.v1']},usage:{requests:2,daily_limit:100,autonomous_requests:0,autonomous_daily_limit:20},authorization:{autonomous_enabled:true},autonomous_global_enabled:false};
fill(config,status);
const partial=testSummary({status:'partial',checks:[{capability:'web.search.v1',status:'completed'}]});
const passed=testSummary({status:'completed',checks:[{capability:'web.search.v1',status:'completed'},{capability:'web.read.v1',status:'completed'}]});
const degraded=testSummary({status:'completed',checks:[{capability:'web.search.v1',status:'completed'},{capability:'web.read.v1',status:'partial'}]});
console.log(JSON.stringify({partial,passed,degraded,fields:Object.fromEntries(fields)}));
`);
"""
    result = subprocess.run([node, "-e", script, str(source)], capture_output=True, text=True, encoding="utf-8", timeout=10, check=True)
    data = json.loads(result.stdout)
    assert not data["partial"]["passed"] and not data["degraded"]["passed"]
    assert data["passed"]["passed"]
    assert data["fields"]["restart"]["textContent"] == "尚未配置联网服务。"
    assert data["fields"]["request-usage"]["textContent"] == "2 / 100"
    assert data["fields"]["read-status"]["textContent"] == "暂不可用"
    assert "全局自主能力开关尚未开启" in data["fields"]["autonomous-status"]["textContent"]
    assert "innerHTML" not in source.read_text(encoding="utf-8")
