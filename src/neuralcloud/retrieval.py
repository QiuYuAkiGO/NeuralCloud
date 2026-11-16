"""Derived identity evidence and bounded hybrid recall. Source DB remains authoritative."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time

from .storage import tokens


def query_terms(query):
    # Avoid matching every Chinese character independently. Exact IDs remain mandatory.
    terms = tokens(query)
    ignored = {"的", "了", "吗", "呢", "我", "你", "是", "还", "对", "什么", "是谁", "记得", "一下"}
    selected = [t for t in terms if t not in ignored and (len(t) > 1 or t.isascii())]
    return selected or [t for t in terms if t not in ignored]


def keyword_expression(query):
    selected = query_terms(query)
    ids = sorted(set(re.findall(r"(?<![0-9])[0-9]{5,16}(?![0-9])", query)))

    def quote(t):
        return '"' + t.replace('"', '""') + '"'

    phrases = quoted_phrases(query)
    phrase_terms = {t for phrase in phrases for t in query_terms(phrase)}
    optional = [t for t in selected if t not in ids and t not in phrase_terms]
    clauses = [quote(t) for t in ids]
    clauses.extend(quote(t) for t in sorted(phrase_terms) if t not in ids)
    if optional:
        clauses.append("(" + " OR ".join(map(quote, optional)) + ")")
    return " AND ".join(clauses)


def quoted_phrases(query):
    return [match[0] or match[1] for match in re.findall(r'"([^"\n]+)"|“([^”\n]+)”', query)]


class IdentityIndex:
    """Explicit, operator-owned evidence manifest; never learns identity from similarity."""

    def __init__(self, path):
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.RLock()
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT)")
        self.db.execute(
            "CREATE TABLE IF NOT EXISTS facts(platform TEXT, account_id TEXT, record_id TEXT, revision INTEGER, confirmation TEXT, name TEXT, relation TEXT, PRIMARY KEY(platform,account_id,record_id))"
        )
        self.db.commit()

    def seed(self, facts, store):
        # Maintenance-only, not a role API. Validate every reference against the source.
        allowed = {"operator_confirmed", "self_reported", "third_party"}
        self.check_database(store, initialize=True)
        for fact in facts:
            if fact["confirmation"] not in allowed:
                raise ValueError("IDENTITY_CONFIRMATION_INVALID")
            record = store.read(fact["record_id"], length=16384)
            if record["status"] != "ONLINE" or record["revision"] != fact["revision"]:
                raise ValueError("IDENTITY_SOURCE_STALE")
            if not fact["account_id"] or not fact["name"]:
                raise ValueError("IDENTITY_FIELDS_REQUIRED")
        with self.lock, self.db:
            for fact in facts:
                self.db.execute(
                    "INSERT OR REPLACE INTO facts VALUES(?,?,?,?,?,?,?)",
                    tuple(
                        fact[k]
                        for k in ("platform", "account_id", "record_id", "revision", "confirmation", "name", "relation")
                    ),
                )

    def lookup(self, subject, store):
        if subject is None:
            return []
        self.check_database(store)
        with self.lock:
            rows = self.db.execute(
                "SELECT * FROM facts WHERE platform=? AND account_id=? ORDER BY CASE confirmation WHEN 'operator_confirmed' THEN 0 WHEN 'self_reported' THEN 1 ELSE 2 END, record_id",
                (subject.platform, subject.account_id),
            ).fetchall()
        valid = []
        for row in rows:
            record = store.read(row["record_id"])
            if record["status"] == "ONLINE" and record["revision"] == row["revision"]:
                record["identity"] = {k: row[k] for k in ("platform", "account_id", "confirmation", "name", "relation")}
                valid.append(record)
        claims = {(r["identity"]["name"], r["identity"]["relation"]) for r in valid}
        for r in valid:
            # Conservatively expose differing claims; do not ask similarity to reconcile them.
            r["identity"]["conflict"] = len(claims) > 1
        return valid[:3]

    def role_accounts(self, role, store):
        self.check_database(store)
        with self.lock:
            rows = self.db.execute(
                "SELECT DISTINCT platform,account_id FROM facts WHERE confirmation='operator_confirmed' AND relation LIKE ?",
                ("%" + role + "%",),
            ).fetchall()
        return [dict(row) for row in rows]

    def check_database(self, store, initialize=False):
        with store.lock:
            identifier = store.db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()
            if not row and initialize:
                with self.db:
                    self.db.execute("INSERT INTO metadata VALUES('database_id',?)", (identifier,))
            elif not row or row[0] != identifier:
                raise ValueError("IDENTITY_DATABASE_MISMATCH")

    def close(self):
        self.db.close()


class Retriever:
    def __init__(self, store, identities=None, semantic=None, *, hybrid=False, threshold=0.0, keyword_threshold=0.0):
        self.store, self.identities, self.semantic = store, identities, semantic
        self.hybrid, self.threshold = hybrid, threshold
        self.keyword_threshold = keyword_threshold

    def recall(self, query, limit=10, subject=None, mode="auto", *, deadline=None):
        start = time.monotonic()
        if mode == "legacy":
            return self.store.recall(query, limit)
        generation = self.store.generation()
        identity = self.identities.lookup(subject, self.store) if self.identities and mode != "semantic" else []
        expression = keyword_expression(query)
        phrases = quoted_phrases(query)
        lexical = []
        if expression:
            with self.store.lock:
                rows = self.store.db.execute(
                    "SELECT record_id FROM search WHERE search MATCH ? ORDER BY rank LIMIT 50", (expression,)
                ).fetchall()
                wanted = set(query_terms(query))
                for (rid,) in rows:
                    raw = self.store.db.execute(
                        "SELECT v.payload FROM records r JOIN revisions v ON r.id=v.record_id AND r.revision=v.revision WHERE r.id=? AND r.status='ONLINE'",
                        (rid,),
                    ).fetchone()
                    if not raw:
                        continue
                    p = json.loads(raw[0])
                    available = set(
                        tokens(p["content"] + " " + " ".join(p.get("aliases", []) + p.get("participants", [])))
                    )
                    if len(wanted & available) / max(1, len(wanted)) >= self.keyword_threshold:
                        lexical.append(rid)
        vectors, degraded = [], None
        want_semantic = mode in ("semantic", "hybrid") or (mode == "auto" and self.hybrid)
        if want_semantic:
            if self.semantic:
                try:
                    vectors = self.semantic.search(
                        query, limit=50, timeout=min(1.2, max(0.01, deadline - time.monotonic())) if deadline else 1.2
                    )
                except Exception as exc:
                    degraded = type(exc).__name__
            else:
                degraded = "SEMANTIC_UNAVAILABLE"
        scores, excerpts = {}, {}
        if mode != "semantic":
            for rank, rid in enumerate(lexical, 1):
                scores[rid] = 1 / (60 + rank)
        for rank, hit in enumerate(vectors, 1):
            if hit["score"] < self.threshold:
                continue
            rid = hit["record_id"]
            scores[rid] = scores.get(rid, 0) + 1 / (60 + rank)
            excerpts[rid] = hit
        if want_semantic and degraded is None and self.threshold > 0:
            # Calibrated abstention applies to the fused topical pool, not just its vector half.
            scores = {rid: score for rid, score in scores.items() if rid in excerpts}
        records = []
        numeric = set(re.findall(r"(?<![0-9])[0-9]{5,16}(?![0-9])", query))
        if (
            subject
            and subject.platform == "qq"
            and re.search(r"我是谁|我.*名字|我.*全名|称呼|认得|记得.*谁|这个.*账号|这个.*QQ|我们的关系", query)
        ):
            numeric.add(subject.account_id)
        with self.store.lock:
            # Recheck live revisions after the potentially slow semantic stage.
            identity = [
                r
                for r in identity
                if (self.store._read(r["record_id"])["status"], self.store._read(r["record_id"]).get("revision"))
                == ("ONLINE", r["revision"])
            ]
            for rid, score in scores.items():
                hit = excerpts.get(rid)
                record = self.store._read(
                    rid, offset=hit["start"] if hit else 0, length=min(2048, hit["end"] - hit["start"]) if hit else 2048
                )
                if record["status"] != "ONLINE":
                    continue
                if hit and record["revision"] != hit["revision"]:
                    # A stale vector must not supply text or ranking. Lexical result remains usable.
                    if rid not in lexical or mode == "semantic":
                        continue
                    record = self.store._read(rid, length=2048)
                    score = 1 / (61 + lexical.index(rid))
                    hit = None
                if numeric or phrases:
                    raw = self.store.db.execute(
                        "SELECT payload FROM revisions WHERE record_id=? AND revision=?", (rid, record["revision"])
                    ).fetchone()[0]
                    p = json.loads(raw)
                    words = set(tokens(p["content"] + " " + " ".join(p.get("aliases", []) + p.get("participants", []))))
                    if not numeric.issubset(words):
                        continue
                    text = p["content"] + " " + " ".join(p.get("aliases", []))
                    if not all(phrase.casefold() in text.casefold() for phrase in phrases):
                        continue
                if record.get("source", {}).get("pinned"):
                    score += 0.005
                record["retrieval"] = {"score": score, "semantic": bool(hit), "keyword": rid in lexical}
                if hit:
                    record["retrieval"].update(start=hit["start"], end=hit["end"])
                records.append(record)
            generation = self.store.generation()
        records.sort(key=lambda r: (-r["retrieval"]["score"], r["record_id"]))
        chosen = identity[: min(3, limit)]
        seen = {r["record_id"] for r in chosen}
        topics = [r for r in records if r["record_id"] not in seen][: min(7 if subject else limit, limit - len(chosen))]
        return {
            "status": "OK",
            "scope": "GLOBAL_ONLINE",
            "generation": generation,
            "items": chosen + topics,
            "coverage": "global_online; source revisions revalidated; offline raw text excluded",
            "diagnostics": {
                "mode": mode,
                "semantic_requested": want_semantic,
                "degraded": degraded,
                "identity_count": len(chosen),
                "keyword_candidates": len(lexical),
                "semantic_candidates": len(vectors),
                "elapsed_ms": round((time.monotonic() - start) * 1000, 2),
            },
        }
