"""Negotiation and bounded Tavily requests without live network or credentials."""

import asyncio
import json

import httpx
import pytest
from pydantic import ValidationError

from harness.embodiment.capabilities import WEB_READ, WEB_SEARCH, EmbodimentState
from harness.embodiment.providers.web import MAX_RESPONSE_BYTES, TavilyWebProvider, validate_api_base_url
from harness.embodiment.web_contracts import WebError, WebReadArguments, WebSearchArguments, validate_public_url
from harness.research.manifest import EmbodimentCondition


TOKEN = "test-web-secret"


def body(handler, *, capabilities=(WEB_SEARCH, WEB_READ), mode="observe", **kwargs):
    condition = EmbodimentCondition(mode=mode, body_instance_id="test-web" if mode != "off" else None,
        capability_requirements=[{"capability": capability, "required": False} for capability in capabilities])
    return TavilyWebProvider(condition, "https://api.tavily.com", TOKEN, "test-web",
                             transport=httpx.MockTransport(handler), **kwargs)


def test_search_wire_contract_and_no_health_requests():
    async def scenario():
        calls = []

        def server(request):
            calls.append(request)
            assert request.method == "POST" and str(request.url) == "https://api.tavily.com/search"
            assert request.headers["authorization"] == "Bearer " + TOKEN
            assert json.loads(request.content) == {"query": "weather", "max_results": 5,
                "search_depth": "basic", "include_answer": False, "include_raw_content": False,
                "include_usage": True, "include_published_date": True, "time_range": "day"}
            return httpx.Response(200, json={"request_id": "request-1", "results": [
                {"title": "Weather", "url": "https://example.com/weather#now", "content": "A forecast",
                 "published_date": "2026-09-12"}], "usage": {"credits": 1, "secret": TOKEN}})

        provider = body(server)
        status = await provider.status()
        assert status.state == EmbodimentState.DEGRADED and status.effective_capabilities == {WEB_SEARCH, WEB_READ}
        assert calls == []
        result = await provider.search(WebSearchArguments(query="weather", time_range="day"))
        assert result.status == "completed" and result.sources[0].source_ref == ""
        assert result.sources[0].url == "https://example.com/weather"
        assert result.sources[0].published_at == "2026-09-12"
        assert result.provider_request_id == "request-1" and result.usage == {"credits": 1}
        await provider.status()
        assert len(calls) == 1
        await provider.close()

    asyncio.run(scenario())


def test_extract_binds_only_requested_sources_and_limits_content():
    async def scenario():
        calls = []

        def server(request):
            calls.append(request)
            assert request.url.path == "/extract"
            assert json.loads(request.content) == {"urls": ["https://example.com/one", "https://example.com/two"],
                "extract_depth": "basic", "format": "text", "include_usage": True}
            return httpx.Response(200, json={"results": [
                {"url": "https://evil.example/", "raw_content": "not requested"},
                {"url": "https://example.com/one", "raw_content": "a" * 13000}],
                "failed_results": [{"url": "https://example.com/two", "error": TOKEN}]})

        provider = body(server)
        result = await provider.read(["https://example.com/one", "https://example.com/two"])
        assert result.status == "partial" and len(result.sources) == 1
        assert result.sources[0].truncated and len(result.sources[0].content) == 12000
        assert result.failures == [{"url": "https://example.com/two", "code": "web_extract_failed"}]
        assert TOKEN not in result.model_dump_json() and len(calls) == 1
        await provider.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("mode,capabilities,terminal", [("off", (WEB_SEARCH,), False),
    ("observe", (WEB_READ,), False), ("observe", (WEB_SEARCH,), True)])
def test_negotiation_and_terminal_guard_never_dispatch(mode, capabilities, terminal):
    async def scenario():
        calls = []
        provider = body(lambda request: calls.append(request), mode=mode, capabilities=capabilities)
        await provider.status()
        if terminal:
            provider.manager.set_terminal("test_terminal")
        with pytest.raises(WebError, match="^web_capability_unavailable$"):
            await provider.search(WebSearchArguments(query="test"))
        assert calls == []
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("status,code", [(401, "web_authentication_failed"), (403, "web_authentication_failed"),
    (429, "web_rate_limited"), (503, "web_provider_unavailable"), (302, "web_provider_rejected")])
def test_http_failures_are_safe_and_not_retried(status, code):
    async def scenario():
        calls = []

        def server(request):
            calls.append(request)
            return httpx.Response(status, json={"error": TOKEN}, headers={"location": "https://other.example/"})

        provider = body(server)
        with pytest.raises(WebError, match="^" + code + "$"):
            await provider.search(WebSearchArguments(query="test"))
        with pytest.raises(WebError, match="^web_capability_unavailable$"):
            await provider.search(WebSearchArguments(query="test"))
        assert len(calls) == 1
        state = await provider.status()
        assert TOKEN not in state.model_dump_json()
        if status in {401, 403}:
            assert not state.effective_capabilities
        else:
            assert WEB_READ in state.effective_capabilities
        await provider.close()
    asyncio.run(scenario())


def test_cooldown_recovery_without_health_probe(monkeypatch):
    async def scenario():
        calls = []
        now = [100.0]
        monkeypatch.setattr("harness.embodiment.providers.web.time.monotonic", lambda: now[0])

        def server(request):
            calls.append(request)
            return httpx.Response(429, json={}) if len(calls) == 1 else httpx.Response(200, json={"results": []})

        provider = body(server)
        with pytest.raises(WebError, match="web_rate_limited"):
            await provider.search(WebSearchArguments(query="test"))
        now[0] += 31
        assert WEB_SEARCH in (await provider.status()).effective_capabilities and len(calls) == 1
        assert (await provider.search(WebSearchArguments(query="test"))).sources == []
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("payload,content_type,code", [(b"{broken", "application/json", "web_response_invalid"),
    (b'{"results":[],"results":[]}', "application/json", "web_response_invalid"),
    (b'{"results":[]}', "text/html", "web_response_invalid"),
    (b" " * (MAX_RESPONSE_BYTES + 1), "application/json", "web_response_too_large")],
    ids=["invalid-json", "duplicate-key", "wrong-media", "too-large"])
def test_malformed_and_oversized_responses_are_bounded(payload, content_type, code):
    async def scenario():
        provider = body(lambda request: httpx.Response(200, content=payload, headers={"content-type": content_type}))
        with pytest.raises(WebError, match="^" + code + "$"):
            await provider.search(WebSearchArguments(query="test"))
        await provider.close()
    asyncio.run(scenario())


def test_network_timeout_fixed_code_and_cooldown():
    async def scenario():
        calls = []

        def server(request):
            calls.append(request)
            raise httpx.ReadTimeout(TOKEN, request=request)

        provider = body(server)
        with pytest.raises(WebError, match="^web_request_timeout$"):
            await provider.search(WebSearchArguments(query="test"))
        assert len(calls) == 1 and WEB_SEARCH not in (await provider.status()).effective_capabilities
        await provider.close()
    asyncio.run(scenario())


def test_slow_response_stream_has_an_absolute_deadline():
    class SlowStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"results":['
            await asyncio.sleep(0.1)
            yield b"]}"

    async def scenario():
        calls = []

        def server(request):
            calls.append(request)
            return httpx.Response(200, stream=SlowStream(), headers={"content-type": "application/json"})

        provider = body(server, timeout_seconds=0.02)
        with pytest.raises(WebError, match="^web_request_timeout$"):
            await provider.search(WebSearchArguments(query="test"))
        assert len(calls) == 1
        await provider.close()
    asyncio.run(scenario())


def test_read_requires_its_own_negotiated_capability():
    async def scenario():
        calls = []
        provider = body(lambda request: calls.append(request), capabilities=(WEB_SEARCH,))
        assert (await provider.status()).effective_capabilities == {WEB_SEARCH}
        with pytest.raises(WebError, match="^web_capability_unavailable$"):
            await provider.read(["https://example.com/source"])
        assert calls == []
        await provider.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["file:///etc/passwd", "http://localhost/", "http://127.0.0.1/",
    "http://127.1/", "http://2130706433/", "http://0x7f000001/", "http://10.0.0.1/",
    "http://[::1]/", "http://[::ffff:127.0.0.1]/", "http://169.254.169.254/", "https://user:secret@example.com/",
    "https://thing.local/", "https://a.internal/", "https://example.com\\@localhost/"])
def test_source_url_rejects_private_and_non_http_destinations(url):
    with pytest.raises(WebError, match="web_source_url_invalid"):
        validate_public_url(url)


def test_api_endpoint_allows_explicit_local_proxy_only_for_http():
    assert validate_api_base_url("http://127.0.0.1:8080/") == "http://127.0.0.1:8080"
    assert validate_api_base_url("https://api.example.com/v1/") == "https://api.example.com/v1"
    for value in ("http://api.example.com", "https://user:secret@api.example.com", "https://api.example.com?key=secret"):
        with pytest.raises(WebError, match="web_api_endpoint_invalid"):
            validate_api_base_url(value)


@pytest.mark.parametrize("value", [{"query": " "}, {"query": "ok", "max_results": True},
    {"query": "ok", "api_key": TOKEN}, {"query": "ok", "time_range": "forever"}])
def test_search_model_arguments_are_strict(value):
    with pytest.raises(ValidationError):
        WebSearchArguments.model_validate(value)


def test_source_reference_bounds_and_activation_configuration():
    for refs in ([], ["x"] * 2, ["x", "y", "z", "w"]):
        with pytest.raises(ValidationError):
            WebReadArguments(source_refs=refs)

    async def scenario():
        calls = []
        provider = body(lambda request: calls.append(request))
        await provider.status()
        provider.base_url = "https://other.example"
        assert (await provider.status()).state == EmbodimentState.TERMINAL
        with pytest.raises(WebError, match="web_capability_unavailable"):
            await provider.search(WebSearchArguments(query="test"))
        assert calls == []
        await provider.close()
    asyncio.run(scenario())
