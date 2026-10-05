"""Transactional, disposable projections of the Runtime journal.

The journal is authoritative. This database contains seek metadata and reduced
state, never a second copy of historical payloads. A transaction is published
only after its journal prefix has been flushed. Audit results live elsewhere.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
import sqlite3
import threading
import time
from uuid import uuid4
from collections.abc import MutableMapping
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

from harness.runtime.models import AgencyState, RuntimeEvent, RuntimeEventType

log = logging.getLogger(__name__)
FORMAT_VERSION = 3


def encode(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)


class ProjectionMap(MutableMapping):
    """Dictionary-compatible, on-demand access to historical identities."""

    def __init__(self, store, namespace, *, tuples=False):
        self.store, self.namespace, self.tuples = store, namespace, tuples

    def __getitem__(self, key):
        with self.store.lock:
            row = self.store.db.execute(
                "SELECT value FROM kv WHERE namespace=? AND key=?",
                (self.namespace, encode(key)),
            ).fetchone()
        if row is None:
            raise KeyError(key)
        value = json.loads(row[0])
        return tuple(value) if self.tuples else value

    def __setitem__(self, key, value):
        with self.store.lock:
            self.store.db.execute(
                "INSERT OR REPLACE INTO kv VALUES (?,?,?)", (self.namespace, encode(key), encode(value))
            )

    def __delitem__(self, key):
        with self.store.lock:
            row = self.store.db.execute("DELETE FROM kv WHERE namespace=? AND key=?", (self.namespace, encode(key)))
            if not row.rowcount:
                raise KeyError(key)

    def __iter__(self):
        yield from (
            json.loads(row[0])
            for row in self.store.rows("SELECT key FROM kv WHERE namespace=? ORDER BY key", (self.namespace,))
        )

    def __len__(self):
        with self.store.lock:
            return self.store.db.execute("SELECT count(*) FROM kv WHERE namespace=?", (self.namespace,)).fetchone()[0]


class JournalValueMap(MutableMapping):
    """Bounded live overlay over journal-backed historical payloads."""

    def __init__(self, store, namespace, project=lambda record: record.data):
        self.store, self.index, self.project = store, store.mapping(namespace), project
        self.recent = OrderedDict()

    def __getitem__(self, key):
        if key in self.recent:
            return self.recent[key]
        return self.project(self.store.read(self.index[key]))

    def __setitem__(self, key, value):
        self.recent[key] = value
        self.recent.move_to_end(key)
        while len(self.recent) > 512:
            self.recent.popitem(last=False)

    def __delitem__(self, key):
        raise TypeError("journal-backed history cannot be deleted")

    def __iter__(self):
        yield from self.index
        yield from (key for key in self.recent if key not in self.index)

    def __len__(self):
        return len(self.index) + sum(key not in self.index for key in self.recent)


class RecoveryStore:
    def __init__(self, path, *, identity, legacy_checkpoint=None):
        self.path = Path(path)
        self.identity = identity
        self.legacy_checkpoint = legacy_checkpoint
        self.lock = threading.RLock()
        self.error = None
        self.fallback_reason = None
        self.mode = "strict"
        self.anchor = None
        self.committed_seq = 0
        self.committed_at = None
        self.last_commit = time.monotonic()
        self.journal = None
        self.persistent = str(path) != ":memory:"
        self.db = self._connect()
        self.projector = RecoveryProjector(self)
        try:
            row = self.db.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()
            if row:
                saved = json.loads(row[0])
                if not isinstance(saved, dict):
                    raise ValueError("recovery checkpoint must be an object")
                checksum = saved.pop("checksum", None)
                if checksum != hashlib.sha256(encode(saved).encode()).hexdigest():
                    raise ValueError("recovery checkpoint checksum mismatch")
                if saved["version"] != FORMAT_VERSION or saved["identity"] != identity:
                    self.fallback_reason = "incompatible recovery projection"
                else:
                    if (
                        not isinstance(saved["state"], dict)
                        or not isinstance(saved["cursor"], int)
                        or saved["cursor"] < 0
                    ):
                        raise ValueError("invalid recovery checkpoint state or cursor")
                    if datetime.fromisoformat(saved["created_at"]).tzinfo is None:
                        raise ValueError("recovery checkpoint time is not timezone aware")
                    self.anchor = saved["anchor"]
                    self.projector.state = saved["state"]
                    self.projector.cursor = saved["cursor"]
                    self.committed_seq = saved["cursor"]
                    self.committed_at = saved["created_at"]
        except (sqlite3.Error, ValueError, KeyError, TypeError) as exc:
            self.fallback_reason = f"invalid recovery checkpoint: {exc}"

    def _connect(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            db = sqlite3.connect(self.path, check_same_thread=False)
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA synchronous=FULL")
            self._schema(db)
            return db
        except sqlite3.Error as exc:
            try:
                db.close()
            except (UnboundLocalError, sqlite3.Error):
                pass
            self.error = str(exc)
            self.fallback_reason = "recovery database unavailable"
            if self.persistent and ("malformed" in str(exc) or "not a database" in str(exc)):
                # The Runtime holds its instance lock. Preserve the damaged
                # derived files for diagnosis, then bootstrap a fresh cache.
                try:
                    suffix = ".corrupt-" + uuid4().hex
                    for extension in ("", "-wal", "-shm"):
                        source = Path(str(self.path) + extension)
                        if source.exists():
                            os.replace(source, Path(str(source) + suffix))
                    db = sqlite3.connect(self.path, check_same_thread=False)
                    db.execute("PRAGMA journal_mode=WAL")
                    db.execute("PRAGMA synchronous=FULL")
                    self._schema(db)
                    return db
                except (OSError, sqlite3.Error):
                    log.exception("could not rebuild damaged recovery cache")
            self.persistent = False
            db = sqlite3.connect(":memory:", check_same_thread=False)
            self._schema(db)
            return db

    @staticmethod
    def _schema(db):
        db.executescript("""
            CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS kv(namespace TEXT,key TEXT,value TEXT NOT NULL,PRIMARY KEY(namespace,key));
            CREATE TABLE IF NOT EXISTS locations(seq INTEGER PRIMARY KEY,path TEXT,offset INTEGER,prev_hash TEXT);
            CREATE TABLE IF NOT EXISTS catalog(seq INTEGER PRIMARY KEY,type TEXT,event_id TEXT,checksum TEXT);
            CREATE INDEX IF NOT EXISTS catalog_type ON catalog(type,seq);
            CREATE TABLE IF NOT EXISTS events(id TEXT PRIMARY KEY,seq INTEGER,receive_seq INTEGER,type TEXT,source TEXT,pending INTEGER);
            CREATE INDEX IF NOT EXISTS events_pending ON events(pending,receive_seq);
            CREATE INDEX IF NOT EXISTS events_range ON events(type,source,receive_seq);
            CREATE INDEX IF NOT EXISTS events_type_receive ON events(type,receive_seq);
            CREATE TABLE IF NOT EXISTS ranges(type TEXT,source TEXT,first INTEGER,last INTEGER,PRIMARY KEY(type,source,first));
            CREATE TABLE IF NOT EXISTS actions(id TEXT PRIMARY KEY,seq INTEGER,proposal INTEGER,prepared INTEGER,
                executed INTEGER DEFAULT 0,committed INTEGER DEFAULT 0,terminal INTEGER DEFAULT 0,idem TEXT,work INTEGER DEFAULT 0);
            CREATE INDEX IF NOT EXISTS actions_idem ON actions(idem);
            CREATE INDEX IF NOT EXISTS actions_work ON actions(terminal,committed);
            CREATE TABLE IF NOT EXISTS commands(id TEXT PRIMARY KEY,intent INTEGER,result INTEGER,service TEXT);
            CREATE INDEX IF NOT EXISTS commands_pending ON commands(result,intent);
        """)
        if "work" not in {row[1] for row in db.execute("PRAGMA table_info(actions)")}:
            db.execute("ALTER TABLE actions ADD COLUMN work INTEGER DEFAULT 0")
        db.execute("CREATE INDEX IF NOT EXISTS actions_pending ON actions(work,seq)")
        db.commit()

    def reset(self):
        with self.lock:
            for table in ("kv", "locations", "catalog", "events", "ranges", "actions", "commands"):
                self.db.execute(f"DELETE FROM {table}")
            self.projector.state = {}
            self.projector.cursor = 0
            self.anchor = None
            self.committed_seq = 0
            self.mode = "strict"

    def mapping(self, namespace, *, tuples=False):
        return ProjectionMap(self, namespace, tuples=tuples)

    def rows(self, query, parameters=()):
        # Bounded pages, and no Python lock across a yield/journal read.
        with self.lock:
            cursor = self.db.execute(query, parameters)
        while True:
            with self.lock:
                page = cursor.fetchmany(256)
            if not page:
                return
            yield from page

    def locate(self, sequence):
        with self.lock:
            return self.db.execute(
                "SELECT seq,path,offset,prev_hash FROM locations WHERE seq<=? ORDER BY seq DESC LIMIT 1", (sequence,)
            ).fetchone()

    def observe(self, record, path=None, offset=None, end=None):
        try:
            self._observe(record, path, offset, end)
        except sqlite3.Error as exc:
            self._fallback_to_memory(exc)

    def _observe(self, record, path=None, offset=None, end=None):
        with self.lock:
            if path is not None:
                if offset == 0 or (record.record_seq - 1) % 128 == 0:
                    self.db.execute(
                        "INSERT OR REPLACE INTO locations VALUES (?,?,?,?)",
                        (record.record_seq, str(path), offset, record.prev_hash),
                    )
                self.anchor = dict(
                    seq=record.record_seq, checksum=record.checksum, path=str(path), offset=offset, end=end
                )
            if record.record_seq > self.projector.cursor:
                self.projector.apply(record)

    def _fallback_to_memory(self, error):
        """A failed derived write must not turn a durable append into failure."""
        if not self.persistent:
            raise error
        journal = self.journal
        with journal._lock, self.lock:
            log.error("recovery cache failed, rebuilding an in-memory projection: %s", error)
            self.error = str(error)
            self.fallback_reason = "recovery cache write failed; using memory projection"
            try:
                self.db.rollback()
                self.db.close()
            except sqlite3.Error:
                pass
            self.db = sqlite3.connect(":memory:", check_same_thread=False)
            self._schema(self.db)
            self.persistent = False
            self.reset()
            previous, sequence = "", 0
            for path in journal._segment_paths():
                with path.open("rb") as handle:
                    while sequence < journal._disk_last_record_seq:
                        offset = handle.tell()
                        chunk = handle.readline()
                        if not chunk:
                            break
                        record = journal._decode_record(chunk, path, offset)
                        journal._verify_record(record, sequence + 1, previous)
                        self._observe(record, path, offset, handle.tell())
                        previous, sequence = record.checksum, record.record_seq
            if sequence != journal._disk_last_record_seq:
                from harness.runtime.journal import JournalCorruptionError

                raise JournalCorruptionError("incomplete journal while rebuilding failed cache")
            for record in journal._buffer:
                self._observe(record)
            journal.recovered_records = sequence

    def sync(self):
        journal = self.journal
        if journal is None:
            return
        boundary = journal.last_record_seq
        while self.projector.cursor < boundary:
            for record in journal.records_after(
                self.projector.cursor, limit=min(500, boundary - self.projector.cursor)
            ):
                self.observe(record)

    def commit(self):
        try:
            return self._commit()
        except sqlite3.Error as exc:
            self._fallback_to_memory(exc)
            return False

    def _commit(self):
        journal = self.journal
        if journal is None:
            return False
        if not self.persistent:
            # Memory fallback supports current operation, but is not a restart
            # checkpoint and must not advertise a durable cursor advance.
            return False
        # All journal->projection operations take locks in this order.
        with journal._lock:
            self.sync()
            if journal.degraded or self.projector.cursor != journal._disk_last_record_seq:
                return False
            journal.flush()
            with self.lock:
                now = datetime.now(UTC).isoformat()
                saved = dict(
                    version=FORMAT_VERSION,
                    identity=self.identity,
                    anchor=self.anchor,
                    cursor=self.projector.cursor,
                    state=self.projector.state,
                    created_at=now,
                )
                saved["checksum"] = hashlib.sha256(encode(saved).encode()).hexdigest()
                self.db.execute("INSERT OR REPLACE INTO meta VALUES ('checkpoint',?)", (encode(saved),))
                self.db.commit()
                self.committed_seq = self.projector.cursor
                self.committed_at = now
                self.last_commit = time.monotonic()
                return True

    def read(self, seq):
        if not seq:
            return None
        record = self.journal.records_after(seq - 1, limit=1)[0]
        with self.lock:
            expected = self.db.execute("SELECT checksum FROM catalog WHERE seq=?", (seq,)).fetchone()
        if expected and record.checksum != expected[0]:
            from harness.runtime.journal import JournalCorruptionError

            raise JournalCorruptionError(f"recovery reference checksum mismatch at {seq}")
        return record

    def cursor_token(self, sequence=None):
        sequence = self.projector.cursor if sequence is None else sequence
        return {"seq": sequence, "checksum": self.read(sequence).checksum if sequence else ""}

    def command_intent_for_result(self, record):
        """Resolve old short-ID collisions using the receipt's request hash."""
        from harness.contracts.envelope import DomainCommandEnvelope

        candidates = []
        if record.idempotency_key:
            candidates.append(self.mapping("command_intents").get(record.idempotency_key))
        with self.lock:
            row = self.db.execute("SELECT intent FROM commands WHERE id=?", (record.command_id,)).fetchone()
        if row:
            candidates.append(row[0])
        result = record.data["result"]

        def matching(sequence):
            if not sequence or sequence >= record.record_seq:
                return None
            intent = self.read(sequence)
            envelope = DomainCommandEnvelope.model_validate(intent.data["envelope"])
            if (intent.command_id == record.command_id
                    and intent.data.get("service") == record.data.get("service")
                    and envelope.effective_request_hash() == result["request_hash"]):
                return intent
            return None

        for sequence in dict.fromkeys(candidates):
            if intent := matching(sequence):
                return intent
        # Older/reconciled records may omit the journal idempotency key. Only
        # inspect this event's prior intents; never pair with a newer collision.
        event_id = result.get("acknowledged_event_id") or record.event_id
        if event_id:
            for sequence, in self.rows(
                "SELECT seq FROM catalog WHERE type='domain_command_intent' AND event_id=? AND seq<? ORDER BY seq DESC",
                (event_id, record.record_seq),
            ):
                if intent := matching(sequence):
                    return intent
        raise ValueError("homeostasis recovery receipt has no matching command intent")

    def validated_cursor(self, token):
        if not isinstance(token, dict):
            return 0
        sequence = token.get("seq")
        if not isinstance(sequence, int) or not 0 < sequence <= self.projector.cursor:
            return 0
        return sequence if self.read(sequence).checksum == token.get("checksum") else 0

    def records(self, *types, after=0):
        self.sync()
        placeholders = ",".join("?" for _ in types)
        for (seq,) in self.rows(
            f"SELECT seq FROM catalog WHERE type IN ({placeholders}) AND seq>? AND seq<=? ORDER BY seq",
            (*types, after, self.projector.cursor),
        ):
            yield self.read(seq)

    def pending_events(self):
        self.sync()
        for (seq,) in self.rows("SELECT seq FROM events WHERE pending=1 ORDER BY receive_seq"):
            yield self.read(seq)

    def pending_commands(self):
        self.sync()
        for (seq,) in self.rows(
            "SELECT intent FROM commands WHERE result IS NULL AND intent IS NOT NULL ORDER BY intent"
        ):
            yield self.read(seq)

    def action_rows(self):
        self.sync()
        yield from self.rows(
            "SELECT id,seq,proposal,prepared,executed,committed FROM actions WHERE work=1 ORDER BY seq"
        )

    def historical_action(self, *, action_id=None, idempotency_key=None):
        self.sync()
        with self.lock:
            row = self.db.execute(
                "SELECT seq FROM actions WHERE " + ("id=?" if action_id else "idem=?") + " LIMIT 1",
                (action_id or idempotency_key,),
            ).fetchone()
        if row:
            from harness.runtime.models import ActionExecution

            return ActionExecution.model_validate(self.read(row[0]).data["action"])
        return None

    def status(self):
        with self.lock:
            pending = {
                "events": self.db.execute("SELECT count(*) FROM events WHERE pending=1").fetchone()[0],
                "commands": self.db.execute(
                    "SELECT count(*) FROM commands WHERE result IS NULL AND intent IS NOT NULL"
                ).fetchone()[0],
                "actions": self.db.execute("SELECT count(*) FROM actions WHERE work=1").fetchone()[0],
            }
        return dict(
            mode=self.mode,
            fallback_reason=self.fallback_reason,
            checkpoint_seq=self.committed_seq,
            checkpoint_at=self.committed_at,
            checkpoint_age_seconds=(
                max(0, (datetime.now(UTC) - datetime.fromisoformat(self.committed_at)).total_seconds())
                if self.committed_at
                else None
            ),
            projected_seq=self.projector.cursor,
            cache_lag=max(0, (self.journal.last_record_seq if self.journal else 0) - self.committed_seq),
            persistent=self.persistent,
            error=self.error,
            pending=pending,
        )

    def close(self):
        with self.lock:
            self.db.close()


class RecoveryProjector:
    def __init__(self, store):
        self.store = store
        self.cursor = 0
        self.state = {}

    def apply(self, record):
        if record.record_seq != self.cursor + 1:
            raise ValueError("noncontiguous recovery projection")
        s, db, d, kind = self.state, self.store.db, record.data, record.record_type
        seq, event_id = record.record_seq, record.event_id
        if seq == 1:
            s["journal_genesis"] = record.checksum
        m = self.store.mapping
        # Catalog only record kinds consumed by recovery/history lookups.
        if kind in {
            "event_accepted",
            "processing_started",
            "processing_completed",
            "domain_command_intent",
            "domain_command_result",
            "domain_command_late_result",
            "domain_command_expired",
            "reply_delivery_intent",
            "reply_delivery",
            "basic_terminal_task",
            "grounded_body_observed",
            "agency_reset",
            "persistent_goal_decision_bound",
            "grounded_decision",
            "body_experience_outcome",
            "body_memory_observed",
        } or isinstance(d.get("action"), dict):
            db.execute("INSERT INTO catalog VALUES (?,?,?,?)", (seq, kind, event_id, record.checksum))
        s["receive_seq"] = max(s.get("receive_seq", 0), record.receive_seq or 0)
        s["process_seq"] = max(s.get("process_seq", 0), record.process_seq or 0)
        s["last_runtime_time"] = record.recorded_at.isoformat()
        if kind == "lifecycle_transition":
            s["lifecycle"] = d.get("current")
        if kind == "event_accepted" and d.get("event"):
            event = RuntimeEvent.model_validate(d["event"])
            db.execute(
                "INSERT OR REPLACE INTO events VALUES (?,?,?,?,?,?)",
                (
                    event.event_id,
                    seq,
                    event.receive_seq,
                    event.event_type.value,
                    event.source,
                    int(event.event_id not in m("completed")),
                ),
            )
            # A later accepted identity may already be covered by a merged range.
            row = db.execute(
                "SELECT last FROM ranges WHERE type=? AND source=? AND first<=? ORDER BY first DESC LIMIT 1",
                (event.event_type.value, event.source, event.receive_seq),
            ).fetchone()
            if row and row[0] >= event.receive_seq:
                db.execute("UPDATE events SET pending=0 WHERE id=?", (event.event_id,))
            if event.event_type == RuntimeEventType.USER_MESSAGE:
                if event.receive_seq >= s.get("last_user_receive_seq", 0):
                    s["last_user_event"] = d["event"]
                    s["last_user_receive_seq"] = event.receive_seq
                    for field in ("session_id", "channel"):
                        value = str(event.payload.get(field) or "").strip()
                        if value:
                            s["last_user_" + field] = value
            if event.event_type == RuntimeEventType.QQ_TURN:
                m("qq_events")[event.event_id] = seq
                m("qq_completed").pop(event.event_id, None)
            if event.event_type == RuntimeEventType.QQ_FACT:
                from harness.homeostasis.models import QQHomeostaticFact

                fact = QQHomeostaticFact.model_validate(event.payload["qq_fact"])
                old = m("qq_facts").get(fact.fact_key)
                if old and old[1] != fact.fact_hash:
                    raise ValueError("qq_fact_journal_conflict")
                m("qq_facts")[fact.fact_key] = [seq, fact.fact_hash]
        elif kind == "processing_started" and event_id:
            m("starts")[event_id] = [
                str(d.get("event_type") or ""),
                str(d.get("source") or ""),
                int(d.get("first_receive_seq") or record.receive_seq or 0),
                int(d.get("last_receive_seq") or record.receive_seq or 0),
            ]
        elif kind == "event_dropped" or (
            kind == "processing_completed" and d.get("status") in {"completed", "ignored"}
        ):
            if event_id:
                m("completed")[event_id] = True
                db.execute("UPDATE events SET pending=0 WHERE id=?", (event_id,))
                m("cancelled").pop(event_id, None)
                bounds = m("starts").pop(event_id, None)
                if kind == "event_dropped":
                    first, last = d.get("sequence_range") or [record.receive_seq, record.receive_seq]
                    bounds = [
                        str(d.get("event_type") or ""),
                        str(d.get("source") or ""),
                        int(first or 0),
                        int(last or 0),
                    ]
                if bounds:
                    self._complete_range(*bounds)
        if kind == "processing_completed" and event_id in m("qq_events"):
            m("qq_completed")[event_id] = seq
        if kind == "domain_command_intent":
            envelope = d.get("envelope")
            if record.idempotency_key is not None and envelope:
                m("command_intents")[record.idempotency_key] = seq
            if (
                event_id
                and d.get("service") == "cognitive"
                and isinstance(envelope, dict)
                and envelope.get("command_type") == "PrepareResponse"
            ):
                _, count = m("prepare_intents").get(event_id, (0, 0))
                m("prepare_intents")[event_id] = [seq, count + 1]
            if record.command_id:
                db.execute(
                    "INSERT INTO commands(id,intent,service) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET intent=excluded.intent,service=excluded.service",
                    (record.command_id, seq, d.get("service")),
                )
        elif kind in {"domain_command_result", "domain_command_late_result", "domain_command_expired"}:
            if kind != "domain_command_expired":
                m("command_results")[record.command_id] = seq
            if record.command_id:
                db.execute(
                    "INSERT INTO commands(id,result) VALUES (?,?) ON CONFLICT(id) DO UPDATE SET result=excluded.result",
                    (record.command_id, seq),
                )
        action = d.get("action")
        if isinstance(action, dict):
            from harness.runtime.models import ActionExecution, ActionExecutionState

            parsed = ActionExecution.model_validate(action)
            if db.execute("SELECT 1 FROM actions WHERE id=?", (parsed.action_id,)).fetchone() is None:
                s["action_count"] = s.get("action_count", 0) + 1
            s["action_floor"] = max(
                s.get("action_floor", 0),
                s.get("action_count", 0),
                parsed.version_vector.runtime_action,
                int(d.get("_runtime_action_revision") or 0),
            )
            db.execute(
                "INSERT INTO actions(id,seq,terminal,idem) VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET seq=excluded.seq,terminal=excluded.terminal,idem=excluded.idem",
                (
                    parsed.action_id,
                    seq,
                    int(parsed.terminal and parsed.state != ActionExecutionState.UNKNOWN),
                    parsed.idempotency_key,
                ),
            )
            if kind == "action_proposed":
                db.execute("UPDATE actions SET proposal=? WHERE id=?", (seq, parsed.action_id))
                if parsed.goal_id:
                    if parsed.goal_id not in m("goals"):
                        s["goal_count"] = s.get("goal_count", 0) + 1
                    m("goals")[parsed.goal_id] = True
            if d.get("prepared"):
                db.execute("UPDATE actions SET prepared=? WHERE id=?", (seq, parsed.action_id))
            if kind == "action_execution_intent":
                db.execute("UPDATE actions SET executed=1 WHERE id=?", (parsed.action_id,))
        if kind == "action_interaction_committed" and d.get("action_id"):
            db.execute("UPDATE actions SET committed=1 WHERE id=?", (d["action_id"],))
        action_id = action.get("action_id") if isinstance(action, dict) else d.get("action_id")
        if action_id:
            db.execute(
                "UPDATE actions SET work=(terminal=0 OR (committed=0 AND prepared IS NOT NULL AND proposal IS NOT NULL)) WHERE id=?",
                (action_id,),
            )
        if kind in {"reply_delivery_intent", "reply_delivery"} and d.get("prepare_id"):
            key = d["prepare_id"]
            # Preserve the first observed receipt semantics.
            if kind == "reply_delivery_intent":
                m("delivery_intents")[key] = seq
            elif d.get("result") and key not in m("deliveries"):
                m("deliveries")[key] = seq
        if kind == "basic_terminal_task" and d.get("task", {}).get("task_id"):
            m("terminal_tasks")[d["task"]["task_id"]] = seq
            if d["task"].get("status") == "working":
                m("terminal_active")[d["task"]["task_id"]] = seq
            else:
                m("terminal_active").pop(d["task"]["task_id"], None)
        if kind == "grounded_decision" and event_id:
            m("research_decisions")[event_id] = seq
        if kind == "body_experience_outcome" and event_id and event_id not in m("research_outcomes"):
            m("research_outcomes")[event_id] = seq
        if kind == "persistent_goal_decision_bound":
            m("agency_bindings")[d["decision_id"]] = seq
        if kind == "grounded_body_observed":
            from harness.contracts.body import BodyStateSnapshot as LegacyBody
            from harness.embodiment.contracts import BodyStateSnapshot as ProviderBody
            from harness.research.grounded_agency.hashes import content_hash

            if d.get("manifest_hash") != self.store.identity.get("manifest"):
                raise ValueError("grounded body journal manifest drift")
            raw = d["body_state"]
            model = LegacyBody if raw.get("schema_version") == "body-state-v1" else ProviderBody
            body = model.model_validate(raw)
            old = s.get("grounded_body")
            if old:
                previous = model.model_validate(old)
                if (
                    body.body_instance_id != previous.body_instance_id
                    or body.revision < previous.revision
                    or body.captured_at < previous.captured_at
                    or body.revision == previous.revision
                    and content_hash(body) != content_hash(previous)
                ):
                    raise ValueError("grounded body journal progress is inconsistent")
            s["grounded_body"] = raw
            s["grounded_body_seq"] = seq
            s["grounded_episode_id"] = getattr(body, "episode_id", None) or s.get("grounded_episode_id")
            if body.energy.terminal:
                s["grounded_terminal_state"] = AgencyState.TERMINAL.value
                s["grounded_terminal_revision"] = body.revision
                s["grounded_terminal_episode"] = s.get("grounded_episode_id")
        if kind == "agency_reset":
            s["grounded_terminal_state"] = AgencyState.AVAILABLE.value
        if kind == "homeostatic_interoception_observed":
            for key in ("homeostatic_salience", "homeostatic_episode_signature"):
                if isinstance(d.get(key), str) and d[key]:
                    s[key] = d[key]
        if kind in {
            "homeostatic_marked_trigger_scheduled",
            "homeostatic_marked_trigger_reused",
            "homeostatic_marked_expression_opportunity",
        }:
            if d.get("homeostatic_episode_signature"):
                m("marked_attempts")[d["homeostatic_episode_signature"]] = record.recorded_at.isoformat()
        if kind == "autonomous_expression_cancelled" and d.get("cancelled_event_id"):
            m("cancelled")[d["cancelled_event_id"]] = True
        if kind == "homeostasis_continuation_deferred" and event_id and isinstance(d.get("homeostasis_revision"), int):
            m("continuations")[event_id] = d["homeostasis_revision"]
        elif kind == "homeostasis_continuation_consumed" and event_id:
            m("continuations").pop(event_id, None)
        if kind == "body_state_observed":
            s["body_episode_id"], s["body_revision"] = d.get("episode_id"), d.get("body_revision")
            if d.get("captured_at"):
                s["body_captured_at"] = d["captured_at"]
            if d.get("interoception"):
                s["interoception"] = d["interoception"]
        agency = s.get("agency_state", AgencyState.AVAILABLE.value)
        if kind == "agency_terminal":
            s["agency_state"] = AgencyState.TERMINAL.value
            s["terminal_episode_id"], s["terminal_body_revision"] = s.get("body_episode_id"), s.get("body_revision")
        elif kind == "agency_reset":
            s["agency_state"] = AgencyState.AVAILABLE.value
        elif kind == "body_monitor_degraded" and agency != AgencyState.TERMINAL:
            s["agency_state"] = AgencyState.DEGRADED.value
        elif kind == "body_state_applied" and not d.get("terminal") and agency != AgencyState.TERMINAL:
            s["agency_state"] = AgencyState.AVAILABLE.value
        self.cursor = seq
        checkpoint = self.store.legacy_checkpoint
        if (
            checkpoint is not None
            and checkpoint.journal_record_seq == seq
            and checkpoint.journal_hash == record.checksum
            and checkpoint.character_id == self.store.identity.get("character")
            and checkpoint.manifest_revision == record.manifest_revision
        ):
            s["receive_seq"] = max(s["receive_seq"], checkpoint.receive_seq)
            s["process_seq"] = max(s["process_seq"], checkpoint.process_seq)
            s["goal_floor"] = checkpoint.version_vector.runtime_goal
            s["action_floor"] = max(s.get("action_floor", 0), checkpoint.version_vector.runtime_action)
            s["agency_state"] = checkpoint.agency_state.value
            s["body_episode_id"], s["body_revision"] = checkpoint.body_episode_id, checkpoint.body_revision
            s["body_captured_at"] = checkpoint.body_captured_at.isoformat() if checkpoint.body_captured_at else None
            if checkpoint.interoceptive_state is not None:
                s["interoception"] = checkpoint.interoceptive_state
            if checkpoint.agency_state == AgencyState.TERMINAL:
                s["terminal_episode_id"], s["terminal_body_revision"] = (
                    checkpoint.body_episode_id,
                    checkpoint.body_revision,
                )

    def _complete_range(self, kind, source, first, last):
        db = self.store.db
        previous = db.execute(
            "SELECT first,last FROM ranges WHERE type=? AND source=? AND first<=? ORDER BY first DESC LIMIT 1",
            (kind, source, first),
        ).fetchone()
        if previous and previous[1] >= first - 1:
            first, last = min(first, previous[0]), max(last, previous[1])
        overlaps = db.execute(
            "SELECT first,last FROM ranges WHERE type=? AND source=? AND first>=? AND first<=?",
            (kind, source, first, last + 1),
        ).fetchall()
        if overlaps:
            last = max(last, max(row[1] for row in overlaps))
        db.execute(
            "DELETE FROM ranges WHERE type=? AND source=? AND first>=? AND first<=?", (kind, source, first, last + 1)
        )
        db.execute("INSERT INTO ranges VALUES (?,?,?,?)", (kind, source, first, last))
        db.execute(
            "UPDATE events SET pending=0 WHERE type=? AND source=? AND receive_seq BETWEEN ? AND ? AND pending=1",
            (kind, source, first, last),
        )
