"""Explicit structured routing and lossless stable-prefix serialization."""
from __future__ import annotations

import ast
from copy import deepcopy
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.core.inference import Hypothesis, IntentHypothesis, Reference


class StructuredEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    model: Literal["deepseek-flash"] = "deepseek-flash"
    api_key_file: Path
    cache_friendly_inputs: bool = True


def build_structured_client(settings):
    if getattr(settings, "agentic_enabled", False) and getattr(settings, "agentic_local_only", True):
        return None
    if getattr(settings, "structured_llm_config", None) is None:
        return None
    from harness.llm.client import OpenAICompatClient
    try:
        config = StructuredEndpoint.model_validate_json(settings.structured_llm_config.read_bytes())
        if not config.api_key_file.is_absolute() or not 0 < config.api_key_file.stat().st_size <= 4096:
            raise ValueError
        key = config.api_key_file.read_text(encoding="utf-8-sig").strip()
        if not key or any(c.isspace() for c in key):
            raise ValueError
    except (OSError, UnicodeError, ValueError):
        raise ValueError("structured_endpoint_configuration_invalid") from None
    dedicated = settings.model_copy(update={"llm_base_url": "https://api.deepseek.com",
        "llm_model": config.model, "llm_api_key": key, "structured_llm_config": None,
        "llm_fallback_config": None, "deepseek_structured_outputs": True,
        "deepseek_cache_friendly_inputs": config.cache_friendly_inputs, "aux_llm_preload": False})
    return OpenAICompatClient(dedicated, extra_payload={"thinking": {"type": "disabled"}})


def structured_target(client):
    seen = set()
    while client is not None and id(client) not in seen:
        seen.add(id(client))
        target = getattr(client, "_structured", None)
        if target is not None:
            return target
        client = getattr(client, "client", None) or getattr(client, "_main", None)
    return None


def helper_options(client, model, name):
    # Existing unconfigured providers and injected test clients keep their API.
    if structured_target(client) is None:
        return {}
    schema = model.model_json_schema() if hasattr(model, "model_json_schema") else deepcopy(model)
    return {"response_format": {"type": "json_schema", "json_schema": {"name": name, "schema": schema}}}


def structured_status(client):
    target = structured_target(client)
    if target is None:
        return {"enabled": False}
    return {"enabled": True, "model": target.settings.llm_model,
            "base_url": target.settings.llm_base_url, "format": "responses_json_schema",
            "reasoning_effort": "none", "cache_friendly_inputs": target.settings.deepseek_cache_friendly_inputs}


_FIXED = ("schema", "decision_schema", "choice_schema", "persona", "persona_name", "general_traits",
          "available_offer_definitions", "instruction", "rules", "conversation_kind", "self_id")
_CONTEXT = ("old_impressions", "memories", "memory", "derived_memory", "recent_conversation",
            "history", "messages", "frozen_sources")
_LIVE = ("production", "homeostatic_context", "homeostasis", "shared_affect", "emotion_guidance",
         "emotion_guidance_text", "weights", "strengths", "intensity", "dialogue_trends",
         "new_source_ids", "omitted_window_messages", "current_source", "current_source_id",
         "current_event", "event", "query", "utterance", "text", "rule_score", "signals", "now", "timestamp")


def cache_user(value: str) -> str:
    """Reorder only top-level object keys; preserve nested data and array order."""
    def unique(pairs):
        result = {}
        for key, item in pairs:
            if key in result:
                raise ValueError("duplicate key")
            result[key] = item
        return result
    try:
        decoded = json.loads(value, object_pairs_hook=unique)
    except json.JSONDecodeError:
        # Legacy helpers serialize Python dictionaries rather than JSON.
        if not value.lstrip().startswith("{") or len(value) > 131072:
            return value
        try:
            tree = ast.parse(value, mode="eval")
            for node in ast.walk(tree):
                if isinstance(node, ast.Dict):
                    keys = [ast.literal_eval(key) for key in node.keys]
                    if any(not isinstance(k, str) for k in keys) or len(keys) != len(set(keys)):
                        return value
            decoded = ast.literal_eval(tree)
        except (ValueError, SyntaxError, TypeError, RecursionError, MemoryError):
            return value
    except (ValueError, TypeError, RecursionError, MemoryError):
        return value
    if not isinstance(decoded, dict) or any(not isinstance(k, str) for k in decoded):
        return value
    keys = [k for k in _FIXED if k in decoded]
    keys += [k for k in decoded if k not in _FIXED and k not in _CONTEXT and k not in _LIVE]
    keys += [k for k in _CONTEXT if k in decoded]
    keys += [k for k in _LIVE if k in decoded]
    try:
        encoded = json.dumps({k: decoded[k] for k in keys}, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        return encoded if json.loads(encoded) == decoded else value
    except (ValueError, TypeError, RecursionError, MemoryError):
        return value


def cache_messages(messages):
    result = deepcopy(messages)
    for message in result:
        if message.get("role") == "user" and isinstance(message.get("content"), str):
            message["content"] = cache_user(message["content"])
    return result


class GateOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: Literal["fast", "deep"]
    reason: str = ""


class ParserOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    speech_act: Literal["question", "statement", "greeting", "request"]
    references: list[Reference] = Field(default_factory=list)
    possible_social_intent: Hypothesis | None = None
    emotion_cues: dict[str, float] = Field(default_factory=dict)
    entities: list[str] = Field(default_factory=list)
    gate: GateOutput


class InterpreterOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    possible_intents: list[IntentHypothesis] = Field(default_factory=list)
    relationship_delta_expected: str | None = None
    recommended_stance: str
    constraint: Literal["avoid_explicit_psychological_explanation"]


class InsightOutput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    persona_conditioned_insight: str | None
    insight_confidence: float = Field(ge=0, le=1)
