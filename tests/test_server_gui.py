"""Shared GUI/control routes use private SSH credentials without exposing or activating them."""

from __future__ import annotations

import json
from types import SimpleNamespace

import asyncssh
import httpx
import pytest
from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException
from fastapi.testclient import TestClient

from gui import runtime_server, server
from harness.runtime.server_config import ServerConfigStore
from harness.runtime.server_routes import add_server_routes


def _private():
    key = asyncssh.generate_private_key("ssh-ed25519")
    value = key.export_private_key("openssh")
    return value.decode() if isinstance(value, bytes) else value


@pytest.fixture
def server_gui(monkeypatch):
    requests = []
    config = {"active_revision": 0, "saved_revision": 0, "settings": {}, "private_key_configured": False}

    def handle(request):
        requests.append(request)
        if request.url.path == "/v1/server/config":
            return httpx.Response(200, json=config)
        if request.url.path == "/v1/server/test":
            return httpx.Response(200, json={"status": "partial", "checks": [], "activated": False})
        if request.url.path == "/v1/server/commands":
            return httpx.Response(200, json={"commands": []})
        if request.url.path == "/v1/server/keys:generate":
            return httpx.Response(200, json={"public_key": "ssh-ed25519 AAAA", "key_type": "ssh-ed25519"})
        return httpx.Response(200, json={"enabled": False, "usage": {"commands": 0}})

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


def test_page_and_configuration_use_authenticated_facade(server_gui):
    client, requests = server_gui
    page = client.get("/server")
    assert page.status_code == 200 and "Linux 服务器" in page.text
    assert "/static/server.js" in page.text and "/static/server.css" in page.text
    assert 'href="/server"' in client.get("/").text
    assert 'href="/server"' in client.get("/goals").text
    config = client.get("/api/server/config")
    status = client.get("/api/server/status")
    assert config.status_code == status.status_code == 200
    assert [request.url.path for request in requests] == ["/v1/server/config", "/v1/server/status"]
    assert all(request.headers["authorization"] == "Bearer private-control-fixture" for request in requests)
    assert "private-control-fixture" not in page.text + config.text + status.text


def test_save_and_test_preserve_payload_without_exposing_secret(server_gui):
    client, requests = server_gui
    payload = {"base_revision": 0, "private_key": _private(), "settings": {"port": 2222}}
    for method, path in [("PUT", "/api/server/config"), ("POST", "/api/server/test")]:
        result = client.request(method, path, json=payload, headers={"Origin": "http://testserver"})
        assert result.status_code == 200
        assert "BEGIN OPENSSH PRIVATE KEY" not in result.text
        assert json.loads(requests[-1].content) == payload


@pytest.mark.parametrize("headers", [{"Origin": "https://unrelated.test"}, {"Sec-Fetch-Site": "cross-site"}])
def test_mutations_reject_cross_origin_without_touching_runtime(server_gui, headers):
    client, requests = server_gui
    assert client.put("/api/server/config", json={}, headers=headers).status_code == 403
    assert client.post("/api/server/test", json={}, headers=headers).status_code == 403
    assert client.post("/api/server/keys/generate", headers=headers).status_code == 403
    assert not requests


def test_legacy_gui_has_no_server_execution_or_configuration_routes(tmp_path):
    app = server.create_app(development=True, control_mode="development_debug", config_path=tmp_path / "legacy.json")
    assert not any(getattr(route, "path", "").startswith(("/server", "/api/server")) for route in app.routes)


@pytest.fixture
def server_control(tmp_path):
    store = ServerConfigStore(tmp_path, environ={})
    calls = []

    async def connection_test(settings, key):
        calls.append((settings, key))
        return {"status": "partial", "checks": [], "activated": False}

    async def status():
        return {"enabled": False}

    async def auth(authorization: str | None = Header(default=None)):
        if authorization != "Bearer private-control-fixture":
            raise HTTPException(status_code=401, detail="invalid_control_token")

    supervisor = SimpleNamespace(
        server_config_store=store,
        server=SimpleNamespace(test=connection_test, status=status, commands=lambda status=None: [], approve=None, reject=None),
        capabilities=None,
    )
    router = APIRouter(dependencies=[Depends(auth)])
    add_server_routes(router, supervisor)
    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        yield client, store, calls


def test_runtime_endpoints_all_require_bearer(server_control):
    client, _, calls = server_control
    for method, path in [("GET", "/v1/server/config"), ("PUT", "/v1/server/config"),
                         ("POST", "/v1/server/test"), ("GET", "/v1/server/status"),
                         ("POST", "/v1/server/keys:generate")]:
        assert client.request(method, path, json={}).status_code == 401
    assert not calls
