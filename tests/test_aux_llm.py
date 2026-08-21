from __future__ import annotations

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import (
    MockLLM,
    OpenAICompatClient,
    RoutingLLMClient,
    build_llm_client,
    keep_alive_value,
    preload_aux_model,
    resolve_aux_endpoint,
)


def test_resolve_aux_endpoint_falls_back_to_main() -> None:
    settings = Settings(
        llm_base_url="http://main.example/v1",
        llm_model="main-model",
        llm_api_key="main-key",
        aux_llm_base_url=None,
        aux_llm_model=None,
        aux_llm_api_key=None,
    )
    assert resolve_aux_endpoint(settings) == (
        "http://main.example/v1",
        "main-model",
        "main-key",
    )


def test_resolve_aux_endpoint_prefers_explicit_aux() -> None:
    settings = Settings(
        llm_base_url="http://main.example/v1",
        llm_model="main-model",
        llm_api_key="main-key",
        aux_llm_base_url="http://127.0.0.1:11434/v1",
        aux_llm_model="qwen3:4b-instruct",
        aux_llm_api_key="aux-key",
    )
    assert resolve_aux_endpoint(settings) == (
        "http://127.0.0.1:11434/v1",
        "qwen3:4b-instruct",
        "aux-key",
    )


def test_routing_json_goes_to_aux_chat_to_main() -> None:
    main = MockLLM(chat_routes={"role-talk": "character-reply"})
    aux = MockLLM(routes={"helper": {"ok": True, "source": "aux"}})
    client = RoutingLLMClient(main, aux)

    parsed = client.complete_json(system="helper", user="extract")
    assert parsed == {"ok": True, "source": "aux"}
    assert aux.calls
    assert not main.calls

    text = client.complete_chat([{"role": "user", "content": "role-talk"}])
    assert text == "character-reply"
    assert main.chat_calls
    assert not aux.chat_calls


def test_keep_alive_value_coerces_numbers() -> None:
    assert keep_alive_value("-1") == -1
    assert keep_alive_value("5m") == "5m"
    assert keep_alive_value("") == -1


def test_preload_aux_model_sends_keep_alive(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, object] = {}

    class FakeResponse:
        status_code = 200

        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"choices": [{"message": {"content": "ok"}}]}

    class FakeClient:
        def __init__(self, timeout: object = None) -> None:
            captured["timeout"] = timeout

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, url: str, json: dict | None = None, headers: dict | None = None) -> FakeResponse:
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return FakeResponse()

    monkeypatch.setattr("harness.llm.client.httpx.Client", FakeClient)
    settings = Settings(
        aux_llm_base_url="http://127.0.0.1:11434/v1",
        aux_llm_model="qwen3:4b-instruct",
        aux_llm_keep_alive="-1",
        aux_llm_api_key="aux-key",
    )
    assert preload_aux_model(settings) is True
    assert captured["url"] == "http://127.0.0.1:11434/api/chat"
    payload = captured["json"]
    assert isinstance(payload, dict)
    assert payload["keep_alive"] == -1
    assert payload["stream"] is False
    assert payload["options"]["num_ctx"] == 8192
    assert payload["options"]["num_gpu"] == 99
    assert payload["options"]["num_predict"] == 1
    assert payload["model"] == "qwen3:4b-instruct"


def test_preload_connect_error_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class BoomClient:
        def __init__(self, timeout: object = None) -> None:
            return None

        def __enter__(self) -> BoomClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr("harness.llm.client.httpx.Client", BoomClient)
    settings = Settings(aux_llm_base_url="http://127.0.0.1:9/v1", aux_llm_model="tiny")
    assert preload_aux_model(settings) is False


def test_preload_timeout_returns_false(monkeypatch: pytest.MonkeyPatch) -> None:
    class TimeoutClient:
        def __init__(self, timeout: object = None) -> None:
            return None

        def __enter__(self) -> TimeoutClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def post(self, *args: object, **kwargs: object) -> None:
            raise httpx.TimeoutException("timeout")

    monkeypatch.setattr("harness.llm.client.httpx.Client", TimeoutClient)
    settings = Settings(aux_llm_base_url="http://127.0.0.1:9/v1", aux_llm_model="tiny")
    assert preload_aux_model(settings) is False


def test_build_llm_client_without_aux_is_plain() -> None:
    settings = Settings(
        llm_base_url="http://127.0.0.1:9/v1",
        llm_model="qwen",
        aux_llm_base_url=None,
        aux_llm_model=None,
        aux_llm_api_key=None,
    )
    client = build_llm_client(settings)
    try:
        assert type(client) is OpenAICompatClient
        assert not isinstance(client, RoutingLLMClient)
    finally:
        client.close()


def test_build_llm_client_with_aux_wraps_two_clients() -> None:
    settings = Settings(
        llm_base_url="http://127.0.0.1:8080/v1",
        llm_model="qwen",
        aux_llm_base_url="http://127.0.0.1:11434/v1",
        aux_llm_model="qwen3:4b-instruct",
    )
    client = build_llm_client(settings)
    try:
        assert isinstance(client, RoutingLLMClient)
        assert client._aux is not client._main
        assert isinstance(client._main, OpenAICompatClient)
        assert isinstance(client._aux, OpenAICompatClient)
        assert client._aux.settings.llm_model == "qwen3:4b-instruct"
        assert client._main.settings.llm_model == "qwen"
        assert client._aux._extra_payload["keep_alive"] == -1
        assert client._aux._extra_payload["options"]["num_ctx"] == 8192
    finally:
        client.close()


def test_rebuild_without_aux_does_not_create_routing_client() -> None:
    from gui.service import HarnessChatService

    service = HarnessChatService(mode="demo")
    service.settings = service.settings.model_copy(
        update={"aux_llm_base_url": None, "aux_llm_model": None, "aux_llm_api_key": None}
    )
    service.rebuild(mode="live", base_url="http://127.0.0.1:9/v1", model="qwen")
    try:
        assert isinstance(service.llm, OpenAICompatClient)
        assert not isinstance(service.llm, RoutingLLMClient)
        assert service.aux_active is False
        payload = service.snapshot_state()["llm"]["aux"]
        assert payload["active"] is False
        assert payload["preloaded"] is False
    finally:
        closer = getattr(service.llm, "close", None)
        if callable(closer):
            closer()


def test_rebuild_with_aux_creates_routing_client() -> None:
    from gui.service import HarnessChatService

    service = HarnessChatService(mode="demo")
    service.rebuild(
        mode="live",
        base_url="http://127.0.0.1:9/v1",
        model="qwen",
        aux_base_url="http://127.0.0.1:9/aux/v1",
        aux_model="qwen3:4b-instruct",
    )
    try:
        assert isinstance(service.llm, RoutingLLMClient)
        assert service.llm._aux is not service.llm._main
        assert service.aux_active is True
        assert service.aux_preloaded is False
        payload = service.snapshot_state()["llm"]["aux"]
        assert payload["active"] is True
        assert payload["model"] == "qwen3:4b-instruct"
        assert payload["base_url"] == "http://127.0.0.1:9/aux/v1"
        assert payload["reachable"] is False
    finally:
        closer = getattr(service.llm, "close", None)
        if callable(closer):
            closer()


def test_list_endpoint_models_from_openai_compat(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.llm.client import list_endpoint_models

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, timeout: object = None) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, headers: dict | None = None) -> FakeResponse:
            del headers
            assert url.endswith("/models")
            return FakeResponse(
                200,
                {"data": [{"id": "neuralcloud-main"}, {"id": "qwen3:4b-instruct"}, {"id": "neuralcloud-main"}]},
            )

    monkeypatch.setattr("harness.llm.client.httpx.Client", FakeClient)
    assert list_endpoint_models("http://127.0.0.1:11434/v1") == [
        "neuralcloud-main",
        "qwen3:4b-instruct",
    ]


def test_list_endpoint_models_falls_back_to_ollama_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.llm.client import list_endpoint_models

    class FakeResponse:
        def __init__(self, status_code: int, payload: dict) -> None:
            self.status_code = status_code
            self._payload = payload

        def json(self) -> dict:
            return self._payload

    class FakeClient:
        def __init__(self, timeout: object = None) -> None:
            return None

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, url: str, headers: dict | None = None) -> FakeResponse:
            del headers
            if url.endswith("/models"):
                return FakeResponse(404, {})
            assert url.endswith("/api/tags")
            return FakeResponse(200, {"models": [{"name": "llama3:latest"}, {"model": "qwen3:4b-instruct"}]})

    monkeypatch.setattr("harness.llm.client.httpx.Client", FakeClient)
    assert list_endpoint_models("http://127.0.0.1:11434/v1") == ["llama3:latest", "qwen3:4b-instruct"]


def test_list_endpoint_models_unreachable_is_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    from harness.llm.client import list_endpoint_models

    class BoomClient:
        def __init__(self, timeout: object = None) -> None:
            return None

        def __enter__(self) -> BoomClient:
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def get(self, *args: object, **kwargs: object) -> None:
            raise httpx.ConnectError("refused")

    monkeypatch.setattr("harness.llm.client.httpx.Client", BoomClient)
    assert list_endpoint_models("http://127.0.0.1:9/v1") == []


def test_rebuild_ollama_defaults_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from gui.persist import DEFAULT_OLLAMA_BASE_URL
    from gui.service import HarnessChatService
    from harness.llm.client import OpenAICompatClient

    monkeypatch.setattr("gui.service.probe_endpoint", lambda *args, **kwargs: False)
    service = HarnessChatService(mode="demo")
    service.settings = service.settings.model_copy(
        update={"aux_llm_base_url": None, "aux_llm_model": None, "aux_llm_api_key": None}
    )
    service.rebuild(mode="ollama")
    try:
        assert service.mode == "ollama"
        assert service.settings.llm_base_url == DEFAULT_OLLAMA_BASE_URL
        assert service.settings.model_version == "live"
        assert isinstance(service.llm, OpenAICompatClient)
    finally:
        closer = getattr(service.llm, "close", None)
        if callable(closer):
            closer()
