"""Build scoped emotion context from verified QQ observations and actual sends."""
from __future__ import annotations

from contextlib import closing, nullcontext
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

from harness.qq.conversation import conversation_audience, conversation_scope


def remove_legacy_emotion_parameters(value):
    """Remove application state fields without rewriting quoted source text."""
    containers = {"homeostatic_interoception", "homeostatic_summary", "homeostasis",
                  "psych", "psychological_state", "emotion_hint"}
    numeric_fields = {"closeness", "trust", "attachment", "recent_tension", "emotional_valence"}
    if isinstance(value, dict):
        return {key: remove_legacy_emotion_parameters(item) for key, item in value.items()
                if key not in containers
                and not (key in numeric_fields and isinstance(item, (float, int)))}
    if isinstance(value, list):
        return [remove_legacy_emotion_parameters(item) for item in value]
    return value


def _protocol_message_time(db, observation, self_member_id):
    """Use a source's immutable receive time for accepted server clock skew."""
    if db is None or observation.author_attribution != "qq_protocol_verified":
        return observation.observed_at
    row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=? AND group_id=?",
                     (observation.capture_item_id, observation.group_id)).fetchone()
    if row is None:
        # Imported protocol history may predate the current transport ledger.
        # Its original timestamp remains unchanged; current triggers are fully
        # bound to an existing row by Runtime's separate _source validation.
        return observation.observed_at
    value = json.loads(row["record_json"])
    if (row["event_id"] != observation.capture_item_id or row["self_id"] != self_member_id
            or row["member_id"] != observation.member_id or row["message_id"] != observation.message_id
            or row["source_record_id"] != observation.record_id
            or value.get("event_id") != row["event_id"] or value.get("group_id") != row["group_id"]
            or value.get("self_member_id") != row["self_id"] or value.get("member_id") != row["member_id"]
            or value.get("message_id") != row["message_id"] or value.get("received_at") != row["received_at"]
            or value.get("bound_observation") != observation.model_dump(mode="json")
            or datetime.fromisoformat(value["message_time"]) != observation.observed_at):
        raise ValueError("qq_emotion_protocol_time_binding_invalid")
    received_at = datetime.fromisoformat(row["received_at"])
    if received_at.tzinfo is None or (observation.observed_at - received_at).total_seconds() > 5:
        raise ValueError("qq_emotion_protocol_clock_skew_invalid")
    return min(observation.observed_at, received_at)


def build_qq_emotion_messages(
    *, memory_db: Path | str, group_id: str, self_member_id: str,
    as_of: datetime, trigger_event_id: str | None = None,
    trigger_member_id: str | None = None,
    triggers: list[tuple[str, str]] | None = None,
    protocol_state_db: Path | str | None = None,
):
    """Read 20 recent records; only the exact validated incoming triggers are new.

    The caller must first validate Runtime's QQ source authority. Search results,
    distilled memory and unsent drafts are deliberately absent from this input.
    Text expiry does not mutate the emotion service's separate decaying balance.
    """
    from harness.emotion.models import EmotionMessage
    from harness.qq.decision import QQDeliveryReceipt
    from harness.qq.feedback import _observation, _readonly, _receipts

    if as_of.tzinfo is None:
        raise ValueError("qq_emotion_requires_aware_time")
    as_of = as_of.astimezone(timezone.utc)
    if triggers is None:
        triggers = [] if trigger_event_id is None else [(trigger_event_id, trigger_member_id)]
    elif trigger_event_id is not None:
        raise ValueError("qq_emotion_trigger_argument_conflict")
    trigger_members = {member_id for _, member_id in triggers}
    scope = conversation_scope(group_id)
    if scope["kind"] == "private" and (
        self_member_id != scope["self_member_id"]
        or (trigger_members and trigger_members != {scope["peer_id"]})
    ):
        raise ValueError("qq_emotion_private_scope_mismatch")
    if any(not event_id or not member_id or member_id == self_member_id
           for event_id, member_id in triggers):
        raise ValueError("qq_emotion_trigger_requires_external_author")
    audience = conversation_audience(group_id)
    cutoff = as_of - timedelta(minutes=30)
    messages = []
    bound_triggers = []
    with closing(_readonly(memory_db)) as db, (
        closing(_readonly(protocol_state_db)) if protocol_state_db is not None else nullcontext(None)
    ) as protocol_db:
        rows = db.execute(
            "SELECT record_id, observed_at FROM qq_visual_observations "
            "WHERE group_id=? AND observed_at>=? AND observed_at<=? "
            "ORDER BY observed_at DESC, rowid DESC LIMIT 80",
            (group_id, cutoff.isoformat(), (as_of + timedelta(seconds=5)).isoformat()),
        ).fetchall()
        extra_rows = []
        for event_id, member_id in triggers:
            matches = db.execute(
                "SELECT record_id, observed_at FROM qq_visual_observations "
                "WHERE group_id=? AND capture_item_id=? AND member_id=?",
                (group_id, event_id, member_id),
            ).fetchall()
            if len(matches) != 1:
                raise ValueError("qq_emotion_trigger_missing_or_ambiguous")
            trigger = _observation(db, group_id, matches[0]["record_id"])
            if (trigger is None or trigger.member_id != member_id
                    or trigger.capture_item_id != event_id
                    or trigger.observed_at != datetime.fromisoformat(matches[0]["observed_at"])
                    or _protocol_message_time(protocol_db, trigger, self_member_id) > as_of):
                raise ValueError("qq_emotion_trigger_invalid")
            bound_triggers.append(trigger)
            extra_rows.append(matches[0])
        if extra_rows:
            seen = {row["record_id"] for row in extra_rows}
            rows = [*extra_rows, *[row for row in rows if row["record_id"] not in seen]]
        trigger_ids = {item.record_id for item in bound_triggers}

        receipts = [item for item in _receipts(db, group_id, cutoff)
                    if item.self_member_id == self_member_id and item.delivered_at <= as_of]
        # Keep segment identities stable when their aggregate is recorded later.
        # The EmotionService merges previous windows, so replacing a prefix by
        # a new aggregate identity would retain both copies across observations.
        receipts = [item for item in receipts
                    if not (isinstance(item, QQDeliveryReceipt) and item.segment_receipt_ids)]
        for row in rows:
            observation = _observation(db, group_id, row["record_id"])
            if observation is None or observation.observed_at != datetime.fromisoformat(row["observed_at"]):
                raise ValueError("qq_emotion_observation_invalid")
            timestamp = _protocol_message_time(protocol_db, observation, self_member_id)
            if timestamp > as_of or (timestamp < cutoff and observation.record_id not in trigger_ids):
                continue
            # Protocol self observations are transport echoes. Only durable send
            # receipts represent this character's protocol output, including when
            # an echo arrives before its receipt. Switching echo/receipt identity
            # on the next turn would duplicate a message in the merged window.
            if observation.member_id == self_member_id and observation.message_id is not None:
                continue
            messages.append(EmotionMessage(
                source_id=observation.record_id, scope=audience,
                role="character" if observation.member_id == self_member_id else "user",
                text=observation.text, timestamp=timestamp, speaker=observation.member_id,
            ))
        messages.extend(EmotionMessage(
            source_id=item.receipt_id, scope=audience, role="character", text=item.delivered_text,
            timestamp=item.delivered_at, speaker=self_member_id,
        ) for item in receipts)

    messages.sort(key=lambda item: (item.timestamp, item.source_id))
    selected = messages[-20:]
    required = [next(item for item in messages if item.source_id == trigger.record_id)
                for trigger in bound_triggers]
    if len(required) > 20:
        raise ValueError("emotion_new_sources_exceed_window")
    selected_ids = {item.source_id for item in selected}
    if any(item.source_id not in selected_ids for item in required):
        required_ids = {item.source_id for item in required}
        remaining = [item for item in messages if item.source_id not in required_ids]
        available = 20 - len(required)
        selected = sorted([*required, *(remaining[-available:] if available else [])],
                          key=lambda item: (item.timestamp, item.source_id))
    return audience, selected, [item.record_id for item in bound_triggers]
