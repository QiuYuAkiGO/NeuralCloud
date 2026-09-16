"""Stable chronological QQ model windows for provider prefix-cache reuse.

Closed blocks are immutable once filled. The open block and per-turn fields stay
in a trailing user message. Window start persists so the prefix only rebuilds
when the window exceeds WINDOW_MAX_OBSERVATIONS.
"""
from __future__ import annotations

from copy import deepcopy
import json
from typing import Any

WINDOW_BLOCK_SIZE = 32
WINDOW_MIN_OBSERVATIONS = 64
WINDOW_MAX_OBSERVATIONS = 128
WINDOW_CANDIDATE_LIMIT = WINDOW_MAX_OBSERVATIONS + WINDOW_MIN_OBSERVATIONS


def observation_sort_key(item: Any) -> tuple[str, str, str]:
    if isinstance(item, dict):
        observed = item.get("observed_at") or ""
        stored = item.get("stored_at") or ""
        record_id = item.get("record_id") or ""
    else:
        observed = getattr(item, "observed_at", "")
        stored = getattr(item, "stored_at", "")
        record_id = getattr(item, "record_id", "")
    if hasattr(observed, "isoformat"):
        observed = observed.isoformat()
    if hasattr(stored, "isoformat"):
        stored = stored.isoformat()
    return (str(observed), str(stored), str(record_id))


def encode_window_start(item: Any | None) -> str | None:
    if item is None:
        return None
    return json.dumps(list(observation_sort_key(item)), separators=(",", ":"))


def decode_window_start(value: str | None) -> tuple[str, str, str] | None:
    if not value:
        return None
    try:
        decoded = json.loads(value)
    except json.JSONDecodeError:
        return None
    if not (isinstance(decoded, list) and len(decoded) == 3 and all(isinstance(part, str) for part in decoded)):
        return None
    return decoded[0], decoded[1], decoded[2]


def select_append_window(
    observations: list,
    *,
    start_key: tuple[str, str, str] | None = None,
    block_size: int = WINDOW_BLOCK_SIZE,
    window_min: int = WINDOW_MIN_OBSERVATIONS,
    window_max: int = WINDOW_MAX_OBSERVATIONS,
) -> tuple[list, tuple[str, str, str] | None]:
    """Keep a sticky start and append; drop oldest whole blocks past window_max."""
    ordered = sorted(observations, key=observation_sort_key)
    if not ordered:
        return [], start_key
    start_index = 0
    if start_key is not None:
        for index, item in enumerate(ordered):
            if observation_sort_key(item) >= start_key:
                start_index = index
                break
        else:
            start_index = 0
    window = ordered[start_index:]
    if len(window) > window_max:
        drop = ((len(window) - window_min) // block_size) * block_size
        start_index += max(block_size, drop)
        window = ordered[start_index:]
    return window, (observation_sort_key(window[0]) if window else start_key)


def split_closed_open(observations: list, *, block_size: int = WINDOW_BLOCK_SIZE) -> tuple[list, list]:
    closed_count = (len(observations) // block_size) * block_size
    return observations[:closed_count], observations[closed_count:]


def split_model_payload(payload: dict) -> tuple[dict, dict]:
    """Stable closed records vs per-turn fields, each independently canonicalized."""
    window = payload.get("conversation_window") or {}
    closed_ids = list(window.get("closed_observation_ids") or [])
    open_ids = list(window.get("open_observation_ids") or [])
    closed_set = set(closed_ids)
    group = deepcopy(payload.get("group_context") or {"members": []})
    retrieval = group.pop("retrieval", None)
    member_retrieval = group.pop("member_retrieval", None)
    open_observations = []
    for member in group.get("members", []):
        kept, opened = [], []
        for item in member.get("observations", []):
            if item.get("record_id") in closed_set:
                kept.append(item)
            else:
                opened.append(item)
        member["observations"] = kept
        open_observations.extend(opened)
    if open_ids:
        wanted = set(open_ids)
        open_observations = [item for item in open_observations if item.get("record_id") in wanted]
    open_observations.sort(key=observation_sort_key)
    base = {
        "coverage": payload.get("coverage"),
        "group_context": group,
        "verified_self_member_id": payload.get("verified_self_member_id"),
        "verified_self_aliases": payload.get("verified_self_aliases", []),
        "conversation_window": {
            "mode": window.get("mode", "chronological_append"),
            "block_size": window.get("block_size", WINDOW_BLOCK_SIZE),
            "closed_observation_ids": closed_ids,
        },
    }
    if "conversation" in payload:
        base["conversation"] = payload["conversation"]
    turn = {key: value for key, value in payload.items() if key not in {
        "coverage", "group_context", "verified_self_member_id", "verified_self_aliases", "conversation",
    }}
    turn_window = dict(turn.get("conversation_window") or window)
    turn_window["open_observation_ids"] = open_ids
    turn_window.pop("closed_observation_ids", None)
    turn["conversation_window"] = turn_window
    turn["open_observations"] = open_observations
    if retrieval is not None:
        turn["retrieval"] = retrieval
    if member_retrieval is not None:
        turn["member_retrieval"] = member_retrieval
    return base, turn


def encode_model_payload(payload: dict) -> tuple[str, str]:
    """Serialize the exact two user messages used for selection and inference."""
    base, turn = split_model_payload(payload)
    options = {"sort_keys": True, "ensure_ascii": False, "separators": (",", ":")}
    return json.dumps(base, **options), json.dumps(turn, **options)


def combined_user_payload(messages: list[dict]) -> dict:
    """Rebuild the logical payload from split user messages. Tests only."""
    merged: dict[str, Any] = {}
    opens: list[dict] = []
    decoder = json.JSONDecoder()
    for message in messages:
        if message.get("role") != "user":
            continue
        text = message.get("content")
        if not isinstance(text, str):
            continue
        index = 0
        while index < len(text):
            start = text.find("{", index)
            if start < 0:
                break
            try:
                value, end = decoder.raw_decode(text, start)
            except json.JSONDecodeError:
                index = start + 1
                continue
            index = end
            if not isinstance(value, dict):
                continue
            opens.extend(item for item in (value.get("open_observations") or []) if isinstance(item, dict))
            if isinstance(merged.get("conversation_window"), dict) and isinstance(value.get("conversation_window"), dict):
                window = {**merged["conversation_window"], **value["conversation_window"]}
                merged.update({key: item for key, item in value.items() if key != "conversation_window"})
                merged["conversation_window"] = window
            else:
                merged.update(value)
    group = merged.get("group_context")
    if opens and isinstance(group, dict):
        existing = {item.get("record_id") for member in group.get("members", [])
                    for item in member.get("observations", [])}
        extra = [item for item in opens if item.get("record_id") not in existing]
        by_member: dict[str, list] = {}
        for item in extra:
            by_member.setdefault(str(item.get("member_id")), []).append(item)
        for member in group.setdefault("members", []):
            member_id = str(member.get("member_id"))
            if member_id in by_member:
                member["observations"] = [*member.get("observations", []), *by_member[member_id]]
        merged.pop("open_observations", None)
    if isinstance(group, dict):
        if "retrieval" in merged:
            group["retrieval"] = merged.pop("retrieval")
        if "member_retrieval" in merged:
            group["member_retrieval"] = merged.pop("member_retrieval")
    return merged


def is_qq_gate_messages(messages: list[dict], *, rule_prefixes: tuple[str, ...]) -> bool:
    if not messages or messages[0].get("role") != "system":
        return False
    content = messages[0].get("content") or ""
    if not isinstance(content, str) or not content.startswith(rule_prefixes):
        return False
    return all(item.get("role") != "assistant" for item in messages)
