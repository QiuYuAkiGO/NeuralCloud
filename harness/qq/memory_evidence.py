"""QQ's trusted bridge from final whole-record selection to memory validation.

Never infer history authority from source ID strings, profiles, style excerpts,
model plans, or arbitrary payload text. Selected records must match an original
validated QQ record or the shared owner's already-authorized recall projection.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from harness.core.inference import MemoryEvidenceEntry
from harness.qq.conversation import conversation_audience, conversation_scope
from harness.qq.emotion_window import remove_legacy_emotion_parameters


def current_qq_burst_ids(*, trusted_context, new_observation_ids: set[str],
                         trigger_record_id: str | None) -> set[str]:
    """Separate first-import history from the current trigger's three-minute burst.

    Never promote a record because it is merely unhandled. Without an exact,
    timestamped trigger there is no reliable temporal boundary, so retain the
    conservative exclusion of the whole new batch. Unknown/future times also
    remain current; only validated records strictly older than the cutoff pass.
    """
    originals = {item.record_id: item for member in trusted_context.members for item in member.observations}
    trigger = originals.get(trigger_record_id)
    anchor = getattr(trigger, "observed_at", None)
    excluded = set(new_observation_ids)
    if trigger_record_id is not None:
        excluded.add(trigger_record_id)
    if not isinstance(anchor, datetime) or anchor.tzinfo is None:
        return excluded
    cutoff = anchor - timedelta(minutes=3)
    for record_id in new_observation_ids:
        if record_id == trigger_record_id:
            continue
        observed_at = getattr(originals.get(record_id), "observed_at", None)
        if isinstance(observed_at, datetime) and observed_at.tzinfo is not None and observed_at < cutoff:
            excluded.discard(record_id)
    return excluded


def build_qq_memory_evidence(*, payload: dict, trusted_context, current_observation_ids: set[str],
                             receipts: list, partial_receipts: list, shared_owner=None) -> list[MemoryEvidenceEntry]:
    group_id = trusted_context.group_id
    if payload["group_context"]["group_id"] != group_id:
        raise ValueError("QQ memory evidence conversation mismatch")
    scope = conversation_scope(group_id)
    audience = conversation_audience(group_id)
    channel = "qq_private" if scope["kind"] == "private" else "qq_group"
    entries = []

    def add(*, source_id, text, refs, status, kind="history", source_channel=channel,
            source_conversation_id=audience, speaker_id=None, observed_at=None):
        if not isinstance(text, str) or not text.strip():
            return
        entries.append(MemoryEvidenceEntry(source_id=source_id, text=text, audience=audience,
            source_channel=source_channel, source_conversation_id=source_conversation_id,
            source_refs=list(refs), status=status, kind=kind,
            speaker_id=speaker_id, observed_at=observed_at))

    originals = {item.record_id: item for member in trusted_context.members for item in member.observations}
    for member in payload["group_context"]["members"]:
        for selected in member["observations"]:
            original = originals.get(selected["record_id"])
            if original is None:
                raise ValueError("QQ memory evidence observation not in trusted retrieval")
            expected = original.model_dump(mode="json", exclude={"evidence", "attribution_evidence"})
            if (selected != expected or original.group_id != group_id
                    or original.member_id != member["member_id"]):
                raise ValueError("QQ memory evidence observation projection mismatch")
            # A self echo is an observation, not an independent delivery proof.
            if original.member_id == payload.get("verified_self_member_id"):
                continue
            add(source_id=original.record_id, text=original.text,
                refs=[*original.evidence, *original.attribution_evidence], status="user_claim",
                kind="current" if original.record_id in current_observation_ids else "history",
                speaker_id=f"qq:member:{original.member_id}", observed_at=original.observed_at)

    originals = {item.receipt_id: item for item in [*receipts, *partial_receipts]}
    for field in ("delivery_receipts", "segment_delivery_receipts"):
        for selected in payload.get(field, []):
            original = originals.get(selected["receipt_id"])
            if (original is None or original.group_id != group_id
                    or original.self_member_id != payload.get("verified_self_member_id")
                    or original.delivered_text != selected["delivered_text"]):
                raise ValueError("QQ memory evidence delivery projection mismatch")
            add(source_id=original.receipt_id, text=original.delivered_text,
                refs=original.evidence_refs, status="verified_delivery",
                speaker_id=f"qq:member:{original.self_member_id}", observed_at=original.delivered_at)

    if shared_owner is None:
        return entries
    # The shared owner validates visibility before producing these projections.
    # Require exact identity with that trusted projection after final selection.
    trusted_shared = getattr(shared_owner, "context", {}) or {}
    emotion_on = getattr(getattr(shared_owner, "settings", None), "emotion_mode", "off") == "on"
    if emotion_on:
        # The main payload intentionally omits legacy state in this mode. Apply
        # exactly that projection to the trusted side; source text and all other
        # evidence fields must still match without alteration.
        trusted_shared = remove_legacy_emotion_parameters(trusted_shared)
    selected_shared = payload.get("shared_memory", {})
    for selected_container, trusted_container in [
        (selected_shared, trusted_shared),
        (selected_shared.get("operator_confirmed_facts", {}), trusted_shared.get("operator_confirmed_facts", {})),
    ]:
        originals = {item["record_id"]: item for item in trusted_container.get("entries", [])}
        for selected in selected_container.get("entries", []):
            if selected != originals.get(selected["record_id"]):
                raise ValueError("QQ shared memory evidence projection mismatch")
            if selected.get("kind") == "qq_profile" or selected.get("authority") == "verified_profile_observation":
                continue
            add(source_id=selected["record_id"], text=selected.get("text"),
                refs=selected.get("evidence_refs", []),
                status="verified_delivery" if selected.get("authority") == "verified_delivery" else "user_claim",
                source_channel=selected["channel"], source_conversation_id=selected["session_id"],
                speaker_id=selected.get("author_principal"), observed_at=selected.get("occurred_at"))

    trusted_derived = getattr(shared_owner, "derived_memory", None) or {}
    if emotion_on:
        trusted_derived = remove_legacy_emotion_parameters(trusted_derived)
    originals = {item["record_id"]: item for item in trusted_derived.get("entries", [])}
    for selected in payload.get("derived_memory", {}).get("entries", []):
        if selected != originals.get(selected["record_id"]):
            raise ValueError("QQ derived memory evidence projection mismatch")
        status = selected.get("status")
        status = status if status in {"provisional", "disputed"} else (
            "provisional" if selected.get("authority") == "misty_tentative_interpretation" else "retained_fact")
        refs = [*selected.get("source_refs", []), *selected.get("supporting_episode_ids", [])]
        for field in ("objective_fact", "summary", "current_impression", "first_impression", "value"):
            add(source_id=selected["record_id"], text=selected.get(field), refs=refs,
                status=status, kind="derived_memory", source_channel=selected.get("source_channel", channel),
                source_conversation_id=selected.get("source_conversation_id", audience),
                speaker_id=selected.get("author_principal"), observed_at=selected.get("occurred_at"))
        for counter in selected.get("counterevidence", []):
            add(source_id=selected["record_id"], text=counter.get("statement"),
                refs=[counter["episode_id"]], status="disputed", kind="derived_memory",
                source_channel=selected.get("source_channel", channel),
                source_conversation_id=selected.get("source_conversation_id", audience))
    return entries
