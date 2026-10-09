"""DeepSeek Responses transport mapping for schema-bound decisions.

The existing client owns dispatch, deadlines, accounting and HTTP lifetime.
This module only maps wire formats and validates complete, unmodified output.
"""
from __future__ import annotations

from copy import deepcopy
from itertools import islice
import json
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator

from harness.config import Settings


class StructuredResponseError(ValueError):
    """Content-free failure codes; never reflect provider messages or reasoning."""

    def __init__(self, code: str, *, diagnostics=()):
        super().__init__(code)
        self.error_code = code
        self.diagnostics = list(diagnostics)


def supported(settings: Settings) -> bool:
    if not settings.deepseek_structured_outputs or settings.llm_model not in {"deepseek-flash", "deepseek-v4-flash"}:
        return False
    raw = settings.llm_base_url
    try:
        url = urlsplit(raw)
        return (raw == raw.strip() and url.scheme == "https" and url.hostname == "api.deepseek.com"
                and url.port in {None, 443} and url.path in {"", "/", "/v1", "/v1/"}
                and url.username is None and url.password is None and not url.query and not url.fragment)
    except ValueError:
        return False


def main_settings(client):
    """Resolve the explicitly configured decision provider, then the main route."""
    from harness.llm.structured import structured_target
    structured = structured_target(client)
    if structured is not None:
        return structured.settings
    seen = set()
    while client is not None and id(client) not in seen:
        seen.add(id(client))
        settings = getattr(client, "settings", None)
        if isinstance(settings, Settings):
            return settings
        client = getattr(client, "_main", None) or getattr(client, "client", None)
    return None


def cache_friendly_enabled(client) -> bool:
    settings = main_settings(client)
    return settings is not None and supported(settings) and settings.deepseek_cache_friendly_inputs


def decision_format(client, schema: dict[str, Any], name: str) -> dict[str, Any]:
    """Keep legacy JSON mode on other backends; resolve existing main-client wrappers."""
    settings = main_settings(client)
    if settings is not None and supported(settings):
        return {"type": "json_schema", "json_schema": {"name": name, "schema": deepcopy(schema)}}
    return {"type": "json_object"}


def request_payload(payload: dict[str, Any], settings: Settings) -> tuple[dict[str, Any], dict[str, Any]]:
    definition = payload["response_format"]["json_schema"]
    # Enforced QQ semantics deliberately orders understanding before action.
    # Alphabetical canonicalization silently reverses that application choice.
    semantic_gate = (definition["name"] == "qq_gate" and "understanding" in
                     definition["schema"].get("$defs", {}).get("QQReplyPlan", {}).get("properties", {}))
    schema = json.loads(json.dumps(definition["schema"], ensure_ascii=False,
                                   sort_keys=not semantic_gate, allow_nan=False))
    Draft202012Validator.check_schema(schema)
    messages = deepcopy(payload["messages"])
    for message in messages:
        role, content = message.get("role"), message.get("content")
        if role not in {"system", "developer", "user", "assistant"}:
            raise StructuredResponseError("deepseek_structured_text_messages_required")
        if isinstance(content, str):
            continue
        if role != "user" or not isinstance(content, list) or not content:
            raise StructuredResponseError("deepseek_structured_text_messages_required")
        converted = []
        for part in content:
            if not isinstance(part, dict):
                raise StructuredResponseError("deepseek_structured_text_messages_required")
            if part.get("type") == "text" and set(part) == {"type", "text"} and isinstance(part["text"], str):
                converted.append({"type": "input_text", "text": part["text"]})
            elif part.get("type") == "image_url" and set(part) == {"type", "image_url"}:
                media = part["image_url"]
                if (not isinstance(media, dict) or not isinstance(media.get("url"), str)
                        or set(media) - {"url", "detail"}):
                    raise StructuredResponseError("deepseek_structured_invalid_image")
                converted.append({"type": "input_image", "image_url": media["url"],
                                  **({"detail": media["detail"]} if "detail" in media else {})})
            else:
                raise StructuredResponseError("deepseek_structured_text_messages_required")
        message["content"] = converted
    return {"model": "deepseek-flash", "input": messages, "stream": False,
            "reasoning": {"effort": "none"}, "temperature": 0,
            "max_output_tokens": payload.get("max_tokens", settings.deepseek_structured_max_output_tokens),
            "text": {"format": {"type": "json_schema", "name": definition["name"], "schema": schema}}}, schema


def chat_body(body):
    """Normalize usage and visible output, keeping raw Responses usage for metering."""
    if not isinstance(body, dict):
        return body
    raw_usage = body.get("usage") or {}
    usage = {"prompt_tokens": raw_usage.get("input_tokens"), "completion_tokens": raw_usage.get("output_tokens"),
             "total_tokens": raw_usage.get("total_tokens"),
             "prompt_tokens_details": raw_usage.get("input_tokens_details"),
             "completion_tokens_details": raw_usage.get("output_tokens_details")}
    parts = []
    refused = False
    tools = False
    output = body.get("output") if isinstance(body.get("output"), list) else []
    for item in output:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text" and isinstance(part.get("text"), str):
                    parts.append(part["text"])
                refused = refused or part.get("type") == "refusal"
        elif item.get("type") != "reasoning":
            tools = True
    reason = "stop" if body.get("status") == "completed" else "length" if body.get("status") == "incomplete" else "unknown"
    return {"id": body.get("id"), "model": body.get("model"), "usage": usage,
            "choices": [{"finish_reason": reason, "message": {"content": "".join(parts),
                         "refusal": refused, "tool_calls": [{}] if tools else []}}]}


def _unique(pairs, names=frozenset()):
    result = {}
    for key, value in pairs:
        if key in result:
            raise StructuredResponseError("deepseek_structured_duplicate_key", diagnostics=[{
                "validator": "unique_keys", "field": key if key in names else "*"}])
        result[key] = value
    return result


def _constant(_value):
    raise StructuredResponseError("deepseek_structured_nonfinite_number")


def response_shape(body):
    """Bounded operational metadata; never include output, errors or reasoning text."""
    if not isinstance(body, dict):
        return {"status": "invalid", "output_items": 0, "message_items": 0, "text_parts": 0}
    status = body.get("status")
    status = status if isinstance(status, str) and status in {
        "in_progress", "completed", "incomplete", "failed"} else "unknown"
    output = body.get("output")
    output = output if isinstance(output, list) else []
    messages = [item for item in output if isinstance(item, dict) and item.get("type") == "message"]
    parts = [part for item in messages if isinstance(item.get("content"), list) for part in item["content"]]
    return {"status": status, "output_items": len(output), "message_items": len(messages),
            "text_parts": sum(isinstance(part, dict) and part.get("type") == "output_text"
                              and isinstance(part.get("text"), str) for part in parts)}


def _json_diagnostic(text, exc):
    # JSONDecodeError.doc/str(exc) and neighborhoods can contain private text.
    # Translate the parser's fixed messages to our own finite vocabulary only.
    reasons = {
        "Expecting value": "expected_value",
        "Expecting property name enclosed in double quotes": "expected_property_name",
        "Expecting ':' delimiter": "expected_colon",
        "Expecting ',' delimiter": "expected_comma",
        "Unterminated string starting at": "unterminated_string",
        "Invalid \\escape": "invalid_escape",
        "Invalid \\uXXXX escape": "invalid_unicode_escape",
        "Invalid control character at": "invalid_control_character",
        "Extra data": "extra_data",
        "Unexpected UTF-8 BOM (decode using utf-8-sig)": "unexpected_bom",
    }
    result = {"validator": "json_decode", "content_characters": len(text),
              "empty": not text.strip(), "leading_code_fence": text.lstrip().startswith("```")}
    if isinstance(exc, json.JSONDecodeError):
        result.update(reason=reasons.get(exc.msg, "syntax_error"), offset=exc.pos,
                      line=exc.lineno, column=exc.colno)
    else:
        result["reason"] = ("nesting_limit" if isinstance(exc, RecursionError) else
                            "invalid_type" if isinstance(exc, TypeError) else "invalid_value")
    return result


def validate_response(body, schema, *, settings=None, summary=None, metadata=None):
    if not isinstance(body, dict) or body.get("status") != "completed" or body.get("error"):
        code = (body.get("error") or {}).get("code") if isinstance(body, dict) else None
        safe = code if code in {"rate_limit_exceeded", "insufficient_quota", "credit_balance_exhausted"} else "incomplete_or_failed"
        raise StructuredResponseError("deepseek_structured_" + safe)
    normalized = chat_body(body)
    message = normalized["choices"][0]["message"]
    if message["refusal"] or message["tool_calls"]:
        raise StructuredResponseError("deepseek_structured_nontext_response")
    if settings is not None and settings.json_repair_enabled:
        from harness.llm.json_output import JSONOutputError, decode_model_json
        try:
            return decode_model_json(message["content"], schema=schema, settings=settings,
                metadata={**(metadata or {}), "response_id": body.get("id")}, summary=summary).text
        except JSONOutputError as exc:
            code = ("deepseek_structured_schema_invalid" if "schema" in exc.error_code or "missing_value" in exc.error_code
                    else "deepseek_structured_nonfinite_number" if "nonfinite" in exc.error_code
                    else "deepseek_structured_json_invalid")
            raise StructuredResponseError(code, diagnostics=exc.diagnostics) from None
    # Only host-defined schema property names are safe to include in diagnostics.
    names = set()

    def collect(node):
        if isinstance(node, dict):
            names.update(node.get("properties", {}))
            for child in node.values():
                collect(child)
        elif isinstance(node, list):
            for child in node:
                collect(child)

    collect(schema)
    try:
        value = json.loads(message["content"], object_pairs_hook=lambda pairs: _unique(pairs, names),
                           parse_constant=_constant)
    except StructuredResponseError:
        raise
    except (ValueError, TypeError, RecursionError) as exc:
        raise StructuredResponseError("deepseek_structured_json_invalid",
            diagnostics=[_json_diagnostic(message["content"], exc)]) from None
    if not isinstance(value, dict):
        raise StructuredResponseError("deepseek_structured_schema_invalid")
    errors = list(islice(Draft202012Validator(schema).iter_errors(value), 8))
    if errors:
        # Only names defined by the host's schema may enter diagnostics. Never
        # include jsonschema's message, instance, enum values or unknown keys.
        diagnostics = [{"validator": error.validator,
            "path": [part if isinstance(part, int) or part in names else "*"
                     for part in list(error.absolute_path)[:16]],
            "missing_fields": [name for name in error.validator_value if name in names and name not in error.instance][:8]
                if error.validator == "required" and isinstance(error.instance, dict) else []}
            for error in errors]
        raise StructuredResponseError("deepseek_structured_schema_invalid", diagnostics=diagnostics)
    return message["content"]
