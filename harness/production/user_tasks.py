"""Runtime-owned durable user jobs; execution and QQ delivery are separate facts.

No model-facing arbitrary-job API is exposed. Capability handlers register with
the Runtime, and ingress must first prove a current private protocol message.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import sqlite3
from uuid import uuid4

from harness.qq.direct_store import canonical, utcnow


class UserTaskStore:
    def __init__(self, production_store):
        self.store = production_store
        with self.store._db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS user_tasks(
                    id TEXT PRIMARY KEY, source_event_id TEXT NOT NULL UNIQUE,
                    request_json TEXT NOT NULL, status TEXT NOT NULL, worker TEXT,
                    result_json TEXT, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS user_task_notices(
                    id TEXT PRIMARY KEY, task_id TEXT NOT NULL, purpose TEXT NOT NULL,
                    text TEXT NOT NULL, delivery_json TEXT,
                    UNIQUE(task_id,purpose));
            """)

    @staticmethod
    def _notice(db, identity, purpose, text):
        db.execute("INSERT INTO user_task_notices(id,task_id,purpose,text) VALUES(?,?,?,?)",
                   (identity + ":" + purpose, identity, purpose, text))

    def accept(self, source, kind, arguments, *, limit=4):
        request = canonical({"source": source, "kind": kind, "arguments": arguments})
        identity = "user-task:" + hashlib.sha256(source["event_id"].encode()).hexdigest()[:32]
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT request_json FROM user_tasks WHERE id=?", (identity,)).fetchone()
            if old:
                if old[0] != request:
                    raise ValueError("user_task_source_conflict")
            else:
                count = db.execute("SELECT count(*) FROM user_tasks WHERE status IN ('queued','running','awaiting_approval')").fetchone()[0]
                if count >= limit:
                    raise ValueError("user_task_queue_full")
                db.execute("INSERT INTO user_tasks VALUES(?,?,?,'queued',NULL,NULL,?)",
                           (identity, source["event_id"], request, utcnow().isoformat()))
                self._notice(db, identity, "task_accepted", f"已收到任务 {identity[-8:]}，会在后台处理，完成后告诉你结果。")
        return self.get(identity)

    @staticmethod
    def _value(row):
        return dict(row) | {"request": json.loads(row["request_json"]),
                            "result": json.loads(row["result_json"]) if row["result_json"] else None}

    def get(self, identity):
        with self.store._db() as db:
            row = db.execute("SELECT * FROM user_tasks WHERE id=?", (identity,)).fetchone()
            if row is None:
                raise ValueError("user_task_missing")
            value = self._value(row)
            value["notifications"] = [dict(n) | {"delivery": json.loads(n["delivery_json"]) if n["delivery_json"] else None}
                for n in db.execute("SELECT * FROM user_task_notices WHERE task_id=? ORDER BY rowid", (identity,))]
        return value

    def claim(self, kinds):
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            # Even two coordinators cannot exceed the global one-worker limit.
            if db.execute("SELECT 1 FROM user_tasks WHERE status='running'").fetchone():
                return None
            row = next((r for r in db.execute("SELECT * FROM user_tasks WHERE status='queued' ORDER BY rowid")
                        if json.loads(r["request_json"])["kind"] in kinds), None)
            if row is None:
                return None
            worker = uuid4().hex
            db.execute("UPDATE user_tasks SET status='running',worker=? WHERE id=?", (worker, row["id"]))
        return self.get(row["id"])

    def finish(self, identity, worker, status, text, *, evidence_refs=()):
        if status not in {"succeeded", "failed", "uncertain"} or not isinstance(text, str) or not text.strip() or len(text) > 2800:
            raise ValueError("user_task_result_invalid")
        result = canonical({"text": text, "evidence_refs": list(evidence_refs)})
        with self.store._db() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM user_tasks WHERE id=?", (identity,)).fetchone()
            if row is None or row["worker"] != worker:
                raise ValueError("user_task_worker_changed")
            if row["status"] == status and row["result_json"] == result:
                return
            if row["status"] != "running":
                raise ValueError("user_task_terminal_immutable")
            db.execute("UPDATE user_tasks SET status=?,result_json=? WHERE id=?", (status, result, identity))
            label = {"succeeded": "处理完成", "failed": "处理失败", "uncertain": "结果待核实"}[status]
            self._notice(db, identity, "task_result", f"任务 {identity[-8:]} {label}：\n{text}")

    def recover(self):
        # Called once by the owning Runtime after its prior workers have stopped.
        with self.store._db() as db:
            interrupted = [(r["id"], r["worker"]) for r in db.execute("SELECT id,worker FROM user_tasks WHERE status='running'")]
        for identity, worker in interrupted:
            self.finish(identity, worker, "uncertain", "处理被中断，结果需要核实；不会自动重复执行。")

    def pending_notices(self):
        with self.store._db() as db:
            return [dict(r) for r in db.execute("""SELECT * FROM user_task_notices
                WHERE delivery_json IS NULL OR json_extract(delivery_json,'$.status') IN ('pending','inflight','unknown')
                ORDER BY rowid LIMIT 16""")]

    def delivery(self, identity, value):
        if value.get("status") not in {"pending", "inflight", "unknown", "delivered", "cancelled"}:
            raise ValueError("user_task_delivery_invalid")
        if value["status"] == "delivered" and not value.get("receipt_id"):
            raise ValueError("user_task_delivery_receipt_required")
        with self.store._db() as db:
            db.execute("UPDATE user_task_notices SET delivery_json=? WHERE id=?", (canonical(value), identity))

    def summary(self):
        with self.store._db() as db:
            return {r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM user_tasks GROUP BY status")}


class UserTaskRunner:
    def __init__(self, runtime):
        self.runtime = runtime
        self.store = UserTaskStore(runtime.store)
        self.handlers = {}
        self.task = None
        self.last_error = None
        self.store.recover()

    def _bridge(self):
        from harness.qq.direct_capabilities import QQCapabilityTransports
        bridge = getattr(self.runtime.supervisor, "qq_capability_bridge", None)
        return bridge.direct if isinstance(bridge, QQCapabilityTransports) else bridge

    def register(self, kind, handler, validate):
        if kind in self.handlers:
            raise ValueError("user_task_handler_already_registered")
        self.handlers[kind] = (handler, validate)

    def accept(self, request, kind, arguments):
        from harness.qq.direct_source import validate_direct_source
        from harness.qq.conversation import conversation_scope
        from harness.qq.direct_store import DirectStore
        from harness.qq.direct_config import DirectQQSettings
        from harness.qq.session import QQSessionSettings
        bridge = self._bridge()
        if kind not in self.handlers or bridge is None:
            raise ValueError("user_task_handler_unavailable")
        scope = conversation_scope(request.group_id)
        if scope["kind"] != "private" or request.idle_intent is not None:
            raise ValueError("user_task_private_message_required")
        configured = os.environ.get("NC_QQ_DIRECT_CONFIG")
        if not configured or Path(configured).absolute() != bridge.config_path.absolute():
            raise ValueError("user_task_transport_binding_changed")
        settings = DirectQQSettings.read(bridge.config_path)
        config = QQSessionSettings.model_validate_json(settings.session_config_path.read_text(encoding="utf-8"))
        validate_direct_source(config, request)
        event = DirectStore(settings.state_db, settings.evidence_dir).get(request.trace.event_id)["event"]
        arguments = self.handlers[kind][1](arguments)
        source = {k: event[k] for k in ("event_id", "group_id", "self_member_id", "member_id", "event_evidence_ref")}
        return self.store.accept(source, kind, arguments)

    def flush_notices(self):
        bridge = self._bridge()
        if bridge is None or not callable(getattr(bridge, "publish_task_notice", None)):
            return
        for notice in self.store.pending_notices():
            try:
                result = bridge.publish_task_notice(self.store.get(notice["task_id"]), notice)
                self.store.delivery(notice["id"], result)
            except (ValueError, OSError, sqlite3.Error):
                # Publication is idempotent. Never rerun the task to resend a result.
                break

    def tick(self):
        try:
            self.flush_notices()
            if self.task is not None:
                if not self.task.done():
                    return
                if not self.task.cancelled():
                    self.task.result()
            row = self.store.claim(self.handlers)
            if row is not None:
                self.task = asyncio.create_task(self._run(row), name=row["id"])
            self.last_error = None
        except (ValueError, OSError, sqlite3.Error) as exc:
            # A task-store failure must not kill Runtime's heartbeat producer.
            # Keep the unfinished claim for explicit recovery; do not dispatch.
            self.last_error = type(exc).__name__

    async def _run(self, row):
        try:
            handler = self.handlers[row["request"]["kind"]][0]
            bridge = self._bridge()
            if bridge is None or not bridge.task_source_authorized(row["request"]["source"]):
                self.store.finish(row["id"], row["worker"], "failed", "任务授权已失效，未开始处理。")
                return
            async with asyncio.timeout(900):
                result = await handler(row)
            self.store.finish(row["id"], row["worker"], "succeeded", result["text"], evidence_refs=result.get("evidence_refs", ()))
        except asyncio.CancelledError:
            self.store.finish(row["id"], row["worker"], "uncertain", "处理被中断，结果需要核实；不会自动重复执行。")
            raise
        except Exception:
            # A handler may have performed an effect before it failed. Do not
            # infer non-execution from an exception, nor leak internal errors.
            self.store.finish(row["id"], row["worker"], "uncertain", "未能确认完整结果；需要核实，不会自动重复执行。")

    async def close(self):
        if self.task is not None and not self.task.done():
            self.task.cancel()
        if self.task is not None:
            await asyncio.gather(self.task, return_exceptions=True)
            # Cancellation can arrive before _run enters its try block.
            # Reconcile that claim here instead of leaving it running forever
            # during a pause/resume in the same Runtime process.
            row = self.store.get(self.task.get_name())
            if row["status"] == "running":
                self.store.finish(row["id"], row["worker"], "uncertain", "处理被中断，结果需要核实；不会自动重复执行。")
