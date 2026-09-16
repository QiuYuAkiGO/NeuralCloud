"""Pure, whole-record selection for the QQ model's bounded user context.

The caller keeps the original validated records for hashing and evidence. No
text is shortened here, and omitted observations are never called considered.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime

from harness.qq.context_window import encode_model_payload
from harness.qq.reply_context import refresh_topic_window


def _size(value: dict) -> int:
    return sum(len(message) for message in encode_model_payload(value))


def project_group_context(context: dict) -> dict:
    """Remove only proof attachments after checking exact profile ownership."""
    projected = deepcopy(context)
    for member in projected["members"]:
        scope = (projected["group_id"], member["member_id"])
        profiles = {profile["record_id"]: profile for profile in member["profiles"]}
        for profile in profiles.values():
            if (profile["group_id"], profile["member_id"]) != scope:
                raise ValueError("QQ model context profile scope mismatch")
            profile.pop("group_evidence", None)
            profile.pop("member_evidence", None)
        for observation in member["observations"]:
            if ((observation["group_id"], observation["member_id"]) != scope
                    or observation["profile_record_id"] not in profiles):
                raise ValueError("QQ model context observation lacks its scoped profile")
            observation.pop("evidence", None)
            observation.pop("attribution_evidence", None)
    return projected


@dataclass(frozen=True)
class ModelContextSelection:
    payload: dict
    observation_ids: tuple[str, ...]
    profile_ids: tuple[str, ...]
    delivery_ids: tuple[str, ...]
    characters: int
    fits: bool


def select_model_context(
    payload: dict, *, required_observation_ids: set[str],
    handled_observation_ids: set[str], max_characters: int, assessment_reserve: int,
) -> ModelContextSelection:
    """Keep mandatory records; remove only optional complete historical items.

    Budget includes both serialized user-message envelopes, omission metadata,
    and the maximum later assessment. Receipt
    inputs must already be validated semantic projections in chronological order.
    Latest numeric profiles remain even when a member's old observations are
    omitted; every retained observation also keeps its original profile snapshot.
    """
    if not 0 <= assessment_reserve < max_characters:
        raise ValueError("Invalid QQ model context budget")
    selected = deepcopy(payload)
    selected["group_context"] = project_group_context(payload["group_context"])
    for alias in selected.get("verified_self_aliases", []):
        alias.pop("evidence_refs", None)
    context = selected["group_context"]
    observations = [item for member in context["members"] for item in member["observations"]]
    all_ids = {item["record_id"] for item in observations}
    if not required_observation_ids.issubset(all_ids):
        raise ValueError("Required QQ observations are outside the selected group context")
    metadata = {
        "proof_refs_stored_outside_model": True,
        "selection_scope": "whole_verified_records_not_complete_chat_history",
        "retrieved_observation_count": len(observations),
        "included_observation_count": len(observations),
        "omitted_observation_record_ids": [],
        "omitted_prior_decision_ids": [],
        "omitted_delivery_receipt_ids": [],
    }
    selected["model_context_selection"] = metadata
    latest_profiles = {
        max(member["profiles"], key=lambda item: (
            datetime.fromisoformat(item["observed_at"]), datetime.fromisoformat(item["stored_at"]), item["record_id"],
        ))["record_id"] for member in context["members"] if member["profiles"]
    }
    deliveries = [(field, item) for field in ("delivery_receipts", "segment_delivery_receipts")
                  for item in selected.get(field, [])]
    latest_delivery_id = max(deliveries, key=lambda pair: (
        datetime.fromisoformat(pair[1]["delivered_at"]), pair[1]["receipt_id"],
    ))[1]["receipt_id"] if deliveries else None

    def refresh() -> None:
        retained = {item["record_id"] for member in context["members"] for item in member["observations"]}
        profile_ids = latest_profiles | {
            item["profile_record_id"] for member in context["members"] for item in member["observations"]
        }
        for member in context["members"]:
            member["profiles"] = [item for item in member["profiles"] if item["record_id"] in profile_ids]
        # Retain full scan/coverage counts, but do not advertise removed matches
        # as presented observations. Original retrieval stays in the fingerprint.
        for retrieval in context.get("member_retrieval", []):
            for field in ("recent_record_ids", "history_record_ids"):
                before = retrieval[field]
                retrieval[field] = [record_id for record_id in before if record_id in retained]
                if len(before) != len(retrieval[field]):
                    retrieval["recent_window_truncated" if field == "recent_record_ids" else "history_truncated"] = True
            retrieval["history_matches"] = [item for item in retrieval["history_matches"] if item["record_id"] in retained]
            member_count = sum(len(member["observations"]) for member in context["members"]
                               if member["member_id"] == retrieval["member_id"])
            retrieval["observations_omitted_count"] = retrieval["total_observations"] - member_count
        metadata["included_observation_count"] = len(retained)
        if "topic_window" in selected:
            refresh_topic_window(selected, as_of=datetime.fromisoformat(selected["topic_window"]["as_of"]))

    def fits() -> bool:
        return _size(selected) + assessment_reserve <= max_characters

    refresh()
    # Derived memory is optional. Preserve whole entries and count it inside the
    # same budget used by the gate and the persona, rather than extending TTL or
    # displacing the mandatory current observation.
    derived = selected.get("derived_memory", {}).get("entries", [])
    while derived and not fits():
        derived.pop()
    for item in list(selected.get("prior_decisions", [])):
        if fits():
            break
        selected["prior_decisions"].remove(item)
        metadata["omitted_prior_decision_ids"].append(item["decision_id"])
    delivery_groups = {}
    for field, item in deliveries:
        identity = item["decision_id"] if field == "segment_delivery_receipts" else item["receipt_id"]
        delivery_groups.setdefault((field, identity), []).append((field, item))
    for group in sorted(delivery_groups.values(), key=lambda group: max(
        (datetime.fromisoformat(item["delivered_at"]), item["receipt_id"]) for _, item in group
    )):
        if fits():
            break
        if any(item["receipt_id"] == latest_delivery_id for _, item in group):
            continue
        for field, item in group:
            selected[field].remove(item)
            metadata["omitted_delivery_receipt_ids"].append(item["receipt_id"])
        refresh()
    matches = {item["record_id"]: item for member in context.get("member_retrieval", [])
               for item in member["history_matches"]}
    optional = sorted((item for item in observations if item["record_id"] in handled_observation_ids
                       and item["record_id"] not in required_observation_ids), key=lambda item: (
                           matches.get(item["record_id"], {}).get("score", 0),
                           datetime.fromisoformat(item["observed_at"]), datetime.fromisoformat(item["stored_at"]), item["record_id"],
                       ))
    window = selected.get("conversation_window") or {}
    closed_ids = list(window.get("closed_observation_ids") or [])
    block_size = window.get("block_size") if isinstance(window.get("block_size"), int) else 0
    if closed_ids and block_size > 0:
        while not fits() and len(closed_ids) >= block_size:
            drop_list = closed_ids[:block_size]
            drop = set(drop_list)
            if drop & required_observation_ids:
                break
            closed_ids = closed_ids[block_size:]
            for member in context["members"]:
                member["observations"] = [item for item in member["observations"] if item["record_id"] not in drop]
            metadata["omitted_observation_record_ids"].extend(drop_list)
            window["closed_observation_ids"] = closed_ids
            selected["conversation_window"] = window
            refresh()
    else:
        for item in optional:
            if fits():
                break
            for member in context["members"]:
                if member["member_id"] == item["member_id"]:
                    member["observations"].remove(item)
            metadata["omitted_observation_record_ids"].append(item["record_id"])
            refresh()
    return ModelContextSelection(
        payload=selected,
        observation_ids=tuple(sorted(item["record_id"] for member in context["members"] for item in member["observations"])),
        profile_ids=tuple(sorted(item["record_id"] for member in context["members"] for item in member["profiles"])),
        delivery_ids=tuple(dict.fromkeys([
            *(item["receipt_id"] for field in ("delivery_receipts", "segment_delivery_receipts") for item in selected.get(field, [])),
            *(ref for turn in selected.get("delivered_style_window", {}).get("turns", []) for ref in turn["receipt_ids"]),
        ])),
        characters=_size(selected), fits=fits(),
    )
