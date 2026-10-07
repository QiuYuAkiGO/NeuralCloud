"""Alternate main completion on provider unavailability within one deadline.

Auxiliary JSON calls retain their route. Physical attempts retain the ordinary
transport, tracing and production budget checks.
"""
from __future__ import annotations

from copy import deepcopy
import logging
import math
import time
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from harness.config import Settings
from harness.llm.deadline import remaining_model_seconds
from infra.trace.request_spans import trace_scope

logger = logging.getLogger(__name__)


class FallbackConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")
    base_url: str
    model: str = Field(min_length=1)
    api_key: str = Field(min_length=1, repr=False)
    primary_timeout_seconds: float = Field(default=20, gt=0, le=120)
    fallback_reserve_seconds: float = Field(default=10, gt=0, le=60)
    max_output_tokens: int = Field(default=2048, ge=1, le=16384)
    health_check_interval_seconds: float = Field(default=0, ge=0, le=300)
    health_check_timeout_seconds: float = Field(default=1, gt=0, le=5)
    health_recovery_successes: int = Field(default=2, ge=1, le=5)

    @field_validator("base_url")
    @classmethod
    def valid_endpoint(cls, value):
        url = urlsplit(value)
        if (url.scheme not in {"http", "https"} or not url.hostname or
                url.username or url.password or url.query or url.fragment):
            raise ValueError("invalid fallback endpoint")
        return value.rstrip("/")


class FallbackLLMClient:
    production_dispatch_aware = True

    def __init__(self, main, fallback, config: FallbackConfig, *, health=None):
        self._main = main
        self._fallback = fallback
        self.settings = main.settings
        self.config = config
        self.health = health

    def complete_json(self, **kwargs):
        return self._main.complete_json(**kwargs)

    def complete_chat(self, messages, *, temperature=0.7, stream=False):
        return self.complete_chat_result(messages, temperature=temperature, stream=stream).text

    def _provider_unavailable(self, exc: httpx.HTTPError) -> bool:
        # Reservation/usage services can also raise HTTP errors. Never bypass
        # their failures by spending on a second provider.
        try:
            request_url = str(exc.request.url)
        except RuntimeError:
            return False
        endpoint = self.settings.llm_base_url.rstrip("/") + "/chat/completions"
        if request_url != endpoint:
            return False
        if isinstance(exc, httpx.HTTPStatusError):
            return exc.response.status_code in {401, 403, 404, 408, 429} or exc.response.status_code >= 500
        return isinstance(exc, httpx.TransportError)

    def complete_chat_result(self, messages, *, temperature=0.7, stream=False,
                             max_tokens=None, timeout_seconds=None, **kwargs):
        frozen = deepcopy(messages)
        remaining = remaining_model_seconds()
        total = self.settings.llm_timeout if timeout_seconds is None else timeout_seconds
        if remaining is not None:
            total = min(total, remaining)
        if not math.isfinite(total) or total <= 0:
            raise ValueError("request timeout must be finite and positive")
        end = time.monotonic() + total
        reserve = min(self.config.fallback_reserve_seconds, total / 2)
        primary_timeout = min(self.config.primary_timeout_seconds, total - reserve)
        attempted_primary = self.health is None or self.health.snapshot()["ready"]
        try:
            if not attempted_primary:
                raise _PrimaryNotReady()
            result = self._main.complete_chat_result(deepcopy(frozen), temperature=temperature,
                stream=stream, max_tokens=max_tokens, timeout_seconds=primary_timeout, **kwargs)
        except _PrimaryNotReady:
            reason = "HealthCircuitOpen"
        except httpx.HTTPError as exc:
            if not self._provider_unavailable(exc):
                raise
            reason = type(exc).__name__
            if self.health is not None:
                self.health.failed(reason)
        else:
            if result.text.strip() or result.has_tool_calls or result.has_refusal:
                return result
            reason = "EmptyCompletion"
            if self.health is not None:
                self.health.failed(reason)
        budget = end - time.monotonic()
        remaining = remaining_model_seconds()
        if remaining is not None:
            budget = min(budget, remaining)
        if budget <= 0:
            raise TimeoutError("main_reply_deadline_exceeded")
        logger.warning("Main provider unavailable (%s); attempting fallback model %s",
                       reason, self.config.model)
        with trace_scope(model_attempt=2 if attempted_primary else 1):
            return self._fallback.complete_chat_result(frozen, temperature=temperature,
                stream=stream, max_tokens=max_tokens if max_tokens is not None else self.config.max_output_tokens,
                timeout_seconds=budget, **kwargs)

    def close(self):
        if self.health is not None:
            self.health.close()
        self._main.close()
        self._fallback.close()


class _PrimaryNotReady(Exception):
    pass


def build_main_client(settings: Settings):
    from harness.llm.client import OpenAICompatClient

    path = settings.llm_fallback_config
    if path is None:
        return OpenAICompatClient(settings)
    try:
        config = FallbackConfig.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except ValidationError:
        raise ValueError("invalid main fallback configuration") from None
    if (settings.llm_base_url.rstrip("/"), settings.llm_model) == (config.base_url, config.model):
        raise ValueError("fallback must differ from main provider")
    alternate = settings.model_copy(update={"llm_base_url": config.base_url,
        "llm_model": config.model, "llm_api_key": config.api_key, "llm_fallback_config": None})
    from harness.llm.health import ProviderHealth
    health = (ProviderHealth(settings, interval=config.health_check_interval_seconds,
              timeout=config.health_check_timeout_seconds, recovery_successes=config.health_recovery_successes)
              if config.health_check_interval_seconds else None)
    return FallbackLLMClient(OpenAICompatClient(settings,
        connect_timeout=config.health_check_timeout_seconds if health else None),
        OpenAICompatClient(alternate, extra_payload={"thinking": {"type": "disabled"}}), config, health=health)


def fallback_status(client) -> dict:
    main = client if isinstance(client, FallbackLLMClient) else getattr(client, "_main", client)
    if isinstance(main, FallbackLLMClient):
        return {"enabled": True, "model": main.config.model,
                "base_url": main.config.base_url,
                "primary_timeout_seconds": main.config.primary_timeout_seconds,
                **({"primary_health": main.health.snapshot()} if main.health is not None else {})}
    return {"enabled": False}
