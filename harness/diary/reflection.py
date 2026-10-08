"""Durable diary coverage and bounded, attributed source batches."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import hashlib
import json
import sqlite3
from zoneinfo import ZoneInfo

from harness.memory.shared import SharedReadScope

VERSION = "diary-reflection-v1"
SCOPE = SharedReadScope(audience="local:main", kinds=("local_incoming", "local_outgoing",
    "qq_observation", "qq_delivery", "qq_segment_delivery", "action_outcome"))


def encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(encoded(value).encode()).hexdigest()


class ReflectionStore:
    def __init__(self, path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS reflection_jobs(id TEXT PRIMARY KEY, basis TEXT, payload TEXT, status TEXT);
                CREATE TABLE IF NOT EXISTS reflection_batches(id TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS reflection_state(key TEXT PRIMARY KEY, payload TEXT);
                CREATE TABLE IF NOT EXISTS reflection_calls(id INTEGER PRIMARY KEY, phase TEXT, usage TEXT);
            """)

    def connect(self):
        return sqlite3.connect(self.path, timeout=5)

    def get(self, table, key):
        assert table in {"reflection_jobs", "reflection_batches", "reflection_state"}
        column = "key" if table == "reflection_state" else "id"
        with closing(self.connect()) as db:
            row = db.execute(f"SELECT payload FROM {table} WHERE {column}=?", (key,)).fetchone()
        return json.loads(row[0]) if row else None

    def state(self, key, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO reflection_state VALUES (?,?) ON CONFLICT(key) DO UPDATE SET payload=excluded.payload", (key, encoded(value)))

    def save_job(self, job):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO reflection_jobs VALUES (?,?,?,?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload,status=excluded.status",
                       (job["id"], job["basis_id"], encoded(job), job["status"]))

    def pending(self, basis=None):
        with closing(self.connect()) as db:
            sql = "SELECT payload FROM reflection_jobs WHERE status IN ('pending','ready')"
            rows = db.execute(sql + (" AND basis=?" if basis else "") + " ORDER BY rowid", (basis,) if basis else ()).fetchall()
        return [json.loads(r[0]) for r in rows]

    def save_batch(self, key, value):
        with closing(self.connect()) as db, db:
            db.execute("INSERT OR REPLACE INTO reflection_batches VALUES (?,?)", (key, encoded(value)))

    def call(self, phase, usage):
        with closing(self.connect()) as db, db:
            db.execute("INSERT INTO reflection_calls(phase,usage) VALUES (?,?)", (phase, encoded(usage)))

    def status(self):
        with closing(self.connect()) as db:
            latest = db.execute("""SELECT status,json_extract(payload,'$.completed_batches'),
                json_extract(payload,'$.expected_batches'),json_extract(payload,'$.error')
                FROM reflection_jobs ORDER BY rowid DESC LIMIT 1""").fetchone()
            return {"jobs": dict(db.execute("SELECT status,count(*) FROM reflection_jobs GROUP BY status")),
                    "latest": dict(zip(("status", "completed_batches", "expected_batches", "error_type"), latest)) if latest else None,
                    "model_calls": db.execute("SELECT count(*) FROM reflection_calls").fetchone()[0],
                    "total_tokens": db.execute("SELECT COALESCE(SUM(json_extract(usage,'$.total_tokens')),0) FROM reflection_calls").fetchone()[0]}


def source_boundary(shared, store, basis):
    """Initialize legacy coverage once; never infer new coverage from model text."""
    if not shared.path.exists():
        if basis.get("coverage", {}).get("end", 0):
            raise ValueError("diary_archive_missing")
        return {"archive": str(shared.path.resolve()), "start": 0, "end": 0,
                "since": basis.get("written_at"), "until": datetime.now().astimezone().isoformat()}
    with closing(shared._connect(write=False)) as db:
        end = db.execute("SELECT COALESCE(MAX(sequence),0) FROM shared_memory_source_index WHERE owner_id=? AND preset_id=?",
                         (shared.owner_id, shared.preset_id)).fetchone()[0]
        previous = basis.get("coverage")
        if previous:
            if previous["archive"] != str(shared.path.resolve()):
                raise ValueError("diary_archive_changed")
            start = previous["end"]
        else:
            key = "legacy:" + digest(basis)
            saved = store.get("reflection_state", key)
            if saved is None:
                stamp = basis.get("written_at")
                start = 0
                if stamp:
                    # Backfilled rows retain stored_at; late discoveries stay after the boundary.
                    start = db.execute("""SELECT COALESCE(MAX(i.sequence),0) FROM shared_memory_source_index i
                        JOIN shared_memory_entries e ON e.record_id=i.record_id
                        WHERE i.owner_id=? AND i.preset_id=? AND julianday(json_extract(e.record_json,'$.stored_at'))<=julianday(?)""",
                        (shared.owner_id, shared.preset_id, stamp)).fetchone()[0]
                saved = {"start": start}
                store.state(key, saved)
            start = saved["start"]
    if start > end:
        raise ValueError("diary_archive_regressed")
    return {"archive": str(shared.path.resolve()), "start": start, "end": end,
            "since": basis.get("coverage", {}).get("until") or basis.get("written_at"),
            "until": datetime.now().astimezone().isoformat()}


def read_sources(shared, coverage):
    records, excluded = [], []
    cursor = coverage["start"]
    while cursor < coverage["end"]:
        with closing(shared._connect(write=False)) as db:
            rows = db.execute("""SELECT sequence,record_id FROM shared_memory_source_index
                WHERE owner_id=? AND preset_id=? AND sequence>? AND sequence<=? ORDER BY sequence LIMIT 256""",
                (shared.owner_id, shared.preset_id, cursor, coverage["end"])).fetchall()
        if not rows:
            break
        selected = shared.read_exact(SCOPE, [r[1] for r in rows])
        records.extend(selected)
        admitted = {r.record_id for r in selected}
        excluded.extend(r[1] for r in rows if r[1] not in admitted)
        cursor = rows[-1][0]
    return records, excluded


def source_events(records):
    """Collapse verified protocol identities, never similar message text."""
    events = {}
    for record in sorted(records, key=lambda r: (r.occurred_at, r.record_id)):
        raw = json.loads(record.source.original_json)
        parts = [(None, record.text)]
        if record.kind == "qq_observation" and raw.get("author_attribution") == "qq_protocol_verified":
            parts = [(raw["message_id"], record.text)]
        elif record.kind == "qq_segment_delivery" and raw.get("verification") == "qq_protocol_verified":
            parts = [(raw["protocol_message_id"], record.text)]
        elif record.kind == "qq_delivery" and raw.get("verification") == "qq_protocol_verified":
            parts = list(zip(raw["protocol_message_ids"], raw.get("delivered_segments") or [record.text], strict=True))
        for message_id, text in parts:
            identity = digest([record.session_id, message_id]) if message_id else record.record_id
            if identity in events:
                events[identity]["source_ids"].append(record.record_id)
                continue
            events[identity] = {"event_id": identity, "source_ids": [record.record_id],
                "at": record.occurred_at.isoformat(), "author": record.author_principal,
                "kind": record.kind, "text": text,
                "self_expression": record.author_principal.startswith("character:")}
    return list(events.values())


def batches(events):
    """Cover every character, preserving offsets across long-message splits."""
    parts = []
    for event in events:
        for offset in range(0, max(1, len(event["text"])), 12000):
            part = {**event, "offset": offset, "text": event["text"][offset:offset+12000]}
            part["day"] = datetime.fromisoformat(event["at"]).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            parts.append(part)
    current, size, day = [], 0, None
    for part in sorted(parts, key=lambda p: (p["day"], p["at"], p["event_id"], p["offset"])):
        if current and (len(current) >= 64 or size + len(part["text"]) > 12000 or part["day"] != day):
            yield current
            current, size = [], 0
        current.append(part)
        size += len(part["text"])
        day = part["day"]
    if current:
        yield current
