"""Private expression through the existing Runtime queue and shared cognition.

The Linux board owns entries; this small journal owns attempts, not memories.
Heartbeat opportunities use the Runtime's existing clock and dispatcher.
"""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import re
import sqlite3
import time
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from harness.runtime.models import EventDispatchResult, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now


class ReflectionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    text: str = Field(default="写下此刻你愿意留在留言板上的一个想法。没有想写的也可以不写。", max_length=1000)


class BoardTurns:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS turns(entry_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        self.db.commit()

    def get(self, entry_id):
        row = self.db.execute("SELECT record_json FROM turns WHERE entry_id=?", (entry_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, record):
        with self.db:
            self.db.execute("INSERT INTO turns VALUES (?,?) ON CONFLICT(entry_id) DO UPDATE SET record_json=excluded.record_json",
                            (record["entry_id"], json.dumps(record, ensure_ascii=False)))

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record_json FROM turns ORDER BY rowid DESC LIMIT 200")]

    @property
    def paused(self):
        row = self.db.execute("SELECT value FROM settings WHERE key='paused'").fetchone()
        return row is not None and row[0] == "1"

    def pause(self, paused):
        with self.db:
            self.db.execute("INSERT INTO settings VALUES ('paused',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", ("1" if paused else "0",))


class BoardController:
    def __init__(self, supervisor, settings, state_dir, *, provider):
        self.supervisor, self.settings = supervisor, settings
        self.turns = BoardTurns(state_dir / "board-turns.sqlite3")
        self.provider = provider
        self.last_activity = time.monotonic()
        self.activity_revision = 0
        self.next_opportunity = time.monotonic() + settings.quiet_seconds
        self.last_error = None
        self.active_entry = None
        self._body_status = None
        self._recovery_queued = set()
        self._recovery_next = {}
        supervisor.add_listener(self.on_event)

    async def on_event(self, topic, value):
        incoming_qq = (value.get("event_type") == "qq_fact"
            and value.get("payload", {}).get("qq_fact", {}).get("kind") == "verified_message")
        if topic == "event.accepted" and (value.get("event_type") in {"user_message", "qq_turn"} or incoming_qq):
            self.last_activity = time.monotonic()
            self.activity_revision += 1

    async def ready(self):
        self._body_status = await self.provider.activate()
        return self._body_status

    def _new_event_id(self, entry_id):
        return "board-event:" + hashlib.sha256((entry_id + uuid4().hex).encode()).hexdigest()[:40]

    def _capability_invalid(self, binding):
        if not binding:
            return None
        controller = self.supervisor.capabilities
        row = controller.calls.get(binding["opportunity_id"])
        if (row is None or row.get("decision", {}).get("decision_id") != binding["selection_ref"]
                or row["generation"] != binding["generation"] or row["deadline_at"] != binding["deadline_at"]
                or row.get("offer", {}).get("scope") != self.settings.scope.model_dump(mode="json")
                or row.get("offer", {}).get("body_instance_id") != self.settings.body_instance_id):
            return "superseded"
        return controller.invalid_reason(row)

    def _autonomous_invalid(self, row):
        if row.get("capability_call"):
            return self._capability_invalid(row["capability_call"])
        controller = getattr(self.supervisor, "capabilities", None)
        if row["trigger"] == "homeostasis" and controller is not None:
            if controller.owner("board") != "legacy":
                return "superseded"
            generation = row.get("owner_generation")
            if generation is not None and generation != controller.generation:
                return "superseded"
        return None

    async def request(self, payload, *, trigger="user_request", capability_call=None):
        if self._capability_invalid(capability_call):
            raise ValueError("board_capability_binding_invalid")
        request = ReflectionRequest.model_validate(payload)
        if self.turns.paused:
            raise ValueError("board_paused")
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            raise ValueError("runtime_not_running")
        if any(row["status"] == "uncertain" for row in self.turns.all()):
            raise ValueError("board_delivery_uncertain")
        entry_id = "board-entry:" + hashlib.sha256((self.settings.board_id + ":" + request.request_id).encode()).hexdigest()[:40]
        prior = self.turns.get(entry_id)
        if prior:
            if prior["request_text"] != request.text or prior["trigger"] != trigger:
                raise ValueError("board_request_conflict")
            return {"accepted": True, "entry_id": entry_id, "status": prior["status"], "event_id": prior["event_id"]}
        if self.active_entry or any(row["status"] in {"queued", "preparing", "prepared", "append_started", "written_pending_memory"}
                                    for row in self.turns.all()):
            raise ValueError("board_busy")
        moment = utc_now()
        record = dict(entry_id=entry_id, event_id=self._new_event_id(entry_id), trigger=trigger,
            request_text=request.text, requested_at=moment.isoformat(), deadline_at=(moment + timedelta(seconds=60)).isoformat(),
            status="queued", scope=self.settings.scope.model_dump(mode="json"), activity_revision=self.activity_revision)
        if capability_call:
            record.update(capability_call=capability_call, deadline_at=capability_call["deadline_at"])
        elif trigger == "homeostasis" and (controller := getattr(self.supervisor, "capabilities", None)) is not None:
            if controller.owner("board") != "legacy":
                raise ValueError("board_autonomous_owner_changed")
            record["owner_generation"] = controller.generation
        self.turns.put(record)
        try:
            event = await self.supervisor.submit_event(event_type=RuntimeEventType.BOARD_REFLECTION,
                priority=RuntimeEventPriority.IDLE, source="board_opportunity" if trigger == "homeostasis" else "board_owner_request",
                event_id=record["event_id"], idempotency_key=record["event_id"], payload={"entry_id": entry_id})
            if event is None:
                raise ValueError("board_queue_rejected")
        except Exception:
            record["status"] = "queue_failed"
            self.turns.put(record)
            raise
        return {"accepted": True, "entry_id": entry_id, "event_id": event.event_id, "status": "queued"}

    async def heartbeat(self):
        """One bounded scheduling check, called after the real shared heartbeat."""
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING or self.active_entry:
            return
        for row in self.turns.all():
            if (row["status"] in {"append_started", "uncertain", "written_pending_memory"}
                    and row["entry_id"] not in self._recovery_queued
                    and time.monotonic() >= self._recovery_next.get(row["entry_id"], 0)
                    and row.get("recovery_attempts", 0) < 3):
                row["event_id"] = self._new_event_id(row["entry_id"])
                self.turns.put(row)
                event = await self.supervisor.submit_event(event_type=RuntimeEventType.BOARD_REFLECTION,
                    priority=RuntimeEventPriority.IDLE, source="board_receipt_recovery", event_id=row["event_id"],
                    payload={"entry_id": row["entry_id"], "reconcile_only": True})
                if event:
                    self._recovery_queued.add(row["entry_id"])
                return
        controller = getattr(self.supervisor, "capabilities", None)
        if getattr(getattr(self.supervisor, "goals", None), "enabled", False):
            return
        if controller is not None and controller.owner("board") != "legacy":
            return
        if (not self.settings.autonomous_enabled or self.turns.paused or not self.supervisor.queue.empty()
                or time.monotonic() < self.next_opportunity
                or time.monotonic() - self.last_activity < self.settings.quiet_seconds):
            return
        summary = self.supervisor._response_homeostatic_interoception()
        if summary is None or summary.get("salience") not in {"noticeable", "marked"}:
            return
        # The last opportunity time is durable: restarting never resets cadence.
        rows = self.turns.all()
        if rows:
            from datetime import datetime
            latest = max(datetime.fromisoformat(row["requested_at"]) for row in rows)
            if (utc_now() - latest).total_seconds() < self.settings.opportunity_seconds:
                return
        self.next_opportunity = time.monotonic() + self.settings.opportunity_seconds
        try:
            await self.request({"request_id": "idle-" + uuid4().hex,
                "text": "这里有一块只在本机可见的留言板。如果此刻有想留下的观点、感受或小念头，可以写下来；没什么想写的就等一等。"}, trigger="homeostasis")
        except ValueError as exc:
            self.last_error = str(exc)

    async def _command(self, event, command_type, payload, deadline=None):
        from harness.runtime.capabilities import before_deadline
        method_name = "prepare_board_entry" if command_type == "PrepareBoardEntry" else "commit_board_entry"
        envelope = self.supervisor._make_command(event, command_type=command_type,
            idempotency_key=f"board:{command_type}:{event.event_id}",
            expected_revision=self.supervisor._cognitive_snapshot.revision, payload=payload)
        if deadline:
            envelope = envelope.model_copy(update={"deadline": min(deadline, envelope.deadline)})
        result = await before_deadline(self.supervisor._invoke_domain(service="cognitive", envelope=envelope,
            method=getattr(self.supervisor.cognitive, method_name), event=event), envelope.deadline)
        current = await before_deadline(self.supervisor.cognitive.get_snapshot(), envelope.deadline)
        self.supervisor._validate_snapshot(current, "cognitive")
        if current.revision < self.supervisor._cognitive_snapshot.revision:
            raise ValueError("board_cognitive_revision_regressed")
        self.supervisor._cognitive_snapshot = current
        return result.result

    async def _reconcile(self, row):
        from harness.embodiment.board_contracts import BoardReconcileRequest
        moment = utc_now()
        decision = row["decision"]
        result = await self.provider.reconcile(BoardReconcileRequest(scope=self.settings.scope,
            entry_id=row["entry_id"], decision_id=decision["decision_id"],
            text_sha256=hashlib.sha256(decision["text"].encode()).hexdigest(),
            requested_at=moment, deadline=moment + timedelta(seconds=10)))
        if result.status != "committed" or result.receipt is None:
            raise ValueError("board_receipt_not_confirmed")
        return result.receipt

    async def dispatch(self, event, process_seq):
        from datetime import datetime
        from harness.embodiment.board_contracts import BoardAppendRequest, BoardReadRequest
        started = time.monotonic()
        row = self.turns.get(event.payload.get("entry_id", ""))
        if row is None or row["event_id"] != event.event_id or row["scope"] != self.settings.scope.model_dump(mode="json"):
            raise ValueError("board_event_source_invalid")
        if row["status"] in {"committed", "wait", "expired", "superseded", "paused", "error", "queue_failed"}:
            return self._result(event, process_seq, row)
        self.active_entry = row["entry_id"]
        outcome = row["status"]
        recover = row["status"] in {"append_started", "uncertain", "written_pending_memory"}
        if recover:
            row["recovery_attempts"] = row.get("recovery_attempts", 0) + 1
            self.turns.put(row)
        try:
            deadline = datetime.fromisoformat(row["deadline_at"])
            if not recover and utc_now() >= deadline:
                return self._finish(event, process_seq, row, "expired")
            if recover:
                await self.ready()
            else:
                await asyncio.wait_for(self.ready(), timeout=max(0, (deadline - utc_now()).total_seconds()))
            if not recover:
                invalid = self._autonomous_invalid(row)
                if invalid:
                    return self._finish(event, process_seq, row, invalid)
                if row["status"] in {"committed", "wait", "expired", "superseded", "error"}:
                    return self._result(event, process_seq, row)
                if self.turns.paused or self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
                    outcome = "paused"
                    return self._finish(event, process_seq, row, outcome)
                if row["trigger"] == "homeostasis" and time.monotonic() - self.last_activity < self.settings.quiet_seconds:
                    return self._finish(event, process_seq, row, "superseded")
                if utc_now() >= deadline:
                    return self._finish(event, process_seq, row, "expired")
                activity = self.activity_revision
                now = utc_now()
                read = await self.provider.read(BoardReadRequest(scope=self.settings.scope,
                    limit=8, latest=True, requested_at=now, deadline=min(deadline, now + timedelta(seconds=5))))
                row["status"] = "preparing"
                self.turns.put(row)
                summary = self.supervisor._response_homeostatic_interoception()
                prepared = await self._command(event, "PrepareBoardEntry", {
                    "entry_id": row["entry_id"], "body_instance_id": self.settings.body_instance_id,
                    "board_id": self.settings.board_id, "deadline_at": row["deadline_at"],
                    "trigger": row["trigger"], "request_text": row["request_text"],
                    "source_event_id": event.event_id,
                    "model_timeout_seconds": self.settings.autonomous_model_seconds if row["trigger"] == "homeostasis" else self.settings.user_model_seconds,
                    "homeostatic_summary": summary or {},
                    "read_entries": [item.model_dump(mode="json") for item in read.entries]}, deadline)
                row["decision"], row["timing"] = prepared["decision"], prepared.get("timing", {})
                decision = row["decision"]
                if (decision["entry_id"] != row["entry_id"] or decision["body_instance_id"] != self.settings.body_instance_id
                        or decision["board_id"] != self.settings.board_id):
                    raise ValueError("board_decision_binding_invalid")
                row["status"] = "prepared"
                self.turns.put(row)
                if decision["action"] == "wait":
                    return self._finish(event, process_seq, row, "wait")
                if decision["action"] != "write":
                    raise ValueError("board_decision_action_invalid")
                if utc_now() >= deadline:
                    return self._finish(event, process_seq, row, "expired")
                if self.turns.paused or self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
                    return self._finish(event, process_seq, row, "paused")
                if self.activity_revision != activity:
                    return self._finish(event, process_seq, row, "superseded")
                invalid = self._autonomous_invalid(row)
                if invalid:
                    return self._finish(event, process_seq, row, invalid)
                append = BoardAppendRequest(scope=self.settings.scope, entry_id=row["entry_id"],
                    decision_id=decision["decision_id"], text=decision["text"], title=decision.get("title", ""),
                    source_event_ids=tuple(decision.get("source_event_ids", [])), requested_at=utc_now(), deadline=deadline)
                row["status"] = "append_started"
                self.turns.put(row)  # durable before the external write
                try:
                    def pre_effect_guard():
                        return (not self.turns.paused and self.supervisor.lifecycle == RuntimeLifecycle.RUNNING
                            and self.activity_revision == activity and not self._autonomous_invalid(row))
                    receipt = await self.provider.append(append, pre_effect_guard=pre_effect_guard)
                except Exception as exc:
                    if getattr(exc, "code", None) == "board_pre_effect_cancelled":
                        return self._finish(event, process_seq, row, "superseded")
                    if getattr(exc, "code", None) in {
                        "provider_not_activated", "capability_unavailable", "request_expired",
                        "scope_not_allowed", "board_authentication_failed", "board_idempotency_conflict",
                        "provider_activation_changed", "board_write_busy",
                    }:
                        row["status"] = "not_written"
                        self.turns.put(row)
                        raise
                    receipt = await self._reconcile(row)  # Never append a second time.
            else:
                receipt = await self._reconcile(row)
            row.update(status="written_pending_memory", receipt=receipt.model_dump(mode="json"))
            self.turns.put(row)
            await self._command(event, "CommitBoardEntry", {"entry_id": row["entry_id"],
                "decision_id": row["decision"]["decision_id"], "receipt": row["receipt"]}, None if recover else deadline)
            row["confirmed_at"] = utc_now().isoformat()
            row["end_to_end_wall_ms"] = round((utc_now() - datetime.fromisoformat(row["requested_at"])).total_seconds() * 1000, 3)
            self.last_error = None
            return self._finish(event, process_seq, row, "committed")
        except Exception as exc:
            row = self.turns.get(row["entry_id"]) or row
            outcome = "written_pending_memory" if row["status"] == "written_pending_memory" else "uncertain" if row["status"] in {"append_started", "uncertain"} else "error"
            if outcome == "error" and utc_now() >= datetime.fromisoformat(row["deadline_at"]):
                outcome = "expired"
            row["error_type"] = type(exc).__name__
            code = getattr(exc, "code", None)
            if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
                row["error_code"] = code
            self.last_error = type(exc).__name__
            return self._finish(event, process_seq, row, outcome)
        finally:
            row["dispatch_monotonic_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.turns.put(row)
            self.active_entry = None
            # Throttle failed read-only recovery; no tight polling or late sends.
            if row["status"] in {"uncertain", "written_pending_memory"}:
                self._recovery_next[row["entry_id"]] = time.monotonic() + 30
            self._recovery_queued.discard(row["entry_id"])

    def _finish(self, event, process_seq, row, status):
        row["status"] = status
        self.turns.put(row)
        return self._result(event, process_seq, row)

    @staticmethod
    def _result(event, process_seq, row):
        return EventDispatchResult(event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
            status="failed" if row["status"] in {"error", "uncertain", "written_pending_memory"} else "completed",
            decision_kind="board_reflection", detail={k: row[k] for k in (
                "entry_id", "status", "receipt", "timing", "error_type", "error_code", "end_to_end_wall_ms") if k in row})

    async def snapshot(self):
        from harness.embodiment.board_contracts import BoardReadRequest
        entries = []
        try:
            await self.ready()
            now = utc_now()
            read = await self.provider.read(BoardReadRequest(scope=self.settings.scope, limit=100, latest=True,
                requested_at=now, deadline=now + timedelta(seconds=5)))
            entries = [entry.model_dump(mode="json") for entry in read.entries]
            if not any(row["status"] in {"uncertain", "written_pending_memory"} for row in self.turns.all()):
                self.last_error = None
        except Exception as exc:
            self.last_error = type(exc).__name__
        return {"enabled": True, "owner": "shared_runtime", "scope": self.settings.scope.model_dump(mode="json"),
            "paused": self.turns.paused, "autonomous_enabled": self.settings.autonomous_enabled,
            "opportunity_seconds": self.settings.opportunity_seconds, "quiet_seconds": self.settings.quiet_seconds,
            "body": self._body_status.embodiment.model_dump(mode="json") if self._body_status else None,
            "active_entry": self.active_entry, "error_type": self.last_error,
            "entries": entries, "turns": [{k: v for k, v in row.items() if k not in {"decision", "request_text"}}
                                           for row in self.turns.all()[:30]]}

    async def close(self):
        await self.provider.close()
        self.turns.db.close()


def add_board_routes(router, supervisor):
    def board():
        value = getattr(supervisor, "board", None)
        if value is None:
            raise HTTPException(status_code=503, detail="board_not_configured")
        return value

    @router.get("/v1/board")
    async def read_board():
        return await board().snapshot()

    @router.post("/v1/board/reflect")
    async def reflect(payload: ReflectionRequest):
        try:
            return await board().request(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/v1/board/pause")
    async def pause():
        board().turns.pause(True)
        return {"paused": True}

    @router.post("/v1/board/resume")
    async def resume():
        board().turns.pause(False)
        # Resuming permits another read-only reconciliation of an unknown result.
        board()._recovery_queued.clear()
        board()._recovery_next.clear()
        for row in board().turns.all():
            if row["status"] in {"uncertain", "written_pending_memory", "append_started"}:
                row["recovery_attempts"] = 0
                board().turns.put(row)
        return {"paused": False}
