"""Explicit, content-bearing research evidence, separate from operational logs.

Nothing is persisted or enabled by configuration alone. Callers supply a sink
and are responsible for using authorized data and an isolated evidence folder.
Sink failures propagate: an experiment must not silently lose its evidence.
"""
from __future__ import annotations

import copy
import hashlib
import json
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import UTC, datetime
from typing import Any, Callable, Iterator

from infra.trace.request_spans import current_trace_context, safe_endpoint

DiagnosticObserver = Callable[[dict[str, Any]], None]
_observer: ContextVar[DiagnosticObserver | None] = ContextVar("nc_diagnostic_observer", default=None)
_metadata: ContextVar[dict[str, Any] | None] = ContextVar("nc_diagnostic_metadata", default=None)
_FORBIDDEN = {"authorization", "api_key", "access_token", "refresh_token", "password",
              "reasoning_content", "reasoning_details", "encrypted_content"}


def _check_keys(value: Any) -> None:
    if isinstance(value, dict):
        if any(str(k).lower() in _FORBIDDEN for k in value):
            raise ValueError("diagnostic_sensitive_field_forbidden")
        for item in value.values():
            _check_keys(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _check_keys(item)


def diagnostic_observer(explicit: DiagnosticObserver | None = None) -> DiagnosticObserver | None:
    return explicit if explicit is not None else _observer.get()


@contextmanager
def observation_scope(observer: DiagnosticObserver) -> Iterator[None]:
    token = _observer.set(observer)
    try:
        yield
    finally:
        _observer.reset(token)


@contextmanager
def diagnostic_scope(**metadata: Any) -> Iterator[None]:
    _check_keys(metadata)
    value = {**(_metadata.get() or {}), **copy.deepcopy(metadata)}
    json.dumps(value, allow_nan=False)
    token = _metadata.set(value)
    try:
        yield
    finally:
        _metadata.reset(token)


def emit_diagnostic(event: dict[str, Any], observer: DiagnosticObserver | None = None) -> None:
    sink = diagnostic_observer(observer)
    if sink is None:
        return
    _check_keys(event)
    record = {"schema_version": "expression-http-evidence-v1",
              "recorded_at": datetime.now(UTC).isoformat(),
              "trace_context": current_trace_context(),
              "diagnostic_context": copy.deepcopy(_metadata.get() or {}), **event}
    sink(copy.deepcopy(record))


def request_evidence(request: Any) -> dict[str, Any]:
    """Capture serialized bytes from the actual HTTPX Request; never headers."""
    raw = request.content
    text = raw.decode("utf-8")
    payload = json.loads(text)
    _check_keys(payload)
    return {"method": request.method, "endpoint": safe_endpoint(str(request.url)),
            "request_body_sha256": hashlib.sha256(raw).hexdigest(),
            "request_body_utf8": text, "payload": payload}


def response_evidence(body: Any) -> dict[str, Any]:
    """Whitelist visible content and usage; exclude reasoning and tool arguments."""
    if not isinstance(body, dict):
        return {"response_shape": "invalid", "response_content": None, "usage": None}
    choices = body.get("choices")
    choice = choices[0] if isinstance(choices, list) and choices and isinstance(choices[0], dict) else {}
    message = choice.get("message") if isinstance(choice.get("message"), dict) else {}
    content = message.get("content")
    usage = body.get("usage")
    safe_usage: dict[str, Any] | None = None
    if isinstance(usage, dict):
        safe_usage = {k: v for k, v in usage.items() if k in {
            "prompt_tokens", "completion_tokens", "total_tokens", "prompt_cache_hit_tokens",
            "prompt_cache_miss_tokens"} and type(v) is int and v >= 0}
        for field, keys in {"completion_tokens_details": {"reasoning_tokens"},
                            "prompt_tokens_details": {"cached_tokens"}}.items():
            details = usage.get(field)
            if isinstance(details, dict):
                safe_usage[field] = {k: v for k, v in details.items()
                                     if k in keys and type(v) is int and v >= 0}
    def identity(key: str) -> str | None:
        value = body.get(key)
        return value if isinstance(value, str) and len(value) <= 200 else None
    finish = choice.get("finish_reason")
    if not isinstance(finish, (str, type(None))) or finish not in {
        "stop", "length", "tool_calls", "function_call", "content_filter", None
    }:
        finish = "unknown"
    return {"response_model": identity("model"), "response_id": identity("id"),
            "system_fingerprint": identity("system_fingerprint"),
            "response_content": content if isinstance(content, str) else None,
            "content_type": "text" if isinstance(content, str) else
                "missing" if "content" not in message else "null" if content is None else "invalid",
            "finish_reason": finish,
            "has_refusal": bool(message.get("refusal")),
            "has_tool_calls": bool(message.get("tool_calls") or message.get("function_call")),
            "usage": safe_usage}
