"""Durable, bounded semantic interruption control shared by text transports.

This controls freshness only. Transport ownership, permissions, deadlines and
unknown delivery checks remain the responsibility of the sender.
"""
from __future__ import annotations

import asyncio
from contextlib import closing, contextmanager
import json
from pathlib import Path
import sqlite3
import time
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator


class InterruptionOptions(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    mode: Literal["off", "shadow", "active"] = "off"
    coalesce_seconds: float = Field(default=.25, ge=0, le=1, allow_inf_nan=False)
    max_hold_seconds: float = Field(default=2, gt=0, le=2, allow_inf_nan=False)
    max_judgments: int = Field(default=2, ge=1, le=2)
    max_regenerations: int = Field(default=1, ge=0, le=1)
    burst_count: int = Field(default=5, ge=2, le=20)
    burst_seconds: float = Field(default=2, gt=0, le=5, allow_inf_nan=False)


class InterruptionDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: Literal["continue", "regenerate", "wait"]
    reason_code: Literal["acknowledgment", "unrelated", "followup", "uncertain",
                         "constraint_changed", "correction", "redirected", "explicit_wait", "cancelled"]
    evidence_ids: list[str] = Field(max_length=20)

    @model_validator(mode="after")
    def grounded_action(self):
        allowed = {"continue": {"acknowledgment", "unrelated", "followup", "uncertain"},
                   "regenerate": {"constraint_changed", "correction", "redirected"},
                   "wait": {"explicit_wait", "cancelled"}}
        if self.reason_code not in allowed[self.action] or (self.action != "continue" and not self.evidence_ids):
            raise ValueError("interruption_action_evidence_required")
        return self


class InterruptionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root_id: str = Field(min_length=1, max_length=200)
    scope: str = Field(min_length=1, max_length=200)
    generation: int = Field(ge=0)
    input_sequence: int = Field(ge=0)
    original_text: str = Field(max_length=16000)
    original_author: str = Field(default="user", max_length=200)
    messages: list[dict[str, str]] = Field(min_length=1, max_length=20)
    delivered: list[str] = Field(default_factory=list, max_length=32)
    candidate: str | None = Field(default=None, max_length=32000)
    timeout_seconds: float = Field(gt=0, le=5, allow_inf_nan=False)


class InterruptionContext(BaseModel):
    """Advisory conversation data, never an authority or a delivery receipt."""
    model_config = ConfigDict(extra="forbid")
    original_request: str = Field(max_length=16000)
    original_author: str = Field(default="user", max_length=200)
    new_messages: list[dict[str, str]] = Field(max_length=20)
    already_delivered: list[str] = Field(max_length=32)
    instruction: str = Field(max_length=200)


class InterruptedReply(Exception):
    def __init__(self, action):
        self.action = action
        super().__init__("text_interruption_" + action)


class InterruptionStore:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS interruption_inputs(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,id TEXT UNIQUE,scope TEXT,author TEXT,text TEXT,at REAL);
                CREATE INDEX IF NOT EXISTS interruption_input_scope ON interruption_inputs(scope,seq);
                CREATE TABLE IF NOT EXISTS interruption_turns(id TEXT PRIMARY KEY,scope TEXT,record TEXT);
                CREATE INDEX IF NOT EXISTS interruption_live_scope
                    ON interruption_turns(scope,json_extract(record,'$.status'));
                CREATE TABLE IF NOT EXISTS interruption_aliases(event TEXT PRIMARY KEY,root TEXT,absorbed INTEGER);
                CREATE TABLE IF NOT EXISTS interruption_runs(run TEXT PRIMARY KEY,event TEXT);
                CREATE TABLE IF NOT EXISTS interruption_audit(id INTEGER PRIMARY KEY,root TEXT,at REAL,record TEXT);
            """)

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=.25)) as db, db:
            db.row_factory = sqlite3.Row
            yield db

    def get(self, event):
        with self.connect() as db:
            row = db.execute("SELECT t.record FROM interruption_aliases a JOIN interruption_turns t ON t.id=a.root WHERE a.event=?", (event,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, row):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO interruption_turns VALUES(?,?,?)", (row["root_id"], row["scope"], json.dumps(row)))

    def audit(self, row, at, **values):
        with self.connect() as db:
            db.execute("INSERT INTO interruption_audit(root,at,record) VALUES(?,?,?)", (row["root_id"], at, json.dumps(values)))

    def inputs(self, scope, after=0):
        with self.connect() as db:
            # Bound the model window, not the input ledger or its watermark.
            return [dict(r) for r in db.execute("SELECT * FROM interruption_inputs WHERE scope=? AND seq>? ORDER BY seq DESC LIMIT 20", (scope, after))][::-1]

    def absorbed(self, event):
        with self.connect() as db:
            row = db.execute("SELECT absorbed FROM interruption_aliases WHERE event=?", (event,)).fetchone()
            return bool(row and row[0])

    def register_run(self, run, event):
        with self.connect() as db:
            db.execute("INSERT OR IGNORE INTO interruption_runs VALUES(?,?)", (run, event))

    def has_run(self, run):
        with self.connect() as db:
            return db.execute("SELECT 1 FROM interruption_runs WHERE run=?", (run,)).fetchone() is not None


class TextInterruption:
    """Single event-loop owner; writes are durable before any effect is released."""
    def __init__(self, path, options, assessor, *, clock=time.time):
        self.options = InterruptionOptions.model_validate(options)
        self.store = InterruptionStore(path)
        self.assessor, self.clock = assessor, clock
        self.tasks = {}

    @property
    def enabled(self):
        return self.options.mode != "off"

    @property
    def active(self):
        return self.options.mode == "active"

    def receive(self, scope, event_id, author, text):
        if not self.enabled:
            return
        now = self.clock()
        with self.store.connect() as db:
            added = db.execute("INSERT OR IGNORE INTO interruption_inputs(id,scope,author,text,at) VALUES(?,?,?,?,?)",
                               (event_id, scope, author, text[:16000], now)).rowcount
            if not added:
                return
            rows = [json.loads(r[0]) for r in db.execute("SELECT record FROM interruption_turns WHERE scope=? AND json_extract(record,'$.status')='active'", (scope,))]
        for row in rows:
            self._consider(row)

    def _consider(self, row):
        now = self.clock()
        if row["status"] != "active" or row["deadline"] <= now or row["loose"]:
            return
        fresh = self.store.inputs(row["scope"], row["reviewed_seq"])
        if not fresh:
            return
        if row["hold_started"] is None:
            row["hold_started"] = now
            self.store.save(row)
        if row["checks"] >= self.options.max_judgments:
            self._release(row, "judgment_limit")
        elif sum(item["at"] >= now-self.options.burst_seconds for item in fresh) >= self.options.burst_count:
            self._release(row, "burst")
        else:
            self._kick(row)

    def begin(self, scope, event_id, text, deadline, *, author="user"):
        if not self.enabled:
            return None
        row = self.store.get(event_id)
        if row:
            if row["current_event"] == event_id and row["status"] == "replacing":
                row.update(status="active", action="continue")
                self.store.save(row)
            # A crashed hold consumes its wall time; it never starts over.
            if row["hold_started"] is not None:
                if self._remaining(row) <= 0:
                    self._release(row, "recovered_hold_limit")
                else:
                    self._kick(row)
            self._consider(self.store.get(event_id))
            return self.store.get(event_id)
        self.receive(scope, event_id, author, text)
        inputs = self.store.inputs(scope)
        seq = max((r["seq"] for r in inputs), default=0)
        row = dict(root_id=event_id, current_event=event_id, scope=scope, original_text=text[:16000],
                   deadline=deadline, reviewed_seq=seq, generation=0, checks=0, regenerations=0,
                   hold_used=0., hold_started=None, stale_results=0, loose=False, status="active", action="continue",
                   delivered=[], delivery_ids=[], candidate=None, continuation=None, original_author=author)
        with self.store.connect() as db:
            db.execute("INSERT INTO interruption_turns VALUES(?,?,?)", (event_id, scope, json.dumps(row)))
            db.execute("INSERT INTO interruption_aliases VALUES(?,?,0)", (event_id, event_id))
        return row

    def _remaining(self, row):
        elapsed = max(0, self.clock()-row["hold_started"]) if row["hold_started"] is not None else 0
        return max(0, min(row["deadline"]-self.clock(), self.options.max_hold_seconds-row["hold_used"]-elapsed))

    def _settle_hold(self, row):
        if row["hold_started"] is not None:
            row["hold_used"] = min(self.options.max_hold_seconds,
                row["hold_used"] + max(0, self.clock()-row["hold_started"]))
            row["hold_started"] = None

    def _release(self, row, reason):
        if row["status"] != "active":
            return
        self._settle_hold(row)
        row.update(loose=True, action="continue")
        self.store.save(row)
        self.store.audit(row, self.clock(), disposition="loose_continue", reason=reason,
                         generation=row["generation"], checks=row["checks"], hold_seconds=row["hold_used"])

    def _kick(self, row):
        self.tasks = {key: task for key, task in self.tasks.items() if not task.done()}
        task = self.tasks.get(row["root_id"])
        if task is None or task.done():
            self.tasks[row["root_id"]] = asyncio.create_task(self._review(row["root_id"]))

    async def _review(self, root):
        try:
            row = self.store.get(root)
            await asyncio.sleep(min(self.options.coalesce_seconds, self._remaining(row)))
            while True:
                row = self.store.get(root)
                if row["loose"] or row["status"] != "active":
                    return
                remaining = self._remaining(row)
                if remaining <= 0 or row["checks"] >= self.options.max_judgments:
                    self._release(row, "judgment_limit")
                    return
                messages = self.store.inputs(row["scope"], row["reviewed_seq"])
                if not messages:
                    self._settle_hold(row)
                    self.store.save(row)
                    return
                seq, generation = messages[-1]["seq"], row["generation"]
                row["checks"] += 1
                self.store.save(row)
                request = InterruptionRequest(root_id=root, scope=row["scope"], generation=generation,
                    input_sequence=seq, original_text=row["original_text"], original_author=row.get("original_author", "user"), candidate=row["candidate"],
                    messages=[{k: m[k] for k in ("id", "author", "text")} for m in messages],
                    delivered=row["delivered"], timeout_seconds=remaining)
                try:
                    decision = InterruptionDecision.model_validate(await asyncio.wait_for(self.assessor(request), remaining))
                    if not set(decision.evidence_ids).issubset({m["id"] for m in messages}):
                        raise ValueError("interruption_unknown_evidence")
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    row = self.store.get(root)
                    status = getattr(getattr(exc, "response", None), "status_code", None)
                    self._release(row, "judge_timeout" if isinstance(exc, (TimeoutError, httpx.TimeoutException)) or status == 408 else
                        "judge_invalid" if isinstance(exc, ValueError) or status == 422 else "judge_unavailable")
                    return
                row = self.store.get(root)
                if row["loose"] or row["generation"] != generation or row["status"] != "active":
                    self.store.audit(row, self.clock(), disposition="late_ignored")
                    return
                if self._remaining(row) <= 0:
                    self._release(row, "hold_limit")
                    return
                if self.store.inputs(row["scope"])[-1]["seq"] != seq:
                    row["stale_results"] += 1
                    self.store.save(row)
                    if row["stale_results"] >= 2:
                        self._release(row, "stale_limit")
                        return
                    continue
                self._settle_hold(row)
                row["reviewed_seq"] = seq
                self.store.audit(row, self.clock(), disposition="shadow" if not self.active else "decision",
                                 **decision.model_dump(), input_sequence=seq, generation=generation)
                if not self.active or decision.action == "continue":
                    self.store.save(row)
                    return
                relevant = [m for m in messages if m["id"] in decision.evidence_ids]
                if decision.action == "wait":
                    row.update(status="waiting", action="wait")
                    with self.store.connect() as db:
                        db.execute("UPDATE interruption_turns SET record=? WHERE id=?", (json.dumps(row), root))
                        for m in relevant:
                            db.execute("INSERT OR REPLACE INTO interruption_aliases VALUES(?,?,1)", (m["id"], root))
                    return
                if row["regenerations"] >= self.options.max_regenerations:
                    self._release(row, "regeneration_limit")
                    return
                # The existing durable ingress processes this follow-up. Aliases
                # carry the ORIGINAL quota/deadline across its new request ID.
                row.update(current_event=relevant[0]["id"], status="replacing", action="regenerate",
                           generation=generation+1, regenerations=row["regenerations"]+1,
                           candidate=None, continuation=[{k: m[k] for k in ("id", "author", "text")} for m in relevant])
                with self.store.connect() as db:
                    db.execute("UPDATE interruption_turns SET record=? WHERE id=?", (json.dumps(row), root))
                    for i, m in enumerate(relevant):
                        db.execute("INSERT OR REPLACE INTO interruption_aliases VALUES(?,?,?)", (m["id"], root, int(i != 0)))
                return
        except asyncio.CancelledError:
            # Durable hold/check counters survive owner shutdown.
            raise

    async def before_send(self, event_id, candidate=None):
        if not self.enabled:
            return "continue"
        row = self.store.get(event_id)
        if row is None:
            return "continue"
        if candidate is not None and row["current_event"] == event_id:
            row["candidate"] = candidate[:32000]
            self.store.save(row)
        if not self.active:
            return "continue"
        while row["status"] == "active" and not row["loose"] and row["hold_started"] is not None:
            if self._remaining(row) <= 0:
                self._release(row, "hold_limit")
                break
            self._kick(row)
            await asyncio.sleep(min(.01, self._remaining(row)))
            row = self.store.get(event_id)
        row = self.store.get(event_id)
        if row["deadline"] <= self.clock():
            return "expired"
        if row["status"] == "fenced":
            return "fenced"
        if row["current_event"] != event_id:
            return "regenerate"
        return row["action"]

    def managed(self, event_id):
        return self.active and self.store.get(event_id) is not None

    def absorbed(self, event_id):
        return self.active and self.store.absorbed(event_id)

    def continuation(self, event_id):
        row = self.store.get(event_id) if self.active else None
        if not row or row["current_event"] != event_id or not row["continuation"]:
            return None
        return {"original_request": row["original_text"], "new_messages": row["continuation"],
                "original_author": row.get("original_author", "user"),
                "already_delivered": row["delivered"],
                "instruction": "Respond to the original request with these updates; do not repeat already delivered text."}

    def delivered(self, event_id, segments, *, receipt_id=None):
        row = self.store.get(event_id) if self.enabled else None
        if row:
            if receipt_id is not None:
                if receipt_id in row.get("delivery_ids", []):
                    return
                row["delivery_ids"] = [*row.get("delivery_ids", []), receipt_id][-32:]
            row["delivered"] = (row["delivered"] + list(segments))[-32:]
            self.store.save(row)

    def deadline(self, event_id, default):
        row = self.store.get(event_id) if self.active else None
        return min(default, row["deadline"]) if row else default

    def fence(self):
        """Persist cancellation on a control boundary; a later resume cannot undo it."""
        if not self.active:
            return
        with self.store.connect() as db:
            rows = [json.loads(item[0]) for item in db.execute("SELECT record FROM interruption_turns WHERE json_extract(record,'$.status') IN ('active','replacing')")]
            for row in rows:
                self._settle_hold(row)
                row.update(status="fenced", action="fenced")
                db.execute("UPDATE interruption_turns SET record=? WHERE id=?", (json.dumps(row), row["root_id"]))

    def finish(self, event_id):
        row = self.store.get(event_id) if self.enabled else None
        if row and row["current_event"] == event_id and row["status"] == "active":
            self._settle_hold(row)
            row.update(status="finished")
            self.store.save(row)

    async def close(self):
        for task in self.tasks.values():
            task.cancel()
        await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    def diagnostics(self):
        with self.store.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute("SELECT record FROM interruption_turns")]
            reasons = {r[0]: r[1] for r in db.execute("SELECT json_extract(record,'$.reason'),count(*) FROM interruption_audit WHERE json_extract(record,'$.disposition')='loose_continue' GROUP BY json_extract(record,'$.reason')")}
        return {"mode": self.options.mode, "turns": len(rows),
                "loose": sum(r["loose"] for r in rows), "judgments": sum(r["checks"] for r in rows),
                "regenerations": sum(r["regenerations"] for r in rows),
                "max_hold_seconds": max((r["hold_used"] for r in rows), default=0),
                "loose_reasons": reasons}
