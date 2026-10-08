"""Local SQLite journal of private diary entries owned by Runtime."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from harness.diary.models import DiaryOriginal


class DiaryStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute(
            """CREATE TABLE IF NOT EXISTS entries(
                entry_id TEXT PRIMARY KEY,
                decision_id TEXT NOT NULL UNIQUE,
                day TEXT NOT NULL,
                written_at TEXT NOT NULL,
                body TEXT NOT NULL,
                body_hash TEXT NOT NULL,
                homeostatic_summary_json TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                revision INTEGER NOT NULL
            )"""
        )
        self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS reflection_evidence(entry_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
        self.db.commit()

    @property
    def paused(self) -> bool:
        row = self.db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        return row is not None and row[0] == "1"

    def pause(self, paused: bool) -> None:
        with self.db:
            self.db.execute(
                "INSERT INTO settings VALUES ('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                ("1" if paused else "0",),
            )

    def count(self, day: str) -> int:
        row = self.db.execute("SELECT COUNT(*) AS n FROM entries WHERE day=?", (day,)).fetchone()
        return int(row["n"])

    def get(self, entry_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM entries WHERE entry_id=?", (entry_id,)).fetchone()
        return dict(row) if row else None

    def get_by_decision(self, decision_id: str) -> dict | None:
        row = self.db.execute("SELECT * FROM entries WHERE decision_id=?", (decision_id,)).fetchone()
        return dict(row) if row else None

    def put(self, original: DiaryOriginal, *, day: str, revision: int, reflection: dict | None = None) -> dict:
        payload = {
            "entry_id": original.entry_id,
            "decision_id": original.decision_id,
            "day": day,
            "written_at": original.written_at.isoformat(),
            "body": original.text,
            "body_hash": original.body_hash,
            "homeostatic_summary_json": json.dumps(original.homeostatic_summary, ensure_ascii=False),
            "source_event_id": original.source_event_id,
            "revision": revision,
        }
        prior = self.get_by_decision(original.decision_id)
        if prior is not None:
            if (prior["entry_id"] != original.entry_id or prior["body_hash"] != original.body_hash
                    or prior["body"] != original.text):
                raise ValueError("diary_decision_conflict")
            return prior
        with self.db:
            self.db.execute(
                """INSERT INTO entries(
                    entry_id, decision_id, day, written_at, body, body_hash,
                    homeostatic_summary_json, source_event_id, revision
                ) VALUES (?,?,?,?,?,?,?,?,?)""",
                (payload["entry_id"], payload["decision_id"], payload["day"], payload["written_at"],
                 payload["body"], payload["body_hash"], payload["homeostatic_summary_json"],
                 payload["source_event_id"], payload["revision"]),
            )
            if reflection:
                self.db.execute("INSERT INTO reflection_evidence VALUES (?,?)", (original.entry_id, json.dumps(reflection, ensure_ascii=False)))
        return payload

    def reflection_basis(self) -> dict:
        rows = self.db.execute("SELECT entry_id,written_at,body FROM entries ORDER BY written_at DESC LIMIT 3").fetchall()
        if not rows:
            return {"recent": []}
        latest = rows[0]
        evidence = self.db.execute("SELECT payload FROM reflection_evidence WHERE entry_id=?", (latest["entry_id"],)).fetchone()
        result = {"entry_id": latest["entry_id"], "written_at": latest["written_at"],
                  "recent": [{"entry_id": r["entry_id"], "written_at": r["written_at"], "text": r["body"]} for r in rows]}
        if evidence:
            result["coverage"] = json.loads(evidence[0])["coverage"]
        return result

    def reflection_status(self):
        basis = self.reflection_basis()
        return {"last_entry_id": basis.get("entry_id"), "coverage": basis.get("coverage")}

    def reflection_evidence(self, entry_id):
        row = self.db.execute("SELECT payload FROM reflection_evidence WHERE entry_id=?", (entry_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def contains_body(self, text):
        return self.db.execute("SELECT 1 FROM entries WHERE trim(body)=? LIMIT 1", (text.strip(),)).fetchone() is not None

    def public_rows(self, *, day: str | None = None, limit: int = 30) -> list[dict]:
        if day:
            rows = self.db.execute(
                "SELECT entry_id, decision_id, day, written_at, body_hash, revision FROM entries WHERE day=? ORDER BY written_at DESC LIMIT ?",
                (day, limit),
            ).fetchall()
        else:
            rows = self.db.execute(
                "SELECT entry_id, decision_id, day, written_at, body_hash, revision FROM entries ORDER BY written_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def close(self) -> None:
        self.db.close()
