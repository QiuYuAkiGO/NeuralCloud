"""Durable frozen daily jobs, stage caches, receipts and conservative token accounting."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3
from typing import Any


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value: Any) -> str:
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(UTC).isoformat()


class DailyRevisionQueue:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS daily_revision_jobs (
                    run_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, preset_id TEXT NOT NULL,
                    day TEXT NOT NULL, partition TEXT NOT NULL, source_hash TEXT NOT NULL,
                    sources_json TEXT NOT NULL, basis_json TEXT NOT NULL, config_digest TEXT NOT NULL,
                    dry_run INTEGER NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
                    attempts INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    next_attempt_at TEXT, lease_until TEXT, last_error TEXT, receipt_json TEXT,
                    tombstoned INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS daily_revision_jobs_due
                    ON daily_revision_jobs(status,next_attempt_at,created_at);
                CREATE TABLE IF NOT EXISTS daily_revision_stages (
                    run_id TEXT NOT NULL, stage TEXT NOT NULL, value_json TEXT NOT NULL,
                    updated_at TEXT NOT NULL, PRIMARY KEY(run_id,stage),
                    FOREIGN KEY(run_id) REFERENCES daily_revision_jobs(run_id)
                );
                CREATE TABLE IF NOT EXISTS daily_revision_usage (
                    request_hash TEXT PRIMARY KEY, run_id TEXT NOT NULL, day TEXT NOT NULL,
                    reserved_tokens INTEGER NOT NULL, actual_tokens INTEGER,
                    usage_json TEXT, status TEXT NOT NULL DEFAULT 'reserved',
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    FOREIGN KEY(run_id) REFERENCES daily_revision_jobs(run_id)
                );
                CREATE INDEX IF NOT EXISTS daily_revision_usage_day ON daily_revision_usage(day);
            """)

    @contextmanager
    def _connection(self, *, write=False):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("PRAGMA journal_mode=WAL")
            if write:
                db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _job(row) -> dict | None:
        if row is None:
            return None
        result = dict(row)
        for column, key in (("sources_json", "sources"), ("basis_json", "basis"), ("receipt_json", "receipt")):
            raw = result.pop(column)
            result[key] = json.loads(raw) if raw is not None else None
        result["dry_run"] = bool(result["dry_run"])
        result["tombstoned"] = bool(result["tombstoned"])
        return result

    def enqueue(self, *, owner_id: str, preset_id: str, day: str, partition: str,
                sources: list[dict], basis: dict, dry_run=False, config_digest="") -> dict:
        source_hash = digest(sources)
        run_id = "revision-" + digest([owner_id, preset_id, day, partition, source_hash, bool(dry_run), config_digest])[:32]
        now = _now()
        with self._connection(write=True) as db:
            if not dry_run:
                revoked = db.execute("""SELECT * FROM daily_revision_jobs WHERE owner_id=? AND preset_id=?
                    AND day=? AND partition=? AND source_hash=? AND tombstoned=1 LIMIT 1""",
                    (owner_id, preset_id, day, partition, source_hash)).fetchone()
                if revoked is not None:
                    return self._job(revoked)
            existing = db.execute("""SELECT * FROM daily_revision_jobs WHERE owner_id=? AND preset_id=?
                AND day=? AND partition=? AND source_hash=? AND config_digest=? AND dry_run=?
                AND status!='invalidated' ORDER BY created_at DESC,rowid DESC LIMIT 1""",
                (owner_id, preset_id, day, partition, source_hash, config_digest, int(dry_run))).fetchone()
            if existing is not None:
                return self._job(existing)
            db.execute("""INSERT OR IGNORE INTO daily_revision_jobs
                (run_id,owner_id,preset_id,day,partition,source_hash,sources_json,basis_json,
                 config_digest,dry_run,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (run_id, owner_id, preset_id, day, partition, source_hash, _json(sources), _json(basis),
                 config_digest, int(dry_run), now, now))
            return self._job(db.execute("SELECT * FROM daily_revision_jobs WHERE run_id=?", (run_id,)).fetchone())

    def get(self, run_id: str) -> dict | None:
        with self._connection() as db:
            job = self._job(db.execute("SELECT * FROM daily_revision_jobs WHERE run_id=?", (run_id,)).fetchone())
            if job is not None:
                job["stages"] = {row["stage"]: json.loads(row["value_json"]) for row in db.execute(
                    "SELECT stage,value_json FROM daily_revision_stages WHERE run_id=?", (run_id,))}
                job["usage"] = [dict(row) for row in db.execute(
                    "SELECT request_hash,day,reserved_tokens,actual_tokens,status FROM daily_revision_usage WHERE run_id=?",
                    (run_id,))]
            return job

    def history(self, *, limit=50, owner_id=None, preset_id=None) -> list[dict]:
        predicates, args = [], []
        for name, value in (("owner_id", owner_id), ("preset_id", preset_id)):
            if value is not None:
                predicates.append(name + "=?")
                args.append(value)
        where = " WHERE " + " AND ".join(predicates) if predicates else ""
        with self._connection() as db:
            return [self._job(row) for row in db.execute("SELECT * FROM daily_revision_jobs" + where
                + " ORDER BY created_at DESC,rowid DESC LIMIT ?", (*args, max(1, min(int(limit), 1000))))]

    def claim(self, *, lease_seconds=300, owner_id=None, preset_id=None) -> dict | None:
        now = _now()
        scope, args = "", [now]
        for name, value in (("owner_id", owner_id), ("preset_id", preset_id)):
            if value is not None:
                scope += " AND " + name + "=?"
                args.append(value)
        with self._connection(write=True) as db:
            row = db.execute("""SELECT * FROM daily_revision_jobs WHERE status IN ('pending','retry')
                AND tombstoned=0 AND (next_attempt_at IS NULL OR next_attempt_at<=?)
                """ + scope + " ORDER BY day,created_at,rowid LIMIT 1", args).fetchone()
            if row is None:
                return None
            db.execute("""UPDATE daily_revision_jobs SET status='running',attempts=attempts+1,
                updated_at=?,lease_until=? WHERE run_id=?""",
                (now, (datetime.now(UTC) + timedelta(seconds=lease_seconds)).isoformat(), row["run_id"]))
            return self._job(db.execute("SELECT * FROM daily_revision_jobs WHERE run_id=?", (row["run_id"],)).fetchone())

    def heartbeat(self, run_id: str, seconds=300) -> bool:
        with self._connection(write=True) as db:
            return bool(db.execute("""UPDATE daily_revision_jobs SET lease_until=?,updated_at=?
                WHERE run_id=? AND status='running' AND tombstoned=0""",
                ((datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(), _now(), run_id)).rowcount)

    def checkpoint(self, run_id: str, stage: str, value: Any) -> None:
        with self._connection(write=True) as db:
            row = db.execute("SELECT tombstoned FROM daily_revision_jobs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["tombstoned"]:
                raise ValueError("daily_revision_job_unavailable")
            db.execute("""INSERT INTO daily_revision_stages VALUES(?,?,?,?)
                ON CONFLICT(run_id,stage) DO UPDATE SET value_json=excluded.value_json,updated_at=excluded.updated_at""",
                (run_id, stage, _json(value), _now()))

    def stage(self, run_id: str, stage: str) -> Any | None:
        with self._connection() as db:
            row = db.execute("SELECT value_json FROM daily_revision_stages WHERE run_id=? AND stage=?", (run_id, stage)).fetchone()
            return json.loads(row[0]) if row else None

    def rebase(self, run_id: str, basis: dict) -> None:
        """Keep frozen raw sources and content-addressed caches; invalidate the final proposal."""
        with self._connection(write=True) as db:
            changed = db.execute("""UPDATE daily_revision_jobs SET basis_json=?,updated_at=?
                WHERE run_id=? AND status IN ('pending','retry','running') AND tombstoned=0""",
                (_json(basis), _now(), run_id)).rowcount
            if not changed:
                raise ValueError("daily_revision_job_unavailable")
            db.execute("DELETE FROM daily_revision_stages WHERE run_id=? AND stage IN ('proposal','diff')", (run_id,))

    def reject_proposal(self, run_id: str, error_code="daily_revision_proposal_rejected") -> None:
        """Archive invalid synthesis/review; a manual retry can reuse valid cleaning."""
        now = _now()
        audit = digest([run_id, now, error_code])[:24]
        with self._connection(write=True) as db:
            rows = list(db.execute("SELECT stage,value_json FROM daily_revision_stages WHERE run_id=?", (run_id,)))
            for row in rows:
                value = json.loads(row["value_json"])
                cached_stage = value.get("stage", "") if isinstance(value, dict) else ""
                remove = row["stage"] in {"proposal", "diff"} or (row["stage"].startswith("cache:")
                    and (cached_stage == "synthesize" or cached_stage.startswith("review:")))
                if remove:
                    db.execute("INSERT OR IGNORE INTO daily_revision_stages VALUES(?,?,?,?)",
                        (run_id, "invalid:" + audit + ":" + row["stage"], row["value_json"], now))
                    db.execute("DELETE FROM daily_revision_stages WHERE run_id=? AND stage=?", (run_id, row["stage"]))

    def finish(self, run_id: str, status="applied", receipt: dict | None = None) -> None:
        if status not in {"applied", "dry_run", "rejected", "blocked", "failed"}:
            raise ValueError("daily_revision_status_invalid")
        with self._connection(write=True) as db:
            changed = db.execute("""UPDATE daily_revision_jobs SET status=?,receipt_json=?,last_error=NULL,
                lease_until=NULL,next_attempt_at=NULL,updated_at=? WHERE run_id=? AND tombstoned=0""",
                (status, _json(receipt) if receipt is not None else None, _now(), run_id)).rowcount
            if not changed:
                raise ValueError("daily_revision_job_unavailable")

    def fail(self, run_id: str, error: str, *, retryable=True, max_attempts=3, retry_after_seconds=None) -> None:
        # Keep machine error codes, never raw provider response bodies or exceptions.
        safe_error = error if isinstance(error, str) and error.replace("_", "").isalnum() and len(error) <= 128 else "daily_revision_failed"
        with self._connection(write=True) as db:
            row = db.execute("SELECT attempts FROM daily_revision_jobs WHERE run_id=? AND tombstoned=0", (run_id,)).fetchone()
            if row is None:
                return
            retry = retryable and row["attempts"] < max_attempts
            delay = min(300, 2 ** min(row["attempts"], 8)) if retry_after_seconds is None else max(0, retry_after_seconds)
            db.execute("""UPDATE daily_revision_jobs SET status=?,last_error=?,lease_until=NULL,
                next_attempt_at=?,updated_at=? WHERE run_id=?""",
                ("retry" if retry else "failed", safe_error,
                 (datetime.now(UTC) + timedelta(seconds=delay)).isoformat() if retry else None, _now(), run_id))

    def retry(self, run_id: str) -> bool:
        with self._connection(write=True) as db:
            return bool(db.execute("""UPDATE daily_revision_jobs SET status='pending',attempts=0,
                next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE run_id=?
                AND status IN ('failed','blocked','retry','rejected') AND tombstoned=0""", (_now(), run_id)).rowcount)

    def recover(self, receipts: set[str]) -> None:
        """Called once after acquiring the exclusive Cognitive owner lease."""
        with self._connection(write=True) as db:
            for run_id in receipts:
                db.execute("""UPDATE daily_revision_jobs SET status='applied',lease_until=NULL,
                    next_attempt_at=NULL,last_error=NULL,updated_at=? WHERE run_id=? AND tombstoned=0""", (_now(), run_id))
            db.execute("""UPDATE daily_revision_jobs SET status='pending',lease_until=NULL,
                next_attempt_at=NULL,updated_at=? WHERE status='running' AND tombstoned=0""", (_now(),))

    def tombstone(self, run_id: str) -> bool:
        with self._connection(write=True) as db:
            return bool(db.execute("""UPDATE daily_revision_jobs SET status='tombstoned',tombstoned=1,
                lease_until=NULL,next_attempt_at=NULL,updated_at=? WHERE run_id=?""", (_now(), run_id)).rowcount)

    def invalidate(self, run_id: str) -> bool:
        with self._connection(write=True) as db:
            return bool(db.execute("""UPDATE daily_revision_jobs SET status='invalidated',
                lease_until=NULL,next_attempt_at=NULL,updated_at=? WHERE run_id=? AND tombstoned=0""",
                (_now(), run_id)).rowcount)

    def requeue_invalidated(self, run_id: str, basis: dict | None = None) -> dict:
        """A dependent revision gets a new auditable run after its basis is rolled back."""
        with self._connection(write=True) as db:
            row = db.execute("SELECT * FROM daily_revision_jobs WHERE run_id=?", (run_id,)).fetchone()
            if row is None or row["tombstoned"]:
                raise ValueError("daily_revision_job_unavailable")
            new_basis = basis if basis is not None else json.loads(row["basis_json"])
            successor = "revision-" + digest([run_id, "rebase", new_basis])[:32]
            now = _now()
            db.execute("""UPDATE daily_revision_jobs SET status='invalidated',lease_until=NULL,
                next_attempt_at=NULL,updated_at=? WHERE run_id=?""", (now, run_id))
            db.execute("""INSERT OR IGNORE INTO daily_revision_jobs
                (run_id,owner_id,preset_id,day,partition,source_hash,sources_json,basis_json,
                 config_digest,dry_run,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (successor, row["owner_id"], row["preset_id"], row["day"], row["partition"], row["source_hash"],
                 row["sources_json"], _json(new_basis), row["config_digest"], row["dry_run"], now, now))
            # Content-addressed cache keys include all model/config/prompt/input
            # content. Cleaning can be reused; rebased synthesis gets a new key.
            db.execute("""INSERT OR IGNORE INTO daily_revision_stages(run_id,stage,value_json,updated_at)
                SELECT ?,stage,value_json,updated_at FROM daily_revision_stages
                WHERE run_id=? AND stage LIKE 'cache:%'""", (successor, run_id))
            return self._job(db.execute("SELECT * FROM daily_revision_jobs WHERE run_id=?", (successor,)).fetchone())

    def reserve(self, run_id: str, request_hash: str, day: str, tokens: int, limit: int | None) -> bool:
        if type(tokens) is not int or tokens <= 0 or (limit is not None and (type(limit) is not int or limit <= 0)):
            raise ValueError("daily_revision_budget_invalid")
        with self._connection(write=True) as db:
            job = db.execute("SELECT tombstoned,status FROM daily_revision_jobs WHERE run_id=?", (run_id,)).fetchone()
            if job is None or job["tombstoned"] or job["status"] == "invalidated":
                raise ValueError("daily_revision_job_unavailable")
            old = db.execute("SELECT run_id,day,reserved_tokens FROM daily_revision_usage WHERE request_hash=?", (request_hash,)).fetchone()
            if old is not None:
                if (old["run_id"], old["day"], old["reserved_tokens"]) != (run_id, day, tokens):
                    raise ValueError("daily_revision_reservation_conflict")
                return True
            used = db.execute("SELECT COALESCE(SUM(COALESCE(actual_tokens,reserved_tokens)),0) FROM daily_revision_usage WHERE day=?", (day,)).fetchone()[0]
            if limit is not None and used + tokens > limit:
                return False
            db.execute("""INSERT INTO daily_revision_usage
                (request_hash,run_id,day,reserved_tokens,created_at,updated_at) VALUES(?,?,?,?,?,?)""",
                (request_hash, run_id, day, tokens, _now(), _now()))
            return True

    def account(self, request_hash: str, usage: dict | None, status="completed") -> None:
        actual = None
        safe_usage = {}
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                value = usage.get(key)
                if type(value) is int and value >= 0:
                    safe_usage[key] = value
            if "prompt_tokens" in safe_usage and "completion_tokens" in safe_usage:
                actual = safe_usage["prompt_tokens"] + safe_usage["completion_tokens"]
            if "total_tokens" in safe_usage:
                actual = max(actual or 0, safe_usage["total_tokens"])
        with self._connection(write=True) as db:
            db.execute("""UPDATE daily_revision_usage SET actual_tokens=?,usage_json=?,status=?,updated_at=?
                WHERE request_hash=?""", (actual, _json(safe_usage), status, _now(), request_hash))

    def status(self) -> dict:
        with self._connection() as db:
            counts = {row[0]: row[1] for row in db.execute("SELECT status,COUNT(*) FROM daily_revision_jobs GROUP BY status")}
            usage = [dict(row) for row in db.execute("""SELECT day,SUM(COALESCE(actual_tokens,reserved_tokens)) AS charged_tokens,
                SUM(CASE WHEN actual_tokens IS NULL THEN reserved_tokens ELSE 0 END) AS unconfirmed_tokens,
                COUNT(*) AS requests FROM daily_revision_usage GROUP BY day ORDER BY day DESC LIMIT 30""")]
        return {"counts": counts, "usage": usage}
