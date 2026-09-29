"""Durable operation facts for tools whose physical costs are owned by NC."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime

from harness.production.store import _json


class ToolAccounting:
    # QQ's protocol receipts and MistyAI's action ledger already own their costs.
    CHANNELS = frozenset({"web", "server", "board", "diary", "voice"})

    def __init__(self, store):
        self.store = store
        with store._db() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS tool_operations(
                identity TEXT PRIMARY KEY, channel TEXT NOT NULL, operation_id TEXT NOT NULL,
                capability TEXT NOT NULL, payload TEXT NOT NULL, status TEXT NOT NULL,
                outcome TEXT, evidence_refs TEXT NOT NULL DEFAULT '[]')""")
            # The Runtime owner lease excludes a second live owner. A recorded
            # dispatch may have completed remotely; recovery never invents success.
            for row in db.execute("SELECT * FROM tool_operations WHERE status='dispatched'").fetchall():
                self._publish(db, row)
                db.execute("UPDATE tool_operations SET status='finished',outcome='unknown' WHERE identity=?", (row["identity"],))

    @staticmethod
    def identity(channel, operation_id):
        if channel not in ToolAccounting.CHANNELS or not isinstance(operation_id, str) or not operation_id:
            raise ValueError("production_tool_identity_invalid")
        return "tool-operation:" + hashlib.sha256(_json([channel, operation_id]).encode()).hexdigest()

    def begin(self, channel, operation_id, capability, *, binding):
        identity = self.identity(channel, operation_id)
        if not isinstance(capability, str) or not capability:
            raise ValueError("production_tool_capability_required")
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tool_operations WHERE identity=?", (identity,)).fetchone()
            if row is not None:
                if row["capability"] != capability:
                    raise ValueError("production_tool_identity_conflict")
                if row["status"] != "cancelled" or "dispatched_at" in json.loads(row["payload"]):
                    return identity
            payload = {"source": "tool", "channel": channel, "operation_id": operation_id,
                "capability": capability, "body_instance_id": binding.get("body_instance_id"),
                "episode_id": binding.get("episode_id")}
            db.execute("""INSERT INTO tool_operations(identity,channel,operation_id,capability,payload,status) VALUES(?,?,?,?,?,'prepared')
                ON CONFLICT(identity) DO UPDATE SET payload=excluded.payload,status='prepared'""",
                (identity, channel, operation_id, capability, _json(payload)))
        return identity

    def dispatched(self, channel, operation_id):
        identity = self.identity(channel, operation_id)
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tool_operations WHERE identity=?", (identity,)).fetchone()
            if row is None or row["status"] == "cancelled":
                raise ValueError("production_tool_not_prepared")
            if row["status"] == "prepared":
                payload = {**json.loads(row["payload"]), "dispatched_at": datetime.now(UTC).isoformat()}
                db.execute("UPDATE tool_operations SET status='dispatched',payload=? WHERE identity=?", (_json(payload), identity))
                # The unit is one dispatch, independent of its eventual outcome.
                # Publish atomically at the final transport boundary so a crash
                # while saving the result cannot lose a real operation's cost.
                self._publish(db, {"identity": identity, "payload": _json(payload)})

    @staticmethod
    def _publish(db, row):
        # Outcome/evidence may be reconciled later; the one dispatched operation
        # is the immutable billed fact and keeps the same body/usage identity.
        usage_id = "usage:" + hashlib.sha256(row["identity"].encode()).hexdigest()
        old = db.execute("SELECT payload FROM usage_outbox WHERE id=?", (usage_id,)).fetchone()
        if old is not None and old["payload"] != row["payload"]:
            raise ValueError("production_tool_usage_identity_conflict")
        db.execute("INSERT OR IGNORE INTO usage_outbox(id,attempt_id,payload) VALUES(?,?,?)",
            (usage_id, row["identity"], row["payload"]))

    def finish(self, channel, operation_id, *, outcome, evidence_refs=()):
        if outcome not in {"completed", "failed", "unknown"}:
            raise ValueError("production_tool_outcome_invalid")
        identity = self.identity(channel, operation_id)
        refs = list(dict.fromkeys(evidence_refs))
        if any(not isinstance(ref, str) or not ref for ref in refs):
            raise ValueError("production_tool_evidence_invalid")
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM tool_operations WHERE identity=?", (identity,)).fetchone()
            if row is None or row["status"] not in {"dispatched", "finished"}:
                raise ValueError("production_tool_not_dispatched")
            if row["outcome"] in {"completed", "failed"}:
                if row["outcome"] != outcome and outcome != "unknown":
                    raise ValueError("production_tool_outcome_conflict")
                return
            self._publish(db, row)
            db.execute("UPDATE tool_operations SET status='finished',outcome=?,evidence_refs=? WHERE identity=?",
                (outcome, _json(refs), identity))

    def cancel(self, channel, operation_id):
        identity = self.identity(channel, operation_id)
        with self.store._db() as db:
            db.execute("UPDATE tool_operations SET status='cancelled' WHERE identity=? AND status='prepared'", (identity,))

    def get(self, channel, operation_id):
        with self.store._db() as db:
            row = db.execute("SELECT * FROM tool_operations WHERE identity=?", (self.identity(channel, operation_id),)).fetchone()
        return dict(row) if row else None
