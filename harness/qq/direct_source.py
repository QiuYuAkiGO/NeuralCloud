"""Validate protocol turn authority for the existing Runtime and Cognitive owner."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3

from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_store import normalize_evidence
from harness.qq.conversation import conversation_scope
from harness.qq.memory import QQVerifiedProfile, QQVisualObservation


class DirectQQSourceExpired(ValueError):
    """A verified protocol source lost its original reply window."""


def validate_direct_source(config, request, *, completed_replay=False):
    path = os.environ.get("NC_QQ_DIRECT_CONFIG")
    if not path:
        raise ValueError("direct_qq_not_configured")
    settings = DirectQQSettings.read(Path(path))
    if request.idle_intent is not None and request.idle_intent.origin == "production":
        from harness.qq.direct_contacts import validate_shared
        return validate_shared(settings, config, request, completed_replay=completed_replay)
    groups = {group.group_id for group in settings.group_roster if group.observe}
    send_groups = {group.group_id for group in settings.group_roster if group.send}
    peers = {contact.peer_id for contact in settings.private_contacts if contact.observe}
    send_peers = {contact.peer_id for contact in settings.private_contacts if contact.send}
    scope = conversation_scope(request.group_id)
    authorized = (scope["peer_id"] in send_peers and scope["peer_id"] in config.allowed_private_peer_ids
        and scope["self_member_id"] == settings.self_member_id and request.trigger_member_id == scope["peer_id"]
        if scope["kind"] == "private" else request.group_id in send_groups and request.group_id in config.allowed_group_ids)
    if (not settings.enabled or request.transport != "onebot" or request.idle_intent is not None
            or not authorized
            or request.self_member_id != settings.self_member_id or request.self_member_id != config.self_member_id
            or settings.session_config_path.is_symlink()):
        raise ValueError("direct_qq_scope_invalid")
    with closing(sqlite3.connect(settings.state_db.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (request.trace.event_id,)).fetchone()
        if row is None:
            raise ValueError("direct_qq_source_missing")
        run = db.execute("SELECT * FROM qq_direct_runs WHERE run_id=?", (row["run_id"],)).fetchone()
        sent = db.execute("SELECT 1 FROM qq_direct_sends WHERE event_id=? LIMIT 1", (row["event_id"],)).fetchone()
    value = json.loads(row["record_json"])
    first_seen = datetime.fromisoformat(row["received_at"])
    if (run is None or run["active"] != 1 or run["self_id"] != request.self_member_id
            or row["group_id"] != request.group_id or row["self_id"] != request.self_member_id
            or value["event_id"] != row["event_id"] or value["run_id"] != row["run_id"]
            or value["group_id"] != row["group_id"] or value["self_member_id"] != row["self_id"]
            or value["received_at"] != row["received_at"]
            or value["member_id"] != request.trigger_member_id or value["member_id"] != row["member_id"]
            or value["member_id"] == request.self_member_id or value["own"] or value["historical"]
            or value["message_id"] != row["message_id"]
            or first_seen != request.trace.first_seen_at
            or not first_seen < request.deadline_at <= first_seen + timedelta(seconds=60)):
        raise ValueError("direct_qq_source_binding_invalid")
    moment = datetime.now(timezone.utc)
    if not completed_replay and (row["state"] not in {"bound", "preparing"} or row["decision_id"] is not None or sent):
        # Direct may mark the same bound event expired before this source read
        # finishes. It is still never sendable. Verify all original evidence
        # below before reporting that one narrow state race as deadline expiry.
        expired_unprepared = (row["state"] == "expired" and row["decision_id"] is None
                              and sent is None and request.deadline_at <= moment)
        if not expired_unprepared:
            raise ValueError("direct_qq_source_not_pending")
    message_time = datetime.fromisoformat(value["message_time"])
    if not -5 <= (first_seen - message_time).total_seconds() <= 60:
        raise ValueError("direct_qq_message_stale")
    if not completed_replay and first_seen > moment + timedelta(seconds=5):
        raise ValueError("direct_qq_source_expired")
    ref = value["event_evidence_ref"]
    if not isinstance(ref, str) or re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", ref) is None:
        raise ValueError("direct_qq_evidence_invalid")
    evidence_path = settings.evidence_dir / (ref.rsplit("/", 1)[1] + ".json")
    if evidence_path.is_symlink() or evidence_path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("direct_qq_evidence_invalid")
    raw = evidence_path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != ref.rsplit("/", 1)[1]:
        raise ValueError("direct_qq_evidence_changed")
    original = normalize_evidence(json.loads(raw), self_id=settings.self_member_id, groups=groups,
        private_peers=peers, received_at=first_seen)
    if original is None or any(original[key] != value[key] for key in (
            "event_id", "self_member_id", "group_id", "member_id", "message_id", "text", "message_time", "images", "own")):
        raise ValueError("direct_qq_protocol_message_changed")
    memory_path = Path(config.memory_db)
    if not memory_path.is_absolute() or memory_path.is_symlink() or not memory_path.is_file():
        raise ValueError("direct_qq_memory_invalid")
    with closing(sqlite3.connect(memory_path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("BEGIN")
        record = db.execute("SELECT * FROM qq_visual_observations WHERE record_id=? AND group_id=?",
            (row["source_record_id"], request.group_id)).fetchone()
        if record is None:
            raise ValueError("direct_qq_observation_missing")
        observation = QQVisualObservation.model_validate_json(record["record_json"])
        profile_row = db.execute("SELECT * FROM qq_visual_profiles WHERE record_id=? AND group_id=? AND member_id=?",
            (observation.profile_record_id, request.group_id, request.trigger_member_id)).fetchone()
    if profile_row is None:
        raise ValueError("direct_qq_observation_profile_missing")
    profile = QQVerifiedProfile.model_validate_json(profile_row["record_json"])
    if (observation.author_attribution != "qq_protocol_verified" or observation.message_id_source != "qq_metadata"
            or observation.record_id != row["source_record_id"] or observation.record_id != record["record_id"]
            or observation.group_id != request.group_id or observation.group_id != record["group_id"]
            or observation.member_id != row["member_id"] or observation.message_id != row["message_id"]
            or observation.member_id != record["member_id"] or observation.profile_record_id != record["profile_record_id"]
            or observation.capture_id != row["event_id"] or observation.capture_id != record["capture_id"]
            or observation.capture_item_id != row["event_id"] or observation.text != value["text"]
            or observation.capture_item_id != record["capture_item_id"] or observation.observed_at != message_time
            or observation.observed_at != datetime.fromisoformat(record["observed_at"])
            or ref not in observation.attribution_evidence
            or observation.model_dump(mode="json") != QQVisualObservation.model_validate(value.get("bound_observation")).model_dump(mode="json")):
        raise ValueError("direct_qq_observation_binding_invalid")
    if (profile.verification != "qq_protocol_verified" or profile.group_id != request.group_id
            or profile.member_id != request.trigger_member_id or profile.record_id != observation.profile_record_id
            or profile.record_id != profile_row["record_id"] or profile.capture_id != profile_row["capture_id"]
            or profile.observed_at != datetime.fromisoformat(profile_row["observed_at"])
            or ref not in profile.member_evidence):
        raise ValueError("direct_qq_observation_profile_invalid")
    if [item.image_index for item in observation.image_analysis] != list(range(1, len(original["images"]) + 1)):
        raise ValueError("direct_qq_image_binding_invalid")
    # Classify only a verified source's elapsed reply window as ordinary expiry.
    # Scope, hash, binding and future-clock errors retain their real failures,
    # including when validation itself finishes after the reply deadline.
    if not completed_replay and request.deadline_at <= datetime.now(timezone.utc):
        raise DirectQQSourceExpired("direct_qq_source_expired")
