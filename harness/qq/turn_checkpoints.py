"""Atomic QQ gate/body checkpoints alongside their durable final decisions."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import sqlite3


def canonical(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class QQTurnStateError(ValueError):
    """A fixed, nonfatal rejection; never an authorization to generate again."""


def binding_identity(binding: dict) -> str:
    required = {"event_id", "transport", "group_id", "self_member_id", "runtime_instance_id",
                "manifest_revision", "deadline_at"}
    if not required <= binding.keys() or any(binding.get(key) is None for key in required):
        raise QQTurnStateError("qq_turn_binding_invalid")
    expiry = datetime.fromisoformat(binding["deadline_at"])
    if expiry.tzinfo is None:
        raise QQTurnStateError("qq_turn_binding_invalid")
    # Identity is stable across a duplicate delivery, while the entire binding
    # (including original TTL, emotion/source/revision/authority) must match.
    return "qq-prepare:" + digest([binding["event_id"], binding["transport"], binding["group_id"],
                                   binding["self_member_id"]])


class QQTurnCheckpointStore:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if not self.path.is_file():
            raise QQTurnStateError("qq_turn_store_missing")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA journal_mode=WAL")
        db.execute("PRAGMA synchronous=FULL")
        db.execute("""CREATE TABLE IF NOT EXISTS qq_turn_checkpoints (
            prepare_id TEXT PRIMARY KEY, binding_hash TEXT NOT NULL, status TEXT NOT NULL,
            snapshot_hash TEXT, resume_hash TEXT, record_json TEXT NOT NULL)""")
        return db

    @staticmethod
    def _checked(row, binding: dict):
        if row["binding_hash"] != digest(binding):
            raise QQTurnStateError("qq_turn_binding_conflict")
        value = json.loads(row["record_json"])
        if value["binding"] != binding:
            raise QQTurnStateError("qq_turn_binding_conflict")
        snapshot = value.get("snapshot")
        if snapshot is not None and digest(snapshot) != row["snapshot_hash"]:
            raise QQTurnStateError("qq_turn_snapshot_corrupt")
        return {**value, "prepare_id": row["prepare_id"], "status": row["status"],
                "snapshot_hash": row["snapshot_hash"], "resume_hash": row["resume_hash"]}

    def get(self, binding: dict):
        identity = binding_identity(binding)
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM qq_turn_checkpoints WHERE prepare_id=?", (identity,)).fetchone()
            return self._checked(row, binding) if row is not None else None

    def load(self, prepare_id: str):
        with closing(self._connect()) as db:
            row = db.execute("SELECT * FROM qq_turn_checkpoints WHERE prepare_id=?", (prepare_id,)).fetchone()
            if row is None:
                raise QQTurnStateError("qq_turn_not_found")
            binding = json.loads(row["record_json"])["binding"]
            if binding_identity(binding) != prepare_id:
                raise QQTurnStateError("qq_turn_binding_conflict")
            return self._checked(row, binding)

    def reserve(self, binding: dict) -> str:
        identity = binding_identity(binding)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_turn_checkpoints WHERE prepare_id=?", (identity,)).fetchone()
            if row is not None:
                self._checked(row, binding)
                raise QQTurnStateError("qq_turn_already_reserved")
            db.execute("INSERT INTO qq_turn_checkpoints VALUES (?,?,?,NULL,NULL,?)",
                       (identity, digest(binding), "gate_running", canonical({"binding": binding})))
        return identity

    def prepared(self, binding: dict, snapshot: dict, public: dict):
        identity = binding_identity(binding)
        snapshot_hash = digest(snapshot)
        public = {**public, "prepare_id": identity, "snapshot_hash": snapshot_hash}
        with closing(self._connect()) as db, db:
            changed = db.execute("UPDATE qq_turn_checkpoints SET status='prepared', snapshot_hash=?, record_json=? "
                "WHERE prepare_id=? AND binding_hash=? AND status='gate_running'",
                (snapshot_hash, canonical({"binding": binding, "snapshot": snapshot, "public": public}),
                 identity, digest(binding))).rowcount
            if changed != 1:
                raise QQTurnStateError("qq_turn_prepare_conflict")
        return public

    def claim_resume(self, binding: dict, snapshot_hash: str, resume_hash: str):
        identity = binding_identity(binding)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_turn_checkpoints WHERE prepare_id=?", (identity,)).fetchone()
            if row is None:
                raise QQTurnStateError("qq_turn_not_found")
            value = self._checked(row, binding)
            if row["snapshot_hash"] != snapshot_hash:
                raise QQTurnStateError("qq_turn_snapshot_conflict")
            if row["resume_hash"] is not None and row["resume_hash"] != resume_hash:
                raise QQTurnStateError("qq_turn_resume_conflict")
            if row["status"] == "completed":
                return value
            if row["status"] != "prepared":
                raise QQTurnStateError("qq_turn_in_progress_or_interrupted")
            if datetime.now(timezone.utc) >= datetime.fromisoformat(binding["deadline_at"]):
                raise QQTurnStateError("qq_turn_expired")
            db.execute("UPDATE qq_turn_checkpoints SET status='persona_running',resume_hash=? WHERE prepare_id=?",
                       (resume_hash, identity))
            return value

    def complete(self, binding: dict, decision: dict, *, used_sources=None):
        identity = binding_identity(binding)
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_turn_checkpoints WHERE prepare_id=?", (identity,)).fetchone()
            if row is None:
                raise QQTurnStateError("qq_turn_not_found")
            value = self._checked(row, binding)
            if row["status"] == "completed":
                if value["public"]["decision"] != decision:
                    raise QQTurnStateError("qq_turn_completion_conflict")
                return value["public"]
            if row["status"] not in {"gate_running", "persona_running"}:
                # Cancellation/expiry wins over a model result which arrives
                # later. A merely prepared gate has not reserved a body call.
                error = "qq_turn_" + row["status"] if row["status"] in {
                    "cancelled", "expired", "interrupted"} else "qq_turn_completion_conflict"
                raise QQTurnStateError(error)
            if (value.get("snapshot") is not None
                    and value["snapshot"]["decision"]["decision_id"] != decision["decision_id"]):
                raise QQTurnStateError("qq_turn_completion_conflict")
            public = {"kind": "completed", "prepare_id": identity, "decision": decision}
            # The already-reserved decision and checkpoint become visible in
            # one transaction. A prepared gate is never a completed wait row.
            db.execute("UPDATE qq_reply_decisions SET status='completed',record_json=? WHERE decision_id=?",
                       (canonical(decision), decision["decision_id"]))
            record = {"binding": binding, "public": public}
            if used_sources is not None:
                record["used_sources"] = used_sources
            elif value.get("used_sources") is not None:
                record["used_sources"] = value["used_sources"]
            if value.get("snapshot") is not None:
                record["snapshot"] = value["snapshot"]
            db.execute("UPDATE qq_turn_checkpoints SET status='completed',record_json=? WHERE prepare_id=?",
                       (canonical(record), identity))
            return public

    def interrupt(self, binding: dict):
        self.terminate(binding, "interrupted", only_gate=True)

    def terminate(self, binding: dict, status: str, *, only_gate: bool = False):
        if status not in {"expired", "cancelled", "interrupted"}:
            raise ValueError("invalid QQ terminal state")
        with closing(self._connect()) as db, db:
            db.execute("UPDATE qq_turn_checkpoints SET status=? WHERE prepare_id=? AND binding_hash=? "
                       "AND status IN ('gate_running','prepared','persona_running') "
                       "AND (?=0 OR status='gate_running')",
                       (status, binding_identity(binding), digest(binding), int(only_gate)))

    def finalize_expired(self, binding: dict, decision: dict):
        if datetime.now(timezone.utc) < datetime.fromisoformat(binding["deadline_at"]):
            raise QQTurnStateError("qq_turn_not_expired")
        if decision.get("outcome") not in {"expired", "error_fallback"} or decision.get("error_type") != "deadline_expired":
            raise QQTurnStateError("qq_turn_expiry_result_invalid")
        with closing(self._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM qq_turn_checkpoints WHERE prepare_id=?", (binding_identity(binding),)).fetchone()
            if row is None:
                raise QQTurnStateError("qq_turn_not_found")
            value = self._checked(row, binding)
            if row["status"] != "completed" or value["public"]["decision"]["decision_id"] != decision["decision_id"]:
                raise QQTurnStateError("qq_turn_completion_conflict")
            record = {key: value[key] for key in ("binding", "snapshot", "public") if key in value}
            record["public"] = {**record["public"], "decision": decision}
            db.execute("UPDATE qq_reply_decisions SET record_json=? WHERE decision_id=?",
                       (canonical(decision), decision["decision_id"]))
            db.execute("UPDATE qq_turn_checkpoints SET record_json=? WHERE prepare_id=?",
                       (canonical(record), binding_identity(binding)))
