"""Rebuildable, revision-bound evidence index. Reads canonical memory without owning it."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import sqlite3
import threading
import time

from .ownership import Ownership

VERSION = "evidence-v1"
SCHEMA = """
CREATE TABLE metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE documents(record_id TEXT PRIMARY KEY,revision INTEGER,occurred REAL,conversation TEXT,
 author TEXT,role TEXT,message_key TEXT,reply_key TEXT,source_instance TEXT);
CREATE INDEX document_conversation ON documents(conversation,occurred);
CREATE INDEX document_message ON documents(message_key);
CREATE INDEX document_reply ON documents(reply_key);
CREATE TABLE facts(id TEXT PRIMARY KEY,record_id TEXT,revision INTEGER,entity TEXT,predicate TEXT,value TEXT,
 basis TEXT,confirmation TEXT,scope TEXT,occurred REAL,refs TEXT,active INTEGER,extractor TEXT,confidence REAL);
CREATE INDEX fact_entity ON facts(entity,predicate,active);
CREATE INDEX fact_source ON facts(record_id);
CREATE TABLE fact_sources(fact_id TEXT,source_id TEXT,revision INTEGER,PRIMARY KEY(fact_id,source_id));
CREATE INDEX fact_dependency ON fact_sources(source_id);
CREATE TABLE jobs(record_id TEXT,revision INTEGER,status TEXT,attempts INTEGER DEFAULT 0,error TEXT,
 PRIMARY KEY(record_id,revision));
"""


def account(value, platform="qq"):
    if not isinstance(value, str):
        return None
    if value.startswith("qq:member:"):
        value = value[len("qq:member:") :]
    if value.startswith("qqbot:"):
        return value if re.fullmatch(r"qqbot:[A-Za-z0-9_:-]+", value) else None
    if value.startswith("qq:"):
        value = value[3:]
    if platform == "qq" and re.fullmatch(r"[1-9][0-9]{4,15}", value):
        return "qq:" + value
    if platform == "qqbot" and value:
        return "qqbot:" + value
    return None


def author_of(payload):
    channel = payload.get("channel")
    if channel:
        if channel["platform"] == "qqbot" and not channel["sender_id"].startswith("qqbot:"):
            return "qqbot:" + channel["account_id"] + ":user:" + channel["sender_id"]
        return account(channel["sender_id"], channel["platform"])
    participants = payload.get("participants", [])
    if len(participants) != 1:
        return None
    return account(participants[0])


def is_primary(payload, self_accounts=()):
    author = author_of(payload)
    return bool(
        author
        and author not in self_accounts
        and not (author.startswith("qqbot:") and ":bot:" in author)
        and payload["kind"] == "observation"
        and (payload.get("provenance") or {}).get("semantic_status", "observation") == "observation"
    )


def self_reported_names(text):
    """Only explicit introductions, never the imperative sense of Chinese 叫."""
    text = text.strip()
    if any(x in text for x in ["?", "？", "吗", "不是", "不叫", "“", "「", '"']):
        return []
    match = re.match(
        r"^(?:另外[，,]\s*)?(?:以后(?:请)?叫我|(?:你)?可以叫我|我叫|叫我)([^\s，,。！!；;：:\n]{1,24})(?=[\s，,。！!；;：:\n]|$)",
        text,
    )
    if not match:
        return []
    name = re.sub(r"[吧哦呀啊~～]+$", "", match[1])
    if not name or re.match(r"你|他|她|它|大家|别人|人家|自己|(?:去|来|帮|把|给|做|开)", name):
        return []
    if re.search(r"(?:来|去)(?:接|拿|帮|买|看|做|找)|做好|准备|干活|工作", name):
        return []
    result = [(name, "self_reported")]
    full = re.search(r"(?:全名|真实姓名|真名)(?:是|叫)?([^\s，,。！!；;：:\n]{1,24})(?=[\s，,。！!；;：:\n]|$)", text)
    if full:
        result.append((full[1], "self_reported_full_name"))
    return result


def load_source(db, rid):
    row = db.execute(
        "SELECT r.revision,r.status,v.payload FROM records r JOIN revisions v ON r.id=v.record_id AND r.revision=v.revision WHERE r.id=?",
        (rid,),
    ).fetchone()
    return (row[0], row[1], json.loads(row[2])) if row else None


class EvidenceIndex:
    def __init__(self, source, target, *, writable=False, self_accounts=()):
        self.source, self.target = Path(source), Path(target)
        self.lock = threading.RLock()
        self.owner = Ownership(self.target.with_suffix(".owner")) if writable else None
        self.writable = writable
        self.self_accounts = set(self_accounts)
        self.src = self.db = None
        try:
            self._open(writable)
        except BaseException:
            self.close()
            raise

    def _open(self, writable):
        self.src = sqlite3.connect(
            self.source.resolve().as_uri() + "?mode=ro", uri=True, check_same_thread=False, timeout=2
        )
        self.src.row_factory = sqlite3.Row
        identifier = self.src.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
        fresh = not self.target.exists()
        if fresh and not writable:
            raise ValueError("EVIDENCE_INITIALIZATION_REQUIRED")
        self.db = sqlite3.connect(
            self.target.resolve().as_uri() + ("?mode=rwc" if writable else "?mode=ro"),
            uri=True,
            check_same_thread=False,
            timeout=2,
        )
        self.db.row_factory = sqlite3.Row
        if writable:
            self.db.execute("PRAGMA journal_mode=WAL")
            self.db.execute("PRAGMA synchronous=FULL")
        if fresh:
            self.db.executescript(SCHEMA)
            self.db.executemany(
                "INSERT INTO metadata VALUES(?,?)",
                [("database_id", identifier), ("cursor", "0"), ("generation", "0"), ("version", VERSION)],
            )
            self.db.commit()
        if writable:
            self.db.executescript(
                "CREATE TABLE IF NOT EXISTS fact_sources(fact_id TEXT,source_id TEXT,revision INTEGER,PRIMARY KEY(fact_id,source_id)); CREATE INDEX IF NOT EXISTS fact_dependency ON fact_sources(source_id);"
            )
            self.db.execute("CREATE INDEX IF NOT EXISTS document_reply ON documents(reply_key)")
            for row in self.db.execute("SELECT id,refs FROM facts").fetchall():
                self.db.executemany(
                    "INSERT OR IGNORE INTO fact_sources VALUES(?,?,?)",
                    [(row[0], r["record_id"], r["revision"]) for r in json.loads(row[1])],
                )
            self.db.commit()
        meta = dict(self.db.execute("SELECT key,value FROM metadata"))
        if meta.get("version") != VERSION:
            raise ValueError("EVIDENCE_VERSION_MISMATCH")
        if meta.get("database_id") != identifier:
            raise ValueError("EVIDENCE_DATABASE_MISMATCH")
        self.self_accounts.update(json.loads(meta.get("self_accounts", "[]")))
        if writable:
            self.db.execute(
                "INSERT OR REPLACE INTO metadata VALUES('self_accounts',?)",
                (json.dumps(sorted(self.self_accounts)),),
            )
            self.db.commit()

    def close(self):
        if getattr(self, "db", None):
            self.db.close()
        if getattr(self, "src", None):
            self.src.close()
        if self.owner:
            self.owner.close()

    def status(self):
        with self.lock:
            meta = dict(self.db.execute("SELECT key,value FROM metadata"))
            current = self.src.execute("SELECT coalesce(max(seq),0) FROM changes").fetchone()[0]
            return {
                "version": VERSION,
                "path": str(self.target.resolve()),
                "generation": int(meta["generation"]),
                "cursor": int(meta["cursor"]),
                "lag": max(0, current - int(meta["cursor"])),
                "facts": self.db.execute("SELECT count(*) FROM facts WHERE active=1").fetchone()[0],
                "calibration_sha256": meta.get("calibration_sha256"),
            }

    def _insert(
        self,
        rid,
        revision,
        p,
        entity,
        predicate,
        value,
        *,
        basis="direct",
        confirmation="self_reported",
        refs=None,
        active=True,
        extractor="rules-v1",
        confidence=1.0,
        scope=None,
    ):
        ref = {"record_id": rid, "revision": revision, "start": 0, "end": len(p["content"])}
        refs = refs or [ref]
        identity = json.dumps([rid, revision, entity, predicate, value, basis, extractor, scope], ensure_ascii=False)
        fact_id = hashlib.sha256(identity.encode()).hexdigest()
        self.db.execute(
            "INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                fact_id,
                rid,
                revision,
                entity,
                predicate,
                value,
                basis,
                confirmation,
                scope,
                p["occurred_at"],
                json.dumps(refs),
                int(active),
                extractor,
                confidence,
            ),
        )
        self.db.execute("DELETE FROM fact_sources WHERE fact_id=?", (fact_id,))
        self.db.executemany(
            "INSERT OR REPLACE INTO fact_sources VALUES(?,?,?)",
            [(fact_id, r["record_id"], r["revision"]) for r in refs],
        )

    def sync_batch(self, limit=128):
        if not self.writable:
            raise ValueError("READ_ONLY_INDEX")
        with self.lock:
            cursor = int(self.db.execute("SELECT value FROM metadata WHERE key='cursor'").fetchone()[0])
            changes = self.src.execute(
                "SELECT seq,record_id FROM changes WHERE seq>? ORDER BY seq LIMIT ?", (cursor, limit)
            ).fetchall()
            if not changes:
                return 0
            with self.db:
                for rid in dict.fromkeys(r["record_id"] for r in changes):
                    source = load_source(self.src, rid)
                    affected = self.db.execute(
                        "SELECT DISTINCT f.record_id,f.revision FROM facts f JOIN fact_sources s ON f.id=s.fact_id WHERE s.source_id=?",
                        (rid,),
                    ).fetchall()
                    for changed in affected:
                        self.db.execute(
                            "UPDATE jobs SET status='PENDING',attempts=0 WHERE record_id=? AND revision=?",
                            tuple(changed),
                        )
                    self.db.execute(
                        "DELETE FROM facts WHERE id IN (SELECT fact_id FROM fact_sources WHERE source_id=?) OR record_id=?",
                        (rid, rid),
                    )
                    self.db.execute("DELETE FROM fact_sources WHERE fact_id NOT IN (SELECT id FROM facts)")
                    self.db.execute("DELETE FROM documents WHERE record_id=?", (rid,))
                    self.db.execute("UPDATE jobs SET status='SOURCE_CHANGED' WHERE record_id=?", (rid,))
                    if not source or source[1] != "ONLINE":
                        continue
                    revision, _, p = source
                    author = author_of(p)
                    ch = p.get("channel", {})
                    role = "source" if is_primary(p, self.self_accounts) else "non_primary"

                    def message_key(mid):
                        return (
                            json.dumps([ch["platform"], ch["account_id"], ch["conversation_id"], mid])
                            if ch and mid
                            else None
                        )

                    self.db.execute(
                        "INSERT INTO documents VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            rid,
                            revision,
                            p["occurred_at"],
                            p.get("conversation_id"),
                            author,
                            role,
                            message_key(ch.get("message_id")),
                            message_key(ch.get("reply_to")),
                            p["source_instance"],
                        ),
                    )
                    self.db.execute(
                        "INSERT INTO jobs(record_id,revision,status) VALUES(?,?,?) ON CONFLICT(record_id,revision) DO UPDATE SET status='PENDING',attempts=0,error=NULL",
                        (rid, revision, "PENDING"),
                    )
                    if role != "source" or not author:
                        continue
                    if ch:
                        for key, confirmation in [
                            ("nickname", "platform_profile"),
                            ("group_card", "platform_group_card"),
                        ]:
                            if ch.get(key):
                                self._insert(
                                    rid,
                                    revision,
                                    p,
                                    author,
                                    "name",
                                    ch[key],
                                    confirmation=confirmation,
                                    scope=p.get("conversation_id") if key == "group_card" else None,
                                )
                    try:
                        observed = json.loads(p["content"])
                    except (ValueError, TypeError):
                        observed = {}
                    if (
                        isinstance(observed, dict)
                        and observed.get("display_name_source") in ["qq_profile", "qq_metadata"]
                        and isinstance(observed.get("observed_display_name"), str)
                    ):
                        self._insert(
                            rid,
                            revision,
                            p,
                            author,
                            "name",
                            observed["observed_display_name"],
                            confirmation="platform_observed",
                            scope=p.get("conversation_id")
                            if observed["display_name_source"] == "qq_metadata"
                            else None,
                        )
                    for name, confirmation in self_reported_names(p["content"]):
                        self._insert(rid, revision, p, author, "name", name, confirmation=confirmation)
                self.db.execute("UPDATE metadata SET value=? WHERE key='cursor'", (str(changes[-1]["seq"]),))
                self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")
            return len(changes)

    def read_source(self, record_id):
        with self.lock:
            return load_source(self.src, record_id)

    def valid_refs(self, refs):
        with self.lock:
            return self._valid_refs(refs)

    def _valid_refs(self, refs):
        for ref in refs:
            source = load_source(self.src, ref["record_id"])
            if not source or source[1] != "ONLINE" or source[0] != ref["revision"]:
                return False
            text = source[2]["content"]
            start, end = ref.get("start", 0), ref.get("end", len(text))
            if not 0 <= start < end <= len(text):
                return False
        return bool(refs)

    def lookup(
        self,
        entities=(),
        predicates=(),
        *,
        conversation=None,
        since=None,
        until=None,
        limit=50,
        terms=(),
        candidates=False,
    ):
        clauses = ["active=1 OR confirmation='model_verified'" if candidates else "active=1"]
        clauses = ["(" + c + ")" for c in clauses]
        args = []
        for col, values in [("entity", entities), ("predicate", predicates)]:
            if values:
                clauses.append(col + " IN (" + ",".join("?" for _ in values) + ")")
                args.extend(values)
        if conversation:
            clauses.append("(scope IS NULL OR scope=?)")
            args.append(conversation)
        if since is not None:
            clauses.append("occurred>=?")
            args.append(since)
        if until is not None:
            clauses.append("occurred<=?")
            args.append(until)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM facts WHERE " + " AND ".join(clauses) + " ORDER BY active DESC,occurred DESC,id LIMIT ?",
                (*args, 1000),
            ).fetchall()
            result = []
            seen = set()
            for row in rows:
                d = dict(row)
                refs = json.loads(d["refs"])
                if not self.valid_refs(refs):
                    continue
                sources = [load_source(self.src, ref["record_id"])[2] for ref in refs]
                if d["confirmation"] in ["self_reported", "self_reported_full_name"] and d["extractor"] == "rules-v1":
                    if (d["value"], d["confirmation"]) not in self_reported_names(sources[0]["content"]):
                        continue
                if d["entity"] not in {author_of(p) for p in sources} or not all(
                    is_primary(p, self.self_accounts) for p in sources
                ):
                    continue
                if terms and not any(t.casefold() in d["value"].casefold() for t in terms):
                    continue
                key = (d["entity"], d["predicate"], d["value"], d["basis"], d["scope"])
                if key in seen:
                    continue
                seen.add(key)
                d["references"] = refs
                d.pop("refs")
                result.append(d)
                if len(result) >= limit:
                    break
            return result

    def alias_targets(self, query):
        matches = {}
        with self.lock:
            for row in self.db.execute(
                "SELECT entity,value,refs,confirmation,extractor FROM facts WHERE predicate='name' AND active=1"
            ).fetchall():
                name = row["value"].strip()
                refs = json.loads(row["refs"])
                if len(name) >= 2 and name.casefold() in query.casefold() and self.valid_refs(refs):
                    sources = [load_source(self.src, ref["record_id"])[2] for ref in refs]
                    if row["extractor"] == "rules-v1" and row["confirmation"] in [
                        "self_reported",
                        "self_reported_full_name",
                    ]:
                        if (row["value"], row["confirmation"]) not in self_reported_names(sources[0]["content"]):
                            continue
                    if row["entity"] in {author_of(p) for p in sources} and all(
                        is_primary(p, self.self_accounts) for p in sources
                    ):
                        matches.setdefault(name, set()).add(row["entity"])
        if not matches:
            return [], False
        longest = max(map(len, matches))
        groups = [values for name, values in matches.items() if len(name) == longest]
        return sorted(set().union(*groups))[:8], any(len(values) > 1 for values in groups)

    def related(self, record_ids, *, limit=8):
        with self.lock:
            rows = []
            for rid in record_ids[:3]:
                row = self.db.execute("SELECT * FROM documents WHERE record_id=?", (rid,)).fetchone()
                if not row:
                    continue
                # Exact reply edges have precedence. Temporal neighbors are explicitly only context.
                if row["reply_key"]:
                    rows.extend(
                        (r[0], "reply")
                        for r in self.db.execute(
                            "SELECT record_id FROM documents WHERE message_key=?", (row["reply_key"],)
                        )
                    )
                if row["message_key"]:
                    rows.extend(
                        (r[0], "reply")
                        for r in self.db.execute(
                            "SELECT record_id FROM documents WHERE reply_key=? LIMIT 4", (row["message_key"],)
                        )
                    )
                if row["conversation"]:
                    rows.extend(
                        (r[0], "temporal_context")
                        for r in self.db.execute(
                            "SELECT record_id FROM documents WHERE conversation=? AND occurred BETWEEN ? AND ? AND record_id!=? ORDER BY abs(occurred-?) LIMIT 4",
                            (row["conversation"], row["occurred"] - 120, row["occurred"] + 120, rid, row["occurred"]),
                        )
                    )
            return list(dict.fromkeys(rows))[:limit]

    def add_model_facts(
        self,
        rid,
        revision,
        facts,
        *,
        activation=False,
        inference_policy=None,
        extractor="model-v1",
        direct_predicates=None,
    ):
        if not self.writable:
            raise ValueError("READ_ONLY_INDEX")
        with self.lock, self.db:
            source = load_source(self.src, rid)
            if not source or source[:2] != (revision, "ONLINE"):
                raise ValueError("SOURCE_CHANGED")
            p = source[2]
            allowed = {
                "name",
                "likes",
                "dislikes",
                "preference",
                "decision",
                "recommendation",
                "reason",
                "relationship",
                "event",
            }
            for fact in facts:
                if fact["predicate"] not in allowed or fact["basis"] not in ["direct", "inferred"]:
                    raise ValueError("FACT_SCHEMA")
                if not isinstance(fact.get("value"), str) or not 1 <= len(fact["value"]) <= 512:
                    raise ValueError("FACT_VALUE_INVALID")
                if not isinstance(fact.get("confidence", 0), (float, int)) or not 0 <= fact.get("confidence", 0) <= 1:
                    raise ValueError("FACT_CONFIDENCE_INVALID")
                refs = fact["references"]
                if not self.valid_refs(refs):
                    raise ValueError("FACT_EVIDENCE_INVALID")
                if not any(x["record_id"] == rid and x["revision"] == revision for x in refs):
                    raise ValueError("ROOT_EVIDENCE_REQUIRED")
                entities = set()
                for ref in refs:
                    cited = load_source(self.src, ref["record_id"])[2]
                    entities.update(filter(None, [author_of(cited)]))
                    if not is_primary(cited, self.self_accounts):
                        raise ValueError("PRIMARY_SOURCE_REQUIRED")
                if fact["entity"] not in entities:
                    raise ValueError("ENTITY_NOT_GROUNDED")
                active = activation and fact.get("verified", False)
                if fact["basis"] == "direct" and direct_predicates is not None:
                    active = active and fact["predicate"] in direct_predicates
                if fact["basis"] == "inferred":
                    policy = (inference_policy or {}).get(fact["predicate"], {})
                    active = (
                        active
                        and policy.get("precision", 0) >= 0.95
                        and policy.get("blind_count", 0) >= 30
                        and fact.get("confidence", 0) >= policy.get("threshold", 1.1)
                    )
                self._insert(
                    rid,
                    revision,
                    p,
                    fact["entity"],
                    fact["predicate"],
                    fact["value"],
                    basis=fact["basis"],
                    confirmation="model_verified" if fact.get("verified") else "candidate",
                    refs=refs,
                    active=active,
                    extractor=extractor,
                    confidence=fact.get("confidence", 0),
                    scope=fact.get("scope"),
                )
            self.db.execute(
                "UPDATE jobs SET status=?,error=NULL WHERE record_id=? AND revision=?",
                ("COMPLETE" if facts else "NO_FACTS", rid, revision),
            )
            self.db.execute("UPDATE metadata SET value=CAST(value AS INTEGER)+1 WHERE key='generation'")


def main():
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--follow", action="store_true")
    parser.add_argument("--self-account", action="append", default=[])
    args = parser.parse_args()
    index = EvidenceIndex(args.source, args.index, writable=True, self_accounts=args.self_account)
    try:
        while True:
            count = index.sync_batch()
            if not count:
                if not args.follow:
                    break
                time.sleep(1)
        print(json.dumps(index.status()))
    finally:
        index.close()


if __name__ == "__main__":
    main()
