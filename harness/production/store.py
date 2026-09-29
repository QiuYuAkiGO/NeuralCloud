"""Durable production budgets, goals and usage publication (not a second job log)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from harness.production.config import ProductionConfig


class ProductionBudgetExceeded(ValueError):
    pass


def _json(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


class ProductionStore:
    def __init__(self, path, config=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.config = config or ProductionConfig()
        with self._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS attempts(
                    id TEXT PRIMARY KEY, purpose TEXT NOT NULL, autonomous INTEGER NOT NULL,
                    at REAL NOT NULL, receipt TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS attempts_window ON attempts(autonomous,at);
                CREATE TABLE IF NOT EXISTS usage_outbox(
                    id TEXT PRIMARY KEY, attempt_id TEXT NOT NULL, payload TEXT NOT NULL,
                    receipt TEXT, acknowledged INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS usage_assignments(
                    usage_id TEXT PRIMARY KEY, assignment_id TEXT NOT NULL UNIQUE,
                    request_hash TEXT NOT NULL, receipt TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS goals(id TEXT PRIMARY KEY,status TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS observations(id TEXT PRIMARY KEY,kind TEXT NOT NULL,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)

    @contextmanager
    def _db(self):
        # Each command owns its connection. No shared connection crosses threads.
        db = sqlite3.connect(self.path, timeout=5)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    @staticmethod
    def _now(now):
        value = now or datetime.now(UTC)
        if value.tzinfo is None:
            raise ValueError("production_timestamp_requires_timezone")
        return value.timestamp()

    def reserve_attempt(self, attempt_id, *, purpose, autonomous=True, now=None):
        if not isinstance(attempt_id, str) or not attempt_id or not isinstance(purpose, str) or not purpose:
            raise ValueError("production_attempt_identity_required")
        at = self._now(now)
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM attempts WHERE id=?", (attempt_id,)).fetchone()
            if old:
                if old["purpose"] != purpose or old["autonomous"] != int(autonomous):
                    raise ValueError("production_attempt_identity_conflict")
                return json.loads(old["receipt"])
            used = db.execute("SELECT count(*) FROM attempts WHERE autonomous=1 AND at>?", (at - 3600,)).fetchone()[0]
            if autonomous and used >= self.config.autonomous_attempts_per_hour:
                raise ProductionBudgetExceeded("production_autonomous_hourly_budget_exhausted")
            receipt = {"attempt_id": attempt_id, "receipt_id": "attempt:" + hashlib.sha256(attempt_id.encode()).hexdigest(),
                       "purpose": purpose, "autonomous": bool(autonomous), "reserved_at": datetime.fromtimestamp(at, UTC).isoformat()}
            db.execute("INSERT INTO attempts VALUES(?,?,?,?,?)", (attempt_id, purpose, int(autonomous), at, _json(receipt)))
            return receipt

    def budget(self, *, now=None):
        at = self._now(now)
        with self._db() as db:
            row = db.execute("SELECT count(*),min(at) FROM attempts WHERE autonomous=1 AND at>?", (at - 3600,)).fetchone()
        return {"used": row[0], "limit": self.config.autonomous_attempts_per_hour,
                "remaining": max(0, self.config.autonomous_attempts_per_hour - row[0]),
                "next_available_at": datetime.fromtimestamp(row[1] + 3600, UTC).isoformat()
                if row[0] >= self.config.autonomous_attempts_per_hour else None}

    def record_usage(self, attempt_id, usage):
        payload = _json(usage)
        identity = "usage:" + hashlib.sha256(attempt_id.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            if not db.execute("SELECT 1 FROM attempts WHERE id=?", (attempt_id,)).fetchone():
                raise ValueError("production_usage_without_reservation")
            old = db.execute("SELECT payload FROM usage_outbox WHERE id=?", (identity,)).fetchone()
            if old and old[0] != payload:
                raise ValueError("production_usage_identity_conflict")
            db.execute("INSERT OR IGNORE INTO usage_outbox(id,attempt_id,payload) VALUES(?,?,?)", (identity, attempt_id, payload))
        return {"usage_id": identity, "attempt_id": attempt_id, "durable": True}

    def pending_usage(self, *, limit=32, body_instance_id=None, episode_id=None):
        clause, parameters = "", []
        if body_instance_id is not None and episode_id is not None:
            clause = """ AND COALESCE(NULLIF(json_extract(u.payload,'$.body_instance_id'),''),
                json_extract(a.receipt,'$.body_instance_id'))=?
                AND COALESCE(NULLIF(json_extract(u.payload,'$.episode_id'),''),json_extract(a.receipt,'$.episode_id'))=?"""
            parameters.extend([body_instance_id, episode_id])
        with self._db() as db:
            return [{"usage_id": r["id"], "attempt_id": r["attempt_id"], "usage": json.loads(r["payload"]),
                     **({"assignment": json.loads(r["assignment"])} if r["assignment"] else {})}
                    for r in db.execute("""SELECT u.*,a.receipt AS assignment FROM usage_outbox u
                        LEFT JOIN usage_assignments a ON a.usage_id=u.id WHERE u.acknowledged=0"""
                        + clause + " ORDER BY u.rowid LIMIT ?", (*parameters, limit))]

    def unassigned_usage(self, *, limit=100, offset=0):
        with self._db() as db:
            return [{"usage_id": row["id"], "attempt_id": row["attempt_id"],
                     "usage_hash": hashlib.sha256(row["payload"].encode()).hexdigest(),
                     "usage": json.loads(row["payload"])} for row in db.execute("""
                SELECT u.* FROM usage_outbox u LEFT JOIN usage_assignments a ON a.usage_id=u.id
                WHERE u.acknowledged=0 AND NULLIF(json_extract(u.payload,'$.episode_id'),'') IS NULL
                AND a.usage_id IS NULL ORDER BY u.rowid LIMIT ? OFFSET ?""", (limit, offset))]

    def unassigned_usage_count(self):
        with self._db() as db:
            return db.execute("""SELECT count(*) FROM usage_outbox u
                LEFT JOIN usage_assignments a ON a.usage_id=u.id WHERE u.acknowledged=0
                AND NULLIF(json_extract(u.payload,'$.episode_id'),'') IS NULL AND a.usage_id IS NULL""").fetchone()[0]

    def usage_assignment(self, request):
        request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
        with self._db() as db:
            old = db.execute("SELECT * FROM usage_assignments WHERE assignment_id=?", (request["assignment_id"],)).fetchone()
        if old is not None:
            if old["request_hash"] != request_hash:
                raise ValueError("production_usage_assignment_identity_conflict")
            return json.loads(old["receipt"])
        return None

    def assign_usage(self, request, *, verified_body):
        request_hash = hashlib.sha256(_json(request).encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT * FROM usage_assignments WHERE assignment_id=?", (request["assignment_id"],)).fetchone()
            if old is not None:
                if old["request_hash"] != request_hash:
                    raise ValueError("production_usage_assignment_identity_conflict")
                return json.loads(old["receipt"])
            row = db.execute("SELECT * FROM usage_outbox WHERE attempt_id=?", (request["attempt_id"],)).fetchone()
            if row is None or row["acknowledged"]:
                raise ValueError("production_usage_assignment_requires_pending_usage")
            usage = json.loads(row["payload"])
            if usage.get("episode_id"):
                raise ValueError("production_usage_explicit_episode_cannot_be_reassigned")
            if usage.get("body_instance_id") and usage["body_instance_id"] != request["body_instance_id"]:
                raise ValueError("production_usage_original_body_mismatch")
            if hashlib.sha256(row["payload"].encode()).hexdigest() != request["expected_usage_hash"]:
                raise ValueError("production_usage_assignment_hash_mismatch")
            if db.execute("SELECT 1 FROM usage_assignments WHERE usage_id=?", (row["id"],)).fetchone():
                raise ValueError("production_usage_already_assigned")
            if any(verified_body.get(key) != request[key] for key in ("body_instance_id", "episode_id")):
                raise ValueError("production_usage_assignment_target_mismatch")
            receipt = {**request, "usage_id": row["id"], "assigned_at": datetime.now(UTC).isoformat(),
                "assignment_basis": "operator_allocation_after_unknown_body",
                "occurrence_body_verified": False, "verified_current_body": verified_body}
            db.execute("INSERT INTO usage_assignments VALUES(?,?,?,?)", (
                row["id"], request["assignment_id"], request_hash, _json(receipt)))
            return receipt

    def record_delivery(self, receipt_id, *, body_instance_id, episode_id):
        """Verified protocol effects are costs, never autonomous model attempts."""
        attempt = "delivery:" + receipt_id
        identity = "usage:" + hashlib.sha256(attempt.encode()).hexdigest()
        payload = _json({"source": "delivery", "receipt_id": receipt_id,
            "body_instance_id": body_instance_id, "episode_id": episode_id})
        with self._db() as db:
            old = db.execute("SELECT payload FROM usage_outbox WHERE id=?", (identity,)).fetchone()
            if old and old[0] != payload:
                raise ValueError("production_delivery_identity_conflict")
            db.execute("INSERT OR IGNORE INTO usage_outbox(id,attempt_id,payload) VALUES(?,?,?)", (identity, attempt, payload))
        return {"usage_id": identity, "durable": True}

    def pending_usage_count(self):
        with self._db() as db:
            return db.execute("SELECT count(*) FROM usage_outbox WHERE acknowledged=0").fetchone()[0]

    def ack_usage(self, usage_id, receipt):
        with self._db() as db:
            db.execute("UPDATE usage_outbox SET acknowledged=1,receipt=? WHERE id=?", (_json(receipt), usage_id))

    def observe(self, event):
        with self._db() as db:
            inserted = db.execute("INSERT OR IGNORE INTO observations VALUES(?,?,?)", (event.event_id, event.event_type.value, event.received_at.timestamp()))
            if inserted.rowcount and event.event_type.value in {"user_message", "qq_turn", "environment_change", "action_result"}:
                db.execute("INSERT INTO state VALUES('signal_revision',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (_json(event.receive_seq),))

    def goal(self, goal_id, *, audience=None):
        with self._db() as db:
            row = db.execute("SELECT payload FROM goals WHERE id=?", (goal_id,)).fetchone()
        result = json.loads(row[0]) if row else None
        if result is not None and audience is not None and result.get("audience", "owner") != str(audience):
            return None
        return result

    def state(self, key, default=None):
        with self._db() as db:
            row = db.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put_state(self, key, value):
        with self._db() as db:
            db.execute("INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, _json(value)))

    def unresolved_rest(self):
        with self._db() as db:
            return [json.loads(row[0]) for row in db.execute("SELECT value FROM state WHERE key LIKE 'rest-request:%' AND json_extract(value,'$.status')<>'succeeded'")]

    def goals(self, *, audience=None):
        with self._db() as db:
            rows = [json.loads(row[0]) for row in db.execute("SELECT payload FROM goals WHERE status IN ('candidate','active','paused','suspended') ORDER BY rowid")]
        return [row for row in rows if audience is None or row.get("audience", "owner") == str(audience)][:32]

    def apply_goal_update(self, update, *, event_id, verified_evidence, audience="owner"):
        from harness.production.models import ProductionGoalUpdate
        update = ProductionGoalUpdate.model_validate(update)
        allowed = set(verified_evidence)
        if set(update.evidence_refs) - allowed or (update.status == "completed" and not update.evidence_refs):
            raise ValueError("production_goal_evidence_unverified")
        identity = update.goal_id or "goal:" + hashlib.sha256(event_id.encode()).hexdigest()
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT payload FROM goals WHERE id=?", (identity,)).fetchone()
            if update.goal_id and old is None:
                raise ValueError("production_goal_not_found")
            row = json.loads(old[0]) if old else {"id": identity, "steps": 0, "no_progress": 0, "outcomes": [], "updates": [], "audience": str(audience)}
            if row.get("audience", "owner") != str(audience):
                raise ValueError("production_goal_audience_mismatch")
            updates = row.setdefault("updates", [])
            hashes = row.setdefault("update_hashes", {})
            fingerprint = hashlib.sha256(_json(update.model_dump(mode="json")).encode()).hexdigest()
            if event_id in updates:
                if hashes.get(event_id) != fingerprint:
                    raise ValueError("production_goal_update_identity_conflict")
                return row
            if update.status == "active":
                if row["steps"] >= self.config.maximum_plan_steps or row["no_progress"] >= self.config.maximum_no_progress:
                    raise ValueError("production_goal_execution_limit")
                count = db.execute("SELECT count(*) FROM goals WHERE status='active' AND id<>?", (identity,)).fetchone()[0]
                if count >= self.config.maximum_active_goals:
                    raise ValueError("production_active_goal_limit")
            row.update(status=update.status, goal={"description": update.description, "success_criteria": update.success_criteria},
                       reason=update.reason, evidence_refs=list(update.evidence_refs),
                       deadline_at=update.deadline_at.isoformat() if update.deadline_at else None)
            updates.append(event_id)
            hashes[event_id] = fingerprint
            db.execute("INSERT INTO goals VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                       (identity, row["status"], _json(row)))
            return row

    def save_goal(self, goal_id, goal, *, audience="owner"):
        """Idempotent submission: never invent psychological targets or rewards."""
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT payload FROM goals WHERE id=?", (goal_id,)).fetchone()
            if existing:
                row = json.loads(existing[0])
                if row.get("audience", "owner") != str(audience):
                    raise ValueError("production_goal_audience_mismatch")
                return row
            if db.execute("SELECT count(*) FROM goals WHERE status='active'").fetchone()[0] >= self.config.maximum_active_goals:
                raise ValueError("production_active_goal_limit")
            row = {"id": goal_id, "status": "active", "goal": goal, "steps": 0, "no_progress": 0, "outcomes": [], "audience": str(audience)}
            db.execute("INSERT INTO goals VALUES(?,?,?)", (goal_id, "active", _json(row)))
            return row

    def record_goal_outcome(self, goal_id, *, evidence_id, progress, completed=False, result=None):
        with self._db() as db:
            db.execute("BEGIN IMMEDIATE")
            saved = db.execute("SELECT payload FROM goals WHERE id=?", (goal_id,)).fetchone()
            if not saved:
                raise ValueError("production_goal_not_found")
            row = json.loads(saved[0])
            if evidence_id in row["outcomes"]:
                return row
            was_active = row["status"] == "active"
            row["outcomes"].append(evidence_id)
            if result is not None:
                row.setdefault("results", []).append({"evidence_id": evidence_id, **result})
            row["steps"] += 1
            row["no_progress"] = 0 if progress else row["no_progress"] + 1
            if completed and was_active:
                row["status"] = "completed"
            elif was_active and (row["steps"] >= self.config.maximum_plan_steps or row["no_progress"] >= self.config.maximum_no_progress):
                row["status"] = "suspended"
            db.execute("UPDATE goals SET status=?,payload=? WHERE id=?", (row["status"], _json(row), goal_id))
            return row
