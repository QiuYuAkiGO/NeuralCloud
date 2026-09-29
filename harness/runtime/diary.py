"""Private diary attempts owned by Runtime; Cognitive owns prepared text and memory."""

from __future__ import annotations

import hashlib
import json
import time
from datetime import datetime, timedelta
from uuid import uuid4
from zoneinfo import ZoneInfo

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field

from harness.diary.config import DiarySettings
from harness.diary.models import DiaryOriginal
from harness.diary.store import DiaryStore
from harness.runtime.models import EventDispatchResult, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now


class DiaryInvite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    request_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.:-]+$")
    text: str = Field(default="如果今天有想留给自己的话，可以写进日记；没有也没关系。", max_length=1000)


class DiaryTurns:
    def __init__(self, path):
        path.parent.mkdir(parents=True, exist_ok=True)
        import sqlite3
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS turns(entry_id TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
        self.db.commit()

    def get(self, entry_id):
        row = self.db.execute("SELECT record_json FROM turns WHERE entry_id=?", (entry_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, record):
        with self.db:
            self.db.execute(
                "INSERT INTO turns VALUES (?,?) ON CONFLICT(entry_id) DO UPDATE SET record_json=excluded.record_json",
                (record["entry_id"], json.dumps(record, ensure_ascii=False)),
            )

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT record_json FROM turns ORDER BY rowid DESC LIMIT 200")]


def _in_window(moment: datetime, window_local: str) -> bool:
    start, end = window_local.split("-", 1)
    stamp = moment.strftime("%H:%M")
    if start <= end:
        return start <= stamp <= end
    return stamp >= start or stamp <= end


def _diary_owner(controller) -> str:
    if controller is None:
        return "legacy"
    return controller.settings.owners.get("diary", "legacy")


class DiaryController:
    def __init__(self, supervisor, settings: DiarySettings, state_dir):
        self.supervisor, self.settings = supervisor, settings
        self.store = DiaryStore(state_dir / "diary.sqlite3")
        self.turns = DiaryTurns(state_dir / "diary-turns.sqlite3")
        self.last_activity = time.monotonic()
        self.activity_revision = 0
        self.next_opportunity = time.monotonic() + settings.quiet_seconds
        self.last_error = None
        self.active_entry = None
        supervisor.add_listener(self.on_event)

    async def on_event(self, topic, value):
        incoming_qq = (value.get("event_type") == "qq_fact"
            and value.get("payload", {}).get("qq_fact", {}).get("kind") == "verified_message")
        if topic == "event.accepted" and (value.get("event_type") in {"user_message", "qq_turn"} or incoming_qq):
            self.last_activity = time.monotonic()
            self.activity_revision += 1

    def _new_event_id(self, entry_id):
        return "diary-event:" + hashlib.sha256((entry_id + uuid4().hex).encode()).hexdigest()[:40]

    def _local_now(self):
        return utc_now().astimezone(ZoneInfo(self.settings.timezone))

    def _day(self, moment=None):
        return (moment or self._local_now()).date().isoformat()

    async def request(self, payload, *, trigger="user_request"):
        invite = DiaryInvite.model_validate(payload)
        if self.store.paused:
            raise ValueError("diary_paused")
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            raise ValueError("runtime_not_running")
        if trigger == "homeostasis" and not self.settings.autonomous_enabled:
            raise ValueError("diary_autonomous_disabled")
        controller = getattr(self.supervisor, "capabilities", None)
        if trigger == "homeostasis" and _diary_owner(controller) != "legacy":
            raise ValueError("diary_autonomous_owner_changed")
        entry_id = "diary-entry:" + hashlib.sha256((invite.request_id + ":" + self._day()).encode()).hexdigest()[:40]
        prior = self.turns.get(entry_id)
        if prior:
            if prior["request_text"] != invite.text or prior["trigger"] != trigger:
                raise ValueError("diary_request_conflict")
            return {"accepted": True, "entry_id": entry_id, "status": prior["status"], "event_id": prior["event_id"]}
        if self.store.count(self._day()) >= self.settings.max_entries_per_day:
            raise ValueError("diary_day_limit")
        if self.active_entry or any(row["status"] in {"queued", "preparing", "prepared"} for row in self.turns.all()):
            raise ValueError("diary_busy")
        moment = utc_now()
        record = dict(
            entry_id=entry_id, event_id=self._new_event_id(entry_id), trigger=trigger,
            request_text=invite.text, requested_at=moment.isoformat(),
            deadline_at=(moment + timedelta(seconds=60)).isoformat(),
            status="queued", activity_revision=self.activity_revision, day=self._day(),
        )
        if trigger == "homeostasis" and controller is not None:
            record["owner_generation"] = controller.generation
        self.turns.put(record)
        try:
            event = await self.supervisor.submit_event(
                event_type=RuntimeEventType.DIARY_REFLECTION,
                priority=RuntimeEventPriority.IDLE,
                source="diary_opportunity" if trigger == "homeostasis" else "diary_owner_request",
                event_id=record["event_id"], idempotency_key=record["event_id"],
                payload={"entry_id": entry_id},
            )
            if event is None:
                raise ValueError("diary_queue_rejected")
        except Exception:
            record["status"] = "queue_failed"
            self.turns.put(record)
            raise
        return {"accepted": True, "entry_id": entry_id, "event_id": event.event_id, "status": "queued"}

    async def heartbeat(self):
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING or self.active_entry:
            return
        controller = getattr(self.supervisor, "capabilities", None)
        # Goals currently dispatch board/QQ/web, not diary. Only the diary
        # owner setting may suppress this independent, day-limited scheduler.
        if _diary_owner(controller) != "legacy":
            return
        now = self._local_now()
        if (not self.settings.autonomous_enabled or self.store.paused or not self.supervisor.queue.empty()
                or time.monotonic() < self.next_opportunity
                or time.monotonic() - self.last_activity < self.settings.quiet_seconds
                or not _in_window(now, self.settings.window_local)
                or self.store.count(self._day(now)) >= self.settings.max_entries_per_day):
            return
        summary = self.supervisor._response_homeostatic_interoception()
        salience = (summary or {}).get("salience")
        if salience not in {"noticeable", "marked"} and self.activity_revision <= 0:
            return
        rows = self.turns.all()
        if rows:
            latest = max(datetime.fromisoformat(row["requested_at"]) for row in rows)
            if (utc_now() - latest).total_seconds() < self.settings.opportunity_seconds:
                return
        self.next_opportunity = time.monotonic() + self.settings.opportunity_seconds
        try:
            await self.request(
                {"request_id": "idle-" + uuid4().hex,
                 "text": "这是只写给自己的日记。回顾今天真正发生过的事和当下的感受；没有想写的就等一等。"},
                trigger="homeostasis",
            )
        except ValueError as exc:
            self.last_error = str(exc)

    async def _command(self, event, command_type, payload, deadline=None):
        from harness.runtime.capabilities import before_deadline
        method_name = "prepare_diary_entry" if command_type == "PrepareDiaryEntry" else "commit_diary_entry"
        envelope = self.supervisor._make_command(
            event, command_type=command_type,
            idempotency_key=f"diary:{command_type}:{event.event_id}",
            expected_revision=self.supervisor._cognitive_snapshot.revision, payload=payload,
        )
        if deadline:
            envelope = envelope.model_copy(update={"deadline": min(deadline, envelope.deadline)})
        result = await before_deadline(
            self.supervisor._invoke_domain(
                service="cognitive", envelope=envelope,
                method=getattr(self.supervisor.cognitive, method_name), event=event,
            ),
            envelope.deadline,
        )
        current = await before_deadline(self.supervisor.cognitive.get_snapshot(), envelope.deadline)
        self.supervisor._validate_snapshot(current, "cognitive")
        if current.revision < self.supervisor._cognitive_snapshot.revision:
            raise ValueError("diary_cognitive_revision_regressed")
        self.supervisor._cognitive_snapshot = current
        return result.result

    async def dispatch(self, event, process_seq):
        started = time.monotonic()
        row = self.turns.get(event.payload.get("entry_id", ""))
        if row is None or row["event_id"] != event.event_id:
            raise ValueError("diary_event_source_invalid")
        if row["status"] in {"committed", "wait", "expired", "superseded", "paused", "error", "queue_failed"}:
            return self._result(event, process_seq, row)
        self.active_entry = row["entry_id"]
        production = getattr(self.supervisor, "production_runtime", None)
        effect_epoch = production.epoch if production is not None else None
        try:
            deadline = datetime.fromisoformat(row["deadline_at"])
            if utc_now() >= deadline:
                return self._finish(event, process_seq, row, "expired")
            if self.store.paused or self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
                return self._finish(event, process_seq, row, "paused")
            if row["trigger"] == "homeostasis" and time.monotonic() - self.last_activity < self.settings.quiet_seconds:
                return self._finish(event, process_seq, row, "superseded")
            if row["trigger"] == "homeostasis" and not _in_window(self._local_now(), self.settings.window_local):
                return self._finish(event, process_seq, row, "expired")
            row["status"] = "preparing"
            self.turns.put(row)
            summary = self.supervisor._response_homeostatic_interoception()
            prepared = await self._command(event, "PrepareDiaryEntry", {
                "entry_id": row["entry_id"], "deadline_at": row["deadline_at"],
                "trigger": row["trigger"], "request_text": row["request_text"],
                "source_event_id": event.event_id, "day": row["day"],
                "model_timeout_seconds": (
                    self.settings.autonomous_model_seconds if row["trigger"] == "homeostasis"
                    else self.settings.user_model_seconds
                ),
                "homeostatic_summary": summary or {},
            }, deadline)
            row["decision"], row["timing"] = prepared["decision"], prepared.get("timing", {})
            decision = row["decision"]
            if decision["entry_id"] != row["entry_id"]:
                raise ValueError("diary_decision_binding_invalid")
            row["status"] = "prepared"
            self.turns.put(row)
            if decision["action"] == "wait":
                return self._finish(event, process_seq, row, "wait")
            if decision["action"] != "write":
                raise ValueError("diary_decision_action_invalid")
            if production is not None:
                await production.begin_tool_operation("diary", row["entry_id"], "expression.diary.write.v1")
            invalid = ("expired" if utc_now() >= deadline else
                "paused" if self.store.paused or self.supervisor.lifecycle != RuntimeLifecycle.RUNNING else
                "superseded" if production is not None and (production.epoch != effect_epoch or
                    production.context_snapshot("owner")["restricted_dialogue"]) else None)
            if invalid:
                if production is not None:
                    production.cancel_tool_operation("diary", row["entry_id"])
                return self._finish(event, process_seq, row, invalid)
            original = DiaryOriginal(
                entry_id=row["entry_id"], decision_id=decision["decision_id"],
                text=decision["text"], written_at=utc_now(),
                body_hash=hashlib.sha256(decision["text"].encode("utf-8")).hexdigest(),
                source_event_id=event.event_id, homeostatic_summary=summary or {},
            )
            if production is not None:
                production.mark_tool_dispatched("diary", row["entry_id"])
            try:
                stored = self.store.put(original, day=row["day"], revision=self.supervisor._cognitive_snapshot.revision + 1)
            except Exception:
                if production is not None:
                    production.finish_tool_operation("diary", row["entry_id"], outcome="failed")
                raise
            if production is not None:
                production.finish_tool_operation("diary", row["entry_id"], outcome="completed",
                    evidence_refs=[stored["entry_id"], "diary-body:sha256:" + stored["body_hash"]])
            await self._command(event, "CommitDiaryEntry", {
                "entry_id": stored["entry_id"], "decision_id": stored["decision_id"],
                "body_hash": stored["body_hash"], "written_at": stored["written_at"],
                "text": stored["body"], "source_event_id": stored["source_event_id"],
                "homeostatic_summary": json.loads(stored["homeostatic_summary_json"]),
            }, deadline)
            row["confirmed_at"] = utc_now().isoformat()
            self.last_error = None
            return self._finish(event, process_seq, row, "committed")
        except Exception as exc:
            row = self.turns.get(row["entry_id"]) or row
            row["error_type"] = type(exc).__name__
            self.last_error = type(exc).__name__
            outcome = "expired" if utc_now() >= datetime.fromisoformat(row["deadline_at"]) else "error"
            return self._finish(event, process_seq, row, outcome)
        finally:
            row["dispatch_monotonic_ms"] = round((time.monotonic() - started) * 1000, 3)
            self.turns.put(row)
            self.active_entry = None

    def _finish(self, event, process_seq, row, status):
        row["status"] = status
        self.turns.put(row)
        return self._result(event, process_seq, row)

    @staticmethod
    def _result(event, process_seq, row):
        return EventDispatchResult(
            event_id=event.event_id, receive_seq=event.receive_seq, process_seq=process_seq,
            status="failed" if row["status"] in {"error", "queue_failed"} else "completed",
            decision_kind="diary_reflection",
            detail={k: row[k] for k in ("entry_id", "status", "timing", "error_type") if k in row},
        )

    def snapshot(self, *, include_bodies: bool = False):
        rows = self.store.public_rows()
        payload = {
            "enabled": True, "paused": self.store.paused,
            "autonomous_enabled": self.settings.autonomous_enabled,
            "window_local": self.settings.window_local,
            "max_entries_per_day": self.settings.max_entries_per_day,
            "today": self._day(), "today_count": self.store.count(self._day()),
            "wrote_today": self.store.count(self._day()) > 0,
            "active_entry": self.active_entry, "error_type": self.last_error,
            "entries": rows,
            "turns": [{k: v for k, v in row.items() if k not in {"decision", "request_text"}}
                      for row in self.turns.all()[:30]],
        }
        if include_bodies:
            payload["bodies"] = [
                {"entry_id": item["entry_id"], "day": item["day"], "written_at": item["written_at"],
                 "text": (self.store.get(item["entry_id"]) or {}).get("body")}
                for item in rows
            ]
        return payload

    def close(self):
        self.store.close()
        self.turns.db.close()


def add_diary_routes(router, supervisor, *, authorize=None):
    from harness.runtime.control import ControlAuthorizationError, ControlPermission

    def diary():
        value = getattr(supervisor, "diary", None)
        if value is None:
            raise HTTPException(status_code=503, detail="diary_not_configured")
        return value

    def require(permission, operation):
        if authorize is None:
            if permission == ControlPermission.DEBUG_MUTATION:
                raise HTTPException(status_code=403, detail="diary_bodies_require_development_debug")
            return
        try:
            authorize(permission, operation=operation)
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/v1/diary")
    async def read_diary():
        require(ControlPermission.DIAGNOSTIC, "diary.snapshot")
        return diary().snapshot()

    @router.get("/v1/diary/entries")
    async def read_diary_bodies():
        require(ControlPermission.DEBUG_MUTATION, "diary.bodies")
        return diary().snapshot(include_bodies=True)

    @router.post("/v1/diary/invite")
    async def invite(payload: DiaryInvite):
        require(ControlPermission.DIAGNOSTIC, "diary.invite")
        try:
            return await diary().request(payload.model_dump())
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None

    @router.post("/v1/diary/pause")
    async def pause():
        require(ControlPermission.DIAGNOSTIC, "diary.pause")
        diary().store.pause(True)
        return {"paused": True}

    @router.post("/v1/diary/resume")
    async def resume():
        require(ControlPermission.DIAGNOSTIC, "diary.resume")
        diary().store.pause(False)
        return {"paused": False}
