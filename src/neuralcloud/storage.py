from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid

from .contracts import Correction, SourceEvent, canonical, digest
from .ownership import Ownership

SCHEMA = """
BEGIN IMMEDIATE;
CREATE TABLE metadata(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE receipts(source TEXT, event TEXT, hash TEXT NOT NULL, seq INTEGER NOT NULL,
 record_id TEXT NOT NULL, revision INTEGER NOT NULL, PRIMARY KEY(source,event));
CREATE TABLE records(id TEXT PRIMARY KEY, revision INTEGER NOT NULL, status TEXT NOT NULL,
 occurred REAL NOT NULL, kind TEXT NOT NULL, pinned INTEGER NOT NULL, batch_id TEXT);
CREATE TABLE revisions(record_id TEXT, revision INTEGER, payload TEXT NOT NULL,
 hash TEXT NOT NULL, PRIMARY KEY(record_id,revision));
CREATE TABLE sources(record_id TEXT, source_id TEXT, PRIMARY KEY(record_id,source_id),
 FOREIGN KEY(record_id) REFERENCES records(id), FOREIGN KEY(source_id) REFERENCES records(id));
CREATE INDEX sources_reverse ON sources(source_id,record_id);
CREATE TABLE changes(seq INTEGER PRIMARY KEY AUTOINCREMENT, record_id TEXT NOT NULL,
 revision INTEGER NOT NULL, operation TEXT NOT NULL, received_at REAL NOT NULL);
CREATE TABLE protections(record_id TEXT, owner TEXT, PRIMARY KEY(record_id,owner),
 FOREIGN KEY(record_id) REFERENCES records(id));
CREATE TABLE archive_batches(id TEXT PRIMARY KEY, state TEXT NOT NULL, plan TEXT NOT NULL,
 envelope_hash TEXT, verification TEXT);
CREATE VIRTUAL TABLE search USING fts5(record_id UNINDEXED, terms, tokenize='unicode61');
PRAGMA user_version=1;
COMMIT;
"""


class Refusal(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def tokens(text: str) -> list[str]:
    """Version 1: Unicode words plus CJK unigrams/bigrams for short names.

    Indexing is derived, bounded per record and independent of a vector model.
    Phrase ranking is deliberately not presented as semantic search.
    """
    found = set(re.findall(r"[a-z0-9_]+", text.casefold()))
    for run in re.findall(r"[\u3400-\u9fff]+", text):
        found.update(run)
        found.update(run[i : i + 2] for i in range(len(run) - 1))
    return sorted(found)


def initialize(path: Path, character_id: str, *, migration_marker=None):
    """Explicit maintenance operation; normal startup never creates/migrates a DB."""
    if not character_id or len(character_id) > 128:
        raise ValueError("invalid character_id")
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb"):
        pass
    db = sqlite3.connect(path)
    try:
        db.executescript(SCHEMA)
        db.executemany(
            "INSERT INTO metadata VALUES(?,?)",
            [
                ("character_id", character_id),
                ("format", "1"),
                ("index_format", "record-rowid-v1"),
                ("database_id", str(uuid.uuid4())),
            ],
        )
        if migration_marker:
            db.executemany(
                "INSERT INTO metadata VALUES(?,?)",
                [("migration." + key, value) for key, value in migration_marker.items()],
            )
        db.commit()
    finally:
        db.close()


class MemoryStore:
    def __init__(self, path: Path, character_id: str, *, maintenance=False):
        started = time.perf_counter()
        if not path.is_file():
            raise Refusal("INITIALIZATION_REQUIRED")
        self.owner = Ownership(path.with_suffix(path.suffix + ".owner"))
        self.lock = threading.RLock()
        self.db = None
        try:
            self.db = sqlite3.connect(
                path.resolve().as_uri() + "?mode=rw", uri=True, isolation_level=None, check_same_thread=False, timeout=2
            )
            self.db.row_factory = sqlite3.Row
            if self.db.execute("PRAGMA user_version").fetchone()[0] != 1:
                raise Refusal("EXPLICIT_MIGRATION_REQUIRED")
            row = self.db.execute("SELECT value FROM metadata WHERE key='character_id'").fetchone()
            if not row or row[0] != character_id:
                raise Refusal("CHARACTER_MISMATCH")
            migration = self.db.execute("SELECT value FROM metadata WHERE key='migration.stage'").fetchone()
            if migration and migration[0] != "COMPLETE" and not maintenance:
                raise Refusal("MIGRATION_NOT_COMPLETE")
            index_format = self.db.execute("SELECT value FROM metadata WHERE key='index_format'").fetchone()
            if (not index_format or index_format[0] != "record-rowid-v1") and not maintenance:
                raise Refusal("EXPLICIT_INDEX_MAINTENANCE_REQUIRED")
            for setting in (
                "journal_mode=WAL",
                "synchronous=FULL",
                "foreign_keys=ON",
                "busy_timeout=2000",
                "cache_size=-8192",
                "mmap_size=0",
                "temp_store=FILE",
                "wal_autocheckpoint=256",
            ):
                self.db.execute("PRAGMA " + setting)
            self.character_id = character_id
            self.path = path
            self.startup_ms = (time.perf_counter() - started) * 1000
        except BaseException:
            self.close()
            raise

    def close(self):
        if self.db is not None:
            self.db.close()
            self.db = None
        self.owner.close()

    @contextmanager
    def transaction(self):
        with self.lock:
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield self.db
                self.db.execute("COMMIT")
            except BaseException:
                if self.db.in_transaction:
                    self.db.execute("ROLLBACK")
                raise

    def generation(self):
        with self.lock:
            return self.db.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]

    def health(self):
        return {
            "schema_version": 1,
            "character_id": self.character_id,
            "status": "TEXT_READY",
            "capabilities": ["text_search", "read_by_id", "durable_ingest"],
            "semantic_search": False,
            "generation": self.generation(),
            "startup_ms": self.startup_ms,
            "cache_bytes_limit": 8 * 1024 * 1024,
        }

    def _identity(self, character):
        if character != self.character_id:
            raise Refusal("CHARACTER_MISMATCH")

    @staticmethod
    def _receipt(row):
        return {"status": "DURABLE", "seq": row["seq"], "record_id": row["record_id"], "revision": row["revision"]}

    def receipt(self, source, event):
        with self.lock:
            row = self.db.execute("SELECT * FROM receipts WHERE source=? AND event=?", (source, event)).fetchone()
            return self._receipt(row) if row else {"status": "NOT_FOUND"}

    def _duplicate(self, db, command):
        row = db.execute(
            "SELECT * FROM receipts WHERE source=? AND event=?", (command.source_instance, command.event_id)
        ).fetchone()
        if row:
            if row["hash"] != digest(command):
                raise Refusal("EVENT_ID_CONTENT_CONFLICT")
            return self._receipt(row)

    def _commit_event(self, db, command, record_id, revision, operation):
        seq = db.execute(
            "INSERT INTO changes(record_id,revision,operation,received_at) VALUES(?,?,?,?)",
            (record_id, revision, operation, time.time()),
        ).lastrowid
        db.execute(
            "INSERT INTO receipts VALUES(?,?,?,?,?,?)",
            (command.source_instance, command.event_id, digest(command), seq, record_id, revision),
        )
        return {"status": "DURABLE", "seq": seq, "record_id": record_id, "revision": revision}

    @staticmethod
    def _index(db, record_id, payload):
        rowid = db.execute("SELECT rowid FROM records WHERE id=?", (record_id,)).fetchone()[0]
        db.execute("DELETE FROM search WHERE rowid=?", (rowid,))
        text = payload["content"] + " " + " ".join(payload.get("aliases", []) + payload.get("participants", []))
        db.execute(
            "INSERT INTO search(rowid,record_id,terms) VALUES(?,?,?)", (rowid, record_id, " ".join(tokens(text)))
        )

    @staticmethod
    def _unindex(db, record_id):
        db.execute("DELETE FROM search WHERE rowid=(SELECT rowid FROM records WHERE id=?)", (record_id,))

    def ingest(self, event: SourceEvent):
        self._identity(event.character_id)
        if len(canonical(event).encode()) > 128 * 1024:
            raise Refusal("EVENT_TOO_LARGE")
        record_id = hashlib.sha256(canonical([event.source_instance, event.event_id]).encode()).hexdigest()
        with self.transaction() as db:
            previous = self._duplicate(db, event)
            if previous:
                return previous
            for ref in event.source_ids:
                row = db.execute("SELECT status FROM records WHERE id=?", (ref,)).fetchone()
                if not row or row[0] == "RETRACTED":
                    raise Refusal("SOURCE_UNAVAILABLE")
            payload = event.model_dump(mode="json")
            db.execute(
                "INSERT INTO records VALUES(?,1,'ONLINE',?,?,?,NULL)",
                (record_id, event.occurred_at, event.kind, int(event.pinned)),
            )
            db.execute("INSERT INTO revisions VALUES(?,1,?,?)", (record_id, canonical(payload), digest(payload)))
            db.executemany("INSERT INTO sources VALUES(?,?)", [(record_id, ref) for ref in event.source_ids])
            self._index(db, record_id, payload)
            return self._commit_event(db, event, record_id, 1, "INGEST")

    def correct(self, command: Correction):
        self._identity(command.character_id)
        with self.transaction() as db:
            previous = self._duplicate(db, command)
            if previous:
                return previous
            row = db.execute("SELECT * FROM records WHERE id=?", (command.record_id,)).fetchone()
            if not row:
                raise Refusal("NOT_FOUND")
            if row["revision"] != command.expected_revision:
                raise Refusal("REVISION_CONFLICT")
            if row["status"] == "ARCHIVED_OFFLINE" and not command.retract:
                raise Refusal("OFFLINE_CORRECTION_REQUIRES_NEW_ONLINE_EVENT")
            if not command.retract and not command.content:
                raise Refusal("CORRECTION_CONTENT_REQUIRED")
            payload_row = db.execute(
                "SELECT payload FROM revisions WHERE record_id=? AND revision=?", (command.record_id, row["revision"])
            ).fetchone()
            payload = json.loads(payload_row[0]) if payload_row else {}
            payload.update({"content": command.content or "", "correction": command.model_dump(mode="json")})
            revision = row["revision"] + 1
            db.execute(
                "INSERT INTO revisions VALUES(?,?,?,?)",
                (command.record_id, revision, canonical(payload), digest(payload)),
            )
            db.execute(
                "UPDATE records SET revision=?,status=? WHERE id=?",
                (revision, "RETRACTED" if command.retract else "ONLINE", command.record_id),
            )
            self._unindex(db, command.record_id)
            if not command.retract:
                self._index(db, command.record_id, payload)
            dependents = db.execute(
                """WITH RECURSIVE affected(id) AS (
                SELECT record_id FROM sources WHERE source_id=? UNION
                SELECT s.record_id FROM sources s JOIN affected a ON s.source_id=a.id)
                SELECT id FROM affected LIMIT 1001""",
                (command.record_id,),
            ).fetchall()
            if len(dependents) > 1000:
                raise Refusal("CORRECTION_REQUIRES_MAINTENANCE_WINDOW")
            for dependent in dependents:
                db.execute("UPDATE records SET status='SOURCE_CHANGED' WHERE id=? AND status='ONLINE'", dependent)
                self._unindex(db, dependent[0])
                db.execute(
                    "INSERT INTO changes(record_id,revision,operation,received_at) SELECT id,revision,'SOURCE_CHANGED',? FROM records WHERE id=?",
                    (time.time(), dependent[0]),
                )
            return self._commit_event(
                db, command, command.record_id, revision, "RETRACT" if command.retract else "CORRECT"
            )

    def _read(self, record_id, offset=0, length=4096):
        row = self.db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if not row:
            return {"status": "NOT_FOUND", "record_id": record_id}
        result = {
            "status": row["status"],
            "record_id": record_id,
            "revision": row["revision"],
            "kind": row["kind"],
            "occurred_at": row["occurred"],
            "generation": self.generation(),
        }
        if row["status"] != "ONLINE":
            return result
        payload = json.loads(
            self.db.execute(
                "SELECT payload FROM revisions WHERE record_id=? AND revision=?", (record_id, row["revision"])
            ).fetchone()[0]
        )
        content = payload.pop("content")
        # Correction metadata must not duplicate the full text around the bounded read.
        payload.pop("correction", None)
        result.update(
            content=content[offset : offset + length], offset=offset, total_characters=len(content), source=payload
        )
        return result

    def read(self, record_id, offset=0, length=4096):
        if offset < 0 or not 1 <= length <= 16384:
            raise Refusal("READ_BOUNDS")
        with self.lock:
            return self._read(record_id, offset, length)

    def recall(self, query, limit=10):
        if not 1 <= limit <= 50 or not 1 <= len(query) <= 256:
            raise Refusal("QUERY_BOUNDS")
        terms = tokens(query)
        with self.lock:
            if not terms:
                ids = []
            else:
                expression = " AND ".join('"' + term.replace('"', '""') + '"' for term in terms)
                ids = self.db.execute(
                    "SELECT record_id FROM search WHERE search MATCH ? ORDER BY rank LIMIT ?", (expression, limit)
                ).fetchall()
            return {
                "status": "OK",
                "scope": "GLOBAL_ONLINE",
                "generation": self.generation(),
                "items": [self._read(row[0], length=2048) for row in ids],
                "coverage": "text_index_only; offline raw text is never searched",
            }

    def changes(self, after, limit=100):
        if after < 0 or not 1 <= limit <= 500:
            raise Refusal("CURSOR_BOUNDS")
        with self.lock:
            rows = self.db.execute("SELECT * FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (after, limit)).fetchall()
            return {"items": [dict(row) for row in rows], "generation": self.generation()}

    def protect(self, record_id, owner, active=True):
        if not owner or len(owner) > 256:
            raise Refusal("PROTECTION_OWNER_REQUIRED")
        with self.transaction() as db:
            if active:
                db.execute("INSERT OR IGNORE INTO protections VALUES(?,?)", (record_id, owner))
            else:
                db.execute("DELETE FROM protections WHERE record_id=? AND owner=?", (record_id, owner))

    def backup(self, target: Path):
        """Online SQLite backup of this domain only; not a cross-service recovery point."""
        with target.open("xb"):
            pass
        with self.lock, sqlite3.connect(target) as destination:
            self.db.backup(destination, pages=256, sleep=0.01)
            if destination.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise Refusal("BACKUP_INVALID")
