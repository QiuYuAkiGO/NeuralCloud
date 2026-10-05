"""Passive Runtime self-check: heartbeat-scheduled, never a second mind or QQ sender."""
from __future__ import annotations

import asyncio
import json
import os
from datetime import timedelta
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harness.files import replace_path
from harness.runtime.models import utc_now


class SelfCheckSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = True
    interval_seconds: int = Field(default=3600, ge=60, le=86400)
    evidence_window_hours: int = Field(default=24, ge=1, le=168)
    retention_days: int = Field(default=30, ge=1, le=365)
    timezone: str = "Asia/Shanghai"

    @classmethod
    def from_environment(cls) -> "SelfCheckSettings":
        value = os.environ.get("NC_SELF_CHECK_CONFIG")
        if not value:
            return cls()
        path = Path(value)
        if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                or path.stat().st_size > 65536):
            raise ValueError("self_check_configuration_invalid")
        return cls.model_validate_json(path.read_text(encoding="utf-8-sig"))


class SelfCheckController:
    def __init__(self, supervisor, state_dir: Path, settings: SelfCheckSettings | None = None):
        self.supervisor = supervisor
        self.settings = settings or SelfCheckSettings()
        self.root = Path(state_dir) / "self-check"
        self.root.mkdir(parents=True, exist_ok=True)
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._closed = False
        self._last_started_at = None
        self._latest: dict[str, Any] | None = self._load_latest()

    def _load_latest(self) -> dict[str, Any] | None:
        path = self.root / "latest.json"
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    async def heartbeat(self) -> None:
        if self._closed or not self.settings.enabled:
            return
        if self._task is not None:
            if not self._task.done():
                return
            task, self._task = self._task, None
            task.result()
        now = utc_now()
        if self._last_started_at is not None:
            elapsed = (now - self._last_started_at).total_seconds()
            if elapsed < self.settings.interval_seconds:
                return
        self._last_started_at = now
        self._task = asyncio.create_task(self._run(), name="runtime-self-check")

    async def run_now(self) -> dict[str, Any]:
        async with self._lock:
            return await self._collect_and_store()

    async def _run(self) -> None:
        async with self._lock:
            await self._collect_and_store()

    async def _collect_and_store(self) -> dict[str, Any]:
        try:
            report = await self.collect()
            await asyncio.to_thread(self._persist, report)
            self._latest = report
            append = getattr(self.supervisor, "_append_journal", None)
            if append:
                append("self_check_completed", data=_journal_summary(report))
            return report
        except Exception as exc:
            append = getattr(self.supervisor, "_append_journal", None)
            if append:
                append("self_check_failed", data={"error_type": type(exc).__name__})
            raise

    def _history_counts(self, since):
        supervisor = self.supervisor
        counts: dict[str, int] = {}
        qq_turns = 0
        web_with_qq = 0
        for record in supervisor.journal.iter_records(reverse=True):
            if record.recorded_at < since:
                break
            counts[record.record_type] = counts.get(record.record_type, 0) + 1
            if record.record_type == "event_accepted":
                event = (record.data or {}).get("event") or {}
                if event.get("event_type") == "qq_turn":
                    qq_turns += 1
            if record.record_type == "web_request_completed":
                payload = record.data or {}
                if "qq" in str(payload.get("call_id") or payload.get("capability") or ""):
                    web_with_qq += 1
        return counts, qq_turns, web_with_qq

    async def collect(self) -> dict[str, Any]:
        supervisor = self.supervisor
        status = supervisor.status().model_dump(mode="json")
        window_hours = self.settings.evidence_window_hours
        since = utc_now() - timedelta(hours=window_hours)
        counts, qq_turns, web_with_qq = await supervisor.heartbeat.thread.run(self._history_counts, since)
        diary = getattr(supervisor, "diary", None)
        diary_snap = diary.snapshot() if diary is not None else None
        goals = supervisor.goals.snapshot() if getattr(supervisor, "goals", None) is not None else None
        capabilities = await supervisor.capabilities.snapshot()
        web = getattr(supervisor, "web", None)
        web_status = await web.status() if web is not None else None
        board = getattr(supervisor, "board", None)
        board_snap = await board.snapshot() if board is not None else None
        voice = getattr(supervisor, "voice", None)
        voice_status = await voice.status() if voice is not None else None
        server = getattr(supervisor, "server", None)
        server_status = await server.status() if server is not None else None
        paths = [
            _path("runtime.lifecycle", "pass" if status.get("lifecycle") == "RUNNING" else "fail",
                  {"lifecycle": status.get("lifecycle")}),
            _path("homeostasis.heartbeat",
                  "pass" if (status.get("health") or {}).get("last_heartbeat_at") else "fail",
                  {"last_heartbeat_at": (status.get("health") or {}).get("last_heartbeat_at")}),
            _path("diary.snapshot", "pass" if diary_snap else "skipped(not_configured)",
                  {"autonomous_enabled": (diary_snap or {}).get("autonomous_enabled"),
                   "wrote_today": (diary_snap or {}).get("wrote_today")}),
            _path("goals.snapshot", "pass" if (goals or {}).get("enabled") else "skipped(disabled)",
                  {"enabled": (goals or {}).get("enabled"), "goal_count": len((goals or {}).get("goals") or [])}),
            _path("capabilities.catalog", "pass" if capabilities.get("enabled") else "skipped(disabled)",
                  {"enabled": capabilities.get("enabled"), "owners": capabilities.get("owners"),
                   "ready": [item.get("capability_id") for item in capabilities.get("catalog") or [] if item.get("ready")][:16]}),
            _path("web.status", _web_status(web_status),
                  {"enabled": (web_status or {}).get("enabled"),
                   "conversation_enabled": (web_status or {}).get("conversation_enabled"),
                   "usage": (web_status or {}).get("usage")}),
            _path("board.snapshot", "pass" if board_snap else "skipped(not_configured)",
                  {"paused": (board_snap or {}).get("paused")} if board_snap else {}),
            _path("voice.status", "pass" if (voice_status or {}).get("enabled") else "skipped(disabled)",
                  {"enabled": (voice_status or {}).get("enabled")}),
            _path("server.status", "pass" if (server_status or {}).get("enabled") else "skipped(disabled)",
                  {"enabled": (server_status or {}).get("enabled")}),
            _path("web.journal", "pass" if counts.get("web_request_completed") else "no_recent_sample",
                  {"count": counts.get("web_request_completed", 0)}),
            _path("qq.web.passive", "pass" if web_with_qq else "no_recent_sample",
                  {"qq_turns": qq_turns, "web_records": counts.get("web_request_completed", 0)}),
            _path("goal.journal", "pass" if counts.get("production_goal_submitted") else "no_recent_sample",
                  {"count": counts.get("production_goal_submitted", 0)}),
        ]
        return {
            "generated_at": utc_now().isoformat(),
            "window_hours": window_hours,
            "lifecycle": status.get("lifecycle"),
            "paths": paths,
            "journal_counts": counts,
            "health": status.get("health"),
        }

    def snapshot(self) -> dict[str, Any]:
        history = sorted((path.name for path in self.root.glob("history-*.json")), reverse=True)[:30]
        return {"enabled": self.settings.enabled, "interval_seconds": self.settings.interval_seconds,
                "latest": self._latest, "history": history,
                "last_started_at": None if self._last_started_at is None else self._last_started_at.isoformat()}

    def _persist(self, report: dict[str, Any]) -> None:
        stamp = utc_now().strftime("%Y%m%dT%H%M%SZ")
        history = self.root / f"history-{stamp}.json"
        latest = self.root / "latest.json"
        payload = json.dumps(report, ensure_ascii=False, indent=2)
        tmp_history = self.root / f".{stamp}.history.tmp"
        tmp_latest = self.root / f".{stamp}.latest.tmp"
        tmp_history.write_text(payload, encoding="utf-8")
        replace_path(tmp_history, history)
        tmp_latest.write_text(payload, encoding="utf-8")
        replace_path(tmp_latest, latest)
        cutoff = utc_now() - timedelta(days=self.settings.retention_days)
        for path in self.root.glob("history-*.json"):
            try:
                stamp_name = path.name.removeprefix("history-").removesuffix(".json")
                from datetime import datetime, timezone
                when = datetime.strptime(stamp_name, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            if when < cutoff:
                path.unlink(missing_ok=True)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None


def _path(name: str, status: str, evidence: dict[str, Any]) -> dict[str, Any]:
    return {"path": name, "status": status, "evidence": evidence}


def _web_status(payload: dict[str, Any] | None) -> str:
    if not payload:
        return "skipped(not_configured)"
    if not payload.get("enabled"):
        return "skipped(disabled)"
    if not payload.get("conversation_enabled"):
        return "skipped(not_configured)"
    return "pass"


def _journal_summary(report: dict[str, Any]) -> dict[str, Any]:
    return {"generated_at": report.get("generated_at"), "lifecycle": report.get("lifecycle"),
            "paths": [{"path": item["path"], "status": item["status"]} for item in report.get("paths") or []]}


def add_self_check_routes(router, supervisor, *, authorize=None):
    from fastapi import HTTPException
    from harness.runtime.control import ControlAuthorizationError, ControlPermission

    def controller():
        value = getattr(supervisor, "self_check", None)
        if value is None:
            raise HTTPException(status_code=503, detail="self_check_unavailable")
        return value

    def require(operation: str) -> None:
        if authorize is None:
            return
        try:
            authorize(ControlPermission.DIAGNOSTIC, operation=operation)
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/v1/selfcheck")
    async def read_selfcheck():
        require("selfcheck.snapshot")
        return controller().snapshot()

    @router.post("/v1/selfcheck:run")
    async def run_selfcheck():
        require("selfcheck.run")
        return await controller().run_now()
