"""JSON-safe dumps for pipeline stages and call logs. Embeddings are stripped."""

from __future__ import annotations

from typing import Any

EMBEDDING_KEY = "embedding"


def dump_value(value: Any) -> Any:
    """Recursively convert a value to JSON-safe data, dropping embedding vectors."""
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return dump_value(value.model_dump(mode="json"))
    if isinstance(value, list):
        return [dump_value(item) for item in value]
    if isinstance(value, tuple):
        return [dump_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): dump_value(item) for key, item in value.items() if key != EMBEDDING_KEY}
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def serialize_stage(kind: str, result: Any) -> Any:
    """Turn a stage result into JSON-safe data. Gate tuples keep request + complexity only."""
    if kind == "gate" and isinstance(result, tuple) and len(result) >= 2:
        situation, request = result[0], result[1]
        complexity = getattr(situation, "complexity", None)
        return {
            "request": dump_value(request),
            "complexity": dump_value(complexity),
        }
    return dump_value(result)
