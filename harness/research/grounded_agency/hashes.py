"""Deterministic JSON identities for frozen research artifacts and model messages."""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from pydantic import BaseModel


def _json_value(value: Any, ancestors: set[int]) -> Any:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("canonical JSON does not support non-finite numbers")
        return value
    if isinstance(value, (dict, list, tuple)):
        identity = id(value)
        if identity in ancestors:
            raise ValueError("canonical JSON does not support circular references")
        ancestors.add(identity)
        try:
            if isinstance(value, dict):
                if any(not isinstance(key, str) for key in value):
                    raise ValueError("canonical JSON requires string object keys")
                return {key: _json_value(item, ancestors) for key, item in value.items()}
            return [_json_value(item, ancestors) for item in value]
        finally:
            ancestors.remove(identity)
    raise ValueError(f"unsupported canonical JSON value: {type(value).__name__}")


def canonical_json(value: Any) -> str:
    """Serialize JSON values or Pydantic artifacts without lossy string fallbacks."""
    try:
        normalized = _json_value(value, set())
    except RecursionError as exc:
        raise ValueError("canonical JSON exceeds supported nesting depth") from exc
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    """SHA-256 of the canonical UTF-8 JSON, retaining message order and content."""
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()
