"""Lossless speech/identity projection of already-selected QQ evidence.

The full selected DTO remains the source of evidence admission and hashing.
This module only removes acquisition bookkeeping from model messages; it never
selects records, truncates text, or promotes observations to verified facts.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import hashlib
import json


PROJECTION_VERSION = "qq-compact-evidence-v1"
_IDENTITY_FIELDS = ("member_id", "display_name", "display_name_source", "verification")
_OBSERVATION_FIELDS = (
    "record_id", "member_id", "observed_at", "text", "author_attribution",
    "claims", "message_id", "message_id_source", "image_analysis",
)
_COVERAGE_FIELDS = (
    "member_id", "total_observations", "recent_window_truncated", "history_truncated",
    "observations_omitted_count", "candidate_scan_truncated",
)


def compact_model_blocks(blocks: list[dict]) -> list[dict]:
    """Project immutable blocks without changing their order or source content."""
    projected = deepcopy(blocks)
    group = projected[-1]["group_context"]
    group_id = group["group_id"]
    identities: dict[str, dict] = {}
    profile_refs: dict[str, tuple[str, str]] = {}
    for member in group["members"]:
        member_id = member["member_id"]
        if member.get("group_id", group_id) != group_id:
            raise ValueError("QQ compact member scope mismatch")
        for profile in member["profiles"]:
            if (profile["group_id"], profile["member_id"]) != (group_id, member_id):
                raise ValueError("QQ compact profile scope mismatch")
            identity = {key: profile[key] for key in _IDENTITY_FIELDS}
            serialized = json.dumps([group_id, identity], ensure_ascii=False,
                                    sort_keys=True, separators=(",", ":"))
            reference = "qq-identity:" + hashlib.sha256(serialized.encode()).hexdigest()[:24]
            binding = (member_id, reference)
            previous = profile_refs.setdefault(profile["record_id"], binding)
            if previous != binding:
                raise ValueError("QQ compact profile identity conflict")
            identities[reference] = identity

    def observation(item: dict, *, member_id: str | None = None) -> dict:
        owner, reference = profile_refs[item["profile_record_id"]]
        if (item["group_id"] != group_id or item["member_id"] != owner
                or member_id is not None and member_id != owner):
            raise ValueError("QQ compact observation scope mismatch")
        return {**{key: item[key] for key in _OBSERVATION_FIELDS if key in item},
                "identity_ref": reference}

    for block in projected[1:-1]:
        block["closed_observations"] = [observation(item) for item in block["closed_observations"]]
    for member in group["members"]:
        profiles = member.pop("profiles")
        if profiles:
            latest = max(profiles, key=lambda item: (datetime.fromisoformat(item["observed_at"]),
                                                    datetime.fromisoformat(item["stored_at"]), item["record_id"]))
            member["current_identity_ref"] = profile_refs[latest["record_id"]][1]
            member["current_identity_observed_at"] = latest["observed_at"]
        member.pop("group_id", None)
        member["observations"] = [observation(item, member_id=member["member_id"])
                                  for item in member["observations"]]
    group["identity_snapshots"] = identities
    if "member_retrieval" in group:
        group["member_retrieval"] = [
            {key: item[key] for key in _COVERAGE_FIELDS if key in item}
            for item in group["member_retrieval"]
        ]
    projected[0]["model_projection"] = PROJECTION_VERSION
    projected[0]["identity_resolution"] = (
        "Each observation identity_ref resolves to its original identity_snapshots entry; "
        "current_identity_ref is the latest observed display name, not a cross-conversation identity."
    )
    return projected
