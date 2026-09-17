"""Durable reply intents over immutable protocol observations, with revision checks."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from harness.qq.direct_store import canonical, utcnow


ACTIVE = {"queued", "observing", "preparing", "frozen"}
BINDING_FIELDS = ("request_id", "revision", "source_event_ids", "authority_anchor")
MAX_BATCH_SOURCES = 20


class ReplyRequestChanged(ValueError):
    def __init__(self, code="direct_request_revision_changed"):
        self.code = code
        super().__init__(code)


def initialize(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS qq_reply_requests(
            request_id TEXT PRIMARY KEY,run_id TEXT,group_id TEXT,self_id TEXT,
            authority_anchor TEXT UNIQUE,member_id TEXT,addressing TEXT,
            revision INTEGER,state TEXT,first_seen_at TEXT,deadline_at TEXT,source_event_ids TEXT);
        CREATE INDEX IF NOT EXISTS qq_reply_queue ON qq_reply_requests(run_id,state,first_seen_at);
        CREATE TABLE IF NOT EXISTS qq_reply_request_sources(
            event_id TEXT PRIMARY KEY,request_id TEXT,position INTEGER,
            UNIQUE(request_id,position));
        CREATE TABLE IF NOT EXISTS qq_reply_scheduler(run_id TEXT PRIMARY KEY,explicit_streak INTEGER);
    """)
    columns = {row[1] for row in db.execute("PRAGMA table_info(qq_reply_scheduler)")}
    for name in ("last_explicit_scope", "last_ambient_scope"):
        if name not in columns:
            db.execute(f"ALTER TABLE qq_reply_scheduler ADD COLUMN {name} TEXT")


def snapshot(row):
    value = dict(row)
    value["source_event_ids"] = json.loads(value["source_event_ids"])
    return value


def binding(value):
    return {key: value[key] for key in BINDING_FIELDS}


def validate_in_transaction(db, expected, *, group_id, self_id, anchor=None, require_pending=True):
    """Use an existing read/write transaction, including immediately before an outbox mutation."""
    row = db.execute("SELECT * FROM qq_reply_requests WHERE request_id=?", (expected["request_id"],)).fetchone()
    if row is None:
        raise ReplyRequestChanged("direct_request_binding_invalid")
    value = snapshot(row)
    if value["revision"] != expected["revision"]:
        raise ReplyRequestChanged()
    if (binding(value) != binding(expected) or value["group_id"] != group_id or value["self_id"] != self_id
            or (anchor is not None and value["authority_anchor"] != anchor)
            or not value["source_event_ids"] or value["source_event_ids"][0] != value["authority_anchor"]):
        raise ReplyRequestChanged("direct_request_binding_invalid")
    run = db.execute("SELECT active,self_id FROM qq_direct_runs WHERE run_id=?", (value["run_id"],)).fetchone()
    if (run is None or run["active"] != 1 or run["self_id"] != self_id
            or (require_pending and value["state"] not in ACTIVE)):
        raise ReplyRequestChanged("direct_request_binding_invalid")
    edges = db.execute("SELECT event_id FROM qq_reply_request_sources WHERE request_id=? ORDER BY position",
                       (value["request_id"],)).fetchall()
    if [item["event_id"] for item in edges] != value["source_event_ids"]:
        raise ReplyRequestChanged("direct_request_binding_invalid")
    for event_id in value["source_event_ids"]:
        event = db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (event_id,)).fetchone()
        if event is None:
            raise ReplyRequestChanged("direct_request_binding_invalid")
        source = json.loads(event["record_json"])
        if (event["run_id"] != value["run_id"] or event["group_id"] != group_id or event["self_id"] != self_id
                or source.get("historical") or source.get("own")
                or (value["addressing"] != "ambient" and event["member_id"] != value["member_id"])):
            raise ReplyRequestChanged("direct_request_binding_invalid")
        if event_id == value["authority_anchor"] and (
                event["received_at"] != value["first_seen_at"] or event["member_id"] != value["member_id"]
                or datetime.fromisoformat(value["deadline_at"]) !=
                    datetime.fromisoformat(event["received_at"]) + timedelta(seconds=60)):
            raise ReplyRequestChanged("direct_request_binding_invalid")
    return value


def validate_reply_request(state_db, request, *, require_pending=True, memory_config=None):
    """Read-only Runtime boundary. This neither creates a schema nor renews a TTL."""
    if getattr(request, "request_id", None) is None:
        return None
    expected = {name: getattr(request, name) for name in BINDING_FIELDS}
    path = Path(state_db)
    if not path.is_absolute() or path.is_symlink() or not path.is_file():
        raise ReplyRequestChanged("direct_request_binding_invalid")
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA query_only=ON")
        db.execute("BEGIN")
        value = validate_in_transaction(db, expected, group_id=request.group_id,
            self_id=request.self_member_id, anchor=request.trace.event_id, require_pending=require_pending)
    if (datetime.fromisoformat(value["first_seen_at"]) != request.trace.first_seen_at
            or request.deadline_at > datetime.fromisoformat(value["deadline_at"])
            or request.trigger_member_id != value["member_id"]):
        raise ReplyRequestChanged("direct_request_binding_invalid")
    if memory_config is not None:
        from harness.qq.direct_source import validate_direct_source
        sources, record_ids = [], []
        with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as db:
            db.row_factory = sqlite3.Row
            db.execute("BEGIN")
            rows = [dict(db.execute("SELECT * FROM qq_direct_events WHERE event_id=?", (event_id,)).fetchone())
                    for event_id in value["source_event_ids"]]
        for row in rows:
            event = json.loads(row["record_json"])
            observation = event.get("bound_observation")
            if not isinstance(observation, dict) or observation.get("record_id") != row["source_record_id"]:
                raise ReplyRequestChanged("direct_request_binding_invalid")
            if row["event_id"] != value["authority_anchor"]:
                # This is an immutable observation proof, not admission of a
                # second turn. Its state/TTL must not be treated as anchor authority.
                first_seen = datetime.fromisoformat(row["received_at"])
                source_request = request.model_copy(update={
                    **{name: None for name in BINDING_FIELDS},
                    "trigger_member_id": row["member_id"],
                    "deadline_at": first_seen + timedelta(seconds=60),
                    "trace": request.trace.model_copy(update={"event_id": row["event_id"], "first_seen_at": first_seen}),
                })
                validate_direct_source(memory_config, source_request, completed_replay=True)
            sources.append(observation)
            record_ids.append(row["source_record_id"])
        # Ingress can revise while individual immutable sources are read.
        validate_reply_request(state_db, request, require_pending=require_pending)
        value = value | {"sources": sources, "source_record_ids": record_ids}
    return value


class ReplyRequests:
    def __init__(self, store):
        self.store = store

    def ingest(self, event, *, addressing):
        """Join only admitted live observations; every new explicit @ is independent."""
        if event["historical"] or event["own"] or addressing not in {"ambient", "self_mention", "private"}:
            return None
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request_id FROM qq_reply_request_sources WHERE event_id=?", (event["event_id"],)).fetchone()
            if old:
                return snapshot(db.execute("SELECT * FROM qq_reply_requests WHERE request_id=?", (old[0],)).fetchone())
            run = db.execute("SELECT active,self_id FROM qq_direct_runs WHERE run_id=?", (event["run_id"],)).fetchone()
            if run is None or run["active"] != 1 or run["self_id"] != event["self_member_id"]:
                raise ReplyRequestChanged("direct_request_binding_invalid")
            candidates = db.execute("""SELECT * FROM qq_reply_requests WHERE run_id=? AND group_id=?
                AND state IN ('queued','observing','preparing','frozen') AND deadline_at>?
                ORDER BY first_seen_at DESC,request_id DESC""", (event["run_id"], event["group_id"], utcnow().isoformat())).fetchall()
            selected = None
            if addressing != "self_mention":
                for row in candidates:
                    candidate = snapshot(row)
                    if candidate["addressing"] != "ambient" and candidate["member_id"] == event["member_id"]:
                        selected = candidate
                        break
                if selected is None and addressing == "ambient":
                    selected = next((snapshot(row) for row in candidates if row["addressing"] == "ambient" and row["state"] == "queued"), None)
            if selected is not None and (len(selected["source_event_ids"]) >= MAX_BATCH_SOURCES or db.execute(
                    "SELECT 1 FROM qq_direct_sends WHERE event_id=? LIMIT 1", (selected["authority_anchor"],)).fetchone()):
                selected = None
            if selected is None:
                ident = "qq-reply-request:" + hashlib.sha256(canonical([event["run_id"], event["event_id"]]).encode()).hexdigest()[:32]
                selected = dict(request_id=ident, run_id=event["run_id"], group_id=event["group_id"],
                    self_id=event["self_member_id"], authority_anchor=event["event_id"], member_id=event["member_id"],
                    addressing=addressing, revision=1, state="queued", first_seen_at=event["received_at"],
                    deadline_at=(datetime.fromisoformat(event["received_at"]) + timedelta(seconds=60)).isoformat(),
                    source_event_ids=[event["event_id"]])
                db.execute("INSERT INTO qq_reply_requests VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", tuple(
                    canonical(value) if key == "source_event_ids" else value for key, value in selected.items()))
            else:
                selected["revision"] += 1
                selected["state"] = "queued"
                selected["source_event_ids"].append(event["event_id"])
                db.execute("UPDATE qq_reply_requests SET revision=?,state='queued',source_event_ids=? WHERE request_id=?",
                           (selected["revision"], canonical(selected["source_event_ids"]), selected["request_id"]))
                # An obsolete generated decision remains in the immutable decision
                # store. Only this unsent inbox intent is queued at its new revision.
                db.execute("UPDATE qq_direct_events SET state='received',decision_id=NULL,terminal_reason=NULL,terminal_stage=NULL,terminal_at=NULL WHERE event_id=?",
                           (selected["authority_anchor"],))
                db.execute("UPDATE qq_direct_events SET state='coalesced' WHERE event_id=?", (event["event_id"],))
            db.execute("INSERT INTO qq_reply_request_sources VALUES(?,?,?)",
                       (event["event_id"], selected["request_id"], len(selected["source_event_ids"]) - 1))
            return selected

    def claim(self, run_id, *, include_unbatched=False, now=None):
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            rows = db.execute("SELECT * FROM qq_reply_requests WHERE run_id=? AND state='queued' ORDER BY first_seen_at,request_id", (run_id,)).fetchall()
            now = (now or utcnow()).isoformat()
            candidates = [{"priority": (0 if row["addressing"] != "ambient" else 1)
                if row["deadline_at"] > now else 2, "deadline_at": row["deadline_at"],
                "event_id": row["authority_anchor"], "scope": row["group_id"], "request": row} for row in rows]
            if include_unbatched:
                # A source already attached to a request can never re-enter the
                # legacy lane, including after configuration changes or a crash.
                # Keep only each scope/category's oldest raw candidate in SQL.
                raw = db.execute("""WITH candidates AS (
                    SELECT e.event_id,e.group_id,e.received_at,
                        CASE WHEN json_extract(e.record_json,'$.historical')<>0
                                OR json_extract(e.record_json,'$.own')<>0 THEN 3
                            WHEN julianday(e.received_at)+60.0/86400<=julianday(?) THEN 2
                            WHEN json_extract(e.record_json,'$.protocol_addressing')
                                IN ('self_mention','private') THEN 0 ELSE 1 END AS priority
                    FROM qq_direct_events e LEFT JOIN qq_reply_request_sources s ON s.event_id=e.event_id
                    WHERE e.run_id=? AND e.state='received' AND s.event_id IS NULL
                ), ranked AS (SELECT *,row_number() OVER (
                    PARTITION BY group_id,priority ORDER BY received_at,event_id) AS position FROM candidates)
                SELECT * FROM ranked WHERE position=1""", (now, run_id)).fetchall()
                candidates.extend({"priority": row["priority"], "deadline_at":
                    (datetime.fromisoformat(row["received_at"]) + timedelta(seconds=60)).isoformat(),
                    "event_id": row["event_id"], "scope": row["group_id"], "request": None} for row in raw)
            if not candidates:
                return None
            candidates.sort(key=lambda item: (item["deadline_at"], item["event_id"]))
            explicit = [item for item in candidates if item["priority"] == 0]
            ambient = [item for item in candidates if item["priority"] == 1]
            cursor = db.execute("SELECT * FROM qq_reply_scheduler WHERE run_id=?", (run_id,)).fetchone()
            streak = cursor["explicit_streak"] if cursor else 0
            # Compatibility-only queues retain their established priority/FIFO.
            # With coalesced work present, raw and request lanes share the 3:1
            # quota and round-robin scopes; within a scope choose earliest TTL.
            if rows and (explicit or ambient):
                priority = 1 if ambient and (not explicit or streak >= 3) else 0
                eligible = ambient if priority else explicit
                heads = {item["scope"]: item for item in reversed(eligible)}
                scopes = sorted(heads)
                field = "last_ambient_scope" if priority else "last_explicit_scope"
                previous = cursor[field] if cursor else None
                scope = next((key for key in scopes if previous is None or key > previous), scopes[0])
                chosen = heads[scope]
            else:
                chosen = min(candidates, key=lambda item: (item["priority"], item["deadline_at"], item["event_id"]))
            if rows and chosen["priority"] < 2:
                streak = min(3, streak + 1) if chosen["priority"] == 0 else 0
                field = "last_explicit_scope" if chosen["priority"] == 0 else "last_ambient_scope"
                db.execute("INSERT INTO qq_reply_scheduler(run_id,explicit_streak) VALUES(?,?) "
                    "ON CONFLICT(run_id) DO UPDATE SET explicit_streak=excluded.explicit_streak", (run_id, streak))
                db.execute(f"UPDATE qq_reply_scheduler SET {field}=? WHERE run_id=?", (chosen["scope"], run_id))
            value = snapshot(chosen["request"]) if chosen["request"] is not None else None
            if value is not None:
                value["state"] = "observing"
                db.execute("UPDATE qq_reply_requests SET state='observing' WHERE request_id=?", (value["request_id"],))
        row = self.store.get(chosen["event_id"])
        if value is None:
            return row
        row["reply_request"] = value
        row["event"]["_reply_request"] = binding(value)
        return row

    def check(self, event, *, state=None):
        expected = event.get("_reply_request")
        if expected is None:
            return None
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE" if state else "BEGIN")
            value = validate_in_transaction(db, expected, group_id=event["group_id"],
                self_id=event["self_member_id"], anchor=event["event_id"])
            if state is not None:
                if state not in ACTIVE:
                    raise ValueError("invalid reply request state")
                db.execute("UPDATE qq_reply_requests SET state=? WHERE request_id=?", (state, value["request_id"]))
            return value

    def finish(self, event, state):
        expected = event.get("_reply_request")
        if expected is None:
            return
        with closing(self.store.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT revision FROM qq_reply_requests WHERE request_id=?", (expected["request_id"],)).fetchone()
            if row is None or row[0] != expected["revision"]:
                return
            db.execute("UPDATE qq_reply_requests SET state=? WHERE request_id=?", (state, expected["request_id"]))
            # These are still individually persisted observations; they simply
            # do not acquire independent send intents after this batch finishes.
            for source in expected["source_event_ids"][1:]:
                db.execute("UPDATE qq_direct_events SET state='observed' WHERE event_id=? AND state IN ('coalesced','bound','received')", (source,))
