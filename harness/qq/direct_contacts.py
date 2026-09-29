"""Production contact requests; the existing Direct actor remains the only sender.

Runtime owns immutable requests and revocation. Direct owns result rows and its
normal inbox/outbox. A contact is never inserted as a protocol observation.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json

from harness.qq.conversation import conversation_scope
from harness.qq.direct_store import canonical, normalize_evidence
from harness.qq.idle import QQIdleIntent
from harness.qq.memory import QQVisualObservation


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS qq_production_contact_requests(
            id TEXT PRIMARY KEY, authorized INTEGER NOT NULL, request_json TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS qq_production_contact_results(
            id TEXT PRIMARY KEY, result_json TEXT NOT NULL);
    """)


def request(db, identity):
    row = db.execute("SELECT authorized,request_json FROM qq_production_contact_requests WHERE id=?", (identity,)).fetchone()
    if row is None:
        raise ValueError("direct_contact_missing")
    return bool(row[0]), json.loads(row[1])


def authorize(settings, config, group_id):
    scope = conversation_scope(group_id)
    allowed = (scope["self_member_id"] == settings.self_member_id
        and scope["peer_id"] in config.allowed_private_peer_ids
        and any(p.peer_id == scope["peer_id"] and p.observe and p.send for p in settings.private_contacts)
        if scope["kind"] == "private" else group_id in config.allowed_group_ids
        and any(g.group_id == group_id and g.observe and g.send for g in settings.group_roster))
    if not settings.enabled or settings.self_member_id != config.self_member_id or not allowed:
        raise ValueError("direct_contact_scope_invalid")


def validate_source(settings, config, db, contact, *, now=None, allow_terminal=False):
    """Verify the original retained message, without giving it a new reply TTL."""
    from harness.qq.feedback import _readonly
    intent = QQIdleIntent.model_validate(contact["intent"])
    if intent.origin != "production":
        raise ValueError("direct_contact_origin_invalid")
    authorize(settings, config, intent.group_id)
    active, saved = request(db, intent.opportunity_ref)
    if saved != contact or not active:
        raise ValueError("direct_contact_revoked")
    now = now or datetime.now(timezone.utc)
    if not allow_terminal and not intent.created_at <= now < intent.deadline_at:
        raise ValueError("direct_contact_expired")
    run = db.execute("SELECT self_id,active FROM qq_direct_runs WHERE run_id=?", (intent.run_id,)).fetchone()
    row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (contact["source_event_id"],)).fetchone()
    if run is None or not run[1] or run[0] != intent.self_member_id or row is None:
        raise ValueError("direct_contact_source_missing")
    source = json.loads(row["record_json"])
    if (source.get("_production_contact") or source["group_id"] != intent.group_id
            or source["self_member_id"] != intent.self_member_id or not row["source_record_id"]):
        raise ValueError("direct_contact_source_invalid")
    ref = source["event_evidence_ref"]
    digest = ref.removeprefix("qq-protocol://sha256/")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise ValueError("direct_contact_evidence_invalid")
    path = settings.evidence_dir / (digest + ".json")
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError("direct_contact_evidence_invalid")
    raw = path.read_bytes()
    if hashlib.sha256(raw).hexdigest() != digest:
        raise ValueError("direct_contact_evidence_changed")
    scope = conversation_scope(intent.group_id)
    original = normalize_evidence(json.loads(raw), self_id=intent.self_member_id,
        groups={intent.group_id} if scope["kind"] == "group" else set(),
        private_peers={scope["peer_id"]} if scope["kind"] == "private" else set(),
        received_at=datetime.fromisoformat(source["received_at"]))
    if original is None or any(original[key] != source[key] for key in (
            "event_id", "group_id", "self_member_id", "member_id", "message_id", "text", "message_time", "images", "own")):
        raise ValueError("direct_contact_evidence_changed")
    with closing(_readonly(config.memory_db)) as memory:
        record = memory.execute("SELECT record_json FROM qq_visual_observations WHERE record_id=? AND group_id=?",
                                (row["source_record_id"], intent.group_id)).fetchone()
    observation = QQVisualObservation.model_validate_json(record[0]) if record else None
    if (observation is None or observation.model_dump(mode="json") != source.get("bound_observation")
            or observation.author_attribution != "qq_protocol_verified" or observation.capture_id != source["event_id"]
            or observation.capture_item_id != source["event_id"] or observation.text != source["text"]
            or observation.member_id != source["member_id"] or observation.message_id != source["message_id"]
            or ref not in observation.attribution_evidence):
        raise ValueError("direct_contact_observation_invalid")
    return intent, dict(row), source


def finish(db, identity, result):
    db.execute("INSERT INTO qq_production_contact_results VALUES(?,?) ON CONFLICT(id) DO UPDATE SET result_json=excluded.result_json",
               (identity, canonical(result)))


def internal_event(store, settings, config, contact):
    """Create an explicit internal delivery envelope, with no fake protocol ID."""
    with closing(store.connect()) as db, db:
        db.execute("BEGIN IMMEDIATE")
        intent, source_row, _ = validate_source(settings, config, db, contact)
        event_id = contact["event_id"]
        old = db.execute("SELECT event_id FROM qq_direct_events WHERE event_id=?", (event_id,)).fetchone()
        if old is None:
            event = {"event_id": event_id, "run_id": intent.run_id, "group_id": intent.group_id,
                "self_member_id": intent.self_member_id, "member_id": None, "message_id": None,
                "text": "", "images": [], "own": False, "historical": False,
                "received_at": intent.created_at.isoformat(), "message_time": None,
                "_production_contact": intent.opportunity_ref}
            db.execute("""INSERT INTO qq_direct_events(event_id,run_id,group_id,self_id,member_id,message_id,
                received_at,state,source_record_id,decision_id,record_json)
                VALUES(?,?,?,?,NULL,NULL,?,'preparing',?,NULL,?)""",
                (event_id, intent.run_id, intent.group_id, intent.self_member_id, event["received_at"],
                 source_row["source_record_id"], canonical(event)))
    return store.get(event_id)["event"]


def validate_shared(settings, config, shared, *, completed_replay=False):
    from harness.qq.feedback import _readonly
    with closing(_readonly(settings.state_db)) as db:
        active, contact = request(db, shared.idle_intent.opportunity_ref)
        intent, _, _ = validate_source(settings, config, db, contact, allow_terminal=completed_replay)
        row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (shared.trace.event_id,)).fetchone()
    if (shared.idle_intent != intent or shared.trace.event_id != contact["event_id"]
            or shared.trace.first_seen_at != intent.created_at or shared.deadline_at > intent.deadline_at
            or row is None or row["group_id"] != intent.group_id or row["run_id"] != intent.run_id
            or json.loads(row["record_json"]).get("_production_contact") != intent.opportunity_ref
            or (not completed_replay and (row["state"] != "preparing" or row["decision_id"] is not None))):
        raise ValueError("direct_contact_request_invalid")
