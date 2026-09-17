"""Deterministic model projections without changing the retained authorities.

Only the model view is reordered. Callers still select current records and
validate offers/evidence against their original, fresh runtime objects.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from typing import Any


OFFER_DEFINITION_RULE = (
    "available_offer_definitions 是能力参数定义；available_offers 是本轮可用机会与当前观察。"
    "按 definition_id 查找每个机会的 input_schema 和 criterion_kinds；"
    "只能选择 available_offers 中的 offer_id，定义本身不表示能力可用或获得执行授权。"
)


def canonical_json(value: Any) -> str:
    """One encoding for shared persona, schemas and nested data objects."""
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def ordered_payload_json(payload: dict[str, Any]) -> str:
    """Preserve the caller's stable-first envelope, canonicalize its values."""
    return "{" + ",".join(canonical_json(key) + ":" + canonical_json(value)
                          for key, value in payload.items()) + "}"


def persona_prefix(persona: Any) -> str:
    value = persona.model_dump(mode="json") if hasattr(persona, "model_dump") else persona
    return "当前完整人格：\n" + canonical_json(value) + "\n\n"


def build_messages(*, persona: Any, rules: str, payload: dict[str, Any],
                   fixed_payload: dict[str, Any] | None = None,
                   emotion_text: str = "") -> list[dict[str, str]]:
    fixed = fixed_payload or {}
    if fixed.keys() & payload.keys():
        raise ValueError("cognitive_prompt_duplicate_field")
    messages = [
        {"role": "system", "content": persona_prefix(persona) + rules},
        {"role": "user", "content": ordered_payload_json({**fixed, **payload})},
    ]
    if emotion_text:
        # This was system guidance before the projection change too. Keep its
        # authority while allowing fixed user schemas to precede changing mood.
        messages.append({"role": "system", "content": emotion_text})
    return messages


def split_offer_catalog(offers: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Separate parameter definitions from per-opportunity identity/state.

    In particular, offer_id, scope, activation revision, expiry and observation
    never participate in the stable definition hash. No offer is cached here.
    """
    definitions: dict[str, dict[str, Any]] = {}
    current = []
    for offer in offers:
        definition = {key: deepcopy(offer[key]) for key in
                      ("channel", "provider_id", "capability_id", "input_schema", "criterion_kinds")
                      if key in offer}
        identity = "capability-definition:" + hashlib.sha256(canonical_json(definition).encode("utf-8")).hexdigest()
        definitions[identity] = {"definition_id": identity, **definition}
        current.append({**{key: deepcopy(value) for key, value in offer.items()
                           if key not in {"input_schema", "criterion_kinds"}}, "definition_id": identity})
    ordered = sorted(definitions.values(), key=lambda item: (
        item.get("channel", ""), item.get("provider_id", ""), item.get("capability_id", ""), item["definition_id"]))
    return ordered, sorted(current, key=lambda item: item["offer_id"])


def chronological_memory(payload: dict[str, Any]) -> dict[str, Any]:
    """Reorder only already selected source records, never reselect or freeze.

    Other context formats are copied unchanged. The shared-memory projection
    has an explicit source timestamp and immutable record ID on every entry.
    """
    result = deepcopy(payload)
    entries = result.get("entries")
    if not isinstance(entries, list) or not entries:
        return result
    try:
        def key(entry: dict[str, Any]) -> tuple[datetime, str]:
            stamp = datetime.fromisoformat(entry["occurred_at"].replace("Z", "+00:00"))
            if stamp.tzinfo is None or not isinstance(entry["record_id"], str):
                raise ValueError("memory_projection_without_order_key")
            return stamp.astimezone(timezone.utc), entry["record_id"]

        result["entries"] = sorted(entries, key=key)
    except (KeyError, TypeError, ValueError, AttributeError):
        # A different projection does not acquire invented timestamps/identity.
        return result
    return result


def project_context_segments(segments: list[Any]) -> list[dict[str, Any]]:
    """Send source text once; evidence binding and hashes remain internal."""
    result = []
    for segment in segments:
        text = segment.text
        try:
            decoded = json.loads(text)
        except (TypeError, ValueError):
            decoded = None
        if (isinstance(decoded, dict) and isinstance(decoded.get("shared_memory"), dict)
                and decoded["shared_memory"].get("schema_version") == "shared-memory-context-v1"):
            decoded["shared_memory"] = chronological_memory(decoded["shared_memory"])
            text = canonical_json(decoded)
        result.append({"kind": segment.kind, "text": text,
                       "source_ids": list(segment.source_ids), "why": segment.why})
    return result
