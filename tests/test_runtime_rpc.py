"""HTTP domain client transport retries and error text."""

from __future__ import annotations

import asyncio

import httpx
import pytest

from harness.contracts.envelope import CONTRACT_SCHEMA_VERSION
from harness.runtime.rpc import DomainRpcError, HttpHomeostasisClient

HEALTH_OK = {
    "service": "homeostasis",
    "healthy": True,
    "schema_version": CONTRACT_SCHEMA_VERSION,
    "revision": 1,
    "acknowledged_receive_seq": 0,
    "detail": {},
}


def _client(handler) -> HttpHomeostasisClient:
    transport = httpx.AsyncClient(
        base_url="http://homeostasis.test",
        transport=httpx.MockTransport(handler),
        headers={"Authorization": "Bearer tok"},
    )
    return HttpHomeostasisClient(
        "http://homeostasis.test",
        service_token="tok",
        client=transport,
    )


def test_domain_rpc_retries_empty_connect_error_once() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.ConnectError("", request=request)
        return httpx.Response(200, json=HEALTH_OK)

    async def scenario() -> None:
        client = _client(handler)
        try:
            health = await client.health()
            assert health.healthy is True
            assert calls["n"] == 2
        finally:
            await client.client.aclose()

    asyncio.run(scenario())


def test_domain_rpc_empty_connect_error_keeps_exception_type() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        raise httpx.ConnectError("", request=request)

    async def scenario() -> None:
        client = _client(handler)
        try:
            with pytest.raises(DomainRpcError, match="ConnectError") as caught:
                await client.health()
            assert "failed:" in str(caught.value)
            assert calls["n"] == 2
        finally:
            await client.client.aclose()

    asyncio.run(scenario())


def test_domain_rpc_does_not_retry_http_status() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503, text="unavailable")

    async def scenario() -> None:
        client = _client(handler)
        try:
            with pytest.raises(DomainRpcError, match="status=503") as caught:
                await client.health()
            assert "HTTPStatusError" in str(caught.value)
            assert "unavailable" in str(caught.value)
            assert calls["n"] == 1
        finally:
            await client.client.aclose()

    asyncio.run(scenario())


def test_domain_rpc_retries_empty_json_body_once() -> None:
    calls = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(200, text="")
        return httpx.Response(200, json=HEALTH_OK)

    async def scenario() -> None:
        client = _client(handler)
        try:
            health = await client.health()
            assert health.healthy is True
            assert calls["n"] == 2
        finally:
            await client.client.aclose()

    asyncio.run(scenario())
