"""Rebuildable observability projection for the Runtime control plane.

The monitor database is deliberately non-authoritative. Runtime correctness
continues to depend on the hash-chained journal and domain stores; failures in
this module are surfaced to operators but never stop event processing.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import json
import math
import sqlite3
import threading
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from harness.runtime.journal import RuntimeJournal, RuntimeJournalRecord
from harness.runtime.models import RuntimeStatus
from harness.runtime.resources import ResourceSample, ResourceSampler
from infra.trace.serialize import dump_value


MONITOR_SCHEMA_VERSION = "monitor-v3"
DIAGNOSTIC_SCHEMA_VERSION = "monitor-diagnostic-v1"
DEFAULT_RETENTION_DAYS = 7
DEFAULT_SAMPLE_SECONDS = 10.0
MAX_PAGE_SIZE = 500


class MonitorEvent(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    observed_at: str
    source: str
    topic: str
    severity: str
    correlation_id: str | None = None
    run_id: str | None = None
    event_id: str | None = None
    payload: dict[str, Any]


class MonitorRun(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    run_id: str
    correlation_id: str | None = None
    event_id: str | None = None
    started_at: str
    completed_at: str | None = None
    updated_at: str
    status: str
    path: str | None = None
    latency_ms: float | None = None
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    revisions: int = 0
    error: str | None = None
    stages: list[dict[str, Any]] = Field(default_factory=list)


class MonitorSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    seq: int = Field(ge=1)
    observed_at: str
    lifecycle: str
    queue_size: int = 0
    receive_process_lag: int = 0
    pending_actions: int = 0
    coalesced: int = 0
    dropped: int = 0
    rejected: int = 0
    healthy: bool = True
    payload: dict[str, Any] = Field(default_factory=dict)


def utc_now() -> datetime:
    return datetime.now(UTC)


def _iso(value: datetime | str | None = None) -> str:
    if value is None:
        return utc_now().isoformat()
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return utc_now().isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC).isoformat()


def _encode_cursor(seq: int) -> str:
    return base64.urlsafe_b64encode(f"seq:{seq}".encode()).decode().rstrip("=")


def _decode_cursor(cursor: str | None) -> int | None:
    if not cursor:
        return None
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = base64.urlsafe_b64decode(padded.encode()).decode()
        prefix, raw = value.split(":", 1)
        if prefix != "seq":
            raise ValueError
        return int(raw)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("invalid monitor cursor") from exc


def _safe_limit(limit: int) -> int:
    return max(1, min(MAX_PAGE_SIZE, int(limit)))


_SECRET_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "control_token",
    "service_token",
    "access_token",
    "refresh_token",
    "password",
    "secret",
}


def sanitize_payload(value: Any) -> Any:
    """Drop vector payloads and redact credentials while preserving debug data."""

    value = dump_value(value)
    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered == "embedding":
                continue
            if (
                lowered in _SECRET_KEYS
                or lowered.endswith("_api_key")
                or lowered.endswith("_password")
                or (lowered.endswith("_token") and lowered not in {"prompt_token", "completion_token"})
            ):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = sanitize_payload(item)
        return cleaned
    if isinstance(value, list):
        return [sanitize_payload(item) for item in value]
    return value


def event_severity(topic: str, payload: dict[str, Any]) -> str:
    text = f"{topic} {payload.get('status', '')} {payload.get('state', '')}".lower()
    if any(word in text for word in ("fault", "failed", "error", "terminal", "unavailable")):
        return "error"
    if any(word in text for word in ("watchdog", "dropped", "rejected", "degraded", "paused")):
        return "warning"
    return "info"


def _stage_identity(stage: dict[str, Any]) -> tuple[str, str] | None:
    meta = stage.get("meta") if isinstance(stage.get("meta"), dict) else {}
    span_id = stage.get("span_id") or meta.get("span_id")
    name = stage.get("stage") or stage.get("kind")
    source_id = span_id or stage.get("source_id")
    return (str(name), str(source_id)) if name and source_id else None


def _merge_stages(existing: list[dict[str, Any]], incoming: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged = list(existing)
    identities = {_stage_identity(item) for item in existing if _stage_identity(item) is not None}
    for item in incoming:
        identity = _stage_identity(item)
        if identity is not None and identity in identities:
            continue
        merged.append(item)
        if identity is not None:
            identities.add(identity)
    return merged


def _percentile(values: list[float], percentile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = (len(ordered) - 1) * percentile
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return round(ordered[lower], 3)
    weight = index - lower
    return round(ordered[lower] * (1 - weight) + ordered[upper] * weight, 3)


class MonitorStore:
    """Thread-safe SQLite projection used by monitoring HTTP handlers."""

    def __init__(self, path: Path | str, *, retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
        self.path = Path(path)
        self.retention_days = max(1, int(retention_days))
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.last_error: str | None = None
        self.startup_error: str | None = None
        self.ingest_dropped = 0
        self._connection = sqlite3.connect(self.path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._initialize()

    @property
    def healthy(self) -> bool:
        return self.last_error is None and self.startup_error is None

    def _initialize(self) -> None:
        with self._lock:
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._connection.execute("PRAGMA synchronous=NORMAL")
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitor_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS monitor_events (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    source TEXT NOT NULL,
                    topic TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    correlation_id TEXT,
                    run_id TEXT,
                    event_id TEXT,
                    source_ref TEXT UNIQUE,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_events_time ON monitor_events(observed_at DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_events_topic ON monitor_events(topic, seq DESC);
                CREATE INDEX IF NOT EXISTS idx_monitor_events_correlation ON monitor_events(correlation_id, seq);
                CREATE INDEX IF NOT EXISTS idx_monitor_events_run ON monitor_events(run_id, seq);
                CREATE INDEX IF NOT EXISTS idx_monitor_events_event ON monitor_events(event_id, seq);
                CREATE TABLE IF NOT EXISTS monitor_runs (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL UNIQUE,
                    correlation_id TEXT,
                    event_id TEXT,
                    started_at TEXT NOT NULL,
                    completed_at TEXT,
                    updated_at TEXT NOT NULL,
                    status TEXT NOT NULL,
                    path TEXT,
                    latency_ms REAL,
                    prompt_tokens INTEGER NOT NULL DEFAULT 0,
                    completion_tokens INTEGER NOT NULL DEFAULT 0,
                    total_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_hit_tokens INTEGER NOT NULL DEFAULT 0,
                    cache_miss_tokens INTEGER NOT NULL DEFAULT 0,
                    revisions INTEGER NOT NULL DEFAULT 0,
                    error TEXT,
                    stages_json TEXT NOT NULL DEFAULT '[]'
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_runs_time ON monitor_runs(updated_at DESC);
                CREATE TABLE IF NOT EXISTS monitor_samples (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    lifecycle TEXT NOT NULL,
                    queue_size INTEGER NOT NULL,
                    receive_process_lag INTEGER NOT NULL,
                    pending_actions INTEGER NOT NULL,
                    coalesced INTEGER NOT NULL,
                    dropped INTEGER NOT NULL,
                    rejected INTEGER NOT NULL,
                    healthy INTEGER NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_samples_time ON monitor_samples(observed_at DESC);
                CREATE TABLE IF NOT EXISTS monitor_actions (
                    action_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL,
                    state TEXT NOT NULL,
                    correlation_id TEXT,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_actions_time ON monitor_actions(updated_at DESC);
                CREATE TABLE IF NOT EXISTS monitor_resources (
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,
                    observed_at TEXT NOT NULL,
                    role TEXT NOT NULL,
                    pid INTEGER,
                    cpu_percent REAL,
                    rss_bytes INTEGER,
                    num_threads INTEGER,
                    host_cpu_percent REAL,
                    host_mem_percent REAL,
                    available INTEGER NOT NULL,
                    detail TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_monitor_resources_time ON monitor_resources(observed_at DESC);
                """
            )
            existing = {row[1] for row in self._connection.execute("PRAGMA table_info(monitor_runs)")}
            for column, decl in (
                ("cache_hit_tokens", "INTEGER NOT NULL DEFAULT 0"),
                ("cache_miss_tokens", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column not in existing:
                    self._connection.execute(f"ALTER TABLE monitor_runs ADD COLUMN {column} {decl}")
            self._connection.execute(
                "INSERT OR REPLACE INTO monitor_meta(key, value) VALUES('schema_version', ?)",
                (MONITOR_SCHEMA_VERSION,),
            )
            self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def append_event(
        self,
        topic: str,
        payload: dict[str, Any],
        *,
        observed_at: datetime | str | None = None,
        source: str | None = None,
        source_ref: str | None = None,
        project: bool = True,
        journal_cursor: dict | None = None,
    ) -> MonitorEvent | None:
        clean = sanitize_payload(payload)
        if not isinstance(clean, dict):
            clean = {"value": clean}
        normalized_topic = topic
        if topic == "cognitive.ui":
            kind = str(clean.get("type") or "event")
            stage = str(clean.get("stage") or "")
            normalized_topic = f"cognitive.stage.{stage}" if kind in {"stage", "final"} and stage else f"cognitive.{kind}"
        timestamp = _iso(
            observed_at
            or clean.get("observed_at")
            or clean.get("received_at")
            or clean.get("completed_at")
            or clean.get("at")
            or clean.get("ts")
        )
        data = clean.get("data") if isinstance(clean.get("data"), dict) else {}
        meta = clean.get("meta") if isinstance(clean.get("meta"), dict) else {}
        run_id = clean.get("run_id") or data.get("run_id")
        event_id = clean.get("event_id") or meta.get("event_id") or data.get("event_id")
        correlation_id = clean.get("correlation_id") or meta.get("correlation_id") or data.get("correlation_id")
        identity = _stage_identity(clean) if topic.startswith("cognitive.") else None
        if source_ref is None and identity is not None and run_id:
            source_ref = f"stage:{run_id}:{identity[0]}:{identity[1]}"
        domain = source or normalized_topic.split(".", 1)[0]
        severity = event_severity(normalized_topic, clean)
        payload_json = json.dumps(clean, ensure_ascii=False, separators=(",", ":"), default=str)
        try:
            with self._lock:
                if not correlation_id and event_id:
                    row = self._connection.execute(
                        "SELECT correlation_id FROM monitor_events WHERE event_id=? AND correlation_id IS NOT NULL ORDER BY seq LIMIT 1",
                        (str(event_id),),
                    ).fetchone()
                    if row:
                        correlation_id = row["correlation_id"]
                if not correlation_id and run_id:
                    row = self._connection.execute(
                        "SELECT correlation_id FROM monitor_runs WHERE run_id=?", (str(run_id),),
                    ).fetchone()
                    correlation_id = (row["correlation_id"] if row else None) or run_id
                if journal_cursor is not None:
                    self._connection.execute("INSERT OR REPLACE INTO monitor_meta VALUES ('journal_cursor',?)",
                                             (json.dumps(journal_cursor),))
                cursor = self._connection.execute(
                    """
                    INSERT OR IGNORE INTO monitor_events(
                        observed_at, source, topic, severity, correlation_id, run_id,
                        event_id, source_ref, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        timestamp,
                        domain,
                        normalized_topic,
                        severity,
                        str(correlation_id) if correlation_id else None,
                        str(run_id) if run_id else None,
                        str(event_id) if event_id else None,
                        source_ref,
                        payload_json,
                    ),
                )
                if cursor.rowcount == 0:
                    self._connection.commit()
                    self.last_error = None
                    return None
                seq = int(cursor.lastrowid)
                if project:
                    self._project_run_locked(seq, normalized_topic, clean, timestamp)
                    self._project_action_locked(clean, timestamp, str(correlation_id) if correlation_id else None)
                self._connection.commit()
                self.last_error = None
            return MonitorEvent(
                seq=seq,
                observed_at=timestamp,
                source=domain,
                topic=normalized_topic,
                severity=severity,
                correlation_id=str(correlation_id) if correlation_id else None,
                run_id=str(run_id) if run_id else None,
                event_id=str(event_id) if event_id else None,
                payload=clean,
            )
        except sqlite3.Error as exc:
            with self._lock:
                self._connection.rollback()
            self.last_error = str(exc)
            if journal_cursor is not None:
                raise
            return None

    def append_journal(self, record: RuntimeJournalRecord, *, journal_cursor: dict | None = None) -> MonitorEvent | None:
        payload = record.model_dump(mode="json")
        payload["integrity"] = {
            "checksum_valid": record.verify_checksum(),
            "chain_position": record.record_seq,
            "has_previous": bool(record.prev_hash),
        }
        return self.append_event(
            f"journal.{record.record_type}",
            payload,
            observed_at=record.recorded_at,
            source="journal",
            source_ref=f"journal:{record.runtime_instance_id}:{record.record_seq}",
            journal_cursor=journal_cursor,
        )

    def journal_cursor(self):
        with self._lock:
            row = self._connection.execute("SELECT value FROM monitor_meta WHERE key='journal_cursor'").fetchone()
        try:
            return json.loads(row[0]) if row else None
        except (ValueError, TypeError):
            return None

    def advance_journal_cursor(self, cursor):
        with self._lock, self._connection:
            self._connection.execute("INSERT OR REPLACE INTO monitor_meta VALUES ('journal_cursor',?)", (json.dumps(cursor),))

    def append_call_log(self, path: Path, payload: dict[str, Any]) -> None:
        if str(payload.get("kind") or "") not in {"respond", "prepare_response", "prepare_autonomous_response"}:
            return
        call_id = str(payload.get("call_id") or path.stem)
        started_at = _iso(payload.get("ts"))
        steps = list(payload.get("steps") or [])
        result = payload.get("result") if isinstance(payload.get("result"), dict) else {}
        metadata = payload.get("source_metadata") if isinstance(payload.get("source_metadata"), dict) else {}
        correlation_id = payload.get("correlation_id") or metadata.get("correlation_id")
        event_id = payload.get("event_id") or metadata.get("event_id") or result.get("event_id")
        stage_rows = []
        for index, step in enumerate(steps):
            if not isinstance(step, dict):
                continue
            stage = str(step.get("kind") or "step")
            meta = {
                **{key: value for key, value in metadata.items() if key in {"correlation_id", "event_id", "command_id", "phase"}},
                **(step.get("meta") if isinstance(step.get("meta"), dict) else {}),
            }
            stage_failed = meta.get("status") == "error"
            event_payload = {
                "type": "error" if stage_failed else "final" if stage == "final" else "stage",
                "run_id": call_id,
                "correlation_id": correlation_id,
                "event_id": event_id,
                "stage": stage,
                "latency_ms": step.get("latency_ms", 0),
                "data": step.get("data") or {},
                "meta": meta,
            }
            identity = _stage_identity(event_payload)
            source_id = f"stage:{call_id}:{identity[0]}:{identity[1]}" if identity else f"call:{call_id}:{index}"
            self.append_event(
                "cognitive.ui",
                event_payload,
                observed_at=meta.get("completed_at") or started_at,
                source="call_log",
                source_ref=source_id,
                project=False,
            )
            stage_rows.append(sanitize_payload({
                "stage": stage, "latency_ms": step.get("latency_ms", 0),
                "data": step.get("data") or {}, "meta": meta, "source_id": source_id,
            }))
        usage = result.get("usage") if isinstance(result.get("usage"), dict) else {}
        failed = payload.get("ok") is False or any(item["meta"].get("status") == "error" for item in stage_rows)
        status = "failed" if failed else "completed" if payload.get("ok") is True or result else "running"
        error_payload = payload.get("error")
        error = (error_payload.get("message") or error_payload.get("type")) if isinstance(error_payload, dict) else error_payload
        if failed and not error:
            error = "pipeline call failed"
        completed_at = (
            _iso(payload.get("completed_at") or (
                datetime.fromisoformat(started_at) + timedelta(milliseconds=float(payload.get("latency_ms") or 0))
            )) if status != "running" else None
        )
        with self._lock:
            row = self._connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (call_id,)).fetchone()
            existing_stages = json.loads(row["stages_json"]) if row else []
            stages_json = json.dumps(_merge_stages(existing_stages, stage_rows), ensure_ascii=False, default=str)
            if row and (row["status"] == "failed" or status == "running"):
                status = row["status"]
                completed_at = completed_at or row["completed_at"]
            updated_at = completed_at or started_at
            self._connection.execute(
                """
                INSERT INTO monitor_runs(
                    run_id, correlation_id, event_id, started_at, completed_at, updated_at,
                    status, path, latency_ms, prompt_tokens, completion_tokens,
                    total_tokens, cache_hit_tokens, cache_miss_tokens, revisions, error, stages_json
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    correlation_id=excluded.correlation_id,
                    event_id=COALESCE(excluded.event_id, monitor_runs.event_id),
                    started_at=MIN(excluded.started_at, monitor_runs.started_at),
                    completed_at=COALESCE(excluded.completed_at, monitor_runs.completed_at),
                    updated_at=excluded.updated_at,
                    status=excluded.status,
                    path=COALESCE(excluded.path, monitor_runs.path),
                    latency_ms=COALESCE(excluded.latency_ms, monitor_runs.latency_ms),
                    prompt_tokens=excluded.prompt_tokens,
                    completion_tokens=excluded.completion_tokens,
                    total_tokens=excluded.total_tokens,
                    cache_hit_tokens=excluded.cache_hit_tokens,
                    cache_miss_tokens=excluded.cache_miss_tokens,
                    revisions=excluded.revisions,
                    error=COALESCE(excluded.error, monitor_runs.error),
                    stages_json=excluded.stages_json
                """,
                (
                    call_id,
                    correlation_id or (row["correlation_id"] if row else None) or call_id,
                    event_id,
                    started_at,
                    completed_at,
                    updated_at,
                    status,
                    result.get("path"),
                    payload.get("latency_ms", result.get("latency_ms")),
                    int(usage.get("prompt_tokens", row["prompt_tokens"] if row else 0) or 0),
                    int(usage.get("completion_tokens", row["completion_tokens"] if row else 0) or 0),
                    int(usage.get("total_tokens", row["total_tokens"] if row else 0) or 0),
                    int(usage.get("cache_hit_tokens", row["cache_hit_tokens"] if row else 0) or 0),
                    int(usage.get("cache_miss_tokens", row["cache_miss_tokens"] if row else 0) or 0),
                    int(result.get("revisions", row["revisions"] if row else 0) or 0),
                    str(error) if error else None,
                    stages_json,
                ),
            )
            self._connection.commit()

    def _project_run_locked(self, _seq: int, topic: str, payload: dict[str, Any], observed_at: str) -> None:
        run_id = payload.get("run_id")
        if not run_id or not topic.startswith("cognitive."):
            return
        run_id = str(run_id)
        existing = self._connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        stages = json.loads(existing["stages_json"]) if existing else []
        stage = payload.get("stage")
        if stage:
            stages = _merge_stages(stages, [sanitize_payload({
                "stage": stage,
                "latency_ms": payload.get("latency_ms", 0),
                "data": payload.get("data") or {},
                "meta": payload.get("meta") or {},
            })])
        kind = str(payload.get("type") or "stage")
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}
        status = "failed" if kind == "error" or meta.get("status") == "error" else "completed" if kind == "final" or stage == "final" else "running"
        error = (data.get("message") or meta.get("error_type") or "pipeline stage failed") if status == "failed" else None
        if existing and (existing["status"] == "failed" or status == "running"):
            status = existing["status"]
        started_at = existing["started_at"] if existing else observed_at
        completed_at = (existing["completed_at"] if existing else None) or (observed_at if status in {"completed", "failed"} else None)
        latency = data.get("latency_ms") if kind == "final" or stage == "final" else (existing["latency_ms"] if existing else None)
        correlation_id = payload.get("correlation_id") or meta.get("correlation_id") or data.get("correlation_id")
        self._connection.execute(
            """
            INSERT INTO monitor_runs(
                run_id, correlation_id, event_id, started_at, completed_at, updated_at,
                status, path, latency_ms, prompt_tokens, completion_tokens,
                total_tokens, cache_hit_tokens, cache_miss_tokens, revisions, error, stages_json
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                correlation_id=excluded.correlation_id,
                event_id=COALESCE(excluded.event_id, monitor_runs.event_id),
                completed_at=COALESCE(excluded.completed_at, monitor_runs.completed_at),
                updated_at=excluded.updated_at,
                status=excluded.status,
                path=COALESCE(excluded.path, monitor_runs.path),
                latency_ms=COALESCE(excluded.latency_ms, monitor_runs.latency_ms),
                prompt_tokens=CASE WHEN excluded.prompt_tokens > 0 THEN excluded.prompt_tokens ELSE monitor_runs.prompt_tokens END,
                completion_tokens=CASE WHEN excluded.completion_tokens > 0 THEN excluded.completion_tokens ELSE monitor_runs.completion_tokens END,
                total_tokens=CASE WHEN excluded.total_tokens > 0 THEN excluded.total_tokens ELSE monitor_runs.total_tokens END,
                cache_hit_tokens=CASE WHEN excluded.cache_hit_tokens > 0 THEN excluded.cache_hit_tokens ELSE monitor_runs.cache_hit_tokens END,
                cache_miss_tokens=CASE WHEN excluded.cache_miss_tokens > 0 THEN excluded.cache_miss_tokens ELSE monitor_runs.cache_miss_tokens END,
                revisions=CASE WHEN excluded.revisions > 0 THEN excluded.revisions ELSE monitor_runs.revisions END,
                error=COALESCE(excluded.error, monitor_runs.error),
                stages_json=excluded.stages_json
            """,
            (
                run_id,
                correlation_id or (existing["correlation_id"] if existing else None) or run_id,
                payload.get("event_id") or meta.get("event_id") or data.get("event_id"),
                started_at,
                completed_at,
                observed_at,
                status,
                data.get("path"),
                float(latency) if latency is not None else None,
                int(usage.get("prompt_tokens") or 0),
                int(usage.get("completion_tokens") or 0),
                int(usage.get("total_tokens") or 0),
                int(usage.get("cache_hit_tokens") or usage.get("prompt_cache_hit_tokens") or 0),
                int(usage.get("cache_miss_tokens") or usage.get("prompt_cache_miss_tokens") or 0),
                int(data.get("revisions") or 0),
                str(error) if error else None,
                json.dumps(stages, ensure_ascii=False, separators=(",", ":"), default=str),
            ),
        )

    def _project_action_locked(self, payload: dict[str, Any], observed_at: str, correlation_id: str | None) -> None:
        candidates = [payload.get("action")]
        data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        candidates.extend([data.get("action"), data.get("current_action")])
        for action in candidates:
            if not isinstance(action, dict) or not action.get("action_id"):
                continue
            clean = sanitize_payload(action)
            self._connection.execute(
                """
                INSERT INTO monitor_actions(action_id, updated_at, state, correlation_id, payload_json)
                VALUES(?, ?, ?, ?, ?)
                ON CONFLICT(action_id) DO UPDATE SET
                    updated_at=excluded.updated_at,
                    state=excluded.state,
                    correlation_id=COALESCE(excluded.correlation_id, monitor_actions.correlation_id),
                    payload_json=excluded.payload_json
                """,
                (
                    str(action["action_id"]),
                    _iso(action.get("updated_at") or observed_at),
                    str(action.get("state") or "UNKNOWN"),
                    correlation_id,
                    json.dumps(clean, ensure_ascii=False, separators=(",", ":"), default=str),
                ),
            )

    def append_sample(self, status: RuntimeStatus) -> MonitorSample | None:
        payload = sanitize_payload(status.model_dump(mode="json"))
        observed_at = utc_now().isoformat()
        queue = status.queue
        healthy = not status.health.degraded_reasons and status.lifecycle.value not in {"FAULTED"}
        try:
            with self._lock:
                cursor = self._connection.execute(
                    """
                    INSERT INTO monitor_samples(
                        observed_at, lifecycle, queue_size, receive_process_lag,
                        pending_actions, coalesced, dropped, rejected, healthy, payload_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        observed_at,
                        status.lifecycle.value,
                        queue.size,
                        max(0, status.receive_seq - status.process_seq),
                        status.pending_actions,
                        queue.coalesced,
                        queue.dropped,
                        queue.rejected,
                        1 if healthy else 0,
                        json.dumps(payload, ensure_ascii=False, separators=(",", ":"), default=str),
                    ),
                )
                self._connection.commit()
                self.last_error = None
            return MonitorSample(
                seq=int(cursor.lastrowid),
                observed_at=observed_at,
                lifecycle=status.lifecycle.value,
                queue_size=queue.size,
                receive_process_lag=max(0, status.receive_seq - status.process_seq),
                pending_actions=status.pending_actions,
                coalesced=queue.coalesced,
                dropped=queue.dropped,
                rejected=queue.rejected,
                healthy=healthy,
                payload=payload,
            )
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return None

    def append_resources(self, samples: list[ResourceSample]) -> int:
        if not samples:
            return 0
        try:
            with self._lock:
                self._connection.executemany(
                    """
                    INSERT INTO monitor_resources(
                        observed_at, role, pid, cpu_percent, rss_bytes, num_threads,
                        host_cpu_percent, host_mem_percent, available, detail
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        (
                            item.observed_at, item.role, item.pid, item.cpu_percent,
                            item.rss_bytes, item.num_threads, item.host_cpu_percent,
                            item.host_mem_percent, 1 if item.available else 0, item.detail,
                        )
                        for item in samples
                    ],
                )
                self._connection.commit()
                self.last_error = None
            return len(samples)
        except sqlite3.Error as exc:
            self.last_error = str(exc)
            return 0

    def query_samples(self, since: str, until: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                """SELECT seq, observed_at, lifecycle, queue_size, receive_process_lag,
                          pending_actions, coalesced, dropped, rejected, healthy,
                          '{}' AS payload_json
                   FROM monitor_samples WHERE observed_at >= ? AND observed_at < ? ORDER BY seq ASC""",
                (_iso(since), _iso(until)),
            ).fetchall()
        return [self._sample_from_row(row).model_dump(mode="json") for row in rows]

    def query_runs(self, since: str, until: str) -> list[dict[str, Any]]:
        return self._query_range("monitor_runs", "started_at", since, until, self._run_from_row)

    def query_events(self, since: str, until: str) -> list[dict[str, Any]]:
        return self._query_range("monitor_events", "observed_at", since, until, self._event_from_row)

    def query_resources(self, since: str, until: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM monitor_resources WHERE observed_at >= ? AND observed_at < ? ORDER BY seq ASC",
                (_iso(since), _iso(until)),
            ).fetchall()
        return [
            {
                "seq": int(row["seq"]), "observed_at": row["observed_at"], "role": row["role"],
                "pid": row["pid"], "cpu_percent": row["cpu_percent"], "rss_bytes": row["rss_bytes"],
                "num_threads": row["num_threads"], "host_cpu_percent": row["host_cpu_percent"],
                "host_mem_percent": row["host_mem_percent"], "available": bool(row["available"]),
                "detail": row["detail"],
            }
            for row in rows
        ]

    def query_actions(self, since: str, until: str) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM monitor_actions WHERE updated_at >= ? AND updated_at < ? ORDER BY updated_at ASC",
                (_iso(since), _iso(until)),
            ).fetchall()
        return [
            {
                "action_id": row["action_id"], "updated_at": row["updated_at"],
                "state": row["state"], "correlation_id": row["correlation_id"],
                "action": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def _query_range(self, table: str, column: str, since: str, until: str, factory) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM {table} WHERE {column} >= ? AND {column} < ? ORDER BY seq ASC",
                (_iso(since), _iso(until)),
            ).fetchall()
        return [factory(row).model_dump(mode="json") for row in rows]

    def list_events(
        self,
        *,
        since: str | None = None,
        until: str | None = None,
        topic: str | None = None,
        severity: str | None = None,
        status: str | None = None,
        correlation_id: str | None = None,
        cursor: str | None = None,
        after_seq: int | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("observed_at >= ?")
            params.append(_iso(since))
        if until:
            clauses.append("observed_at <= ?")
            params.append(_iso(until))
        if topic:
            clauses.append("topic LIKE ?")
            params.append(f"{topic}%")
        if severity:
            clauses.append("severity = ?")
            params.append(severity)
        if correlation_id:
            clauses.append("correlation_id = ?")
            params.append(correlation_id)
        if status:
            clauses.append("payload_json LIKE ?")
            params.append(f'%"status":"{status}"%')
        cursor_seq = _decode_cursor(cursor)
        if cursor_seq is not None:
            clauses.append("seq < ?")
            params.append(cursor_seq)
        if after_seq is not None:
            clauses.append("seq > ?")
            params.append(int(after_seq))
        page_size = _safe_limit(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        order = "ASC" if after_seq is not None else "DESC"
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM monitor_events {where} ORDER BY seq {order} LIMIT ?",
                (*params, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [self._event_from_row(row).model_dump(mode="json") for row in rows]
        next_cursor = _encode_cursor(int(rows[-1]["seq"])) if has_more and rows else None
        return {"items": items, "next_cursor": next_cursor, "has_more": has_more}

    def list_runs(
        self,
        *,
        since: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        clauses: list[str] = []
        params: list[Any] = []
        if since:
            clauses.append("updated_at >= ?")
            params.append(_iso(since))
        if status:
            clauses.append("status = ?")
            params.append(status)
        cursor_seq = _decode_cursor(cursor)
        if cursor_seq is not None:
            clauses.append("seq < ?")
            params.append(cursor_seq)
        page_size = _safe_limit(limit)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._lock:
            rows = self._connection.execute(
                f"SELECT * FROM monitor_runs {where} ORDER BY seq DESC LIMIT ?",
                (*params, page_size + 1),
            ).fetchall()
        has_more = len(rows) > page_size
        rows = rows[:page_size]
        items = [self._run_from_row(row).model_dump(mode="json") for row in rows]
        return {
            "items": items,
            "next_cursor": _encode_cursor(int(rows[-1]["seq"])) if has_more and rows else None,
            "has_more": has_more,
        }

    def get_run(self, run_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute("SELECT * FROM monitor_runs WHERE run_id=?", (run_id,)).fetchone()
        return self._run_from_row(row).model_dump(mode="json") if row else None

    def list_actions(self, *, limit: int = 100) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM monitor_actions ORDER BY updated_at DESC LIMIT ?", (_safe_limit(limit),)
            ).fetchall()
        return [
            {
                "action_id": row["action_id"],
                "updated_at": row["updated_at"],
                "state": row["state"],
                "correlation_id": row["correlation_id"],
                "action": json.loads(row["payload_json"]),
            }
            for row in rows
        ]

    def overview(self, *, window: str, status: RuntimeStatus, manifest: dict[str, Any]) -> dict[str, Any]:
        seconds = _window_seconds(window)
        since = (utc_now() - timedelta(seconds=seconds)).isoformat()
        with self._lock:
            counts = self._connection.execute(
                """SELECT COUNT(*) AS events,
                          COALESCE(SUM(severity = 'warning'), 0) AS warnings,
                          COALESCE(SUM(severity = 'error'), 0) AS errors,
                          COALESCE(SUM(CASE WHEN topic = 'event.completed' THEN json_extract(payload_json, '$.status') = 'completed' ELSE 0 END), 0) AS completed,
                          COALESCE(SUM(CASE WHEN topic = 'event.completed' THEN json_extract(payload_json, '$.status') = 'failed' ELSE 0 END), 0) AS failed,
                          COALESCE(SUM(CASE WHEN topic = 'event.completed' THEN json_extract(payload_json, '$.status') = 'deferred' ELSE 0 END), 0) AS deferred
                   FROM monitor_events WHERE observed_at >= ?""", (since,)
            ).fetchone()
            alert_rows = self._connection.execute(
                """SELECT * FROM monitor_events WHERE observed_at >= ?
                   AND severity IN ('warning', 'error') ORDER BY seq DESC LIMIT 20""", (since,)
            ).fetchall()
            run_rows = self._connection.execute(
                """SELECT status, path, latency_ms, total_tokens, revisions
                   FROM monitor_runs WHERE updated_at >= ? ORDER BY seq DESC""", (since,)
            ).fetchall()
        runs = [dict(row) for row in run_rows]
        latencies = [float(run["latency_ms"]) for run in runs if run.get("latency_ms") is not None]
        completed, failed, deferred = counts["completed"], counts["failed"], counts["deferred"]
        with self._lock:
            samples = self._connection.execute(
                """SELECT seq, observed_at, lifecycle, queue_size, receive_process_lag,
                          pending_actions, coalesced, dropped, rejected, healthy,
                          '{}' AS payload_json
                   FROM monitor_samples WHERE observed_at >= ? ORDER BY seq ASC""", (since,)
            ).fetchall()
        sample_items = [self._sample_from_row(row).model_dump(mode="json") for row in _downsample(samples, 720)]
        alerts = [self._event_from_row(row).model_dump(mode="json") for row in alert_rows]
        paths: dict[str, int] = {}
        for run in runs:
            key = str(run.get("path") or "unknown")
            paths[key] = paths.get(key, 0) + 1
        return {
            "schema_version": MONITOR_SCHEMA_VERSION,
            "window": window,
            "since": since,
            "runtime": sanitize_payload(status.model_dump(mode="json")),
            "manifest": sanitize_payload(manifest),
            "monitor": {
                "healthy": self.healthy,
                "last_error": self.last_error or self.startup_error,
                "ingest_dropped": self.ingest_dropped,
                "retention_days": self.retention_days,
            },
            "aggregates": {
                "events": counts["events"],
                "warnings": counts["warnings"],
                "errors": counts["errors"],
                "completed": completed,
                "failed": failed,
                "deferred": deferred,
                "throughput_per_minute": round(completed * 60 / seconds, 3),
                "failure_rate": round(failed / max(1, completed + failed + deferred), 4),
                "runs": len(runs),
                "run_failures": sum(1 for run in runs if run["status"] == "failed"),
                "latency_p50_ms": _percentile(latencies, 0.5),
                "latency_p95_ms": _percentile(latencies, 0.95),
                "total_tokens": sum(int(run.get("total_tokens") or 0) for run in runs),
                "revisions": sum(int(run.get("revisions") or 0) for run in runs),
                "paths": paths,
            },
            "samples": sample_items,
            "alerts": alerts,
        }

    def diagnostic_bundle(
        self,
        correlation_id: str,
        *,
        status: RuntimeStatus,
        manifest: dict[str, Any],
    ) -> dict[str, Any]:
        events = self.list_events(correlation_id=correlation_id, limit=MAX_PAGE_SIZE)["items"]
        run = self.get_run(correlation_id)
        action_rows = [item for item in self.list_actions(limit=MAX_PAGE_SIZE) if item.get("correlation_id") == correlation_id]
        return {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "exported_at": utc_now().isoformat(),
            "correlation_id": correlation_id,
            "runtime": sanitize_payload(status.model_dump(mode="json")),
            "manifest": sanitize_payload(manifest),
            "run": run,
            "events": events,
            "journal": [item for item in events if item["topic"].startswith("journal.")],
            "actions": action_rows,
            "version_vector": sanitize_payload(status.version_vector.model_dump(mode="json")),
        }

    def cleanup(self, *, now: datetime | None = None) -> int:
        cutoff = ((now or utc_now()) - timedelta(days=self.retention_days)).isoformat()
        with self._lock:
            removed = 0
            for table, column in (
                ("monitor_events", "observed_at"),
                ("monitor_runs", "updated_at"),
                ("monitor_samples", "observed_at"),
                ("monitor_actions", "updated_at"),
                ("monitor_resources", "observed_at"),
            ):
                cursor = self._connection.execute(f"DELETE FROM {table} WHERE {column} < ?", (cutoff,))
                removed += max(0, cursor.rowcount)
            self._connection.commit()
        return removed

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> MonitorEvent:
        return MonitorEvent(
            seq=int(row["seq"]),
            observed_at=row["observed_at"],
            source=row["source"],
            topic=row["topic"],
            severity=row["severity"],
            correlation_id=row["correlation_id"],
            run_id=row["run_id"],
            event_id=row["event_id"],
            payload=json.loads(row["payload_json"]),
        )

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> MonitorRun:
        return MonitorRun(
            seq=int(row["seq"]),
            run_id=row["run_id"],
            correlation_id=row["correlation_id"],
            event_id=row["event_id"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            updated_at=row["updated_at"],
            status=row["status"],
            path=row["path"],
            latency_ms=row["latency_ms"],
            prompt_tokens=int(row["prompt_tokens"]),
            completion_tokens=int(row["completion_tokens"]),
            total_tokens=int(row["total_tokens"]),
            cache_hit_tokens=int(row["cache_hit_tokens"] or 0),
            cache_miss_tokens=int(row["cache_miss_tokens"] or 0),
            revisions=int(row["revisions"]),
            error=row["error"],
            stages=json.loads(row["stages_json"]),
        )

    @staticmethod
    def _sample_from_row(row: sqlite3.Row) -> MonitorSample:
        return MonitorSample(
            seq=int(row["seq"]),
            observed_at=row["observed_at"],
            lifecycle=row["lifecycle"],
            queue_size=int(row["queue_size"]),
            receive_process_lag=int(row["receive_process_lag"]),
            pending_actions=int(row["pending_actions"]),
            coalesced=int(row["coalesced"]),
            dropped=int(row["dropped"]),
            rejected=int(row["rejected"]),
            healthy=bool(row["healthy"]),
            payload=json.loads(row["payload_json"]),
        )


class MonitorRecorder:
    """Async, lossy-by-design bridge from live events and durable sources."""

    def __init__(
        self,
        store: MonitorStore,
        *,
        journal: RuntimeJournal,
        status_provider: Callable[[], RuntimeStatus],
        trace_dir: Path | str,
        sample_seconds: float = DEFAULT_SAMPLE_SECONDS,
        queue_capacity: int = 2048,
        resource_sampler: ResourceSampler | None = None,
    ) -> None:
        self.store = store
        self.journal = journal
        self.status_provider = status_provider
        self.trace_dir = Path(trace_dir)
        self.sample_seconds = max(1.0, float(sample_seconds))
        self.resource_sampler = resource_sampler or ResourceSampler()
        self.queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue(maxsize=queue_capacity)
        self.subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._tasks: list[asyncio.Task[None]] = []
        self._last_journal_seq = 0
        self._journal_identity = None
        self._ingest_lock = asyncio.Lock()
        self.backfill_state = "pending"

    def enqueue(self, topic: str, payload: dict[str, Any]) -> None:
        try:
            self.queue.put_nowait((topic, payload))
        except asyncio.QueueFull:
            self.store.ingest_dropped += 1

    async def start(self) -> None:
        self._tasks = [
            asyncio.create_task(self._consume(), name="monitor-event-projector"),
            asyncio.create_task(self._poll_journal(), name="monitor-journal-projector"),
            asyncio.create_task(self._sample(), name="monitor-runtime-sampler"),
            asyncio.create_task(self._cleanup(), name="monitor-retention"),
            asyncio.create_task(self._backfill_traces(), name="monitor-trace-backfill"),
        ]

    async def _backfill_traces(self):
        try:
            work = asyncio.create_task(asyncio.to_thread(self._backfill))
            try:
                await asyncio.shield(work)
            except asyncio.CancelledError:
                with suppress(Exception):
                    await work
                raise
        except Exception as exc:
            self.store.last_error = f"monitor trace backfill failed: {exc}"

    def recovery_status(self):
        return {"backfill_state": self.backfill_state, "cursor": self._last_journal_seq,
                "lag": max(0, self.journal.last_record_seq-self._last_journal_seq)}

    async def close(self) -> None:
        await self.flush()
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with suppress(asyncio.CancelledError):
                await task
        await asyncio.to_thread(self.store.close)

    async def flush(self) -> None:
        while not self.queue.empty():
            topic, payload = self.queue.get_nowait()
            event = await asyncio.to_thread(self.store.append_event, topic, payload)
            if event is not None:
                self._publish(event.model_dump(mode="json"))
        await self._ingest_journal_once()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=512)
        self.subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self.subscribers.discard(queue)

    async def _consume(self) -> None:
        while True:
            topic, payload = await self.queue.get()
            event = await asyncio.to_thread(self.store.append_event, topic, payload)
            if event is not None:
                self._publish(event.model_dump(mode="json"))

    async def _poll_journal(self) -> None:
        while True:
            try:
                await self._ingest_journal_once()
            except Exception as exc:
                self.backfill_state = "error"
                self.store.last_error = f"monitor journal backfill failed: {exc}"
            await asyncio.sleep(0.5)

    async def _ingest_journal_once(self) -> None:
        async with self._ingest_lock:
            if self._journal_identity is None:
                first = await asyncio.to_thread(self.journal.records_after, 0, limit=1)
                if not first:
                    return
                self._journal_identity = first[0].checksum
                saved = await asyncio.to_thread(self.store.journal_cursor)
                if isinstance(saved, dict) and saved.get("identity") == self._journal_identity:
                    seq = saved.get("seq", 0)
                    if isinstance(seq, int) and 0 < seq <= self.journal.last_record_seq:
                        anchor = await asyncio.to_thread(self.journal.records_after, seq-1, limit=1)
                        if anchor[0].checksum == saved.get("checksum"):
                            self._last_journal_seq = seq
            boundary = self.journal.last_record_seq
            cutoff = utc_now() - timedelta(days=self.store.retention_days)
            self.backfill_state = "running"
            while self._last_journal_seq < boundary:
                records = await asyncio.to_thread(self.journal.records_after, self._last_journal_seq,
                                                 limit=min(500, boundary-self._last_journal_seq))
                if not records:
                    break
                for record in records:
                    checkpoint = dict(identity=self._journal_identity, seq=record.record_seq, checksum=record.checksum)
                    if record.recorded_at >= cutoff:
                        event = await asyncio.to_thread(self.store.append_journal, record, journal_cursor=checkpoint)
                    else:
                        await asyncio.to_thread(self.store.advance_journal_cursor, checkpoint)
                        event = None
                    self._last_journal_seq = record.record_seq
                    if event is not None:
                        self._publish(event.model_dump(mode="json"))
                await asyncio.sleep(0)
            self.backfill_state = "complete"

    async def _sample(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.store.append_sample, self.status_provider())
                samples = await asyncio.to_thread(self.resource_sampler.sample)
                await asyncio.to_thread(self.store.append_resources, samples)
            except Exception as exc:
                self.store.last_error = str(exc)
            await asyncio.sleep(self.sample_seconds)

    async def _cleanup(self) -> None:
        while True:
            await asyncio.sleep(3600)
            await asyncio.to_thread(self.store.cleanup)

    def _publish(self, event: dict[str, Any]) -> None:
        for queue in list(self.subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(event)

    def _backfill(self) -> None:
        cutoff = utc_now() - timedelta(days=self.store.retention_days)
        calls_dir = self.trace_dir / "calls"
        if calls_dir.exists():
            for path in sorted(calls_dir.rglob("*.json")):
                try:
                    if datetime.fromtimestamp(path.stat().st_mtime, UTC) < cutoff:
                        continue
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        self.store.append_call_log(path, payload)
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        self.store.cleanup()


def _window_seconds(window: str) -> int:
    mapping = {"1h": 3600, "6h": 6 * 3600, "24h": 24 * 3600, "7d": 7 * 86400}
    if window not in mapping:
        raise ValueError("window must be one of 1h, 6h, 24h, 7d")
    return mapping[window]


def window_since(window: str) -> str:
    return (utc_now() - timedelta(seconds=_window_seconds(window))).isoformat()


def _downsample(rows: list[sqlite3.Row], maximum: int) -> list[sqlite3.Row]:
    if len(rows) <= maximum:
        return rows
    step = math.ceil(len(rows) / maximum)
    result = rows[::step]
    if result[-1]["seq"] != rows[-1]["seq"]:
        result.append(rows[-1])
    return result


async def call_maybe_async(callback: Callable[..., Any], *args: Any) -> Any:
    result = callback(*args)
    return await result if inspect.isawaitable(result) else result
