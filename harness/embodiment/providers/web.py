"""Tavily transport behind negotiated, provider-neutral web capabilities."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
import hashlib
import ipaddress
import json
import math
import re
import time
from urllib.parse import urlsplit

import httpx

from harness.embodiment.capabilities import (
    WEB_READ, WEB_SEARCH, CapabilityDescriptor, CapabilityStatus, EmbodimentState,
    EmbodimentStatus, HealthStatus, ProviderCapabilities, ProviderHealth,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.web_contracts import WebError, WebResult, WebSearchArguments, WebSource, validate_public_url
from harness.research.manifest import EmbodimentCondition


WEB_CAPABILITIES = (WEB_SEARCH, WEB_READ)
MAX_RESPONSE_BYTES = 2 * 1024 * 1024
COOLDOWN_SECONDS = 30


def validate_api_base_url(value: str) -> str:
    """A trusted configured API may use HTTPS or an explicit loopback HTTP proxy."""
    try:
        if not isinstance(value, str) or len(value) > 4096 or "\\" in value or any(ord(c) <= 32 for c in value):
            raise ValueError
        parsed = urlsplit(value)
        if not parsed.hostname or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
            raise ValueError
        if parsed.port is not None and not 1 <= parsed.port <= 65535:
            raise ValueError
        if parsed.scheme == "http":
            try:
                loopback = ipaddress.ip_address(parsed.hostname).is_loopback
            except ValueError:
                loopback = parsed.hostname.lower() == "localhost"
            if not loopback:
                raise ValueError
        elif parsed.scheme != "https":
            raise ValueError
        return value.rstrip("/")
    except (TypeError, ValueError):
        raise WebError("web_api_endpoint_invalid") from None


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate_key")
        result[key] = value
    return result


def _reject_constant(_value):
    raise ValueError("non_finite_number")


class WebPageError(WebError):
    """A single inaccessible source does not disable the provider."""


class WebProviderTransport:
    provider_id = "tavily-web"
    supports_dispatch_hook = True
    def __init__(self, condition: EmbodimentCondition, base_url: str, api_key: str,
                 body_instance_id: str, timeout_seconds: float = 12,
                 transport: httpx.AsyncBaseTransport | None = None):
        self.base_url = validate_api_base_url(base_url)
        if not isinstance(api_key, str) or not api_key.strip() or any(ord(c) <= 32 or ord(c) > 126 for c in api_key):
            raise WebError("web_api_key_missing")
        if not isinstance(body_instance_id, str) or not body_instance_id.strip() or len(body_instance_id) > 256:
            raise WebError("web_body_identity_invalid")
        if isinstance(timeout_seconds, bool) or not isinstance(timeout_seconds, (int, float)) or not 0 < timeout_seconds <= 60:
            raise WebError("web_timeout_invalid")
        self.body_instance_id = body_instance_id
        self.timeout_seconds = float(timeout_seconds)
        self._api_key = api_key
        self._client = httpx.AsyncClient(
            timeout=self.timeout_seconds, trust_env=False, follow_redirects=False,
            transport=transport, headers={"Authorization": "Bearer " + api_key, "Accept": "application/json"},
        )
        self.manager = EmbodimentManager(condition.model_copy(deep=True), self)
        self._condition_hash = self._configuration_hash()
        self._client_identity = id(self._client)
        self._activated = False
        self._closed = False
        self._activation_lock = asyncio.Lock()
        self._call_lock = asyncio.Lock()
        self._states = dict.fromkeys(WEB_CAPABILITIES, HealthStatus.DEGRADED)
        self._reasons = dict.fromkeys(WEB_CAPABILITIES, "web_api_unverified")
        self._cooldowns: dict[str, float] = {}
        self._auth_failed = False

    def _configuration_hash(self):
        value = {"condition": self.manager.condition.model_dump(mode="json"), "base_url": self.base_url,
                 "body_instance_id": self.body_instance_id, "timeout_seconds": self.timeout_seconds}
        return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_id=self.provider_id, provider_kind="web_information_api",
            provider_revision="web-v1:" + self._condition_hash, body_instance_id=self.body_instance_id,
            observed_at=datetime.now(UTC), capabilities=[
                CapabilityDescriptor(capability=capability, status=CapabilityStatus.AVAILABLE,
                    implementation_version="1", limits={"max_results": 10, "max_read_sources": 3,
                        "max_response_bytes": MAX_RESPONSE_BYTES, "timeout_seconds": self.timeout_seconds,
                        "automatic_retries": False}, detail="Attributed external data, not verified claims.")
                for capability in WEB_CAPABILITIES],
        )

    async def health(self) -> ProviderHealth:
        now = time.monotonic()
        for capability, until in list(self._cooldowns.items()):
            if now >= until:
                self._cooldowns.pop(capability, None)
                self._states[capability] = HealthStatus.DEGRADED
                self._reasons[capability] = "web_api_unverified"
        states = dict(self._states)
        if self._auth_failed or self._closed:
            states = dict.fromkeys(WEB_CAPABILITIES, HealthStatus.UNAVAILABLE)
        values = set(states.values())
        overall = (HealthStatus.UNAVAILABLE if values == {HealthStatus.UNAVAILABLE} else
                   HealthStatus.HEALTHY if values == {HealthStatus.HEALTHY} else HealthStatus.DEGRADED)
        return ProviderHealth(provider_id=self.provider_id, body_instance_id=self.body_instance_id, status=overall,
            capability_health=states, observed_at=datetime.now(UTC), detail={"reason_codes": dict(self._reasons)})

    async def status(self) -> EmbodimentStatus:
        async with self._activation_lock:
            if self._configuration_hash() != self._condition_hash or id(self._client) != self._client_identity:
                self.manager.set_terminal("web_provider_activation_changed")
            if not self._activated:
                await self.manager.activate()
                self._activated = True
            else:
                await self.manager.refresh()
            return self.manager.status()

    async def _guard(self, capability: str):
        status = await self.status()
        if (status.state in {EmbodimentState.DISABLED, EmbodimentState.TERMINAL, EmbodimentState.UNAVAILABLE}
                or capability not in status.effective_capabilities):
            raise WebError("web_capability_unavailable")

    def _failure(self, capability: str, code: str, *, auth: bool = False):
        if auth:
            self._auth_failed = True
            self._states = dict.fromkeys(WEB_CAPABILITIES, HealthStatus.UNAVAILABLE)
            self._reasons = dict.fromkeys(WEB_CAPABILITIES, code)
        else:
            self._states[capability] = HealthStatus.UNAVAILABLE
            self._reasons[capability] = code
            self._cooldowns[capability] = time.monotonic() + COOLDOWN_SECONDS

    def _check_status(self, capability: str, status: int):
        if status in {401, 403}:
            self._failure(capability, "web_authentication_failed", auth=True)
            raise WebError("web_authentication_failed")
        if status == 429:
            raise WebError("web_rate_limited")
        if not 200 <= status < 300:
            raise WebError("web_provider_unavailable" if status >= 500 else "web_provider_rejected")

    async def _post(self, capability: str, path: str, payload: dict, *, before_dispatch=None) -> dict:
        # A fresh local guard immediately precedes each request; no discovery or
        # health probe spends credits. The HTTP transport never follows redirects.
        await self._guard(capability)
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if before_dispatch is not None:
                    before_dispatch()
                async with self._client.stream("POST", self.base_url + path, json=payload) as response:
                    status = response.status_code
                    self._check_status(capability, status)
                    media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                    if media_type != "application/json" and not media_type.endswith("+json"):
                        raise WebError("web_response_invalid")
                    data = bytearray()
                    async for chunk in response.aiter_bytes(chunk_size=64 * 1024):
                        if len(data) + len(chunk) > MAX_RESPONSE_BYTES:
                            raise WebError("web_response_too_large")
                        data.extend(chunk)
                    try:
                        value = json.loads(data, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
                        if not isinstance(value, dict):
                            raise ValueError
                    except (ValueError, TypeError, UnicodeError, RecursionError):
                        raise WebError("web_response_invalid") from None
                    return value
        except WebPageError:
            raise
        except WebError as exc:
            if capability not in self._cooldowns and not self._auth_failed:
                self._failure(capability, exc.code)
            raise
        except (TimeoutError, httpx.TimeoutException):
            self._failure(capability, "web_request_timeout")
            raise WebError("web_request_timeout") from None
        except (httpx.HTTPError, OSError):
            self._failure(capability, "web_network_error")
            raise WebError("web_network_error") from None

    def _success(self, capability: str):
        self._states[capability] = HealthStatus.HEALTHY
        self._reasons[capability] = "web_api_available"
        self._cooldowns.pop(capability, None)

    def _text(self, value, maximum: int) -> tuple[str, bool]:
        if not isinstance(value, str):
            raise WebError("web_response_invalid")
        value = value.replace(self._api_key, "[redacted]")
        return value[:maximum], len(value) > maximum

    def _metadata(self, value: dict) -> dict:
        request_id = value.get("request_id")
        if not isinstance(request_id, str) or re.fullmatch(r"[a-zA-Z0-9_.:-]{1,256}", request_id) is None:
            request_id = None
        usage = value.get("usage", {})
        safe_usage = {}
        if isinstance(usage, dict):
            for key in ("credits", "input_tokens", "output_tokens", "total_tokens"):
                item = usage.get(key)
                if isinstance(item, (int, float)) and not isinstance(item, bool) and math.isfinite(item) and item >= 0:
                    safe_usage[key] = item
        return {"provider_request_id": request_id, "usage": safe_usage}

    async def close(self) -> None:
        self._closed = True
        await self._client.aclose()


class TavilyWebProvider(WebProviderTransport):
    async def search(self, request: WebSearchArguments, *, before_dispatch=None) -> WebResult:
        try:
            request = WebSearchArguments.model_validate(request)
        except (TypeError, ValueError):
            raise WebError("web_search_arguments_invalid") from None
        payload = {"query": request.query, "max_results": request.max_results, "search_depth": "basic",
                   "include_answer": False, "include_raw_content": False, "include_usage": True,
                   "include_published_date": True}
        if request.time_range is not None:
            payload["time_range"] = request.time_range
        async with self._call_lock:
            value = await self._post(WEB_SEARCH, "/search", payload, before_dispatch=before_dispatch)
            try:
                rows = value.get("results")
                if not isinstance(rows, list) or len(rows) > 100:
                    raise WebError("web_response_invalid")
                sources, failures, seen = [], [], set()
                for index, row in enumerate(rows[:request.max_results]):
                    try:
                        if not isinstance(row, dict):
                            raise WebError("web_response_invalid")
                        url = validate_public_url(row.get("url"))
                        if url in seen:
                            continue
                        title, title_cut = self._text(row.get("title", ""), 1000)
                        snippet, snippet_cut = self._text(row.get("content", ""), 2000)
                        published = row.get("published_date")
                        published = self._text(published, 128)[0] if isinstance(published, str) else None
                        sources.append(WebSource(title=title, url=url, snippet=snippet, retrieved_at=datetime.now(UTC),
                            published_at=published, truncated=title_cut or snippet_cut))
                        seen.add(url)
                    except (WebError, TypeError, ValueError):
                        failures.append({"index": index, "code": "web_search_result_invalid"})
                result = WebResult(status="partial" if failures else "completed", sources=sources,
                                   failures=failures, **self._metadata(value))
            except WebError as exc:
                self._failure(WEB_SEARCH, exc.code)
                raise
            self._success(WEB_SEARCH)
            return result

    async def read(self, urls: list[str], *, before_dispatch=None) -> WebResult:
        if not isinstance(urls, list) or not 1 <= len(urls) <= 3:
            raise WebError("web_read_arguments_invalid")
        urls = [validate_public_url(url) for url in urls]
        if len(set(urls)) != len(urls):
            raise WebError("web_read_arguments_invalid")
        payload = {"urls": urls, "extract_depth": "basic", "format": "text", "include_usage": True}
        async with self._call_lock:
            value = await self._post(WEB_READ, "/extract", payload, before_dispatch=before_dispatch)
            try:
                rows, failed_rows = value.get("results"), value.get("failed_results", [])
                if not isinstance(rows, list) or not isinstance(failed_rows, list) or len(rows) > 100 or len(failed_rows) > 100:
                    raise WebError("web_response_invalid")
                found = {}
                for row in rows:
                    try:
                        if not isinstance(row, dict):
                            raise WebError("web_response_invalid")
                        url = validate_public_url(row.get("url"))
                        if url not in urls or url in found:
                            continue
                        content, content_cut = self._text(row.get("raw_content"), 12000)
                        title, title_cut = self._text(row.get("title", ""), 1000)
                        if not content.strip():
                            continue
                        found[url] = WebSource(origin="read", title=title, url=url, content=content, retrieved_at=datetime.now(UTC),
                                               truncated=content_cut or title_cut)
                    except (WebError, TypeError, ValueError):
                        continue
                failures = [{"url": url, "code": "web_extract_failed"} for url in urls if url not in found]
                result = WebResult(status="partial" if failures else "completed", sources=[found[url] for url in urls if url in found],
                                   failures=failures, **self._metadata(value))
            except WebError as exc:
                self._failure(WEB_READ, exc.code)
                raise
            self._success(WEB_READ)
            return result

    async def close(self) -> None:
        self._closed = True
        await self._client.aclose()
