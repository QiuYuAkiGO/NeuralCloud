"""Browsers must not reuse public ingress credentials across origins."""

import pytest
from fastapi import FastAPI, WebSocket
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from gui.origin import GUIOriginMiddleware
from gui import runtime_server


def app_for(origin=""):
    app = FastAPI()
    app.add_middleware(GUIOriginMiddleware, public_origin=origin)

    @app.post("/mutation")
    async def mutation():
        return {"ok": True}

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(socket: WebSocket):
        await socket.accept()
        await socket.send_json({"ok": True})
        await socket.close()

    return app


def test_public_origin_accepts_external_https_port_behind_loopback_proxy():
    with TestClient(app_for("https://misty.example:12345")) as client:
        assert client.post("/mutation", headers={"Origin": "https://misty.example:12345"}).status_code == 200
        assert client.get("/health").status_code == 200
        with client.websocket_connect("/ws", headers={"Origin": "https://misty.example:12345"}) as ws:
            assert ws.receive_json() == {"ok": True}


@pytest.mark.parametrize("headers", [
    {}, {"Origin": "null"}, {"Origin": "https://evil.example"},
    {"Origin": "https://misty.example"}, {"Origin": "http://misty.example:12345"},
    {"Origin": "https://misty.example:12345", "Sec-Fetch-Site": "cross-site"},
    {"Origin": "https://evil.example", "X-Forwarded-Host": "misty.example:12345", "X-Forwarded-Proto": "https"},
])
def test_public_mutations_and_websockets_reject_untrusted_origin(headers):
    with TestClient(app_for("https://misty.example:12345")) as client:
        assert client.post("/mutation", headers=headers).status_code == 403
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect("/ws", headers=headers):
                pass
        assert error.value.code == 1008


def test_loopback_clients_remain_compatible_but_cross_site_is_blocked():
    with TestClient(app_for()) as client:
        assert client.post("/mutation").status_code == 200
        assert client.post("/mutation", headers={"Origin": "http://testserver"}).status_code == 200
        assert client.post("/mutation", headers={"Origin": "https://evil.example"}).status_code == 403


@pytest.mark.parametrize("origin", ["http://misty.example", "https://misty.example/path", "https://user:pass@misty.example"])
def test_public_configuration_rejects_non_https_or_non_origin(monkeypatch, origin):
    monkeypatch.setenv("NC_PUBLIC_ORIGIN", origin)
    with pytest.raises(ValueError):
        runtime_server.create_app()


def test_facade_protects_all_mutations_before_contacting_runtime(monkeypatch):
    monkeypatch.setenv("NC_PUBLIC_ORIGIN", "https://misty.example:12345")
    # Without a lifespan there is deliberately no gateway: rejected requests
    # must not reach any handler or backend, including historically unguarded ones.
    client = TestClient(runtime_server.create_app())
    for method, path in [("POST", "/api/chat"), ("POST", "/api/config"), ("POST", "/api/reset"),
                         ("DELETE", "/api/presets/misty"), ("POST", "/api/ops/runtime/resume")]:
        assert client.request(method, path, json={}).status_code == 403
