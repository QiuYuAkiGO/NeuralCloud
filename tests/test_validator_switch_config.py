"""The response-validator switch survives owner rebuilds and persisted reloads."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from gui import runtime_server, server
from gui.persist import load_gui_settings, persist_gui_settings
from harness.config import Settings


@pytest.fixture(autouse=True)
def offline_services(monkeypatch):
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: False)
    monkeypatch.setattr("gui.service.probe_aux_llm", lambda *args, **kwargs: False)
    monkeypatch.delenv("NC_VALIDATOR_ENABLED", raising=False)


def test_old_configuration_keeps_response_validation_enabled(tmp_path):
    path = tmp_path / "gui.json"
    path.write_text('{"mode":"demo","model":"old-model"}', encoding="utf-8")
    assert Settings(_env_file=None).validator_enabled is True
    assert load_gui_settings(path).validator_enabled is True
    with TestClient(server.create_app(development=True, control_mode="development_debug", config_path=path)) as client:
        assert client.get("/api/status").json()["inference_config"]["validator_enabled"] is True


def test_switch_persists_as_a_boolean_with_false_preserved(tmp_path):
    path = tmp_path / "gui.json"
    settings = Settings(validator_enabled=False)
    persist_gui_settings(mode="demo", settings=settings, path=path)
    assert json.loads(path.read_text(encoding="utf-8"))["validator_enabled"] is False
    assert load_gui_settings(path).validator_enabled is False


@pytest.mark.parametrize("enabled", [None, False, True])
def test_standalone_qq_configuration_restores_switch_without_truthy_filtering(tmp_path, enabled):
    from harness.qq.draft import _load_configuration

    path = tmp_path / "qq-config.json"
    config = {"mode": "live", "model": "synthetic", "base_url": "http://model.invalid/v1"}
    if enabled is not None:
        config["validator_enabled"] = enabled
    path.write_text(json.dumps(config), encoding="utf-8")
    preset_path = Path(__file__).resolve().parents[1] / "presets/builtin/misty.json"
    settings, _ = _load_configuration(path, preset_path)
    assert settings.validator_enabled is (True if enabled is None else enabled)


def test_gui_switch_applies_immediately_survives_unrelated_rebuild_and_restart(tmp_path):
    path = tmp_path / "gui.json"
    with TestClient(server.create_app(development=True, control_mode="development_debug", config_path=path)) as client:
        response = client.post("/api/config", json={"validator_enabled": False})
        assert response.status_code == 200
        assert response.json()["inference_config"]["validator_enabled"] is False
        service = client.app.state.service
        assert service.settings.validator_enabled is False
        assert service.memory.settings.validator_enabled is False
        assert service.pipeline.settings.validator_enabled is False
        assert service.pipeline.validator.settings.validator_enabled is False
        assert client.post("/api/config", json={"model": "another-demo-model"}).status_code == 200
        assert service.settings.validator_enabled is False
    assert load_gui_settings(path).validator_enabled is False
    with TestClient(server.create_app(development=True, control_mode="development_debug", config_path=path)) as client:
        assert client.get("/api/status").json()["inference_config"]["validator_enabled"] is False
        response = client.post("/api/config", json={"validator_enabled": True})
        assert response.status_code == 200
        assert response.json()["inference_config"]["validator_enabled"] is True
    assert load_gui_settings(path).validator_enabled is True


@pytest.mark.parametrize("value", ["false", 0, {"enabled": False}])
def test_config_boolean_remains_strictly_validated(value):
    with pytest.raises(ValidationError):
        server.ConfigRequest.model_validate({"validator_enabled": value})


def test_shared_owner_config_switch_reaches_new_qq_bindings_and_survives_restart(tmp_path, monkeypatch):
    from tests.test_basic_persona_continuity import _host

    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")

    async def scenario():
        host, _ = _host(tmp_path, monkeypatch, [])
        try:
            assert host.chat.settings.validator_enabled is True
            result = await host.control("config", {"validator_enabled": False})
            assert result["inference_config"]["validator_enabled"] is False
            assert host.adapter.pipeline is host.chat.pipeline
            assert host.adapter.pipeline.settings.validator_enabled is False
            assert host.chat.pipeline.validator.settings.validator_enabled is False
            assert host.shared.qq_binding(group_id="10001", member_id="20001").settings.validator_enabled is False
            with pytest.raises(ValueError, match="must be a boolean"):
                await host.control("config", {"validator_enabled": "false"})
            assert load_gui_settings(host.config_path).validator_enabled is False
            await host.control("config", {"model": "new-demo-name"})
            assert host.chat.settings.validator_enabled is False
        finally:
            await host.close()
        host, _ = _host(tmp_path, monkeypatch, [])
        try:
            assert host.chat.settings.validator_enabled is False
            assert host.chat.pipeline.settings.validator_enabled is False
            await host.control("config", {"validator_enabled": True})
            assert host.chat.pipeline.settings.validator_enabled is True
        finally:
            await host.close()

    asyncio.run(scenario())


def test_shared_gui_forwards_switch_without_constructing_an_owner(monkeypatch):
    requests = []

    def handle(request):
        assert request.url.path == "/v1/cognitive/control"
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"inference_config": {"validator_enabled": False}})

    original_init = runtime_server.RuntimeGateway.__init__

    def gateway_init(self, base_url, token):
        original_init(self, base_url, token)
        self.client = httpx.AsyncClient(base_url=base_url, transport=httpx.MockTransport(handle))

    async def no_stream(self):
        pass

    monkeypatch.setattr(runtime_server.RuntimeGateway, "__init__", gateway_init)
    monkeypatch.setattr(runtime_server.RuntimeGateway, "start", no_stream)
    with TestClient(server.create_app(runtime_url="http://runtime.test", control_token="synthetic")) as client:
        response = client.post("/api/config", json={"validator_enabled": False})
        assert response.status_code == 200
        assert response.json()["inference_config"]["validator_enabled"] is False
    assert requests == [{"operation": "config", "payload": {"validator_enabled": False}}]
