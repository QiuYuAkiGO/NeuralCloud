"""QQ protocol identities and a durable inbox/outbox, separate from GUI evidence."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from uuid import uuid4
from urllib.parse import urlsplit

from harness.qq.conversation import private_scope_id
from harness.qq.telemetry import direct_termination


def utcnow():
    return datetime.now(timezone.utc)


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def numeric(value, *, signed=False):
    if type(value) not in {str, int}:
        raise ValueError("onebot_numeric_identity_invalid")
    value = str(value)
    if re.fullmatch(r"-?[0-9]{1,20}" if signed else r"[1-9][0-9]{0,19}", value) is None:
        raise ValueError("onebot_numeric_identity_invalid")
    return value


def _same_image_locator(left, right):
    if left == right:
        return True
    if not isinstance(left, str) or not isinstance(right, str):
        return False
    try:
        a, b = urlsplit(left), urlsplit(right)
        if (a.scheme != "https" or a.username or b.username or a.password or b.password
                or a.netloc != b.netloc or a.scheme != b.scheme or a.path != b.path or a.fragment != b.fragment
                or a.hostname not in {"multimedia.nt.qq.com", "multimedia.nt.qq.com.cn",
                                      "gchat.qpic.cn", "c2cpicdw.qpic.cn", "c2cpicdw2.qpic.cn"}):
            return False
        # Preserve the raw spelling/order of every other parameter. Only the
        # proven rotating access key is excluded, never the media identity.
        def stable(query):
            return [part for part in query.split("&") if part.split("=", 1)[0] != "rkey"]
        return stable(a.query) == stable(b.query)
    except ValueError:
        return False


def equivalent_image_segments(left, right):
    if left == right:
        return True
    if not isinstance(left, list) or not isinstance(right, list) or len(left) != len(right):
        return False
    return all(isinstance(a, dict) and isinstance(b, dict)
        and a.get("file") and a.get("file") == b.get("file")
        and {k: v for k, v in a.items() if k != "url"} == {k: v for k, v in b.items() if k != "url"}
        and _same_image_locator(a.get("url"), b.get("url")) for a, b in zip(left, right))


def message_parts(parts):
    if not isinstance(parts, list) or len(parts) > 200:
        raise ValueError("onebot_array_message_required")
    text, images = [], []
    for part in parts:
        if not isinstance(part, dict) or not isinstance(part.get("data"), dict):
            raise ValueError("onebot_message_segment_invalid")
        kind, data = part.get("type"), part["data"]
        if kind == "text":
            if not isinstance(data.get("text"), str):
                raise ValueError("onebot_text_invalid")
            text.append(data["text"])
        elif kind == "at":
            target = "全体成员" if data.get("qq") == "all" else numeric(data.get("qq"))
            text.append("@" + target + " ")
        elif kind == "image":
            if len(images) >= 8:
                raise ValueError("onebot_image_count_exceeded")
            images.append(data)
            text.append(f"[图片 {len(images)}]")
        elif kind == "face":
            text.append("[QQ表情]")
        elif kind == "reply":
            text.append("[引用消息 " + numeric(data.get("id"), signed=True) + "] ")
        else:
            text.append("[未解析的消息附件]")
    result = "".join(text)
    if not result.strip() or len(result) > 32768:
        raise ValueError("onebot_message_text_invalid")
    return result, images


def normalize_message(payload, *, self_id, groups, private_peers=(), received_at=None, history_peer_id=None):
    if self_id.startswith("qqbot:"):
        from harness.qq.official_events import normalize_official
        if history_peer_id is not None:
            raise ValueError("qqbot_private_scope_not_supported")
        return normalize_official(payload, self_id=self_id, groups=groups, private_peers=private_peers, received_at=received_at)
    if not isinstance(payload, dict):
        raise ValueError("onebot_message_event_invalid")
    if payload.get("post_type") not in {"message", "message_sent"} or payload.get("message_type") not in {"group", "private"}:
        return None
    if numeric(payload.get("self_id")) != self_id:
        raise ValueError("onebot_account_mismatch")
    member = numeric(payload.get("user_id"))
    if payload["message_type"] == "private":
        if payload.get("sub_type") != "friend" or payload.get("group_id") not in (None, 0):
            return None
        # NapCat own-message events do not identify their recipient. Only a
        # response to a peer-bound history request can establish that scope.
        if history_peer_id is None and member == self_id:
            return None
        peer = history_peer_id or member
        if peer not in private_peers or member not in {self_id, peer}:
            return None
        group = private_scope_id(self_id, peer)
        target = payload.get("target_id")
        # NapCat handleMsg adds target_id=peerUin at the WS push layer for
        # both incoming and outgoing private events. It is the conversation
        # counterpart, not the recipient; parseMessage/history omits it.
        if target is not None and numeric(target) != peer:
            raise ValueError("onebot_private_target_mismatch")
    else:
        if history_peer_id is not None:
            return None
        group = numeric(payload.get("group_id"))
        if group not in groups:
            return None
    sender = payload.get("sender")
    if not isinstance(sender, dict) or numeric(sender.get("user_id")) != member:
        raise ValueError("onebot_sender_mismatch")
    message_id = numeric(payload.get("message_id"), signed=True)
    timestamp = payload.get("time")
    if type(timestamp) not in {int, float} or not math.isfinite(timestamp):
        raise ValueError("onebot_message_time_invalid")
    try:
        message_time = datetime.fromtimestamp(timestamp, timezone.utc)
    except (ValueError, OverflowError, OSError):
        raise ValueError("onebot_message_time_invalid") from None
    moment = received_at or utcnow()
    if (message_time - moment).total_seconds() > 5:
        raise ValueError("onebot_message_time_invalid")
    text, images = message_parts(payload.get("message"))
    label = sender.get("card") or sender.get("nickname") or member
    if not isinstance(label, str) or not label.strip() or len(label) > 256:
        label = member
    identity = canonical([self_id, group, message_id])
    protocol_addressing = "ambient"
    if member != self_id:
        if payload["message_type"] == "private":
            protocol_addressing = "private"
        elif any(part.get("type") == "at" and str(part["data"].get("qq")) == self_id
                 for part in payload["message"]):
            protocol_addressing = "self_mention"
    return dict(event_id="qq-watch-event:" + hashlib.sha256(identity.encode()).hexdigest()[:32],
        self_member_id=self_id, group_id=group, member_id=member, message_id=message_id,
        display_name=label, text=text, images=images, received_at=moment.isoformat(),
        message_time=message_time.isoformat(), own=member == self_id,
        protocol_addressing=protocol_addressing)


def normalize_evidence(payload, *, self_id, groups, private_peers=(), received_at=None):
    """Read a raw event or an actor-created peer-bound history envelope."""
    if isinstance(payload, dict) and payload.get("action") == "get_friend_msg_history":
        if payload.get("self_id") != self_id or payload.get("peer_id") not in private_peers:
            raise ValueError("onebot_private_history_scope_invalid")
        message = dict(payload["result"])
        message.setdefault("post_type", "message")
        message.setdefault("self_id", int(self_id))
        return normalize_message(message, self_id=self_id, groups=groups, private_peers=private_peers,
            received_at=received_at, history_peer_id=payload["peer_id"])
    return normalize_message(payload, self_id=self_id, groups=groups, private_peers=private_peers,
        received_at=received_at)


def verified_protocol_addressing(event, *, evidence_dir, self_id, groups, private_peers=()):
    """Recheck the original wire evidence before granting a freshness exception.

    Normalized text, model addressing and the stored scheduling hint alone are
    insufficient: plain text can imitate an at segment. Legacy inbox records
    without the hint can still be verified against their retained wire event.
    """
    try:
        if event["own"] or event["historical"]:
            return "ambient"
        ref = event["event_evidence_ref"]
        if not isinstance(ref, str) or re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", ref) is None:
            return "ambient"
        digest = ref.rsplit("/", 1)[1]
        path = Path(evidence_dir) / (digest + ".json")
        if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
            return "ambient"
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != digest:
            return "ambient"
        original = normalize_evidence(json.loads(raw), self_id=self_id, groups=groups,
            private_peers=private_peers, received_at=datetime.fromisoformat(event["received_at"]))
        if original is None or any(original[key] != event[key] for key in (
                "event_id", "self_member_id", "group_id", "member_id", "message_id",
                "text", "message_time", "images", "own")):
            return "ambient"
        return original["protocol_addressing"]
    except (OSError, ValueError, KeyError, TypeError):
        return "ambient"


class DirectStore:
    def __init__(self, path, evidence_dir):
        self.path, self.evidence_dir = Path(path), Path(evidence_dir)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS qq_direct_runs(run_id TEXT PRIMARY KEY,self_id TEXT,started_at TEXT,active INTEGER);
                CREATE TABLE IF NOT EXISTS qq_direct_events(event_id TEXT PRIMARY KEY,run_id TEXT,group_id TEXT,
                    self_id TEXT,member_id TEXT,message_id TEXT,received_at TEXT,state TEXT,
                    source_record_id TEXT,decision_id TEXT,record_json TEXT,
                    UNIQUE(self_id,group_id,message_id));
                CREATE INDEX IF NOT EXISTS qq_direct_inbox ON qq_direct_events(run_id,state,received_at);
                CREATE TABLE IF NOT EXISTS qq_direct_sends(send_id TEXT PRIMARY KEY,event_id TEXT,decision_id TEXT,
                    group_id TEXT,segment_index INTEGER,status TEXT,message_id TEXT,receipt_id TEXT,record_json TEXT,
                    UNIQUE(decision_id,segment_index));
            """)
            columns = {row["name"] for row in db.execute("PRAGMA table_info(qq_direct_events)")}
            for column in ("terminal_reason", "terminal_stage", "terminal_at"):
                if column not in columns:
                    db.execute(f"ALTER TABLE qq_direct_events ADD COLUMN {column} TEXT")
            from harness.qq.reply_requests import initialize
            initialize(db)
            from harness.qq.direct_contacts import initialize as initialize_contacts
            initialize_contacts(db)
        from harness.qq.reply_requests import ReplyRequests
        self.reply_requests = ReplyRequests(self)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        return db

    def start_run(self, self_id):
        if isinstance(self_id, str) and self_id.startswith("qqbot:"):
            from harness.qq.official_identity import identity
            app_id = self_id.split(":")[1]
            if self_id != identity(app_id, "bot", app_id):
                raise ValueError("qqbot_self_identity_invalid")
        else:
            self_id = numeric(self_id)
        run_id = "qq-direct-run:" + uuid4().hex
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE qq_direct_runs SET active=0")
            for row in db.execute("SELECT send_id,status,record_json FROM qq_direct_sends WHERE status IN ('attempted','reserved')").fetchall():
                status = "unknown" if row["status"] == "attempted" else "cancelled"
                value = json.loads(row["record_json"]) | {"status": status}
                db.execute("UPDATE qq_direct_sends SET status=?,record_json=? WHERE send_id=?",
                           (status, canonical(value), row["send_id"]))
            db.execute("UPDATE qq_direct_events SET state='expired' WHERE state IN ('received','bound','preparing','ready')")
            db.execute("INSERT INTO qq_direct_runs VALUES(?,?,?,1)", (run_id, self_id, utcnow().isoformat()))
        return run_id

    def evidence(self, payload):
        data = canonical(payload).encode("utf-8")
        digest = hashlib.sha256(data).hexdigest()
        path = self.evidence_dir / (digest + ".json")
        if path.is_symlink():
            raise ValueError("onebot_evidence_invalid")
        if path.exists() and path.read_bytes() != data:
            raise ValueError("onebot_evidence_changed")
        if not path.exists():
            path.write_bytes(data)
        return "qq-protocol://sha256/" + digest

    def receive(self, event, *, run_id, evidence_ref, historical=False):
        event = event | {"run_id": run_id, "event_evidence_ref": evidence_ref,
                         "historical": bool(historical), "image_analysis": []}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            run = db.execute("SELECT self_id,active FROM qq_direct_runs WHERE run_id=?", (run_id,)).fetchone()
            if run is None or run["active"] != 1 or run["self_id"] != event["self_member_id"]:
                raise ValueError("onebot_receive_run_invalid")
            existing = db.execute("SELECT record_json FROM qq_direct_events WHERE event_id=?", (event["event_id"],)).fetchone()
            if existing:
                prior = json.loads(existing[0])
                if (any(prior[key] != event[key] for key in ("group_id", "self_member_id", "member_id", "message_id", "text", "message_time", "own"))
                        or not equivalent_image_segments(prior["images"], event["images"])):
                    raise ValueError("onebot_duplicate_identity_conflict")
                return False
            db.execute("""INSERT INTO qq_direct_events(event_id,run_id,group_id,self_id,member_id,message_id,
                received_at,state,source_record_id,decision_id,record_json)
                VALUES(?,?,?,?,?,?,?,'received',NULL,NULL,?)""",
                (event["event_id"], run_id, event["group_id"], event["self_member_id"], event["member_id"],
                 event["message_id"], event["received_at"], canonical(event)))
        return True

    def get(self, event_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (event_id,)).fetchone()
        if row is None:
            raise ValueError("onebot_event_missing")
        return dict(row) | {"event": json.loads(row["record_json"])}

    def pending(self, run_id, *, scheduler_mode="legacy"):
        # scheduler_mode remains a compatible argument. Persisted per-message
        # choices and existing requests determine the lanes, so changing a
        # rollout allowlist cannot detach or silently drop an accepted batch.
        return self.reply_requests.claim(run_id, include_unbatched=True, now=utcnow())

    def update(self, event_id, *, state=None, source_record_id=None, decision_id=None, observation=None,
               terminal_reason=None, terminal_stage=None, reply_request=None):
        terminal = None
        if terminal_reason is not None or terminal_stage is not None:
            terminal = direct_termination(terminal_reason, terminal_stage)
        with closing(self.connect()) as db, db:
            if reply_request is not None:
                db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (event_id,)).fetchone()
            if row is None:
                raise ValueError("onebot_event_missing")
            if reply_request is not None:
                from harness.qq.reply_requests import validate_in_transaction
                validate_in_transaction(db, reply_request, group_id=row["group_id"], self_id=row["self_id"], anchor=event_id)
            value = json.loads(row["record_json"])
            if observation is not None:
                value["bound_observation"] = observation
            db.execute("""UPDATE qq_direct_events SET state=?,source_record_id=?,decision_id=?,record_json=?,
                terminal_reason=?,terminal_stage=?,terminal_at=? WHERE event_id=?""",
                (state or row["state"], source_record_id or row["source_record_id"], decision_id or row["decision_id"],
                 canonical(value), terminal["reason"] if terminal else row["terminal_reason"],
                 terminal["stage"] if terminal else row["terminal_stage"],
                 utcnow().isoformat() if terminal else row["terminal_at"], event_id))

    def newer(self, event, *, same_member_only=False, excluded_event_ids=()):
        # A bound explicit request survives other members' chatter, but its
        # author's next message may cancel or correct it and still supersedes it.
        member_filter = " AND member_id=?" if same_member_only else ""
        params = (event["run_id"], event["group_id"], event["received_at"])
        if same_member_only:
            params += (event["member_id"],)
        if excluded_event_ids:
            if len(excluded_event_ids) > 128:
                raise ValueError("onebot_source_count_invalid")
            member_filter += " AND event_id NOT IN (" + ",".join("?" for _ in excluded_event_ids) + ")"
            params += tuple(excluded_event_ids)
        with closing(self.connect()) as db:
            return db.execute("SELECT 1 FROM qq_direct_events WHERE run_id=? AND group_id=? "
                "AND member_id<>self_id AND received_at>? AND json_extract(record_json,'$.historical')=0"
                + member_filter + " LIMIT 1", params).fetchone() is not None

    def uncertain(self, *, untransmitted_send=None):
        with closing(self.connect()) as db:
            excluded = ""
            if untransmitted_send is not None:
                # Only the owning sender's immediate pre-call check may omit its
                # exact durable intent. An ACK, changed row or any other intent
                # remains uncertain; this does not authorize retry or recovery.
                row = db.execute("SELECT * FROM qq_direct_sends WHERE send_id=?",
                                 (untransmitted_send.get("send_id"),)).fetchone()
                if (row is None or row["status"] != "attempted" or row["message_id"] is not None
                        or row["receipt_id"] is not None or json.loads(row["record_json"]) != untransmitted_send):
                    return True
                excluded = row["send_id"]
            return db.execute("SELECT 1 FROM qq_direct_sends WHERE status IN ('attempted','unknown') "
                              "AND send_id<>? LIMIT 1", (excluded,)).fetchone() is not None

    def reserve(self, event, decision, index, text):
        from harness.qq.decision import effective_reply_segments, sendable_speak_outcome

        segments = effective_reply_segments(decision)
        if (type(index) is not int or not 0 <= index < len(segments) or text != segments[index]
                or decision.group_id != event["group_id"] or decision.self_member_id != event["self_member_id"]
                or not decision.new_decision or decision.replayed or not sendable_speak_outcome(decision.outcome)):
            raise ValueError("onebot_send_plan_invalid")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            if event.get("_reply_request") is not None:
                from harness.qq.reply_requests import validate_in_transaction
                validate_in_transaction(db, event["_reply_request"], group_id=event["group_id"],
                    self_id=event["self_member_id"], anchor=event["event_id"])
            row = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (event["event_id"],)).fetchone()
            run = db.execute("SELECT * FROM qq_direct_runs WHERE run_id=?", (event["run_id"],)).fetchone()
            if (row is None or run is None or run["active"] != 1 or run["self_id"] != event["self_member_id"]
                    or row["run_id"] != event["run_id"] or row["group_id"] != event["group_id"]
                    or row["self_id"] != event["self_member_id"] or row["state"] != "ready"
                    or row["decision_id"] != decision.decision_id or not row["source_record_id"]):
                raise ValueError("onebot_send_source_invalid")
            stored = json.loads(row["record_json"])
            if (stored["historical"] or stored["own"] or any(stored[key] != event[key] for key in (
                    "event_id", "run_id", "self_member_id", "group_id", "member_id", "message_id", "text", "received_at"))
                    or row["source_record_id"] not in decision.source_record_ids):
                raise ValueError("onebot_send_source_invalid")
            if db.execute("SELECT 1 FROM qq_direct_sends WHERE status IN ('attempted','unknown') LIMIT 1").fetchone():
                raise ValueError("onebot_send_uncertain")
            prior = db.execute("SELECT decision_id,segment_index,status FROM qq_direct_sends WHERE event_id=? ORDER BY segment_index",
                               (event["event_id"],)).fetchall()
            if len(prior) != index or any(item["decision_id"] != decision.decision_id or item["segment_index"] != offset
                    or item["status"] != "sent" for offset, item in enumerate(prior)):
                raise ValueError("onebot_send_prefix_invalid")
            value = dict(send_id="qq-direct-send:" + uuid4().hex, event_id=event["event_id"],
                decision_id=decision.decision_id, group_id=event["group_id"], self_member_id=event["self_member_id"],
                segment_index=index, text=text, source_record_id=row["source_record_id"],
                received_at=event["received_at"], status="reserved", message_id=None, receipt_id=None)
            if event.get("_reply_request") is not None:
                value["reply_request"] = event["_reply_request"]
            db.execute("INSERT INTO qq_direct_sends VALUES(?,?,?,?,?,'reserved',NULL,NULL,?)",
                (value["send_id"], event["event_id"], decision.decision_id, event["group_id"], index, canonical(value)))
        return value

    def update_send(self, value, status, **changes):
        value = value | changes | {"status": status}
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_direct_sends WHERE send_id=?", (value["send_id"],)).fetchone()
            if row is None:
                raise ValueError("onebot_send_missing")
            stored = json.loads(row["record_json"])
            if stored.get("reply_request") != value.get("reply_request"):
                raise ValueError("onebot_send_transition_invalid")
            if status == "attempted" and row["status"] == "reserved" and stored.get("reply_request") is not None:
                from harness.qq.reply_requests import validate_in_transaction
                validate_in_transaction(db, stored["reply_request"], group_id=stored["group_id"],
                    self_id=stored["self_member_id"], anchor=stored["event_id"])
            allowed = {"reserved": {"attempted", "cancelled"}, "attempted": {"attempted", "sent", "unknown"},
                       "sent": {"sent"}, "unknown": {"unknown"}, "cancelled": {"cancelled"}}
            if status not in allowed.get(row["status"], set()) or any(stored[key] != value[key] for key in (
                    "send_id", "event_id", "decision_id", "group_id", "self_member_id", "segment_index", "text", "source_record_id")):
                raise ValueError("onebot_send_transition_invalid")
            if status == "sent" and (not value.get("message_id") or not value.get("receipt_id") or not value.get("delivered_at")):
                raise ValueError("onebot_send_receipt_required")
            if row["status"] in {"sent", "unknown", "cancelled"} and stored != value:
                raise ValueError("onebot_send_terminal_immutable")
            db.execute("UPDATE qq_direct_sends SET status=?,message_id=?,receipt_id=?,record_json=? WHERE send_id=?",
                (status, value.get("message_id"), value.get("receipt_id"), canonical(value), value["send_id"]))
        return value

    def cooldown(self, group_id, seconds, *, exclude_events=(), text=None):
        with closing(self.connect()) as db:
            # A task acknowledgement must not rate-limit the user's next
            # conversational turn. Task notices have their own bounded queue.
            rows = db.execute("""SELECT record_json FROM qq_direct_sends WHERE group_id=? AND status='sent'
                AND json_extract(record_json,'$.task_notice_id') IS NULL""", (group_id,))
            records = [json.loads(row[0]) for row in rows]
            moments = [datetime.fromisoformat(row["delivered_at"]) for row in records
                       if row["event_id"] not in exclude_events and (text is None or row.get("text") == text)]
        return bool(moments and (utcnow() - max(moments)).total_seconds() < seconds)

    def cancel_untransmitted(self, value, *, reason="direct_send_budget_insufficient"):
        """Cancel an intent while its owning sender has not called the protocol.

        Only the uninterrupted pre-call path may use this API. Once a protocol
        call starts, even a timeout without an ACK must remain uncertain.
        """
        if reason not in {"direct_send_budget_insufficient", "direct_owner_unavailable", "direct_group_read_only",
                "direct_delivery_unknown", "direct_newer_context", "direct_request_revision_changed",
                "direct_request_binding_invalid"}:
            raise ValueError("onebot_send_pretransmission_reason_invalid")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_direct_sends WHERE send_id=?", (value["send_id"],)).fetchone()
            if (row is None or row["status"] != "attempted" or row["message_id"] is not None
                    or row["receipt_id"] is not None or json.loads(row["record_json"]) != value):
                raise ValueError("onebot_send_pretransmission_cancel_invalid")
            result = value | {"status": "cancelled", "cancelled_at": utcnow().isoformat(),
                "cancel_reason": reason, "cancel_phase": "before_protocol_call"}
            db.execute("UPDATE qq_direct_sends SET status='cancelled',record_json=? WHERE send_id=?",
                       (canonical(result), value["send_id"]))
        return result

    def outstanding_reconciliation(self, *, limit=8, include_unidentified=False):
        """Return uncertain sends for read-only proof recovery."""
        if type(limit) is not int or not 1 <= limit <= 32:
            raise ValueError("onebot_reconciliation_limit_invalid")
        with closing(self.connect()) as db:
            rows = db.execute("""SELECT * FROM qq_direct_sends WHERE status IN ('attempted','unknown')
                AND json_extract(record_json,'$.task_notice_id') IS NULL
                AND (message_id IS NOT NULL OR ?) ORDER BY rowid LIMIT ?""", (include_unidentified, limit)).fetchall()
            result = []
            for row in rows:
                value = json.loads(row["record_json"])
                event = db.execute("SELECT self_id FROM qq_direct_events WHERE event_id=?", (row["event_id"],)).fetchone()
                if (event is None or value["self_member_id"] != event["self_id"]
                        or any(value[key] != row[key] for key in ("send_id", "event_id", "decision_id", "group_id", "segment_index", "status", "message_id", "receipt_id"))):
                    raise ValueError("onebot_reconciliation_source_invalid")
                if value["message_id"] is not None:
                    numeric(value["message_id"], signed=True)
                result.append(value)
        return result

    def _protocol_evidence(self, ref):
        if not isinstance(ref, str) or re.fullmatch(r"qq-protocol://sha256/[0-9a-f]{64}", ref) is None:
            raise ValueError("onebot_reconciliation_evidence_invalid")
        digest = ref.rsplit("/", 1)[-1]
        path = self.evidence_dir / (digest + ".json")
        if path.is_symlink() or path.stat().st_size > 1024 * 1024:
            raise ValueError("onebot_reconciliation_evidence_invalid")
        data = path.read_bytes()
        if hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("onebot_reconciliation_evidence_invalid")
        return json.loads(data)

    def reconciliation_echo(self, value, *, db=None):
        """Find one exact, live own echo near the original group-send attempt.

        This only proposes an identity. A separate protocol read must verify it
        before binding, and no deadline or sending authorization is renewed.
        """
        if db is None:
            with closing(self.connect()) as connection:
                return self.reconciliation_echo(value, db=connection)
        if value.get("message_id") is not None or value.get("status") not in {"unknown", "attempted"}:
            return None
        # Private own pushes do not establish the recipient on this bridge.
        if not value["group_id"].isdigit() or not value.get("attempted_at"):
            return None
        attempted = datetime.fromisoformat(value["attempted_at"])
        if attempted.tzinfo is None:
            raise ValueError("onebot_reconciliation_source_invalid")
        rows = db.execute("""SELECT record_json FROM qq_direct_events
            WHERE self_id=? AND group_id=? AND member_id=self_id
            AND json_extract(record_json,'$.own')=1 AND json_extract(record_json,'$.historical')=0
            AND json_extract(record_json,'$.text')=?
            AND julianday(received_at) BETWEEN julianday(?) AND julianday(?)
            AND julianday(json_extract(record_json,'$.message_time')) BETWEEN julianday(?) AND julianday(?)
            LIMIT 2""", (value["self_member_id"], value["group_id"], value["text"],
                attempted.isoformat(), (attempted + timedelta(seconds=15)).isoformat(),
                attempted.replace(microsecond=0).isoformat(), (attempted + timedelta(seconds=10)).isoformat())).fetchall()
        if len(rows) != 1:
            return None
        event = json.loads(rows[0]["record_json"])
        payload = self._protocol_evidence(event["event_evidence_ref"])
        confirmed = normalize_message(payload, self_id=value["self_member_id"], groups=[value["group_id"]],
                                      received_at=datetime.fromisoformat(event["received_at"]))
        if (confirmed is None or confirmed["images"] or not confirmed["own"]
                or any(part.get("type") != "text" for part in payload.get("message", []))
                or any(confirmed[key] != event[key] for key in confirmed)
                or confirmed["text"] != value["text"]):
            raise ValueError("onebot_reconciliation_echo_invalid")
        used = db.execute("""SELECT 1 FROM qq_direct_sends WHERE group_id=? AND message_id=?
            AND json_extract(record_json,'$.self_member_id')=? LIMIT 1""",
            (value["group_id"], event["message_id"], value["self_member_id"])).fetchone()
        return None if used else event

    def bind_reconciliation_echo(self, value, *, event_id, proof_ref):
        """Bind an exact own echo only after independently verified protocol proof."""
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_direct_sends WHERE send_id=?", (value["send_id"],)).fetchone()
            if (row is None or row["status"] not in {"attempted", "unknown"} or row["message_id"] is not None
                    or json.loads(row["record_json"]) != value):
                raise ValueError("onebot_reconciliation_binding_invalid")
            echo = self.reconciliation_echo(value, db=db)
            if echo is None or echo["event_id"] != event_id:
                raise ValueError("onebot_reconciliation_echo_invalid")
            proof = self._protocol_evidence(proof_ref)
            message = proof.get("result", {})
            if (proof.get("action") not in {"get_msg", "get_group_msg_history"}
                    or proof.get("group_id") != value["group_id"]
                    or numeric(message.get("group_id")) != value["group_id"]
                    or numeric(message.get("message_id"), signed=True) != echo["message_id"]
                    or numeric(message.get("sender", {}).get("user_id")) != value["self_member_id"]
                    or message_parts(message.get("message")) != (value["text"], [])
                    or any(part.get("type") != "text" for part in message["message"])):
                raise ValueError("onebot_reconciliation_proof_invalid")
            result = value | {"message_id": echo["message_id"],
                "recovery_echo_ref": echo["event_evidence_ref"], "recovery_proof_ref": proof_ref}
            db.execute("UPDATE qq_direct_sends SET message_id=?,record_json=? WHERE send_id=?",
                       (echo["message_id"], canonical(result), value["send_id"]))
        return result

    def confirm_reconciled(self, value, *, receipt_id, delivered_at):
        """Commit externally verified receipt proof; this never authorizes sending."""
        stamp = datetime.fromisoformat(delivered_at) if isinstance(delivered_at, str) else delivered_at
        if (not isinstance(stamp, datetime) or stamp.tzinfo is None or not isinstance(receipt_id, str)
                or re.fullmatch(r"qq-segment-delivery:[0-9a-f]{32}", receipt_id) is None):
            raise ValueError("onebot_reconciliation_receipt_invalid")
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_direct_sends WHERE send_id=?", (value["send_id"],)).fetchone()
            if row is None or row["status"] not in {"attempted", "unknown", "sent"} or not row["message_id"]:
                raise ValueError("onebot_reconciliation_source_invalid")
            stored = json.loads(row["record_json"])
            if any(stored[key] != value[key] for key in ("send_id", "event_id", "decision_id", "group_id",
                    "self_member_id", "segment_index", "text", "source_record_id", "message_id")):
                raise ValueError("onebot_reconciliation_binding_invalid")
            if row["status"] == "sent":
                if stored["receipt_id"] != receipt_id or datetime.fromisoformat(stored["delivered_at"]) != stamp:
                    raise ValueError("onebot_reconciliation_receipt_conflict")
                return stored
            result = stored | {"status": "sent", "receipt_id": receipt_id, "delivered_at": stamp.isoformat()}
            db.execute("UPDATE qq_direct_sends SET status='sent',receipt_id=?,record_json=? WHERE send_id=?",
                       (receipt_id, canonical(result), value["send_id"]))
        return result

    def counts(self):
        with closing(self.connect()) as db:
            return {name: dict(db.execute(f"SELECT {field},COUNT(*) FROM {table} GROUP BY {field}"))
                for name, table, field in (("events", "qq_direct_events", "state"), ("sends", "qq_direct_sends", "status"))}
