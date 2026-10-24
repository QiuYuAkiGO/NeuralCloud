"""LLM client protocol: OpenAI-compatible HTTP + deterministic mock."""

from __future__ import annotations

import json
import logging
import math
import re
import time
from typing import Any, Literal, Protocol
from urllib.parse import urlsplit
from uuid import uuid4

import httpx
from pydantic import BaseModel, Field

from harness.config import Settings, get_settings
from harness.core.inference import TokenUsage
from infra.trace.request_spans import model_request_span, trace_scope
from infra.trace.diagnostics import (
    DiagnosticObserver, diagnostic_observer, emit_diagnostic, request_evidence, response_evidence,
)

logger = logging.getLogger(__name__)

VIOLATING_CARE_REPLY = "因为我还是担心你啊。昨天只是生气了，说的都是气话。对不起。"
REVISED_IN_CHARACTER_REPLY = "……我说的是不想管。\n\n她把视线挪回书上。\n\n又没说真的会不管。"
GREETING_REPLY = "嗯。"
DEFAULT_REPLY = "……知道了。"


class ChatResult(BaseModel):
    text: str
    usage: TokenUsage = Field(default_factory=TokenUsage)
    finish_reason: str | None = None
    response_id: str | None = None
    content_type: Literal["text", "null", "missing", "array", "object", "invalid"] | None = None
    reasoning_tokens: int | None = Field(default=None, ge=0)
    has_tool_calls: bool = False
    # Never include arguments or private reasoning in operational metadata.
    tool_calls: list[Any] = Field(default_factory=list, exclude=True, repr=False)
    reasoning_content: str | None = Field(default=None, exclude=True, repr=False)
    has_refusal: bool = False
    original_content_characters: int | None = None
    json_repair: dict[str, Any] | None = None

    def response_metadata(self) -> dict[str, Any]:
        """Operational fields only; never completion, refusal or reasoning text."""
        return {
            "finish_reason": self.finish_reason,
            "response_id": self.response_id,
            "content_type": self.content_type,
            "content_characters": self.original_content_characters if self.original_content_characters is not None else len(self.text),
            "reasoning_tokens": self.reasoning_tokens,
            "has_tool_calls": self.has_tool_calls,
            "has_refusal": self.has_refusal,
        }


class LLMClient(Protocol):
    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]: ...

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str: ...

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        thinking: dict[str, str] | None = None,
    ) -> ChatResult: ...


_JSON_RE = re.compile(r"\{.*\}", re.DOTALL)


def parse_json_object(text: str, *, schema=None, settings=None) -> dict[str, Any]:
    settings = settings or get_settings()
    if settings.json_repair_enabled:
        from harness.llm.json_output import decode_model_json
        return decode_model_json(text, schema=schema, settings=settings).value
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?", "", text).removesuffix("```").strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    match = _JSON_RE.search(text)
    if not match:
        raise ValueError("LLM response did not contain a JSON object")
    value = json.loads(match.group(0))
    if not isinstance(value, dict):
        raise ValueError("LLM JSON was not an object")
    return value


def approx_tokens(text: str) -> int:
    """Same CJK-friendly heuristic as the context compiler."""
    if not text:
        return 0
    return max(1, (len(text) + 1) // 2)


def estimate_chat_usage(messages: list[dict[str, Any]], text: str) -> TokenUsage:
    # Multimodal fallback counts only text. Image token costs are provider-specific;
    # never count a data URL as prompt text or treat a content list as a string.
    prompt = 0
    for message in messages:
        content = message.get("content") or ""
        if isinstance(content, str):
            prompt += approx_tokens(content)
        elif isinstance(content, list):
            prompt += sum(
                approx_tokens(block["text"])
                for block in content
                if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str)
            )
    completion = approx_tokens(text)
    return TokenUsage(prompt_tokens=prompt, completion_tokens=completion, source="estimate")


def _nonneg_int(value: Any) -> int:
    return value if type(value) is int and value >= 0 else 0


def _optional_count(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def usage_from_api_payload(raw: Any, messages: list[dict[str, Any]], text: str) -> TokenUsage:
    if isinstance(raw, dict):
        prompt_value = _optional_count(raw.get("prompt_tokens"))
        completion_value = _optional_count(raw.get("completion_tokens"))
        completion = completion_value or 0
        total = _nonneg_int(raw.get("total_tokens"))
        details = raw.get("prompt_tokens_details") if isinstance(raw.get("prompt_tokens_details"), dict) else {}
        hit = _optional_count(raw.get("prompt_cache_hit_tokens"))
        if hit is None:
            hit = _optional_count(details.get("cached_tokens"))
        miss = _optional_count(raw.get("prompt_cache_miss_tokens"))
        if prompt_value is None and hit is not None and miss is not None:
            prompt_value = hit + miss
        if prompt_value is not None:
            if hit is not None and miss is None and hit <= prompt_value:
                miss = prompt_value - hit
            elif miss is not None and hit is None and miss <= prompt_value:
                hit = prompt_value - miss
        known = (prompt_value is not None and hit is not None and miss is not None
                 and hit + miss == prompt_value)
        prompt = prompt_value or 0
        if prompt_value is not None or completion_value is not None or total or hit is not None or miss is not None:
            return TokenUsage(
                prompt_tokens=prompt,
                completion_tokens=completion,
                total_tokens=total,
                cache_hit_tokens=(hit or 0) if known else 0,
                cache_miss_tokens=(miss or 0) if known else 0,
                cache_known_prompt_tokens=prompt if known else 0,
                cache_status="known" if known else "unknown",
                prompt_tokens_source="api" if prompt_value is not None else "unknown",
                completion_tokens_source="api" if completion_value is not None else "unknown",
                source="api",
            )
    return estimate_chat_usage(messages, text)


def _record_provider_response(span: dict[str, Any], body: Any, messages: list[dict[str, Any]]) -> None:
    """Retain returned usage even if response validation/deadline rejects it."""
    if not isinstance(body, dict):
        return
    # These fields also describe rejected responses. Recording them only after
    # validation loses finish_reason and output length precisely when needed.
    try:
        span.update(_chat_result(body, messages).response_metadata())
    except (ValueError, TypeError, KeyError, IndexError, AttributeError):
        pass
    try:
        content = body["choices"][0]["message"]["content"]
        if isinstance(content, str) and content.lstrip().startswith("{"):
            interpretation = json.loads(content).get("attention_assessment")
            if interpretation is not None:
                from harness.homeostasis.dynamics import AttentionAssessment
                span["attention_assessment"] = AttentionAssessment.model_validate(interpretation).model_dump(mode="json")
    except (ValueError, TypeError, KeyError, IndexError, RecursionError):
        pass
    model = body.get("model")
    if isinstance(model, str) and re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", model):
        span["response_model"] = model
    if isinstance(body.get("usage"), dict):
        usage = usage_from_api_payload(body["usage"], messages, "")
        span.update(usage=usage.model_dump(mode="json"), usage_source=usage.source)


def _record_production_response(metering: dict[str, Any], body: Any) -> None:
    """Preserve actual provider counts before response/deadline validation.

    TokenUsage includes derived and default counts and is not raw evidence.
    Numeric nested fields retain their original path; no text is persisted.
    """
    raw = body.get("usage") if isinstance(body, dict) else None
    counts: dict[str, int] = {}

    def visit(value, prefix="", depth=0):
        if not isinstance(value, dict) or depth > 4:
            return
        for key, item in value.items():
            if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", key):
                continue
            path = prefix + key
            if type(item) is int and item >= 0:
                counts[path] = item
            elif isinstance(item, dict):
                visit(item, path + ".", depth + 1)

    visit(raw)
    actual = {"source": "api", "raw_usage": counts} if counts else {"source": "unavailable"}
    metering["usage"] = actual
    checkpoint = metering.get("checkpoint")
    if callable(checkpoint):
        checkpoint(actual)


def _chat_result(body: dict[str, Any], messages: list[dict[str, Any]]) -> ChatResult:
    choice = body["choices"][0]
    message = choice["message"]
    content = message.get("content")
    content_type = ("missing" if "content" not in message else "null" if content is None else
                    "text" if isinstance(content, str) else "array" if isinstance(content, list) else
                    "object" if isinstance(content, dict) else "invalid")
    # Structured/tool-only responses must not become Python repr strings that
    # downstream callers could mistake for generated natural-language text.
    text = content if isinstance(content, str) else ""
    finish = choice.get("finish_reason")
    finish = (finish if finish in {"stop", "length", "tool_calls", "function_call", "content_filter"}
              else None if finish is None else "unknown") if isinstance(finish, (str, type(None))) else "unknown"
    response_id = body.get("id")
    if not isinstance(response_id, str) or not re.fullmatch(r"[A-Za-z0-9_.:/-]{1,200}", response_id):
        response_id = None
    raw_usage = body.get("usage")
    details = raw_usage.get("completion_tokens_details") if isinstance(raw_usage, dict) else None
    reasoning_tokens = details.get("reasoning_tokens") if isinstance(details, dict) else None
    if type(reasoning_tokens) is not int or reasoning_tokens < 0:
        reasoning_tokens = None
    return ChatResult(text=text, usage=usage_from_api_payload(raw_usage, messages, text),
        finish_reason=finish, response_id=response_id, content_type=content_type,
        reasoning_tokens=reasoning_tokens, has_tool_calls=bool(message.get("tool_calls") or message.get("function_call")),
        has_refusal=bool(message.get("refusal")),
        tool_calls=message.get("tool_calls") if isinstance(message.get("tool_calls"), list) else [],
        reasoning_content=message.get("reasoning_content") if isinstance(message.get("reasoning_content"), str) else None)


def _native_tool_payload(payload, tools, tool_choice):
    if tools:
        payload.update(tools=tools, tool_choice=tool_choice, parallel_tool_calls=False)
    else:
        if tool_choice not in ("auto", "none", None):
            raise ValueError("native_tools_required_but_empty")
        # OpenAI-compatible servers may reject an explicitly empty tools array.
        # No-tool calls also override any tools inherited from extra_payload.
        for key in ("tools", "tool_choice", "parallel_tool_calls"):
            payload.pop(key, None)


class OpenAICompatClient:
    production_dispatch_aware = True
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        extra_payload: dict[str, Any] | None = None,
        request_observer: DiagnosticObserver | None = None,
        connect_timeout: float | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.agentic_enabled and self.settings.agentic_local_only:
            from harness.agentic.context import local_endpoint
            if not local_endpoint(self.settings.llm_base_url):
                raise ValueError("agentic_requires_local_main_endpoint")
        self._extra_payload = dict(extra_payload or {})
        self._request_observer = request_observer
        self._connect_timeout = connect_timeout
        # One long-lived client: keep-alive reuse across all pipeline stages
        # instead of a fresh TCP+TLS handshake per call.
        self._client = httpx.Client(timeout=self.settings.llm_timeout)

    def close(self) -> None:
        self._client.close()

    def complete_agent_turn(self, messages, *, tools, tool_choice="auto", **kwargs):
        """Native main-model route, without structured routing or affect injection."""
        from harness.agentic.background import dispatch
        if dispatch.get() is not None:
            return dispatch.get()(self, messages, tools=tools, tool_choice=tool_choice, **kwargs)
        return self.complete_chat_result(messages, _agent_tools=tools,
            _agent_tool_choice=tool_choice, **kwargs)

    async def acomplete_agent_turn(self, messages, *, tools, tool_choice="auto",
                                   max_tokens=512, timeout_seconds=25, temperature=0.1, response_format=None):
        """Cancellation closes HTTP; server slot release requires live measurement."""
        from harness.production.metering import production_attempt, mark_production_dispatch
        payload = {**self._extra_payload, "model": self.settings.llm_model,
            "messages": messages, "max_tokens": max_tokens,
            "temperature": temperature, "chat_template_kwargs": {"enable_thinking": False},
            "stream": True, "stream_options": {"include_usage": True}}
        _native_tool_payload(payload, tools, tool_choice)
        from harness.agentic.deepseek_tokens import supported as deepseek_agent_supported
        if deepseek_agent_supported(self.settings):
            payload.pop("chat_template_kwargs", None)
            payload["thinking"] = {"type": "disabled"}
        if response_format is not None:
            payload['response_format'] = response_format
        with production_attempt(model=self.settings.llm_model) as metering, model_request_span(
                self.settings, role="main", messages=messages) as span:
            async with httpx.AsyncClient(timeout=timeout_seconds) as client:
                mark_production_dispatch(metering)
                from harness.llm.native_stream import NativeStream
                stream = NativeStream()
                # A streaming reader detects disconnects promptly in llama.cpp.
                # Merely cancelling a non-streaming HTTP request can leave its
                # generation occupying a slot until max_tokens is reached.
                async with client.stream("POST", self.settings.llm_base_url.rstrip("/") + "/chat/completions",
                        json=payload, headers={"Authorization": "Bearer " + self.settings.llm_api_key}) as response:
                    span["http_status"] = response.status_code
                    response.raise_for_status()
                    async for line in response.aiter_lines():
                        stream.line(line)
                body = stream.result()
                _record_production_response(metering, body)
                _record_provider_response(span, body, messages)
                return _chat_result(body, messages)

    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        with trace_scope(model_role="aux"):
            content = self.complete_chat_result(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.1,
                max_tokens=max_tokens,
                **({} if response_format is None else {"response_format": response_format}),
                **({} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}),
            ).text
        return parse_json_object(content, settings=self.settings)

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        return self.complete_chat_result(messages, temperature=temperature, stream=stream).text

    def complete_chat_result(
        self,
        messages: list[dict[str, Any]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        thinking: dict[str, str] | None = None,
        _agent_tools: list[dict[str, Any]] | None = None,
        _agent_tool_choice: Any = "auto",
    ) -> ChatResult:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        from harness.production.metering import current_production_context
        from harness.llm import deepseek_structured
        if (deepseek_structured.supported(self.settings) and self.settings.deepseek_cache_friendly_inputs
                and (response_format or {}).get("type") == "json_schema"):
            from harness.llm.structured import cache_messages
            messages = cache_messages(messages)
        production_context = current_production_context()
        if production_context is not None and _agent_tools is None:
            from harness.cognitive.production import augment_messages
            messages = augment_messages(messages, production_context,
                cache_friendly=deepseek_structured.cache_friendly_enabled(self)
                and (response_format or {}).get("type") == "json_schema")
        from harness.llm.deadline import remaining_model_seconds

        remaining = remaining_model_seconds()
        if remaining is not None:
            timeout_seconds = min(remaining, timeout_seconds if timeout_seconds is not None else self.settings.llm_timeout)
        request_options: dict[str, Any] = {}
        if timeout_seconds is not None:
            if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
                raise ValueError("request timeout must be finite and positive")
            request_options["timeout"] = timeout_seconds
        if self._connect_timeout is not None:
            total_timeout = timeout_seconds if timeout_seconds is not None else self.settings.llm_timeout
            request_options["timeout"] = httpx.Timeout(total_timeout, connect=min(total_timeout, self._connect_timeout))
        payload: dict[str, Any] = {
            **self._extra_payload,
            "model": self.settings.llm_model,
            "messages": messages,
            "temperature": temperature,
        }
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens
        if response_format is not None:
            payload["response_format"] = response_format
        if reasoning_effort is not None:
            payload["reasoning_effort"] = reasoning_effort
        if thinking is not None:
            payload["thinking"] = thinking
        if _agent_tools is not None:
            _native_tool_payload(payload, _agent_tools, _agent_tool_choice)
            payload["chat_template_kwargs"] = {**payload.get("chat_template_kwargs", {}),
                "enable_thinking": self.settings.agentic_thinking,
                "reasoning_effort": reasoning_effort or "low", "preserve_thinking": True}
            from harness.agentic.deepseek_tokens import supported as deepseek_agent_supported
            if deepseek_agent_supported(self.settings):
                payload.pop("chat_template_kwargs", None)
                payload["thinking"] = {"type": "enabled" if self.settings.agentic_thinking else "disabled"}
        response_schema = None
        endpoint = f"{self.settings.llm_base_url.rstrip('/')}/chat/completions"
        if (deepseek_structured.supported(self.settings)
                and (response_format or {}).get("type") == "json_schema"):
            payload, response_schema = deepseek_structured.request_payload(payload, self.settings)
            endpoint = "https://api.deepseek.com/responses"
        headers = {"Authorization": f"Bearer {self.settings.llm_api_key}"}
        trace_settings = (self.settings.model_copy(update={"llm_model": payload["model"], "llm_base_url": endpoint})
                          if response_schema is not None else self.settings)
        from harness.production.metering import mark_production_dispatch, production_attempt
        with production_attempt(model=payload["model"]) as metering, model_request_span(trace_settings, role="main", messages=messages) as span:
            if diagnostic_observer(self._request_observer) is not None:
                result = self._complete_observed(payload, headers, request_options, messages, span, metering,
                                                 endpoint=endpoint, response_schema=response_schema)
                return result
            mark_production_dispatch(metering)
            response = self._client.post(
                endpoint,
                json=payload,
                headers=headers,
                **request_options,
            )
            span["http_status"] = response.status_code
            try:
                body = response.json()
            except (ValueError, UnicodeError):
                body = None
            _record_production_response(metering, body)
            if response_schema is not None:
                span["structured_response"] = deepseek_structured.response_shape(body)
            normalized = deepseek_structured.chat_body(body) if response_schema is not None else body
            _record_provider_response(span, normalized, messages)
            remaining_model_seconds()
            response.raise_for_status()
            if not isinstance(body, dict):
                raise ValueError("llm_response_invalid_json_object")
            result = self._validated_json_result(body, normalized, messages, response_schema, response_format, span)
            span.update(usage=result.usage.model_dump(mode="json"), usage_source=result.usage.source,
                        **result.response_metadata())
            return result

    def _validated_json_result(self, body, normalized, messages, response_schema, response_format, span):
        from harness.llm import deepseek_structured
        from harness.llm.json_output import decode_model_json
        result = _chat_result(normalized, messages)
        summary = {}
        if response_schema is not None:
            try:
                text = deepseek_structured.validate_response(body, response_schema, settings=self.settings, summary=summary,
                    metadata={"span_id": span.get("span_id")})
            finally:
                if summary:
                    span["json_repair"] = summary
        elif self.settings.json_repair_enabled and (response_format or {}).get("type") in {"json_object", "json_schema"}:
            if result.finish_reason not in {None, "stop"} or result.has_refusal or result.has_tool_calls or result.content_type != "text":
                raise ValueError("json_response_contract_invalid")
            schema = (response_format or {}).get("json_schema", {}).get("schema")
            try:
                text = decode_model_json(result.text, schema=schema, settings=self.settings,
                    metadata={"response_id": result.response_id, "span_id": span.get("span_id")}, summary=summary).text
            finally:
                span["json_repair"] = summary
        else:
            return result
        return result.model_copy(update={"text": text, "original_content_characters": len(result.text),
                                         "json_repair": summary or None})

    def _complete_observed(self, payload, headers, request_options, messages, span, metering=None,
                           *, endpoint=None, response_schema=None) -> ChatResult:
        """Opt-in evidence at the serialization boundary, with no production log content."""
        from harness.llm.deadline import remaining_model_seconds
        from harness.llm import deepseek_structured

        observer = diagnostic_observer(self._request_observer)
        # Hooks/auth flows can change the request after validation, including
        # after transmission. The opt-in evidence path requires a plain client.
        if any(self._client.event_hooks.values()) or self._client.auth is not None:
            raise ValueError("diagnostic_http_hooks_or_auth_unsupported")
        request = self._client.build_request("POST",
            endpoint or f"{self.settings.llm_base_url.rstrip('/')}/chat/completions",
            json=payload, headers=headers, **request_options)
        request_id = f"diagnostic-request-{uuid4().hex}"
        before = request_evidence(request)
        emit_diagnostic({"record_type": "http_request_prepared", "request_id": request_id,
                         "payload_verified": False, **before}, observer)
        started = time.perf_counter()
        response = None
        try:
            from harness.production.metering import mark_production_dispatch
            mark_production_dispatch(metering if metering is not None else {})
            response = self._client.send(request)
            # Hooks/auth are excluded above, so the returned request still
            # describes the body passed to the transport.
            actual = request_evidence(response.request)
            span["http_status"] = response.status_code
            try:
                body = response.json()
            except (ValueError, UnicodeError):
                body = None
            _record_production_response(metering if metering is not None else {}, body)
            if response_schema is not None:
                span["structured_response"] = deepseek_structured.response_shape(body)
            normalized = deepseek_structured.chat_body(body) if response_schema is not None else body
            _record_provider_response(span, normalized, messages)
            emit_diagnostic({"record_type": "http_response_received", "request_id": request_id,
                "http_status": response.status_code, "payload_verified": True, **actual,
                "request_changed_after_prepare": actual["request_body_sha256"] != before["request_body_sha256"],
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3),
                **response_evidence(normalized)}, observer)
            remaining_model_seconds()
            response.raise_for_status()
            if not isinstance(body, dict):
                raise ValueError("llm_response_invalid_json_object")
            result = self._validated_json_result(body, normalized, messages, response_schema, payload.get("response_format"), span)
            span.update(usage=result.usage.model_dump(mode="json"), usage_source=result.usage.source,
                        **result.response_metadata())
            return result
        except BaseException as exc:
            emit_diagnostic({"record_type": "http_request_error", "request_id": request_id,
                "error_type": type(exc).__name__, "http_status": span.get("http_status"),
                "transmission_status": "response_received" if response is not None else "unknown",
                "elapsed_ms": round((time.perf_counter() - started) * 1000, 3)}, observer)
            raise
        finally:
            if response is not None:
                response.close()


def _filled(value: str | None) -> bool:
    return bool((value or "").strip())


def deepseek_low_reasoning_options(settings: Settings) -> dict[str, str]:
    """Opt bounded decisions into low thinking on the supported official API."""
    if settings.llm_model not in {"deepseek-v4-flash", "deepseek-v4-pro"}:
        return {}
    raw = settings.llm_base_url
    try:
        endpoint = urlsplit(raw)
        supported = (raw == raw.strip() and endpoint.scheme == "https"
            and endpoint.hostname == "api.deepseek.com" and endpoint.port in {None, 443}
            and endpoint.path in {"", "/", "/v1", "/v1/"}
            and endpoint.username is None and endpoint.password is None
            and not endpoint.query and not endpoint.fragment)
    except ValueError:
        return {}
    return {"reasoning_effort": "low"} if supported else {}


def resolve_aux_endpoint(settings: Settings) -> tuple[str, str, str]:
    """Resolve (base_url, model, api_key) for helper JSON calls, falling back to the character model."""
    base_url = (settings.aux_llm_base_url or "").strip() or settings.llm_base_url
    model = (settings.aux_llm_model or "").strip() or settings.llm_model
    api_key = (settings.aux_llm_api_key or "").strip() or settings.llm_api_key
    return base_url, model, api_key


def aux_explicitly_configured(settings: Settings) -> bool:
    return any(
        _filled(value)
        for value in (settings.aux_llm_base_url, settings.aux_llm_model, settings.aux_llm_api_key)
    )


def aux_endpoint_distinct(settings: Settings) -> bool:
    return resolve_aux_endpoint(settings) != (
        settings.llm_base_url,
        settings.llm_model,
        settings.llm_api_key,
    )


def aux_model_distinct(settings: Settings) -> bool:
    aux_url, aux_model, _ = resolve_aux_endpoint(settings)
    return (aux_url.rstrip("/"), aux_model) != (settings.llm_base_url.rstrip("/"), settings.llm_model)


def resolve_qq_memory_endpoint(settings: Settings) -> tuple[str, str, str]:
    """Resolve (base_url, model, api_key) for QQ memory proposals.

    Explicit qq_memory_llm_* keeps cleaning on that authorized backend when the
    character-model endpoint changes. Unset fields fall back to llm_*.
    """
    base_url = (settings.qq_memory_llm_base_url or "").strip() or settings.llm_base_url
    model = (settings.qq_memory_llm_model or "").strip() or settings.llm_model
    api_key = (settings.qq_memory_llm_api_key or "").strip() or settings.llm_api_key
    return base_url, model, api_key


def qq_memory_proposal_settings(settings: Settings) -> Settings:
    """Settings for QQ memory proposals: dedicated endpoint, then character model."""
    base_url, model, api_key = resolve_qq_memory_endpoint(settings)
    return settings.model_copy(update={
        "llm_base_url": base_url, "llm_model": model, "llm_api_key": api_key,
        "llm_timeout": settings.qq_memory_model_timeout_seconds,
        "llm_json_max_tokens": 8192, "aux_llm_preload": False,
        "record_interaction": False, "call_log_enabled": False,
    })


def keep_alive_value(raw: str | None) -> str | int:
    text = (raw or "").strip()
    if not text:
        return -1
    if text.lstrip("-").isdigit():
        return int(text)
    return text


def aux_runtime_payload(settings: Settings) -> dict[str, Any]:
    """ollama extras so the helper stays on GPU with a bounded KV cache."""
    return {
        "keep_alive": keep_alive_value(settings.aux_llm_keep_alive),
        "options": {"num_ctx": settings.aux_llm_num_ctx, "num_gpu": 99},
    }


def ollama_native_root(base_url: str) -> str | None:
    stripped = base_url.rstrip("/")
    if stripped.endswith("/v1"):
        stripped = stripped[: -len("/v1")].rstrip("/")
    if stripped.endswith(":11434"):
        return stripped
    return None


class RoutingLLMClient:
    """Send complete_json to the aux client and complete_chat* to the character-model client."""

    def __init__(self, main: LLMClient, aux: LLMClient, structured: LLMClient | None = None) -> None:
        self._main = main
        self._aux = aux
        self._structured = structured

    @property
    def production_dispatch_aware(self) -> bool:
        return getattr(self._main, "production_dispatch_aware", False) is True

    def complete_agent_turn(self, messages, **kwargs):
        return self._main.complete_agent_turn(messages, **kwargs)

    async def acomplete_agent_turn(self, messages, **kwargs):
        return await self._main.acomplete_agent_turn(messages, **kwargs)

    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if self._structured is not None:
            format_ = response_format or {"type": "json_schema", "json_schema": {
                "name": "legacy_helper", "schema": {"type": "object"}}}
            return parse_json_object(self.complete_chat_result(
                [{"role": "system", "content": system}, {"role": "user", "content": user}],
                temperature=0, max_tokens=max_tokens,
                timeout_seconds=timeout_seconds, response_format=format_).text)
        with trace_scope(model_role="aux"):
            options = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
            if response_format is not None:
                options["response_format"] = response_format
            return self._aux.complete_json(system=system, user=user, max_tokens=max_tokens, **options)

    def complete_validation_json(
        self, *, system: str, user: str, timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Keep complete critic evidence within the auxiliary context window.

        Count the verified Qwen template locally when its tokenizer is available;
        otherwise use UTF-8 bytes as a conservative input bound. Keep template
        and output reserves in either case. This policy is specific to validation.
        """
        if self._structured is not None:
            from harness.core.inference import ValidationResult
            from harness.llm.structured import helper_options
            return self.complete_json(system=system, user=user, max_tokens=1024,
                timeout_seconds=timeout_seconds, **helper_options(self, ValidationResult, "response_validation"))
        output_tokens = 1024
        template_tokens = 256
        aux_settings = getattr(self._aux, "settings", None)
        aux_context = getattr(aux_settings, "aux_llm_num_ctx", None)
        options = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
        if not self.aux_distinct or not isinstance(aux_context, int) or aux_context <= 0:
            return self.complete_json(system=system, user=user, **options)
        from harness.llm.validation_tokens import count_validation_prompt_tokens

        counting_started = time.monotonic()
        exact_tokens = count_validation_prompt_tokens(
            model=getattr(aux_settings, "llm_model", None), system=system, user=user,
        )
        input_bound = (exact_tokens if exact_tokens is not None else
                       len(system.encode("utf-8")) + len(user.encode("utf-8"))) + template_tokens
        if timeout_seconds is not None and math.isfinite(timeout_seconds) and timeout_seconds > 0:
            remaining = timeout_seconds - (time.monotonic() - counting_started)
            if remaining <= 0:
                raise TimeoutError("validator input counting exceeded model deadline")
            options["timeout_seconds"] = remaining
        if input_bound + output_tokens > aux_context:
            main_settings = getattr(self._main, "settings", None)
            reasoning = deepseek_low_reasoning_options(main_settings) if isinstance(main_settings, Settings) else {}
            logger.info("validator uses main model: input token upper bound %d + output reserve %d exceeds aux context %d",
                        input_bound, output_tokens, aux_context)
            with trace_scope(model_role="main"):
                response = self._main.complete_chat_result(
                    [{"role": "system", "content": system}, {"role": "user", "content": user}],
                    temperature=0.1, stream=False, max_tokens=4096, **reasoning, **options,
                )
            return parse_json_object(response.text)
        return self.complete_json(system=system, user=user, max_tokens=output_tokens, **options)

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        with trace_scope(model_role="main"):
            return self._main.complete_chat(messages, temperature=temperature, stream=stream)

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        thinking: dict[str, str] | None = None,
    ) -> ChatResult:
        if self._structured is not None and (response_format or {}).get("type") in {"json_schema", "json_object"}:
            if response_format["type"] == "json_object":
                response_format = {"type": "json_schema", "json_schema": {
                    "name": "legacy_object", "schema": {"type": "object"}}}
            with trace_scope(model_role="structured"):
                return self._structured.complete_chat_result(
                    messages,
                    temperature=0, stream=stream, max_tokens=max_tokens, timeout_seconds=timeout_seconds,
                    response_format=response_format)
        with trace_scope(model_role="main"):
            options = {} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}
            if response_format is not None:
                options["response_format"] = response_format
            if reasoning_effort is not None:
                options["reasoning_effort"] = reasoning_effort
            if thinking is not None:
                options["thinking"] = thinking
            return self._main.complete_chat_result(
                messages,
                temperature=temperature,
                stream=stream,
                max_tokens=max_tokens,
                **options,
            )

    @property
    def aux_distinct(self) -> bool:
        return self._aux is not self._main

    def complete_aux_text_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.1,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
    ) -> ChatResult:
        """Run a free-form text completion on the distinct helper endpoint."""

        if not self.aux_distinct:
            raise RuntimeError("a distinct auxiliary model is not configured")
        with trace_scope(model_role="aux"):
            return self._aux.complete_chat_result(
                messages,
                temperature=temperature,
                stream=False,
                max_tokens=max_tokens,
                **({} if timeout_seconds is None else {"timeout_seconds": timeout_seconds}),
            )

    def close(self) -> None:
        if self._structured is not None:
            self._structured.close()
        if self._aux is not self._main:
            closer = getattr(self._aux, "close", None)
            if callable(closer):
                closer()
        closer = getattr(self._main, "close", None)
        if callable(closer):
            closer()


def build_llm_client(settings: Settings) -> LLMClient:
    """Live-mode client: route helpers to aux when configured, otherwise a single endpoint."""
    if settings.agentic_enabled and settings.agentic_local_only:
        # No cloud fallback, structured helper, or auxiliary probe is part of
        # the local agent deployment, including legacy owner/resume calls.
        return OpenAICompatClient(settings)
    from harness.llm.fallback import build_main_client
    from harness.llm.structured import build_structured_client
    main = build_main_client(settings)
    structured = build_structured_client(settings)
    if not (aux_explicitly_configured(settings) or aux_endpoint_distinct(settings)):
        return RoutingLLMClient(main, main, structured) if structured is not None else main
    if not aux_endpoint_distinct(settings):
        return RoutingLLMClient(main, main, structured)
    base_url, model, api_key = resolve_aux_endpoint(settings)
    aux_settings = settings.model_copy(
        update={"llm_base_url": base_url, "llm_model": model, "llm_api_key": api_key}
    )
    return RoutingLLMClient(
        main, OpenAICompatClient(aux_settings, extra_payload=aux_runtime_payload(settings)), structured
    )


def probe_endpoint(base_url: str, api_key: str, *, timeout: float = 0.8) -> bool:
    url = f"{base_url.rstrip('/')}/models"
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(url, headers=headers)
            return 200 <= response.status_code < 400
    except httpx.HTTPError:
        return False


def _model_ids_from_openai(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, list):
        return []
    names: list[str] = []
    for item in data:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        mid = item.get("id") or item.get("name")
        if isinstance(mid, str) and mid.strip():
            names.append(mid.strip())
    return names


def _model_ids_from_ollama_tags(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    models = payload.get("models")
    if not isinstance(models, list):
        return []
    names: list[str] = []
    for item in models:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        mid = item.get("name") or item.get("model")
        if isinstance(mid, str) and mid.strip():
            names.append(mid.strip())
    return names


def list_endpoint_models(base_url: str, api_key: str = "not-needed", *, timeout: float = 2.0) -> list[str]:
    """Return installed model ids from OpenAI-compat `/models` or Ollama `/api/tags`."""
    url = (base_url or "").strip()
    if not url:
        return []
    headers = {"Authorization": f"Bearer {api_key or 'not-needed'}"}
    names: list[str] = []
    try:
        with httpx.Client(timeout=timeout) as client:
            response = client.get(f"{url.rstrip('/')}/models", headers=headers)
            if 200 <= response.status_code < 400:
                try:
                    names.extend(_model_ids_from_openai(response.json()))
                except ValueError:
                    names = []
            if not names:
                native = ollama_native_root(url)
                if native:
                    tags = client.get(f"{native}/api/tags")
                    if 200 <= tags.status_code < 400:
                        try:
                            names.extend(_model_ids_from_ollama_tags(tags.json()))
                        except ValueError:
                            names = []
    except httpx.HTTPError:
        return []
    return list(dict.fromkeys(names))


def preload_aux_model(settings: Settings, *, timeout: float = 120.0) -> bool:
    """Issue a 1-token chat call so ollama/llama.cpp load the aux model onto GPU."""
    from harness.production.maintenance import ProductionMaintenanceDeferred
    from harness.production.metering import production_attempt

    base_url, model, api_key = resolve_aux_endpoint(settings)
    extras = aux_runtime_payload(settings)
    headers = {"Authorization": f"Bearer {api_key}"}
    native_root = ollama_native_root(base_url)
    try:
        with httpx.Client(timeout=timeout) as client, production_attempt(model=model) as metering:
            if native_root:
                response = client.post(
                    f"{native_root}/api/chat",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "stream": False,
                        "keep_alive": extras["keep_alive"],
                        "options": {**extras["options"], "num_predict": 1},
                    },
                )
            else:
                response = client.post(
                    f"{base_url.rstrip('/')}/chat/completions",
                    json={
                        "model": model,
                        "messages": [{"role": "user", "content": "ping"}],
                        "max_tokens": 1,
                        **extras,
                    },
                    headers=headers,
                )
            try:
                body = response.json()
            except (ValueError, UnicodeError):
                body = None
            if native_root and isinstance(body, dict):
                # Native Ollama exposes measured counts at the root. Preserve
                # those names; never invent OpenAI fields or missing totals.
                body = {"usage": {key: body[key] for key in ("prompt_eval_count", "eval_count") if key in body}}
            _record_production_response(metering, body)
            response.raise_for_status()
        return True
    except ProductionMaintenanceDeferred:
        raise
    except (httpx.HTTPError, OSError, TimeoutError, ValueError) as exc:
        logger.warning("aux model preload failed for %s @ %s: %s", model, base_url, exc)
        return False


class MockLLM:
    """Keyword-routed deterministic JSON / chat. Tests never need a GPU."""

    def __init__(
        self,
        routes: dict[str, dict[str, Any]] | None = None,
        chat_routes: dict[str, str] | None = None,
        chat_script: list[str] | None = None,
    ) -> None:
        self.routes = routes or {}
        self.chat_routes = chat_routes or {}
        self.chat_script = list(chat_script or [])
        self._script_i = 0
        self.calls: list[tuple[str, str]] = []
        self.chat_calls: list[list[dict[str, str]]] = []
        self.chat_call_options: list[dict[str, Any]] = []

    def complete_json(
        self, *, system: str, user: str, max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.calls.append((system, user))
        blob = system + "\n" + user
        for key, value in self.routes.items():
            if key in blob:
                return dict(value)
        if "retrieval_plan" in blob or "time_scope" in blob:
            return {
                "time_scope": None,
                "entities": ["user", "character"],
                "event_type": None,
                "emotional_relevance": None,
                "channels": [],
            }
        if "persona_conditioned_insight" in blob:
            return {"persona_conditioned_insight": None, "insight_confidence": 0.0}
        if '"ops"' in blob or "curator" in blob.lower():
            return {"ops": [{"op": "NO_OP", "reason": "mock_default"}]}
        return {}

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        return self.complete_chat_result(messages, temperature=temperature, stream=stream).text

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
        reasoning_effort: str | None = None,
        thinking: dict[str, str] | None = None,
    ) -> ChatResult:
        if stream:
            raise NotImplementedError("streaming is reserved for a later milestone")
        self.chat_calls.append(messages)
        self.chat_call_options.append({"temperature": temperature, "stream": stream,
            "max_tokens": max_tokens, "timeout_seconds": timeout_seconds, "response_format": response_format,
            **({"reasoning_effort": reasoning_effort} if reasoning_effort is not None else {}),
            **({"thinking": thinking} if thinking is not None else {})})
        blob = "\n".join(message.get("content", "") for message in messages)
        for key, value in self.chat_routes.items():
            if key in blob:
                text = value
                break
        else:
            if any(
                token in blob
                for token in (
                    "Revision instruction",
                    "嘴硬",
                    "不要道歉",
                    "迂回",
                    "premature_apology",
                    "direct_care",
                )
            ):
                text = REVISED_IN_CHARACTER_REPLY
            elif self.chat_script:
                idx = min(self._script_i, len(self.chat_script) - 1)
                self._script_i += 1
                text = self.chat_script[idx]
            elif "不会再管" in blob or "昨天不是说" in blob:
                text = VIOLATING_CARE_REPLY
            elif any(token in blob for token in ("早上好", "晚上好", "你在干嘛", "一起吃饭")):
                text = GREETING_REPLY
            else:
                text = DEFAULT_REPLY
        return ChatResult(text=text, usage=estimate_chat_usage(messages, text), finish_reason="stop", content_type="text")
