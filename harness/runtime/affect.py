"""Runtime-owned affect window reads and coalesced isolated appraisal."""
from __future__ import annotations

import asyncio
import hashlib
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from harness.embodiment.capabilities import (
    AFFECT_APPRAISE,
    AFFECT_WINDOW_READ,
    CapabilityDescriptor,
    CapabilityStatus,
)
from harness.runtime.emotion import project_homeostasis


class _ScopeState:
    def __init__(self) -> None:
        self.in_flight: asyncio.Task | None = None
        self.pending: dict[str, dict[str, Any]] = {}
        self.last_submit_at: float | None = None
        self.last_window_hash: str | None = None


class AffectWorker:
    def __init__(self, supervisor) -> None:
        self.supervisor = supervisor
        self.tasks: set[asyncio.Task] = set()
        self._scopes: dict[str, _ScopeState] = {}
        self._appraise_degraded = False
        self.min_interval_seconds = 2.0
        self.max_pending = 20

    @property
    def enabled(self) -> bool:
        manifest = self.supervisor.manifest
        return (getattr(manifest, "emotion_mode", "off") != "off"
                and getattr(manifest, "grounded_agency", None) is None
                and getattr(manifest, "production", None) is None)

    def status(self) -> list[CapabilityDescriptor]:
        if not self.enabled:
            return []
        status = CapabilityStatus.DEGRADED if self._appraise_degraded else CapabilityStatus.AVAILABLE
        return [
            CapabilityDescriptor(capability=AFFECT_WINDOW_READ, status=status),
            CapabilityDescriptor(capability=AFFECT_APPRAISE, status=status),
        ]

    def queue_status(self) -> dict:
        return {"pending": sum(len(scope.pending) for scope in self._scopes.values()),
                "running": sum(not task.done() for task in self.tasks),
                "scopes": len(self._scopes), "degraded": self._appraise_degraded}

    def submit(self, scope: str, trigger: dict[str, Any], *, event) -> None:
        if not self.enabled:
            return
        state = self._scopes.setdefault(scope, _ScopeState())
        identity = str(trigger["event_id"])
        record = {**trigger, "_event": event}
        if identity not in state.pending:
            state.pending[identity] = record
        if len(state.pending) > self.max_pending:
            ordered = sorted(state.pending.values(), key=lambda item: item["first_seen_at"])
            while len(state.pending) > self.max_pending:
                dropped = ordered.pop(0)
                state.pending.pop(dropped["event_id"], None)
                self.supervisor._append_journal(
                    "affect_source_dropped_expired", event=event,
                    data={"scope": scope, "event_id": dropped["event_id"]},
                )
        self._ensure_drain(scope)

    def _ensure_drain(self, scope: str) -> None:
        state = self._scopes[scope]
        if state.in_flight is not None and not state.in_flight.done():
            return
        task = asyncio.create_task(self._drain(scope), name=f"affect-drain:{scope}")
        state.in_flight = task
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def _drain(self, scope: str) -> None:
        state = self._scopes[scope]
        try:
            while state.pending:
                if state.last_submit_at is not None:
                    wait = self.min_interval_seconds - (time.monotonic() - state.last_submit_at)
                    if wait > 0:
                        await asyncio.sleep(wait)
                batch = dict(state.pending)
                state.pending.clear()
                await self._appraise(scope, batch)
                state.last_submit_at = time.monotonic()
        finally:
            if state.in_flight is asyncio.current_task():
                state.in_flight = None
            if state.pending:
                self._ensure_drain(scope)

    async def _appraise(self, scope: str, batch: dict[str, dict[str, Any]]) -> None:
        records = list(batch.values())
        latest = max(records, key=lambda item: item["first_seen_at"])
        event = latest["_event"]
        triggers = [{key: value for key, value in item.items() if key != "_event"} for item in records]
        event_ids = sorted(item["event_id"] for item in triggers)
        context_id = "affect:" + hashlib.sha256((scope + "\0" + "\0".join(event_ids)).encode()).hexdigest()
        try:
            homeostasis = project_homeostasis(
                self.supervisor._homeostasis_snapshot.payload,
                self.supervisor._homeostasis_snapshot.revision,
            )
        except ValueError:
            self.supervisor._append_journal(
                "affect_submit_skipped_critical", event=event,
                data={"scope": scope, "trigger_count": len(triggers)},
            )
            return
        method = getattr(self.supervisor.cognitive, "appraise_affect", None)
        if method is None:
            self.supervisor._append_journal(
                "affect_appraise_failed", event=event,
                data={"scope": scope, "error_type": "appraise_affect_unavailable"},
            )
            self._appraise_degraded = True
            return
        first = records[0]
        envelope = self.supervisor._make_command(
            event, command_type="AppraiseAffect", idempotency_key=context_id, expected_revision=None,
            payload={"context_id": context_id, "scope": scope, "group_id": first["group_id"],
                     "self_member_id": first["self_member_id"], "transport": first["transport"],
                     "triggers": triggers, "homeostasis": homeostasis},
        )
        appraisal_s = 6.0
        guidance_s = 3.0
        settings = getattr(getattr(self.supervisor.cognitive, "pipeline", None), "settings", None)
        if settings is not None:
            appraisal_s = getattr(settings, "emotion_affect_appraisal_timeout_seconds", appraisal_s)
            guidance_s = getattr(settings, "emotion_affect_guidance_timeout_seconds", guidance_s)
        deadline = datetime.now(timezone.utc) + timedelta(seconds=appraisal_s + guidance_s + 1)
        envelope = envelope.model_copy(update={"deadline": deadline})
        try:
            result = await self.supervisor._invoke_domain(
                service="cognitive", envelope=envelope, method=method, event=event,
                degrade_on_failure=False)
            payload = result.result or {}
            self._appraise_degraded = False
            if payload.get("window_hash"):
                self._scopes[scope].last_window_hash = payload["window_hash"]
            self.supervisor._append_journal(
                "affect_appraise_committed", event=event,
                data={"scope": scope, "accepted_count": len(payload.get("accepted_ids") or []),
                      "revision": payload.get("emotion_revision"),
                      "appraisal_ms": payload.get("appraisal_ms"),
                      "guidance_ms": payload.get("guidance_ms"),
                      "merged": len(triggers) > 1},
            )
        except Exception as exc:
            error_type = type(exc).__name__
            if "aux_unavailable" in str(exc):
                self._appraise_degraded = True
            self.supervisor._append_journal(
                "affect_appraise_failed", event=event,
                data={"scope": scope, "error_type": error_type},
            )

    async def read(self, scope: str, as_of: datetime, *, timeout: float | None = None) -> dict[str, Any] | None:
        if not self.enabled:
            return None
        method = getattr(self.supervisor.cognitive, "read_affect_snapshot", None)
        started = time.monotonic()
        read_timeout = 2.0 if timeout is None else timeout
        settings = getattr(getattr(self.supervisor.cognitive, "pipeline", None), "settings", None)
        if timeout is None and settings is not None:
            read_timeout = getattr(settings, "emotion_affect_read_timeout_seconds", read_timeout)
        try:
            homeostasis = project_homeostasis(
                self.supervisor._homeostasis_snapshot.payload,
                self.supervisor._homeostasis_snapshot.revision,
            )
        except ValueError:
            self.supervisor._append_journal(
                "affect_snapshot_unavailable", data={"scope": scope, "error_type": "critical_homeostasis"})
            return None
        if method is None:
            self.supervisor._append_journal(
                "affect_snapshot_unavailable", data={"scope": scope, "error_type": "read_unavailable"})
            return None
        try:
            snapshot = await asyncio.wait_for(
                method({"scope": scope, "as_of": as_of.isoformat(), "homeostasis": homeostasis}),
                timeout=max(0.001, read_timeout),
            )
        except Exception as exc:
            self.supervisor._append_journal(
                "affect_snapshot_unavailable",
                data={"scope": scope, "error_type": type(exc).__name__},
            )
            return None
        if not snapshot or snapshot.get("status") == "off" or (snapshot.get("guidance") or {}).get("status") == "off":
            return None
        elapsed = round((time.monotonic() - started) * 1000, 3)
        self.supervisor._append_journal(
            "affect_snapshot_read",
            data={"scope": scope, "revision": snapshot.get("revision"),
                  "window_hash": snapshot.get("window_hash"),
                  "guidance_status": (snapshot.get("guidance") or {}).get("status"),
                  "affect_read_ms": elapsed},
        )
        return snapshot

    async def drain(self, timeout: float) -> bool:
        pending = {task for task in self.tasks if not task.done()}
        if not pending:
            return True
        _, remaining = await asyncio.wait(pending, timeout=timeout)
        return not remaining
