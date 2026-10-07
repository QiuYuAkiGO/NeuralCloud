"""Optional whole-record history targets; mandatory source text never shrinks."""
from __future__ import annotations

from copy import deepcopy
from collections.abc import Callable
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime
import json

from harness.qq.context_budget import ModelContextSelection, project_group_context, restrict_vision_offer, select_model_context
from harness.qq.context_window import encode_model_payload
from harness.qq.conversation import conversation_scope
from harness.qq.memory import QQPersonContext, QQVerifiedProfile, QQVisualObservation


def history_wire_characters(payload: dict, *, projection: str) -> int:
    blocks = [json.loads(item) for item in encode_model_payload(payload, projection=projection)]
    def canonical(value):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    return len(canonical(blocks[-1]["group_context"])) + sum(
        len(canonical(block)) for block in blocks[1:-1])


def _records(payload):
    return [item for member in payload["group_context"]["members"] for item in member["observations"]]


def include_trusted_reply_references(path, context, record_ids):
    """Load only IDs resolved from the verified protocol reply metadata.

    This private caller boundary never interprets model output or quoted user
    text. A reference grants neither a new conversation nor a remote fetch.
    """
    from harness.qq.decision import _connect

    if not record_ids:
        return context, []
    if (not isinstance(record_ids, list) or len(record_ids) > 256
            or any(not isinstance(item, str) or not item.strip() for item in record_ids)):
        raise ValueError("qq_required_reference_invalid")
    wanted = set(record_ids)
    existing = {item.record_id for member in context.members for item in member.observations}
    scope = conversation_scope(context.group_id)
    members = {member.member_id: member.model_copy(deep=True) for member in context.members}
    added = []
    with closing(_connect(path)) as db:
        for record_id in sorted(wanted - existing):
            row = db.execute("SELECT * FROM qq_visual_observations WHERE record_id=? AND group_id=?",
                             (record_id, context.group_id)).fetchone()
            if row is None:
                raise ValueError("qq_required_reference_missing")
            try:
                item = QQVisualObservation.model_validate_json(row["record_json"])
            except ValueError as exc:
                raise ValueError("qq_required_reference_invalid") from exc
            if (item.record_id != record_id or item.group_id != context.group_id
                    or any(getattr(item, field) != row[field] for field in (
                        "member_id", "profile_record_id", "capture_id", "capture_item_id"))
                    or (scope["kind"] == "private" and item.member_id not in {
                        scope["self_member_id"], scope["peer_id"]})):
                raise ValueError("qq_required_reference_invalid")
            profile_row = db.execute("SELECT * FROM qq_visual_profiles WHERE record_id=? AND group_id=?",
                                     (item.profile_record_id, context.group_id)).fetchone()
            if profile_row is None:
                raise ValueError("qq_required_reference_missing")
            try:
                profile = QQVerifiedProfile.model_validate_json(profile_row["record_json"])
            except ValueError as exc:
                raise ValueError("qq_required_reference_invalid") from exc
            if (profile.record_id != item.profile_record_id or profile.group_id != context.group_id
                    or profile.member_id != item.member_id or profile.member_id != profile_row["member_id"]
                    or profile.capture_id != profile_row["capture_id"]):
                raise ValueError("qq_required_reference_invalid")
            member = members.setdefault(item.member_id, QQPersonContext(group_id=context.group_id,
                member_id=item.member_id, profiles=[], observations=[]))
            if profile.record_id not in {value.record_id for value in member.profiles}:
                member.profiles.append(profile)
            member.observations.append(item)
            added.append(item)
    for member in members.values():
        member.observations.sort(key=lambda item: (item.observed_at, item.stored_at, item.record_id))
    ordered = [*context.participants, *sorted(set(members) - set(context.participants), key=int)]
    return context.model_copy(update={"members": [members[item] for item in ordered], "participants": ordered}), added


def _subset(payload, retained: set[str], *, original_ids: list[str]):
    value = deepcopy(payload)
    restrict_vision_offer(value, retained)
    for member in value["group_context"]["members"]:
        member["observations"] = [item for item in member["observations"] if item["record_id"] in retained]
        latest = max(member["profiles"], key=lambda item: (datetime.fromisoformat(item["observed_at"]),
            datetime.fromisoformat(item["stored_at"]), item["record_id"]), default=None)
        profiles = {item["profile_record_id"] for item in member["observations"]}
        if latest:
            profiles.add(latest["record_id"])
        member["profiles"] = [item for item in member["profiles"] if item["record_id"] in profiles]
    window = value.get("conversation_window") or {}
    # Never join fragments from two old closed blocks into a rewritten cached
    # block. Mandatory records from an evicted block move to the open tail.
    old_closed = list(window.get("closed_observation_ids") or [])
    size = window.get("block_size", 32)
    preserved_closed = [item for start in range(0, len(old_closed), size)
                        if set(old_closed[start:start + size]) <= retained
                        for item in old_closed[start:start + size]]
    for field in ("closed_observation_ids", "open_observation_ids"):
        if field in window:
            window[field] = [item for item in window[field] if item in retained]
    if old_closed:
        window["closed_observation_ids"] = preserved_closed
        window["open_observation_ids"] = list(dict.fromkeys([
            *(item for item in old_closed if item in retained and item not in set(preserved_closed)),
            *window.get("open_observation_ids", [])]))
    for field in ("new_observation_record_ids", "current_request_observation_ids"):
        if field in value:
            value[field] = [item for item in value[field] if item in retained]
    # This coverage survives until select_model_context refreshes retrieval
    # diagnostics from the full scan. It never labels omitted text considered.
    value["bounded_history"] = {"coverage": "selected_whole_records_only",
        "retrieved_observation_count": len(original_ids), "included_observation_count": len(retained),
        "omitted_observation_record_ids": [item for item in original_ids if item not in retained]}
    return value


def _bounded(payload, *, required_ids: set[str], target: int, max_characters: int,
             assessment_reserve: int, projection: str, original_ids: list[str],
             accept: Callable[[ModelContextSelection], bool] | None = None) -> ModelContextSelection:
    records = sorted(_records(payload), key=lambda item: (datetime.fromisoformat(item["observed_at"]),
                     datetime.fromisoformat(item["stored_at"]), item["record_id"]))
    retained = {item["record_id"] for item in records}
    if not required_ids <= retained:
        raise ValueError("Mandatory QQ source is absent from bounded history")
    window = payload.get("conversation_window") or {}
    closed = list(window.get("closed_observation_ids") or [])
    size = window.get("block_size", 32)
    groups = [set(closed[start:start + size]) - required_ids for start in range(0, len(closed), size)]
    closed_set = set(closed)
    groups.extend({item["record_id"]} for item in records
                  if item["record_id"] not in closed_set and item["record_id"] not in required_ids)
    groups = [group for group in groups if group]
    def candidate(count):
        removed = set().union(*groups[:count]) if count else set()
        return _subset(payload, retained - removed, original_ids=original_ids)

    # Whole-record sizes are monotonic. Avoid repeatedly projecting the entire
    # 128-record window for every record during history selection.
    low, high = 0, len(groups)
    while low < high:
        middle = (low + high) // 2
        if history_wire_characters(candidate(middle), projection=projection) <= target:
            high = middle
        else:
            low = middle + 1
    def finalize(count):
        value = candidate(count)
        included = {item["record_id"] for item in _records(value)}
        result = select_model_context(value, required_observation_ids=included, handled_observation_ids=set(),
            max_characters=max_characters, assessment_reserve=assessment_reserve, projection=projection)
        result.payload["model_context_selection"].update(
            retrieved_observation_count=len(original_ids), included_observation_count=len(included),
            omitted_observation_record_ids=[item for item in original_ids if item not in included])
        # The final omission manifest is part of the actual wire budget too.
        characters = sum(map(len, encode_model_payload(result.payload, projection=projection)))
        result = ModelContextSelection(result.payload, result.observation_ids, result.profile_ids, result.delivery_ids,
                                       characters, characters + assessment_reserve <= max_characters)
        return replace(result, fits=result.fits and (accept is None or accept(result)))

    selected = finalize(low)
    if selected.fits:
        return selected
    # A small configured hard budget can be tighter than the history target.
    # Remove further optional whole records before declaring mandatory overflow.
    high = len(groups)
    while low < high:
        middle = (low + high) // 2
        if finalize(middle).fits:
            high = middle
        else:
            low = middle + 1
    return finalize(low)


@dataclass(frozen=True)
class QQHistorySelections:
    gate: ModelContextSelection
    body: ModelContextSelection


def select_bounded_history(payload, *, required_ids: set[str], gate_target: int, body_target: int,
                           max_characters: int, assessment_reserve: int, projection: str) -> QQHistorySelections:
    # Soft targets measure model-visible history. Large private proof attachments
    # are retained in the source store, but must not evict dialogue from prompts.
    payload = {**payload, "group_context": project_group_context(payload["group_context"])}
    original_ids = [item["record_id"] for item in _records(payload)]
    if not required_ids and original_ids:
        latest = max(_records(payload), key=lambda item: (datetime.fromisoformat(item["observed_at"]),
            datetime.fromisoformat(item["stored_at"]), item["record_id"]))
        required_ids = {latest["record_id"]}
    options = dict(required_ids=required_ids, max_characters=max_characters, assessment_reserve=assessment_reserve,
                   projection=projection, original_ids=original_ids)
    body = _bounded(payload, target=max(gate_target, body_target), **options)
    gate = _bounded(body.payload, target=gate_target, **options)
    body = fit_body_history(body, gate, prefix_characters=gate.characters, max_characters=max_characters,
        assessment_reserve=assessment_reserve, projection=projection, original_ids=original_ids)
    return QQHistorySelections(gate, body)


def fit_body_history(body: ModelContextSelection, gate: ModelContextSelection, *, prefix_characters: int,
                     max_characters: int, assessment_reserve: int, projection: str,
                     original_ids: list[str]) -> ModelContextSelection:
    """Fit appended history around an immutable gate/tool prefix and reserves.

    Additional messages repeat profile/omission metadata. They must fit as
    serialized together, both before the gate and after a vision tool returns.
    Every source already shown to the gate remains pinned; no source text or
    tool result is shortened to make room.
    """
    def combined_fits(candidate):
        extra = additional_body_messages(candidate, gate, projection=projection)
        return prefix_characters + sum(len(item["content"]) for item in extra) + assessment_reserve <= max_characters

    if gate.fits and body.fits and not combined_fits(body):
        return _bounded(body.payload, target=history_wire_characters(body.payload, projection=projection),
            required_ids=set(gate.observation_ids), max_characters=max_characters,
            assessment_reserve=assessment_reserve, projection=projection, original_ids=original_ids, accept=combined_fits)
    return body


def additional_body_messages(body: ModelContextSelection, gate: ModelContextSelection, *, projection: str):
    extra = set(body.observation_ids) - set(gate.observation_ids)
    if not extra:
        return []
    # Only additional historical records are appended after the original gate.
    # They retain their own identity snapshots and explicit limited coverage.
    payload = {key: deepcopy(value) for key, value in body.payload.items()
               if key in {"group_context", "coverage", "verified_self_member_id", "conversation", "conversation_window"}}
    payload = _subset(payload, extra, original_ids=list(body.observation_ids))
    payload["coverage"] = "additional_frozen_history_for_body_not_a_new_request"
    # Whole-record selection refreshes the retrieval lists and profile closure.
    selected = select_model_context(payload, required_observation_ids=extra, handled_observation_ids=set(),
        max_characters=10_000_000, assessment_reserve=0, projection=projection)
    return [{"role": "user", "content": item} for item in encode_model_payload(selected.payload, projection=projection)]
