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
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StringConstraints, model_validator
from harness.runtime.ownership import require_development
from harness.qq.attention import is_group

from harness.qq.memory import (
    QQID,
    QQVerifiedProfileInput, QQVisualMemoryConflict, QQVisualMemoryStorageError,
    QQVisualMemoryStore, QQVisualObservationInput,
)

class QQSessionSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    memory_db: Path
    token_file: Path
    allowed_group_ids: list[QQID] = Field(default_factory=list, max_length=20)
    allowed_private_peer_ids: list[QQID] = Field(default_factory=list, max_length=20)
    self_member_id: QQID
    decision_enabled: bool = True
    development_mode: StrictBool = False
    model_config_path: Path | None = None
    preset_path: Path | None = None
    watcher_status_file: Path | None = None
    direct_status_file: Path | None = None

    @model_validator(mode="after")
    def conversation_scope(self):
        if not self.allowed_group_ids and not self.allowed_private_peer_ids:
            raise ValueError("qq_session_conversation_required")
        if (len(set(self.allowed_group_ids)) != len(self.allowed_group_ids)
                or len(set(self.allowed_private_peer_ids)) != len(self.allowed_private_peer_ids)
                or self.self_member_id in self.allowed_private_peer_ids):
            raise ValueError("qq_session_conversation_invalid")
        if self.self_member_id.startswith("qqbot:"):
            from harness.qq.official_identity import identity, split_identity
            app_id = self.self_member_id.split(":")[1]
            if self.self_member_id != identity(app_id, "bot", app_id):
                raise ValueError("qqbot_session_scope_invalid")
            for peer in self.allowed_private_peer_ids:
                split_identity(peer, kind="user", app_id=app_id)
            for group_id in self.allowed_group_ids:
                split_identity(group_id, kind="group", app_id=app_id)
        elif any(value.startswith("qqbot:") for value in [*self.allowed_group_ids, *self.allowed_private_peer_ids]):
            raise ValueError("qqbot_session_transport_mismatch")
        return self


def _watcher_measurements(record: dict, *, allowed: frozenset[str] | None = None) -> dict:
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

    for key in ("verified_segments_sent", "partial_deliveries"):
        if number(record.get(key), integer=True):
            result[key] = record[key]
    roster = record.get("group_roster")
    if isinstance(roster, list) and len(roster) <= 16:
        groups = []
        seen = set()
        for entry in roster:
            if not isinstance(entry, dict):
                continue
            group_id = entry.get("group_id")
            if (not isinstance(group_id, str) or not is_group(group_id)
                    or group_id in seen or allowed is not None and group_id not in allowed):
                continue
            seen.add(group_id)
            safe = {"group_id": group_id}
            label = entry.get("label")
            if isinstance(label, str) and len(label) <= 128 and not re.search(r"[\x00-\x1f\x7f]", label):
                safe["label"] = label
            for key in ("observe", "send"):
                if type(entry.get(key)) is bool:
                    safe[key] = entry[key]
            for key in ("message_count", "read_count"):
                if number(entry.get(key), integer=True):
                    safe[key] = entry[key]
            if entry.get("last_observed_at") is None or timestamp(entry.get("last_observed_at")):
                safe["last_observed_at"] = entry.get("last_observed_at")
            error = entry.get("last_error")
            if error is None or isinstance(error, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,99}", error):
                safe["last_error"] = error
            groups.append(safe)
        result["group_roster"] = groups
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
    for field in ("latency_ms", "idle_latency_ms"):
        latency = record.get(field)
        if isinstance(latency, dict):
            result[field] = {key: latency[key] for key in ("samples", "p50", "p95", "max")
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
    homeostasis = record.get("homeostasis")
    if isinstance(homeostasis, dict):
        safe = {}
        if type(homeostasis.get("enabled")) is bool:
            safe["enabled"] = homeostasis["enabled"]
        for key in ("revision", "feedback_pending", "feedback_applied", "feedback_retired"):
            if number(homeostasis.get(key), integer=True):
                safe[key] = homeostasis[key]
        if homeostasis.get("salience") in {"nominal", "noticeable", "marked", "critical"}:
            safe["salience"] = homeostasis["salience"]
        profile = homeostasis.get("profile_ref")
        if isinstance(profile, str) and re.fullmatch(r"[a-zA-Z0-9_.-]{1,80}@[0-9]{1,10}", profile):
            safe["profile_ref"] = profile
        if number(homeostasis.get("tick_seconds")):
            safe["tick_seconds"] = homeostasis["tick_seconds"]
        if timestamp(homeostasis.get("last_activity_at")):
            safe["last_activity_at"] = homeostasis["last_activity_at"]
        result["homeostasis"] = safe
    body = record.get("qq_body")
    if isinstance(body, dict) and isinstance(body.get("embodiment"), dict):
        embodiment = body["embodiment"]
        safe = {}
        if embodiment.get("state") in {"disabled", "available", "degraded", "unavailable", "terminal"}:
            safe["state"] = embodiment["state"]
        known = {"communication.qq.read.v1", "communication.qq.send.v1",
                 "communication.qq.reconcile.v1", "evidence.reference.v1"}
        capabilities = embodiment.get("effective_capabilities")
        if isinstance(capabilities, list):
            safe["effective_capabilities"] = sorted(known.intersection(item for item in capabilities if isinstance(item, str)))
        negotiated = embodiment.get("negotiated")
        if isinstance(negotiated, dict):
            # The protocol uses a property; its JSON stores the two failure sets.
            required_sets = [negotiated.get(name) for name in ("missing_required", "incompatible_required")]
            if all(isinstance(values, list) and len(values) <= 64
                   and all(isinstance(item, str) and len(item) <= 128 for item in values)
                   for values in required_sets):
                safe["required_satisfied"] = not any(required_sets)
        result["qq_body"] = safe
    if timestamp(record.get("last_idle_intent_at")):
        result["last_idle_intent_at"] = record["last_idle_intent_at"]
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
            "development_mode", "cognitive_owner",
        )} | _watcher_measurements(record, allowed=allowed)
    except (OSError, ValueError, KeyError, TypeError):
        return inactive


def read_direct_status(path: Path | None, *, allowed: frozenset[str], self_member_id: str,
                       allowed_private: frozenset[str] = frozenset()) -> dict:
    """A bounded protocol heartbeat; never publish connection credentials or messages."""
    inactive = {"running": False, "connected": False, "automatic_send": False}
    if path is None:
        return inactive
    try:
        if not path.is_absolute() or path.is_symlink() or path.stat().st_size > 65536:
            return inactive
        record = json.loads(path.read_text(encoding="utf-8"))
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(record["heartbeat_at"])).total_seconds()
        expected_service = "qq-direct-official-v1" if self_member_id.startswith("qqbot:") else "qq-direct-onebot-v1"
        if (record.get("service") != expected_service or record.get("self_member_id") != self_member_id
                or record.get("running") is not True or not 0 <= age <= 30):
            return inactive
        result = {"running": True, "transport": "qqbot" if self_member_id.startswith("qqbot:") else "onebot"}
        for name in ("connected", "automatic_send", "images_enabled", "paused", "desktop_owner_active"):
            result[name] = record.get(name) is True
        if type(record.get("pid")) is int and record["pid"] > 0:
            result["pid"] = record["pid"]
        for name in ("state", "last_error_type", "last_decision_action"):
            value = record.get(name)
            if value is None or isinstance(value, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", value):
                result[name] = value
        result.update(_watcher_measurements(record, allowed=allowed))
        contacts = record.get("private_contacts", [])
        if not isinstance(contacts, list) or len(contacts) > 20:
            return inactive
        from harness.qq.direct_config import QQPrivateContact
        if contacts:
            result["private_contacts"] = []
        for item in contacts:
            contact = QQPrivateContact.model_validate(item)
            if contact.peer_id not in allowed_private or contact.peer_id == self_member_id:
                return inactive
            result["private_contacts"].append(contact.model_dump())
        result["counts"] = {key: {name: value for name, value in values.items()
            if isinstance(name, str) and re.fullmatch(r"[a-z_]{1,32}", name)
            and type(value) is int and 0 <= value <= 1e15}
            for key, values in record.get("counts", {}).items()
            if key in {"events", "sends"} and isinstance(values, dict)}
        for name in ("started_at", "heartbeat_at", "last_received_at", "last_delivery_at"):
            value = record.get(name)
            if value is None:
                result[name] = None
            elif isinstance(value, str) and len(value) <= 64 and datetime.fromisoformat(value).utcoffset() is not None:
                result[name] = value
        return result
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
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
    if settings.development_mode:
        require_development("qq.session_local_decision")
        if decide is None:
            from harness.qq.decision import decide_qq_reply
            decide = decide_qq_reply
    elif decide is not None:
        raise ValueError("shared_cognitive_callback_requires_development")
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
        if not settings.development_mode:
            raise HTTPException(status_code=409, detail="QQ decisions belong to the shared Runtime")
        require_development("qq.session_local_decision")
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
        direct = read_direct_status(settings.direct_status_file, allowed=allowed, self_member_id=settings.self_member_id,
            allowed_private=frozenset(settings.allowed_private_peer_ids))
        protocol_active = direct.get("running") and direct.get("connected") and not direct.get("paused") and not direct.get("desktop_owner_active")
        return {
            "service": "qq-memory-session-v1", "status": "healthy", "started_at": started_at,
            "persistent_memory": True, "group_member_counts": counts,
            # Keep the configured absolute spelling. Packaged Windows apps can
            # resolve this same file into a redirected LocalCache alias; that
            # must not make the launcher mistake it for another database.
            "memory_db": str(settings.memory_db.absolute()),
            "allowed_group_ids": sorted(allowed), "self_member_id": settings.self_member_id,
            "allowed_private_peer_ids": sorted(settings.allowed_private_peer_ids),
            "configured_decision_enabled": settings.decision_enabled,
            "autonomous_decision_enabled": settings.decision_enabled and (
                settings.development_mode or watcher.get("cognitive_owner") == "shared_runtime" or protocol_active),
            "new_verified_observations_trigger_decision": settings.decision_enabled and settings.development_mode,
            "development_mode": settings.development_mode,
            "cognitive_owner": "isolated_development" if settings.development_mode else "shared_runtime",
            "automatic_screen_capture": watcher.get("automatic_screen_capture") is True,
            "automatic_send": watcher.get("automatic_send") is True or direct.get("automatic_send") is True,
            "automatic_protocol_read": bool(protocol_active), "transport": direct.get("transport", "onebot") if protocol_active else "desktop",
            "watcher": watcher, "direct": direct,
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
        if incoming.verification != "qq_ui_verified":
            raise HTTPException(status_code=422, detail="This collection route requires QQ UI evidence")
        try:
            return store.record_profile(incoming).model_dump(mode="json")
        except QQVisualMemoryConflict as exc:
            raise HTTPException(status_code=409, detail="QQ profile evidence conflicts") from exc
        except (sqlite3.Error, OSError, QQVisualMemoryStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ memory is unavailable") from exc

    @app.post("/v1/observations")
    def observe(incoming: QQVisualObservationInput):
        check_group(incoming.group_id)
        if incoming.author_attribution != "qq_ui_verified":
            raise HTTPException(status_code=422, detail="This collection route requires QQ UI evidence")
        try:
            result = store.record_observation(incoming)
        except QQVisualMemoryConflict as exc:
            raise HTTPException(status_code=409, detail="QQ observation evidence conflicts") from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="QQ observation needs matching verified identity evidence") from exc
        except (sqlite3.Error, OSError, QQVisualMemoryStorageError) as exc:
            raise HTTPException(status_code=503, detail="QQ memory is unavailable") from exc
        response = {"memory": result.model_dump(mode="json"), "decision": None,
                    "decision_status": "duplicate" if not result.created else "disabled" if settings.development_mode else "shared_runtime_owned",
                    "automatic_send": False}
        if result.created and settings.decision_enabled and settings.development_mode:
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
