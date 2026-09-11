"""Authenticated, persistent QQ memory and speech decisions, without GUI delivery.

An authorized collector submits independently attributed observations. Each new
observation invokes the enabled speak/wait gate. QQ text is never a control API.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime, timezone
import hmac
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from harness.qq.memory import (
    QQVerifiedProfileInput, QQVisualMemoryConflict, QQVisualMemoryStorageError,
    QQVisualMemoryStore, QQVisualObservationInput,
)

QQID = Annotated[str, StringConstraints(strict=True, pattern=r"^[1-9][0-9]{0,19}$")]


class QQSessionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_db: Path
    token_file: Path
    allowed_group_ids: list[QQID] = Field(min_length=1, max_length=20)
    self_member_id: QQID
    decision_enabled: bool = True
    model_config_path: Path | None = None
    preset_path: Path | None = None
    watcher_status_file: Path | None = None


def _watcher_measurements(record: dict) -> dict:
    """Whitelist measurements; never expose a nested tree, prompt or exception."""
    result = {}

    def number(value, *, integer=False):
        return ((type(value) is int if integer else type(value) in (int, float))
                and 0 <= value <= 1e15 and math.isfinite(value))

    def timestamp(value):
        if not isinstance(value, str) or len(value) > 64:
            return False
        try:
            moment = datetime.fromisoformat(value)
            return moment.tzinfo is not None and moment.utcoffset() is not None
        except ValueError:
            return False

    observer = record.get("observer")
    if isinstance(observer, dict):
        safe = {}
        if type(observer.get("running")) is bool:
            safe["running"] = observer["running"]
        for key in ("polls", "changes", "overflows"):
            if number(observer.get(key), integer=True):
                safe[key] = observer[key]
        for key in ("last_polled_at", "last_change_at", "gap_started_at"):
            if observer.get(key) is None or timestamp(observer.get(key)):
                safe[key] = observer.get(key)
        if number(observer.get("gap_seconds")):
            safe["gap_seconds"] = observer["gap_seconds"]
        error = observer.get("last_error_type")
        if error is None or isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", error):
            safe["last_error_type"] = error
        result["observer"] = safe
    latency = record.get("latency_ms")
    if isinstance(latency, dict):
        result["latency_ms"] = {key: latency[key] for key in ("samples", "p50", "p95", "max")
                                if number(latency.get(key), integer=key == "samples")}
    if number(record.get("backlog"), integer=True):
        result["backlog"] = record["backlog"]
    for key in ("current_stage", "last_turn_outcome"):
        value = record.get(key)
        if value is None or isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_.-]{0,63}", value):
            result[key] = value
    trace = record.get("last_trace_id")
    if trace is None or isinstance(trace, str) and re.fullmatch(r"qq-trace:[a-f0-9]{32}", trace):
        result["last_trace_id"] = trace
    return result


def read_watcher_status(path: Path | None, *, allowed: frozenset[str], self_member_id: str) -> dict:
    """Report a fresh, scoped worker heartbeat; never treat configuration as activity."""
    inactive = {"running": False, "automatic_screen_capture": False, "automatic_send": False}
    if path is None:
        return inactive
    try:
        if not path.is_absolute() or path.stat().st_size > 65536:
            return inactive
        record = json.loads(path.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["heartbeat_at"])).total_seconds()
        if (not 0 <= age <= 30 or record.get("group_id") not in allowed
                or record.get("self_member_id") != self_member_id or record.get("running") is not True):
            return inactive
        # This file lives beside the private configuration, not in QQ content.
        return {key: record.get(key) for key in (
            "running", "pid", "started_at", "heartbeat_at", "state", "automatic_screen_capture", "automatic_send",
            "last_capture_at", "last_decision_action", "last_delivery_at", "last_error_type", "captures",
            "decisions", "deliveries", "group_id", "self_member_id",
        )} | _watcher_measurements(record)
    except (OSError, ValueError, KeyError, TypeError):
        return inactive


class DecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    trigger_member_id: QQID | None = None
    query: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=512)] | None = None
    request: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=4096)] | None = None


def create_qq_session_app(settings: QQSessionSettings, *, decide=None) -> FastAPI:
    """Create only the QQ-scoped service; no standard Runtime is started."""
    if not settings.memory_db.is_absolute() or not settings.token_file.is_absolute():
        raise ValueError("QQ session state paths must be absolute")
    if not settings.memory_db.is_file():
        raise ValueError("Prepare the persistent QQ memory database before starting")
    token = settings.token_file.read_text(encoding="utf-8").strip()
    if len(token) < 32 or len(token) > 4096 or any(char.isspace() for char in token):
        raise ValueError("QQ session token file is invalid")
    if decide is None:
        from harness.qq.decision import decide_qq_reply
        decide = decide_qq_reply
    store = QQVisualMemoryStore(settings.memory_db)
    started_at = datetime.now(timezone.utc).isoformat()
    allowed = frozenset(settings.allowed_group_ids)

    def authenticate(authorization: str | None = Header(default=None)) -> None:
        if not authorization or not hmac.compare_digest(authorization.encode("utf-8"), ("Bearer " + token).encode("utf-8")):
            raise HTTPException(status_code=401, detail="QQ session authentication required")

    def check_group(group_id: str) -> None:
        if group_id not in allowed:
            raise HTTPException(status_code=403, detail="Group is outside this QQ session")

    def decision(group_id: str, request: DecisionRequest):
        check_group(group_id)
        if not settings.decision_enabled:
            raise HTTPException(status_code=409, detail="QQ speech decisions are disabled")
        result = decide(
            memory_db=settings.memory_db, group_id=group_id,
            trigger_member_id=request.trigger_member_id,
            self_member_id=settings.self_member_id,
            query=request.query, request=request.request,
            config_path=settings.model_config_path, preset_path=settings.preset_path,
        )
        return result.model_dump(mode="json")

    app = FastAPI(title="QQ memory and speech decisions", docs_url=None, redoc_url=None, openapi_url=None,
                  dependencies=[Depends(authenticate)])

    @app.get("/v1/status")
    def status():
        # Health must not scan everyone's entire lifetime history on every
        # probe. Check the existing schema and count stable person identifiers.
        try:
            if not settings.memory_db.is_file():
                raise OSError("memory database is missing")
            with closing(sqlite3.connect(settings.memory_db, timeout=2)) as connection:
                counts = {group_id: connection.execute(
                    "SELECT COUNT(DISTINCT member_id) FROM qq_visual_profiles WHERE group_id=?", (group_id,)
                ).fetchone()[0] for group_id in allowed}
        except Exception as exc:
            raise HTTPException(status_code=503, detail="QQ memory is unavailable") from exc
        watcher = read_watcher_status(settings.watcher_status_file, allowed=allowed, self_member_id=settings.self_member_id)
        return {
            "service": "qq-memory-session-v1", "status": "healthy", "started_at": started_at,
            "persistent_memory": True, "group_member_counts": counts,
            # Keep the configured absolute spelling. Packaged Windows apps can
            # resolve this same file into a redirected LocalCache alias; that
            # must not make the launcher mistake it for another database.
            "memory_db": str(settings.memory_db.absolute()),
            "allowed_group_ids": sorted(allowed), "self_member_id": settings.self_member_id,
            "autonomous_decision_enabled": settings.decision_enabled,
            "new_verified_observations_trigger_decision": settings.decision_enabled,
            "automatic_screen_capture": watcher.get("automatic_screen_capture") is True,
            "automatic_send": watcher.get("automatic_send") is True, "watcher": watcher,
            "standard_runtime_started_by_this_service": False,
        }

    @app.get("/v1/groups/{group_id}/memory")
    def memory(group_id: str, query: str | None = Query(default=None, min_length=1, max_length=512)):
        check_group(group_id)
        try:
            return store.group_context(group_id=group_id, query=query).model_dump(mode="json")
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="Invalid QQ memory query") from exc
        except (sqlite3.Error, OSError, QQVisualMemoryStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ memory is unavailable") from exc

    @app.post("/v1/profiles")
    def profile(incoming: QQVerifiedProfileInput):
        check_group(incoming.group_id)
        try:
            return store.record_profile(incoming).model_dump(mode="json")
        except QQVisualMemoryConflict as exc:
            raise HTTPException(status_code=409, detail="QQ profile evidence conflicts") from exc
        except (sqlite3.Error, OSError, QQVisualMemoryStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ memory is unavailable") from exc

    @app.post("/v1/observations")
    def observe(incoming: QQVisualObservationInput):
        check_group(incoming.group_id)
        try:
            result = store.record_observation(incoming)
        except QQVisualMemoryConflict as exc:
            raise HTTPException(status_code=409, detail="QQ observation evidence conflicts") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="QQ observation needs matching verified identity evidence") from exc
        except (sqlite3.Error, OSError, QQVisualMemoryStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ memory is unavailable") from exc
        response = {"memory": result.model_dump(mode="json"), "decision": None,
                    "decision_status": "duplicate" if not result.created else "disabled", "automatic_send": False}
        if result.created and settings.decision_enabled:
            try:
                response["decision"] = decision(incoming.group_id, DecisionRequest(trigger_member_id=incoming.member_id))
                response["decision_status"] = "completed"
            except Exception as exc:
                # The observation is already durable. Never pretend its write
                # failed and never retry a model/delivery operation implicitly.
                response["decision_status"] = "failed"
                response["decision_error_type"] = type(exc).__name__
        return response

    @app.post("/v1/groups/{group_id}/decisions")
    def prepare(group_id: str, incoming: DecisionRequest):
        try:
            return decision(group_id, incoming)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(status_code=503, detail="QQ decision unavailable; no message was sent") from exc

    return app


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path, help="Private session configuration; token is a file reference")
    parser.add_argument("--port", type=int, default=8789)
    args = parser.parse_args(argv)
    if not 1024 <= args.port <= 65535:
        parser.error("port must be between 1024 and 65535")
    try:
        settings = QQSessionSettings.model_validate_json(args.config.read_text(encoding="utf-8"))
        app = create_qq_session_app(settings)
    except Exception as exc:
        print(f"QQ session could not start ({type(exc).__name__}); inspect private configuration paths.")
        return 2
    import uvicorn
    uvicorn.run(app, host="127.0.0.1", port=args.port, access_log=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
