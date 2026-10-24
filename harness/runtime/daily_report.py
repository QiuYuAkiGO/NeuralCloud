"""Natural-day Runtime report aggregation, archive, and heartbeat finalize."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field

from harness.homeostasis.models import DIMENSIONS
from harness.files import replace_path
from harness.runtime.monitor import MonitorStore, _percentile, utc_now
from harness.runtime.report_timing import capture_report_timing, report_phase, report_timed


async def _report_io(function, *args, **kwargs):
    """Keep report I/O off the runtime loop and drain it before stores close."""
    task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
    try:
        return await asyncio.shield(task)
    except asyncio.CancelledError:
        await task
        raise


_FLASH_OFFPEAK = (0.007, 0.22, 0.66)
_FLASH_PEAK = (0.014, 0.44, 1.32)
_MARKED_TOPICS = (
    "journal.homeostatic_marked_trigger_scheduled",
    "journal.homeostatic_marked_trigger_reused",
    "journal.homeostatic_marked_trigger_failed",
    "journal.homeostatic_marked_trigger_deferred_lifecycle",
    "journal.homeostatic_marked_trigger_stale",
)


class DailyReportSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = "Asia/Shanghai"
    finalize_time: str = "04:00"
    retention_days: int = Field(default=365, ge=7, le=3650)
    qq_trace_path: Path | None = None
    qq_ledger_path: Path | None = None

    @classmethod
    def from_environment(cls) -> "DailyReportSettings":
        value = os.environ.get("NC_DAILY_REPORT_CONFIG")
        payload: dict[str, Any] = {}
        if value:
            path = Path(value)
            if (not path.is_absolute() or path.is_symlink() or not path.is_file()
                    or path.stat().st_size > 65536):
                raise ValueError("daily_report_configuration_invalid")
            payload = json.loads(path.read_text(encoding="utf-8-sig"))
        payload.setdefault("qq_trace_path", _env_path("NC_QQ_DIRECT_CONFIG", "state_db", suffix=".trace.jsonl"))
        payload.setdefault("qq_ledger_path", _env_path("NC_QQ_WATCHER_CONFIG", "state_db"))
        cleaned = {key: item for key, item in payload.items() if item not in {None, ""}}
        return cls.model_validate(cleaned)


def _env_path(env_name: str, field: str, *, suffix: str | None = None) -> str | None:
    raw = os.environ.get(env_name)
    if not raw:
        return None
    path = Path(raw)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
        value = Path(str(payload.get(field) or ""))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if suffix:
        value = value.with_suffix(suffix)
    return str(value) if value else None


def day_bounds(day: str, timezone: str) -> tuple[datetime, datetime]:
    zone = ZoneInfo(timezone)
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    return start.astimezone(UTC), end.astimezone(UTC)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _token_totals(prompt: int, completion: int, hit: int, miss: int) -> dict[str, Any]:
    if hit + miss == 0 and prompt:
        miss = prompt
    denom = hit + miss
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "total_tokens": prompt + completion if prompt else hit + miss + completion,
        "hit_rate": round(hit / denom, 4) if denom else 0.0,
    }


def _cost(hit: int, miss: int, completion: int, moment: datetime) -> float:
    hour = moment.astimezone(UTC).hour
    rates = _FLASH_PEAK if (1 <= hour < 4 or 6 <= hour < 10) else _FLASH_OFFPEAK
    return (hit * rates[0] + miss * rates[1] + completion * rates[2]) / 1_000_000


def _mean(values: list[float]) -> float | None:
    return round(sum(values) / len(values), 3) if values else None


class DailyReportStore:
    def __init__(self, directory: Path | str, *, retention_days: int = 365) -> None:
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.meta_path = self.directory / "meta.json"

    def path_for(self, day: str) -> Path:
        return self.directory / f"{day}.json"

    def load(self, day: str) -> dict[str, Any] | None:
        path = self.path_for(day)
        if not path.is_file():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save(self, report: dict[str, Any]) -> Path:
        path = self.path_for(str(report["day"]))
        tmp = path.with_suffix(".json.tmp")
        # Source visibility/retraction must still apply when opening archives.
        # Store numerical/operator receipts; refresh sensitive projections.
        stored = {key: value for key, value in report.items() if key not in {"timeline", "memories"}}
        tmp.write_text(json.dumps(stored, ensure_ascii=False, indent=2), encoding="utf-8")
        replace_path(tmp, path)
        meta = self.meta()
        if report.get("finalized"):
            meta["last_finalized_day"] = report["day"]
        self.meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        self.cleanup()
        return path

    def meta(self) -> dict[str, Any]:
        if not self.meta_path.is_file():
            return {"last_finalized_day": None}
        try:
            payload = json.loads(self.meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"last_finalized_day": None}
        return payload if isinstance(payload, dict) else {"last_finalized_day": None}

    def days(self) -> list[str]:
        names = sorted(path.stem for path in self.directory.glob("????-??-??.json"))
        return names

    def cleanup(self, *, now: datetime | None = None) -> int:
        cutoff = ((now or utc_now()).date() - timedelta(days=self.retention_days)).isoformat()
        removed = 0
        for path in self.directory.glob("????-??-??.json"):
            if path.stem < cutoff:
                path.unlink()
                removed += 1
        return removed


class DailyReportBuilder:
    def __init__(
        self,
        *,
        monitor: MonitorStore,
        state_dir: Path,
        settings: DailyReportSettings,
        homeostasis: Any | None = None,
        diary_store: Any | None = None,
        capabilities: Any | None = None,
        board: Any | None = None,
        goals: Any | None = None,
        cognitive: Any | None = None,
        runtime_status: Any | None = None,
        runtime_started_at: datetime | None = None,
    ) -> None:
        self.monitor = monitor
        self.state_dir = Path(state_dir)
        self.settings = settings
        self.homeostasis = homeostasis
        self.diary_store = diary_store
        self.capabilities = capabilities
        self.board = board
        self.goals = goals
        self.cognitive = cognitive
        self.runtime_status = runtime_status
        self.runtime_started_at = runtime_started_at

    async def build(self, day: str, *, finalized: bool = False) -> dict[str, Any]:
        start, end = day_bounds(day, self.settings.timezone)
        since, until = start.isoformat(), end.isoformat()
        with report_phase("monitor_wait"):
            samples, runs, events, resources, actions = await _report_io(self._monitor_rows, since, until)
        traces = await self._traces(since, until)
        revision = await self._revision(day)
        qq_tokens = await _report_io(self._qq_tokens, start, end)
        qq_idle = await _report_io(self._qq_idle, start, end)
        resource_summary = await _report_io(self._resources, resources)
        from harness.runtime.report_insights import operation_counts
        with report_phase("operations"):
            operations = await _report_io(operation_counts, self.capabilities, self.board, self.goals, start, end)
        goals_summary = {"available": False}
        if self.goals is not None and hasattr(self.goals, "budget"):
            with report_phase("goal_budget"):
                goals_summary = {"available": True, "enabled": self.goals.enabled, "budget": self.goals.budget(),
                                 "episode_count": operations["episode_count"]}
        with report_phase("assembly"):
            report = {
                "schema_version": "daily-report-v2",
                "day": day,
                "timezone": self.settings.timezone,
                "window": {"since": since, "until": until},
                "finalized": finalized,
                "generated_at": utc_now().isoformat(),
                "uptime": self._uptime(samples, start, end),
                "resources": resource_summary,
                "homeostasis": self._homeostasis(traces, events),
                "intent": self._intent(events, start, end, qq=qq_idle, operations=operations),
                "pipelines": self._pipelines(runs, events),
                "memory_revision": revision,
                "tokens": self._tokens(runs, qq_tokens, revision, start),
                "diary": {"wrote": bool(self.diary_store and self.diary_store.count(day) > 0)},
                "extras": self._extras(events, actions, start, end, goals_summary=goals_summary),
            }
        await self.add_insights(report, traces=traces, events=events)
        return report

    @report_timed("cognitive")
    async def agent_report(self, day, *, section="daily", offset=0, limit=25, run_since=None, after=None):
        start, end = day_bounds(day, self.settings.timezone)
        method = getattr(self.cognitive, "control", None)
        if not callable(method):
            return {"available": False, "reason": "cognitive_report_unavailable"}
        try:
            return await method("report.daily", {
                "since": start.isoformat(), "until": end.isoformat(), "run_since": run_since,
                "section": section, "offset": offset, "limit": limit, "after": after,
            })
        except Exception as exc:
            return {"available": False, "error_type": type(exc).__name__}

    @report_timed("insights")
    async def add_insights(self, report, *, traces=None, events=None, archived=False):
        from harness.runtime.report_insights import failure_events, run_health, timeline
        start, end = day_bounds(report["day"], self.settings.timezone)
        now = utc_now()
        run_start = self.runtime_started_at if self.runtime_started_at and self.runtime_started_at < end else None
        data = await self.agent_report(report["day"], run_since=run_start.isoformat() if run_start and not archived else None)
        if traces is None:
            traces = await self._traces(start.isoformat(), end.isoformat())
        if events is None:
            events = await _report_io(self.monitor.query_report_events, start.isoformat(), end.isoformat())
        with report_phase("timeline"):
            report["timeline"] = await _report_io(timeline, traces, data.get("affect") or {}, events)
        report["timeline"]["affect_available"] = bool(data.get("available"))
        report["memories"] = data.get("memories") or {"available": False, "items": [], "error_type": data.get("error_type")}
        today = data.get("today") or {}
        with report_phase("failures"):
            failures = {
                "items": sorted(failure_events(events) + today.get("failures", []), key=lambda row: row["at"], reverse=True),
                "legacy_undated": today.get("legacy_undated_failures", []),
                "background_available": bool(data.get("available")),
            }
        if not archived or "failures" not in report:
            report["failures"] = failures
        if not archived or "compaction_today" not in report:
            report["compaction_today"] = today.get("compaction")
        if not archived or "telemetry_started_at" not in report:
            report["telemetry_started_at"] = data.get("telemetry_started_at")
        if not archived:
            run_samples, run_events = [], []
            as_of = min(now, end)
            if run_start:
                with report_phase("run_rows"):
                    run_samples, run_events = await _report_io(lambda: (
                        self.monitor.query_samples(run_start.isoformat(), as_of.isoformat()),
                        self.monitor.query_report_events(run_start.isoformat(), as_of.isoformat())))
            with report_phase("run_health"):
                status = (self.runtime_status() if callable(self.runtime_status) else self.runtime_status) if as_of == now else None
                report["current_run"] = run_health(run_samples, run_start, as_of, status)
            report["current_run"]["compaction"] = (data.get("run") or {}).get("compaction")
            report["current_run"]["failures"] = sorted(
                failure_events(run_events) + (data.get("run") or {}).get("failures", []),
                key=lambda row: row["at"], reverse=True)

    @report_timed("monitor")
    def _monitor_rows(self, since: str, until: str):
        rows = []
        for label, reader in (
            ("samples", self.monitor.query_samples), ("runs", self.monitor.query_runs),
            ("events", self.monitor.query_report_events), ("resource_rows", self.monitor.query_resources),
            ("actions", self.monitor.query_actions),
        ):
            with report_phase(label):
                rows.append(reader(since, until))
        return tuple(rows)

    @report_timed("homeostasis")
    async def _traces(self, since: str, until: str) -> list[dict[str, Any]]:
        client = self.homeostasis
        if client is None or not hasattr(client, "read_traces"):
            return []
        try:
            reader = getattr(client, "read_report_traces", client.read_traces)
            traces = await reader(since=since, until=until)
        except Exception:
            return []
        return [dict(item) for item in traces or [] if isinstance(item, dict)]

    @report_timed("revision")
    async def _revision(self, day: str) -> dict[str, Any]:
        method = getattr(self.cognitive, "control", None)
        if not callable(method):
            return {"available": False}
        try:
            history = await method("daily_revision.history", {"limit": 200})
        except Exception as exc:
            return {"available": False, "error_type": type(exc).__name__}
        jobs = [job for job in history.get("runs") or [] if str(job.get("day") or "") == day]
        details = []
        for job in jobs:
            run_id = job.get("run_id")
            diff = {}
            if run_id:
                try:
                    diff = await method("daily_revision.diff", {"run_id": run_id})
                except Exception:
                    diff = {}
            changes = _revision_changes(diff.get("diff") if isinstance(diff, dict) else None)
            usage = job.get("usage") if isinstance(job.get("usage"), dict) else {}
            details.append({
                "run_id": run_id, "status": job.get("status"), "attempts": job.get("attempts"),
                "last_error": job.get("last_error"), "source_count": job.get("source_count"),
                "usage": usage, "changes": changes, "dry_run": job.get("dry_run"),
            })
        tokens = sum(int((item["usage"] or {}).get("actual_tokens") or 0) for item in details)
        return {"available": True, "jobs": details, "job_count": len(details), "actual_tokens": tokens}

    def _uptime(self, samples: list[dict[str, Any]], start: datetime, end: datetime) -> dict[str, Any]:
        gap_limit = 30.0
        by_lifecycle: dict[str, float] = {}
        gaps = 0
        gap_seconds = 0.0
        restarts = 0
        previous = None
        previous_lifecycle = None
        for sample in samples:
            moment = _parse_dt(sample.get("observed_at"))
            if moment is None:
                continue
            if previous is not None:
                delta = (moment - previous).total_seconds()
                if delta > gap_limit:
                    gaps += 1
                    gap_seconds += delta
                else:
                    key = previous_lifecycle or "unknown"
                    by_lifecycle[key] = by_lifecycle.get(key, 0.0) + max(0.0, delta)
                if previous_lifecycle == "STOPPED" and sample.get("lifecycle") not in {None, "STOPPED"}:
                    restarts += 1
                if previous_lifecycle in {"BOOTING", "STARTING"} and sample.get("lifecycle") == "RUNNING" and delta > 1:
                    restarts += 1
            previous = moment
            previous_lifecycle = str(sample.get("lifecycle") or "unknown")
        current = None
        if self.runtime_status is not None:
            status = self.runtime_status() if callable(self.runtime_status) else self.runtime_status
            since = getattr(status, "lifecycle_since", None)
            if since is not None:
                current = max(0.0, (utc_now() - since).total_seconds())
        covered = sum(by_lifecycle.values())
        window = (end - start).total_seconds()
        return {
            "window_seconds": window,
            "sampled_seconds": round(covered, 3),
            "gap_count": gaps,
            "gap_seconds": round(gap_seconds, 3),
            "lifecycle_seconds": {key: round(value, 3) for key, value in by_lifecycle.items()},
            "current_continuous_seconds": current,
            "restart_estimate": restarts,
            "sample_count": len(samples),
        }

    @report_timed("resources")
    def _resources(self, rows: list[dict[str, Any]]) -> dict[str, Any]:
        if not rows:
            return {"available": False, "roles": {}, "host": {}, "state_bytes": _dir_size(self.state_dir)}
        by_role: dict[str, list[dict[str, Any]]] = {}
        for row in rows:
            by_role.setdefault(str(row.get("role") or "unknown"), []).append(row)
        roles = {}
        for role, items in by_role.items():
            cpu = [float(item["cpu_percent"]) for item in items if item.get("cpu_percent") is not None]
            rss = [float(item["rss_bytes"]) for item in items if item.get("rss_bytes") is not None]
            roles[role] = {
                "samples": len(items),
                "available": any(item.get("available") for item in items),
                "cpu_mean": _mean(cpu), "cpu_peak": max(cpu) if cpu else None,
                "rss_mean_bytes": int(sum(rss) / len(rss)) if rss else None,
                "rss_peak_bytes": int(max(rss)) if rss else None,
            }
        host_cpu = [float(item["host_cpu_percent"]) for item in rows if item.get("host_cpu_percent") is not None]
        host_mem = [float(item["host_mem_percent"]) for item in rows if item.get("host_mem_percent") is not None]
        return {
            "available": True,
            "roles": roles,
            "host": {"cpu_mean": _mean(host_cpu), "cpu_peak": max(host_cpu) if host_cpu else None,
                     "mem_mean": _mean(host_mem), "mem_peak": max(host_mem) if host_mem else None},
            "state_bytes": _dir_size(self.state_dir),
            "sqlite_bytes": _sqlite_sizes(self.state_dir),
        }

    def _homeostasis(self, traces: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
        if not traces:
            return {"available": False, "trace_count": 0}
        dimensions: dict[str, dict[str, float | None]] = {}
        for name in DIMENSIONS:
            series: list[float] = []
            first = last = None
            for trace in traces:
                after = (trace.get("values_after") or {}).get(name)
                before = (trace.get("values_before") or {}).get(name)
                value = after if after is not None else before
                if value is None:
                    continue
                number = float(value)
                if first is None:
                    first = float(before) if before is not None else number
                last = number
                series.append(number)
            dimensions[name] = {
                "start": first, "end": last, "min": min(series) if series else None,
                "max": max(series) if series else None,
                "delta": None if first is None or last is None else round(last - first, 6),
            }
        salience: dict[str, int] = {}
        needs: dict[str, int] = {}
        affect: list[tuple[float, float, float]] = []
        episodes: set[str] = set()
        for trace in traces:
            label = trace.get("salience")
            if label:
                salience[str(label)] = salience.get(str(label), 0) + 1
            for need in trace.get("dominant_needs") or []:
                needs[str(need)] = needs.get(str(need), 0) + 1
            if trace.get("episode_id"):
                episodes.add(str(trace["episode_id"]))
            if None not in (trace.get("affect_valence"), trace.get("affect_arousal"), trace.get("affect_dominance")):
                affect.append((float(trace["affect_valence"]), float(trace["affect_arousal"]), float(trace["affect_dominance"])))
        return {
            "available": True, "trace_count": len(traces), "dimensions": dimensions,
            "salience": salience, "dominant_needs": needs, "episode_count": len(episodes),
            "core_affect_mean": (
                {"valence": _mean([item[0] for item in affect]),
                 "arousal": _mean([item[1] for item in affect]),
                 "dominance": _mean([item[2] for item in affect])}
                if affect else {"available": False}
            ),
        }

    def _intent(self, events: list[dict[str, Any]], start: datetime, end: datetime, *, qq: dict[str, Any],
                operations: dict[str, Any]) -> dict[str, Any]:
        marked: dict[str, int] = {}
        for event in events:
            topic = str(event.get("topic") or "")
            if topic in _MARKED_TOPICS:
                key = topic.rsplit("_", 1)[-1]
                marked[key] = marked.get(key, 0) + 1
        capabilities: dict[str, Any] = {"available": operations["capabilities_available"]}
        if operations["capabilities_available"]:
            rows = operations["calls"]
            by_channel: dict[str, dict[str, int]] = {}
            for row in rows:
                moment = _parse_dt(row.get("created_at") or row.get("completed_at") or row.get("requested_at"))
                if moment is None or not (start <= moment < end):
                    continue
                channel = str((row.get("offer") or {}).get("channel") or row.get("channel") or "")
                if not channel:
                    channel = "goal" if row.get("goal_binding") else "unknown"
                bucket = by_channel.setdefault(channel, {})
                action = str(
                    (row.get("decision") or {}).get("action")
                    or row.get("error_code")
                    or row.get("status")
                    or "unknown"
                )
                bucket[action] = bucket.get(action, 0) + 1
            capabilities = {"available": True, "by_channel": by_channel}
        board = {"available": operations["board_available"]}
        if operations["board_available"]:
            counts = {"write": 0, "wait": 0, "failed": 0}
            for row in operations["turns"]:
                moment = _parse_dt(row.get("requested_at"))
                if moment is None or not (start <= moment < end):
                    continue
                status = str(row.get("status") or "")
                if status == "committed":
                    counts["write"] += 1
                elif status == "wait":
                    counts["wait"] += 1
                elif status in {"error", "uncertain", "queue_failed"}:
                    counts["failed"] += 1
            board = {"available": True, **counts}
        idle = {"completed": 0, "deferred": 0, "failed": 0}
        for event in events:
            if event.get("topic") != "event.completed":
                continue
            payload = event.get("payload") or {}
            if payload.get("event_type") != "idle":
                continue
            status = str(payload.get("status") or "completed")
            idle[status] = idle.get(status, 0) + 1
        return {"marked": marked, "capabilities": capabilities, "board": board, "idle": idle, "qq": qq}

    @report_timed("qq_idle")
    def _qq_idle(self, start: datetime, end: datetime) -> dict[str, Any]:
        path = self.settings.qq_ledger_path
        if path is None or not Path(path).is_file():
            return {"available": False}
        connection: sqlite3.Connection | None = None
        try:
            connection = sqlite3.connect(path)
            connection.row_factory = sqlite3.Row
            tables = {
                str(row[0])
                for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
            }
            opportunities = (
                connection.execute("SELECT registered_at, record_json FROM qq_watch_idle_opportunities").fetchall()
                if "qq_watch_idle_opportunities" in tables else []
            )
            sends = (
                connection.execute("SELECT status, record_json FROM qq_watch_sends").fetchall()
                if "qq_watch_sends" in tables else []
            )
            attempts = (
                connection.execute("SELECT phase FROM qq_watch_delivery_attempts").fetchall()
                if "qq_watch_delivery_attempts" in tables else []
            )
            decisions = (
                connection.execute("SELECT record_json FROM qq_reply_decisions").fetchall()
                if "qq_reply_decisions" in tables else []
            )
        except sqlite3.Error:
            return {"available": False}
        finally:
            if connection is not None:
                connection.close()
        count = 0
        for row in opportunities:
            moment = _parse_dt(row["registered_at"])
            if moment is not None and start <= moment < end:
                count += 1
        send_status: dict[str, int] = {}
        for row in sends:
            try:
                payload = json.loads(row["record_json"]) if row["record_json"] else {}
            except json.JSONDecodeError:
                payload = {}
            moment = _parse_dt(payload.get("reserved_at") or payload.get("created_at"))
            if moment is None or not (start <= moment < end):
                continue
            status = str(row["status"] or "unknown")
            send_status[status] = send_status.get(status, 0) + 1
        homeostasis_decisions = 0
        homeostasis_sent = 0
        for row in decisions:
            try:
                payload = json.loads(row["record_json"]) if row["record_json"] else {}
            except json.JSONDecodeError:
                continue
            if str(payload.get("initiated_by") or "") != "homeostasis":
                continue
            moment = _parse_dt(payload.get("decided_at") or payload.get("created_at") or payload.get("completed_at"))
            if moment is not None and not (start <= moment < end):
                continue
            homeostasis_decisions += 1
            if str(payload.get("action") or "") in {"speak", "send"}:
                homeostasis_sent += 1
        return {
            "available": True, "idle_opportunities": count, "sends": send_status,
            "delivery_attempts": len(attempts),
            "homeostasis_decisions": homeostasis_decisions,
            "homeostasis_speak": homeostasis_sent,
        }

    def _pipelines(self, runs: list[dict[str, Any]], events: list[dict[str, Any]]) -> dict[str, Any]:
        paths: dict[str, int] = {}
        status: dict[str, int] = {}
        latencies: list[float] = []
        by_path: dict[str, list[float]] = {}
        stages: dict[str, list[float]] = {}
        revisions = 0
        for run in runs:
            path = str(run.get("path") or "unknown")
            paths[path] = paths.get(path, 0) + 1
            kind = str(run.get("status") or "unknown")
            status[kind] = status.get(kind, 0) + 1
            revisions += int(run.get("revisions") or 0)
            if run.get("latency_ms") is not None:
                latency = float(run["latency_ms"])
                latencies.append(latency)
                by_path.setdefault(path, []).append(latency)
            for stage in run.get("stages") or []:
                name = str(stage.get("stage") or stage.get("kind") or "unknown")
                if stage.get("latency_ms") is None:
                    continue
                stages.setdefault(name, []).append(float(stage["latency_ms"]))
        event_types: dict[str, int] = {}
        for event in events:
            if event.get("topic") != "event.completed":
                continue
            kind = str((event.get("payload") or {}).get("event_type") or "unknown")
            event_types[kind] = event_types.get(kind, 0) + 1
        return {
            "run_count": len(runs),
            "paths": paths,
            "status": status,
            "revisions": revisions,
            "latency_ms": {"p50": _percentile(latencies, 0.5), "p95": _percentile(latencies, 0.95),
                           "p99": _percentile(latencies, 0.99)},
            "latency_by_path": {key: {"p50": _percentile(values, 0.5), "p95": _percentile(values, 0.95)}
                                for key, values in by_path.items()},
            "stages": {key: {"mean": _mean(values), "p95": _percentile(values, 0.95), "count": len(values)}
                       for key, values in stages.items()},
            "event_types": event_types,
        }

    @report_timed("qq_tokens")
    def _qq_tokens(self, start: datetime, end: datetime) -> dict[str, Any]:
        path = self.settings.qq_trace_path
        if path is None or not Path(path).is_file():
            return {"available": False}
        hit = miss = completion = prompt = turns = 0
        try:
            for line in Path(path).read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                moment = _parse_dt(row.get("recorded_at") or row.get("first_seen_at"))
                if moment is None or not (start <= moment < end):
                    continue
                counters = row.get("counters") if isinstance(row.get("counters"), dict) else {}
                turns += 1
                hit += int(counters.get("gate_cache_hit_tokens") or 0) + int(counters.get("persona_cache_hit_tokens") or 0)
                miss += int(counters.get("gate_cache_miss_tokens") or 0) + int(counters.get("persona_cache_miss_tokens") or 0)
                completion += int(counters.get("completion_tokens") or 0)
                prompt += int(counters.get("prompt_tokens") or 0)
        except OSError:
            return {"available": False}
        totals = _token_totals(prompt or (hit + miss), completion, hit, miss)
        totals.update({"available": True, "turns": turns})
        return totals

    def _tokens(self, runs: list[dict[str, Any]], qq: dict[str, Any], revision: dict[str, Any], start: datetime) -> dict[str, Any]:
        prompt = sum(int(run.get("prompt_tokens") or 0) for run in runs)
        completion = sum(int(run.get("completion_tokens") or 0) for run in runs)
        hit = sum(int(run.get("cache_hit_tokens") or 0) for run in runs)
        miss = sum(int(run.get("cache_miss_tokens") or 0) for run in runs)
        monitor = _token_totals(prompt, completion, hit, miss)
        qq_hit = int(qq.get("cache_hit_tokens") or 0) if qq.get("available") else 0
        qq_miss = int(qq.get("cache_miss_tokens") or 0) if qq.get("available") else 0
        qq_out = int(qq.get("completion_tokens") or 0) if qq.get("available") else 0
        revision_tokens = int(revision.get("actual_tokens") or 0)
        combined = _token_totals(
            monitor["prompt_tokens"] + int(qq.get("prompt_tokens") or 0),
            monitor["completion_tokens"] + qq_out + revision_tokens,
            monitor["cache_hit_tokens"] + qq_hit,
            monitor["cache_miss_tokens"] + qq_miss,
        )
        combined["estimated_usd"] = round(_cost(
            combined["cache_hit_tokens"], combined["cache_miss_tokens"],
            combined["completion_tokens"], start,
        ), 6)
        combined["estimated"] = True
        return {"monitor_runs": monitor, "qq_trace": qq, "daily_revision_tokens": revision_tokens, "combined": combined}

    def _extras(self, events: list[dict[str, Any]], actions: list[dict[str, Any]], start: datetime, end: datetime,
                *, goals_summary: dict[str, Any]) -> dict[str, Any]:
        warnings = sum(1 for event in events if event.get("severity") == "warning")
        errors = sum(1 for event in events if event.get("severity") == "error")
        alerts = [event for event in events if event.get("severity") in {"warning", "error"}][:20]
        action_states: dict[str, int] = {}
        for item in actions:
            moment = _parse_dt(item.get("updated_at"))
            if moment is None or not (start <= moment < end):
                continue
            state = str(item.get("state") or "unknown")
            action_states[state] = action_states.get(state, 0) + 1
        watchdog = sum(1 for event in events if str(event.get("topic") or "").startswith("watchdog"))
        llm_errors = 0
        timeouts = 0
        truncated = 0
        for event in events:
            payload = event.get("payload") or {}
            error = str(payload.get("error") or payload.get("error_type") or "").lower()
            if "timeout" in error or payload.get("error_is_timeout"):
                timeouts += 1
            if "truncat" in error or "length" in error or payload.get("error_is_truncated"):
                truncated += 1
            if event.get("topic") == "event.completed" and payload.get("status") == "failed":
                llm_errors += 1
        return {
            "warnings": warnings, "errors": errors, "alerts": alerts,
            "actions": action_states, "watchdog": watchdog,
            "ingest_dropped": self.monitor.ingest_dropped,
            "llm_failed_events": llm_errors, "timeouts": timeouts, "truncated": truncated,
            "goals": goals_summary,
        }


class DailyReportController:
    def __init__(self, builder: DailyReportBuilder, store: DailyReportStore, settings: DailyReportSettings) -> None:
        self.builder = builder
        self.store = store
        self.settings = settings
        self._task: asyncio.Task | None = None
        self._finalize_lock = asyncio.Lock()
        self._closed = False
        self._last_finalized_day: str | None = None

    async def heartbeat(self) -> dict[str, Any] | None:
        if self._closed:
            return None
        if self._task is not None:
            if not self._task.done():
                return None
            task, self._task = self._task, None
            # Preserve the supervisor's daily_report_failed journal entry.
            task.result()
        zone = ZoneInfo(self.settings.timezone)
        now = utc_now().astimezone(zone)
        if now.strftime("%H:%M") < self.settings.finalize_time:
            return None
        day = (now.date() - timedelta(days=1)).isoformat()
        if day == self._last_finalized_day:
            return None
        self._task = asyncio.create_task(self._finalize_due(day), name="daily-report-finalize")
        # A report is an observability projection; it must never occupy the
        # sequential event dispatcher while waiting on a domain or disk.
        return None

    async def _finalize_due(self, day: str) -> dict[str, Any] | None:
        async with self._finalize_lock:
            archived = await _report_io(self.store.load, day)
            if archived is not None and archived.get("finalized"):
                self._last_finalized_day = day
                return None
            return await self._build_and_save(day)

    async def close(self) -> None:
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None

    @report_timed("report")
    async def report(self, day: str, *, live: bool = True) -> dict[str, Any]:
        with report_phase("archive"):
            archived = await _report_io(self.store.load, day)
        if archived is not None:
            await self.builder.add_insights(archived, archived=True)
            return archived
        if not live:
            raise FileNotFoundError(day)
        return await self.builder.build(day, finalized=False)

    async def finalize(self, day: str) -> dict[str, Any]:
        async with self._finalize_lock:
            return await self._build_and_save(day)

    async def _build_and_save(self, day: str) -> dict[str, Any]:
        report = await self.builder.build(day, finalized=True)
        await _report_io(self.store.save, report)
        self._last_finalized_day = day
        return report

    def days(self) -> list[str]:
        return self.store.days()


def add_report_routes(router, supervisor, *, authorize=None):
    from fastapi import HTTPException
    from fastapi.encoders import jsonable_encoder
    from starlette.responses import JSONResponse
    from harness.runtime.control import ControlAuthorizationError, ControlPermission

    def controller():
        value = getattr(supervisor, "daily_report", None)
        if value is None:
            raise HTTPException(status_code=503, detail="daily_report_unavailable")
        return value

    def require(operation: str) -> None:
        if authorize is None:
            return
        try:
            authorize(ControlPermission.DIAGNOSTIC, operation=operation)
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @router.get("/v1/report/daily")
    async def daily(day: str | None = None):
        require("report.daily")
        report = controller()
        target = day or utc_now().astimezone(ZoneInfo(report.settings.timezone)).date().isoformat()
        try:
            if len(target) != 10 or datetime.fromisoformat(target).strftime("%Y-%m-%d") != target:
                raise ValueError("invalid day")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid day") from exc
        with capture_report_timing() as timing:
            data = await report.report(target)
            # Match FastAPI's default encoding, while measuring the two stages
            # that happen after the controller returns.
            with report_phase("encode"):
                encoded = jsonable_encoder(data)
            with report_phase("render"):
                response = JSONResponse(encoded)
        response.headers["Server-Timing"] = timing.header()
        if timing.total_ms >= 10_000:
            logging.getLogger(__name__).warning("daily_report_slow %s", json.dumps(timing.snapshot()))
        return response

    @router.get("/v1/report/days")
    async def days():
        require("report.days")
        return {"days": controller().days(), "last_finalized_day": controller().store.meta().get("last_finalized_day")}

    @router.get("/v1/report/memories")
    async def memories(day: str, offset: int = 0, limit: int = 100, after: str | None = None):
        require("report.memories")
        try:
            if len(day) != 10 or datetime.fromisoformat(day).strftime("%Y-%m-%d") != day:
                raise ValueError("invalid day")
            if not 0 <= offset <= 1_000_000 or not 1 <= limit <= 200:
                raise ValueError("invalid page")
            if after is not None:
                from harness.agentic.reporting import decode_memory_cursor
                start, end = day_bounds(day, controller().builder.settings.timezone)
                decode_memory_cursor(after, start.timestamp(), end.timestamp())
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return await controller().builder.agent_report(day, section="memories", offset=offset, limit=limit, after=after)

    @router.post("/v1/report/daily:finalize")
    async def finalize(payload: dict[str, Any] | None = None):
        require("report.finalize")
        day = str((payload or {}).get("day") or "")
        try:
            if len(day) != 10 or datetime.fromisoformat(day).strftime("%Y-%m-%d") != day:
                raise ValueError("invalid day")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="invalid day") from exc
        return await controller().finalize(day)


def _revision_changes(diff: Any) -> dict[str, int]:
    if not isinstance(diff, dict):
        return {}
    counts: dict[str, int] = {}
    for key in ("episodic", "semantic", "impressions", "relationships", "facts", "entries"):
        value = diff.get(key)
        if isinstance(value, list):
            counts[key] = len(value)
        elif isinstance(value, dict):
            counts[key] = len(value)
    return counts


def _dir_size(path: Path) -> int:
    total = 0
    if not path.exists():
        return 0
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total


def _sqlite_sizes(path: Path) -> dict[str, int]:
    sizes: dict[str, int] = {}
    if not path.exists():
        return sizes
    for item in path.rglob("*.sqlite3"):
        try:
            sizes[item.name] = item.stat().st_size
        except OSError:
            continue
    return sizes
