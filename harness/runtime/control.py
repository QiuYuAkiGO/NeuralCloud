"""Authenticated local Runtime control plane and read-only event stream."""

from __future__ import annotations

import asyncio
import inspect
import json
import secrets
from enum import StrEnum
from typing import Any

from harness.homeostasis.models import HomeostaticProfile
from harness.homeostasis.profiles import ProfileRevisionStore
from harness.memory.qq_observations import QQObservationStore

from harness.runtime.monitor import MonitorRecorder, window_since
from harness.runtime.models import ActionBackendResult, DeliveryResult, RuntimeEventPriority, RuntimeEventType
from harness.runtime.supervisor import RuntimeSupervisor


class ControlPermission(StrEnum):
    DIAGNOSTIC = "diagnostic"
    DEBUG_MUTATION = "debug_mutation"
    MANIFEST_CHANGE = "manifest_change"
    MEMORY_MAINTENANCE = "memory_maintenance"


class ControlAuthorizationError(PermissionError):
    pass


class RuntimeEventStream:
    def __init__(self, *, subscriber_capacity: int = 256) -> None:
        self.subscriber_capacity = subscriber_capacity
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._listeners: list[Any] = []

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        item = {"topic": topic, "payload": payload}
        for listener in list(self._listeners):
            try:
                result = listener(topic, payload)
                if inspect.isawaitable(result):
                    await result
            except Exception:
                continue
        for queue in list(self._subscribers):
            if queue.full():
                try:
                    queue.get_nowait()
                except asyncio.QueueEmpty:
                    pass
            queue.put_nowait(item)

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self.subscriber_capacity)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def add_listener(self, listener: Any) -> None:
        self._listeners.append(listener)


class ControlPlane:
    def __init__(
        self,
        supervisor: RuntimeSupervisor,
        *,
        token: str,
        event_stream: RuntimeEventStream | None = None,
    ) -> None:
        if not token:
            raise ValueError("control token cannot be blank")
        self.supervisor = supervisor
        self._token = token
        self.event_stream = event_stream or RuntimeEventStream()
        supervisor.add_listener(self.event_stream.publish)

    def authenticate(self, token: str) -> None:
        if not secrets.compare_digest(token, self._token):
            raise ControlAuthorizationError("invalid control token")

    def authorize(self, permission: ControlPermission, *, operation: str) -> None:
        mode = self.supervisor.manifest.control_mode
        if permission == ControlPermission.DIAGNOSTIC:
            return
        if permission == ControlPermission.MEMORY_MAINTENANCE and operation in {
            "daily_revision.configure", "daily_revision.run", "daily_revision.dry_run",
            "daily_revision.retry", "daily_revision.rollback",
        }:
            return
        allowed = mode == "development_debug" and permission in {
            ControlPermission.DEBUG_MUTATION,
            ControlPermission.MANIFEST_CHANGE,
        }
        if not allowed:
            self.supervisor._append_journal(
                "control_command_rejected",
                data={"permission": permission.value, "operation": operation, "mode": mode},
            )
            raise ControlAuthorizationError(
                f"{permission.value} is not allowed while control_mode={mode}"
            )

    async def submit_control(self, event_type: RuntimeEventType, *, reason: str) -> dict[str, Any]:
        event = await self.supervisor.submit_event(
            event_type=event_type,
            priority=RuntimeEventPriority.CONTROL,
            source="control_plane",
            payload={"reason": reason},
        )
        return event.model_dump(mode="json") if event else {"accepted": False}


def create_control_app(
    control: ControlPlane,
    *,
    profile_store: ProfileRevisionStore | None = None,
    monitor: MonitorRecorder | None = None,
    qq_observation_store: QQObservationStore | None = None,
    qq_session_config_path=None,
):
    """Create a localhost-oriented FastAPI app.

    Binding to loopback is a deployment responsibility; the app always requires
    a separate local bearer token.
    """

    from fastapi import Depends, FastAPI, Header, HTTPException
    from fastapi.responses import StreamingResponse

    app = FastAPI(title="NeuralCloud Runtime Control Plane")

    async def authenticate(authorization: str | None = Header(default=None)) -> None:
        prefix = "Bearer "
        if not authorization or not authorization.startswith(prefix):
            raise HTTPException(status_code=401, detail="missing bearer token")
        try:
            control.authenticate(authorization[len(prefix) :])
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    if qq_observation_store is not None:
        from harness.runtime.qq_observations import create_qq_observation_router

        app.include_router(create_qq_observation_router(qq_observation_store, authenticate=authenticate))

    from harness.runtime.qq_homeostasis import create_qq_homeostasis_router
    app.include_router(create_qq_homeostasis_router(control, authenticate=authenticate, config_path=qq_session_config_path))
    from harness.runtime.qq_capabilities import create_qq_capability_router
    app.include_router(create_qq_capability_router(control, authenticate=authenticate, config_path=qq_session_config_path))
    from fastapi import APIRouter
    from harness.runtime.board import add_board_routes
    board_router = APIRouter(dependencies=[Depends(authenticate)])
    add_board_routes(board_router, control.supervisor)
    app.include_router(board_router)
    from harness.runtime.diary import add_diary_routes
    from harness.runtime.daily_report import add_report_routes
    from harness.runtime.self_check import add_self_check_routes
    diary_router = APIRouter(dependencies=[Depends(authenticate)])
    add_diary_routes(diary_router, control.supervisor, authorize=control.authorize)
    add_report_routes(diary_router, control.supervisor, authorize=control.authorize)
    add_self_check_routes(diary_router, control.supervisor, authorize=control.authorize)
    app.include_router(diary_router)
    from harness.runtime.voice import add_voice_routes
    voice_router = APIRouter(dependencies=[Depends(authenticate)])
    add_voice_routes(voice_router, control.supervisor)
    app.include_router(voice_router)
    from harness.runtime.web_routes import add_web_routes
    web_router = APIRouter(dependencies=[Depends(authenticate)])
    add_web_routes(web_router, control.supervisor)
    app.include_router(web_router)
    from harness.runtime.server_routes import add_server_routes
    server_router = APIRouter(dependencies=[Depends(authenticate)])
    add_server_routes(server_router, control.supervisor)
    app.include_router(server_router)

    @app.get("/v1/capabilities", dependencies=[Depends(authenticate)])
    async def capabilities() -> dict[str, Any]:
        return await control.supervisor.capabilities.snapshot()

    from harness.runtime.goals import create_goal_router
    app.include_router(create_goal_router(control, authenticate=authenticate))

    @app.get("/v1/status", dependencies=[Depends(authenticate)])
    async def status() -> dict[str, Any]:
        return control.supervisor.status().model_dump(mode="json")

    @app.get("/v1/snapshot", dependencies=[Depends(authenticate)])
    async def composite_snapshot() -> dict[str, Any]:
        cognitive, homeostasis = await asyncio.gather(
            control.supervisor.cognitive.get_snapshot(),
            control.supervisor.homeostasis.get_snapshot(),
        )
        ui_snapshot: dict[str, Any] | None = None
        ui_method = getattr(control.supervisor.cognitive, "ui_snapshot", None)
        if callable(ui_method):
            ui_snapshot = await ui_method()
        return {
            "runtime": control.supervisor.status().model_dump(mode="json"),
            "cognitive": cognitive.model_dump(mode="json"),
            "cognitive_ui": ui_snapshot,
            "homeostasis": homeostasis.model_dump(mode="json"),
            "manifest": control.supervisor.manifest.model_dump(mode="json"),
            "pending_start": profile_store.pending() if profile_store is not None else None,
        }

    @app.get("/v1/health", dependencies=[Depends(authenticate)])
    async def health() -> dict[str, Any]:
        return control.supervisor.status().health.model_dump(mode="json")

    def require_monitor() -> MonitorRecorder:
        if monitor is None:
            raise HTTPException(status_code=501, detail="monitoring projection is unavailable")
        return monitor

    @app.get("/v1/monitor/overview", dependencies=[Depends(authenticate)])
    async def monitor_overview(window: str = "24h") -> dict[str, Any]:
        recorder = require_monitor()
        try:
            return await asyncio.to_thread(
                recorder.store.overview,
                window=window,
                status=control.supervisor.status(),
                manifest=control.supervisor.manifest.model_dump(mode="json"),
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/monitor/events", dependencies=[Depends(authenticate)])
    async def monitor_events(
        window: str = "24h",
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
        recorder = require_monitor()
        try:
            return await asyncio.to_thread(
                recorder.store.list_events,
                since=since or window_since(window),
                until=until,
                topic=topic,
                severity=severity,
                status=status,
                correlation_id=correlation_id,
                cursor=cursor,
                after_seq=after_seq,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/monitor/runs", dependencies=[Depends(authenticate)])
    async def monitor_runs(
        window: str = "24h",
        since: str | None = None,
        status: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        recorder = require_monitor()
        try:
            return await asyncio.to_thread(
                recorder.store.list_runs,
                since=since or window_since(window),
                status=status,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/monitor/runs/{run_id}", dependencies=[Depends(authenticate)])
    async def monitor_run(run_id: str) -> dict[str, Any]:
        recorder = require_monitor()
        value = await asyncio.to_thread(recorder.store.get_run, run_id)
        if value is None:
            raise HTTPException(status_code=404, detail="monitor run not found")
        return value

    @app.get("/v1/monitor/journal", dependencies=[Depends(authenticate)])
    async def monitor_journal(
        window: str = "24h",
        since: str | None = None,
        record_type: str | None = None,
        correlation_id: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> dict[str, Any]:
        recorder = require_monitor()
        try:
            topic = f"journal.{record_type}" if record_type else "journal."
            return await asyncio.to_thread(
                recorder.store.list_events,
                since=since or window_since(window),
                topic=topic,
                correlation_id=correlation_id,
                cursor=cursor,
                limit=limit,
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.get("/v1/monitor/actions", dependencies=[Depends(authenticate)])
    async def monitor_actions(limit: int = 100) -> dict[str, Any]:
        recorder = require_monitor()
        return {"items": await asyncio.to_thread(recorder.store.list_actions, limit=limit)}

    @app.get("/v1/monitor/export/{correlation_id}", dependencies=[Depends(authenticate)])
    async def monitor_export(correlation_id: str) -> dict[str, Any]:
        recorder = require_monitor()
        return await asyncio.to_thread(
            recorder.store.diagnostic_bundle,
            correlation_id,
            status=control.supervisor.status(),
            manifest=control.supervisor.manifest.model_dump(mode="json"),
        )

    @app.get("/v1/monitor/stream", dependencies=[Depends(authenticate)])
    async def monitor_stream(after_seq: int = 0) -> StreamingResponse:
        recorder = require_monitor()
        queue = recorder.subscribe()

        async def generate_monitor():
            last_sent = max(0, int(after_seq))
            try:
                while True:
                    page = await asyncio.to_thread(
                        recorder.store.list_events,
                        after_seq=last_sent,
                        limit=500,
                    )
                    items = page["items"]
                    for item in items:
                        seq = int(item["seq"])
                        if seq <= last_sent:
                            continue
                        last_sent = seq
                        yield f"id: {seq}\nevent: monitor\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    if not page["has_more"]:
                        break
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                        seq = int(item["seq"])
                        if seq <= last_sent:
                            continue
                        last_sent = seq
                        yield f"id: {seq}\nevent: monitor\ndata: {json.dumps(item, ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                recorder.unsubscribe(queue)

        return StreamingResponse(generate_monitor(), media_type="text/event-stream")

    @app.post("/v1/messages", dependencies=[Depends(authenticate)])
    async def message(payload: dict[str, Any]) -> dict[str, Any]:
        event = await control.supervisor.submit_user_message(
            str(payload.get("text") or ""),
            session_id=str(payload.get("session_id") or "sess_default"),
            speaker=str(payload.get("speaker") or "user"),
            channel=str(payload.get("channel") or "chat"),
            correlation_id=payload.get("correlation_id"),
        )
        return event.model_dump(mode="json") if event else {"accepted": False}

    @app.post("/v1/cognitive/control", dependencies=[Depends(authenticate)])
    async def cognitive_control(payload: dict[str, Any]) -> dict[str, Any]:
        operation = str(payload.get("operation") or "")
        diagnostic = operation in {"presets.list", "debug.state", "models", "snapshot",
            "daily_revision.status", "daily_revision.history", "daily_revision.diff"}
        maintenance = operation in {"daily_revision.configure", "daily_revision.run", "daily_revision.dry_run",
            "daily_revision.retry", "daily_revision.rollback"}
        try:
            control.authorize(
                ControlPermission.DIAGNOSTIC if diagnostic else
                ControlPermission.MEMORY_MAINTENANCE if maintenance else ControlPermission.DEBUG_MUTATION,
                operation=operation,
            )
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        method = getattr(control.supervisor.cognitive, "control", None)
        if not callable(method):
            raise HTTPException(status_code=501, detail="cognitive control surface is unavailable")
        try:
            return await method(operation, dict(payload.get("payload") or {}))
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.get("/v1/homeostasis/profile", dependencies=[Depends(authenticate)])
    async def homeostasis_profile() -> dict[str, Any]:
        snapshot = await control.supervisor.homeostasis.get_snapshot()
        profile = snapshot.payload.get("profile") or {}
        return {
            "profile": profile,
            "profile_ref": (
                f"{profile.get('profile_id')}@{profile.get('revision')}"
                if profile.get("profile_id")
                else None
            ),
            "pending_start": profile_store.pending() if profile_store is not None else None,
        }

    @app.post("/v1/homeostasis/profile/validate", dependencies=[Depends(authenticate)])
    async def validate_homeostasis_profile(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            profile = HomeostaticProfile.model_validate(payload.get("profile") or payload)
        except Exception as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        return {"valid": True, "profile": profile.model_dump(mode="json")}

    @app.post("/v1/homeostasis/profile/revisions", dependencies=[Depends(authenticate)])
    async def create_homeostasis_profile_revision(payload: dict[str, Any]) -> dict[str, Any]:
        if profile_store is None:
            raise HTTPException(status_code=501, detail="profile revision store is unavailable")
        try:
            control.authorize(ControlPermission.MANIFEST_CHANGE, operation="homeostasis.profile.revision")
            snapshot = await control.supervisor.homeostasis.get_snapshot()
            current = HomeostaticProfile.model_validate(snapshot.payload.get("profile"))
            return profile_store.stage(
                current_profile=current,
                current_manifest=control.supervisor.manifest,
                candidate=dict(payload.get("profile") or {}),
                base_revision=int(payload.get("base_revision", -1)),
            )
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (ValueError, FileExistsError, PermissionError) as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc

    @app.post("/v1/events", dependencies=[Depends(authenticate)])
    async def event(payload: dict[str, Any]) -> dict[str, Any]:
        if payload.get("event_type") in {RuntimeEventType.QQ_FACT, RuntimeEventType.QQ_TURN}:
            raise HTTPException(status_code=403, detail="dedicated QQ ingress required")
        if payload.get("event_type") == RuntimeEventType.BOARD_REFLECTION:
            raise HTTPException(status_code=403, detail="dedicated board ingress required")
        if payload.get("event_type") == RuntimeEventType.DIARY_REFLECTION:
            raise HTTPException(status_code=403, detail="dedicated diary ingress required")
        if payload.get("event_type") == RuntimeEventType.CAPABILITY_OPPORTUNITY:
            raise HTTPException(status_code=403, detail="dedicated internal ingress required")
        accepted = await control.supervisor.submit_event(
            event_type=payload["event_type"],
            priority=payload.get("priority", RuntimeEventPriority.PERCEPTION),
            source=str(payload.get("source") or "control_plane"),
            source_time=payload.get("source_time"),
            event_id=payload.get("event_id"),
            correlation_id=payload.get("correlation_id"),
            idempotency_key=payload.get("idempotency_key"),
            payload=dict(payload.get("payload") or {}),
        )
        return accepted.model_dump(mode="json") if accepted else {"accepted": False}

    @app.post("/v1/debug/events", dependencies=[Depends(authenticate)])
    async def debug_event(payload: dict[str, Any]) -> dict[str, Any]:
        try:
            control.authorize(ControlPermission.DEBUG_MUTATION, operation="runtime.event.inject")
            accepted = await control.supervisor.submit_event(
                event_type=payload["event_type"],
                priority=payload.get("priority", RuntimeEventPriority.TELEMETRY),
                source=str(payload.get("source") or "ops_debug"),
                source_time=payload.get("source_time"),
                event_id=payload.get("event_id"),
                correlation_id=payload.get("correlation_id"),
                idempotency_key=payload.get("idempotency_key"),
                payload=dict(payload.get("payload") or {}),
            )
            return accepted.model_dump(mode="json") if accepted else {"accepted": False}
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc
        except (KeyError, ValueError) as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/v1/pause", dependencies=[Depends(authenticate)])
    async def pause(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.submit_control(
            RuntimeEventType.PAUSE,
            reason=str((payload or {}).get("reason") or "control plane pause"),
        )

    @app.post("/v1/resume", dependencies=[Depends(authenticate)])
    async def resume(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.submit_control(
            RuntimeEventType.RESUME,
            reason=str((payload or {}).get("reason") or "control plane resume"),
        )

    @app.post("/v1/recover", dependencies=[Depends(authenticate)])
    async def recover(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.submit_control(
            RuntimeEventType.RECOVER,
            reason=str((payload or {}).get("reason") or "control plane recover"),
        )

    @app.post("/v1/emergency-stop", dependencies=[Depends(authenticate)])
    async def emergency_stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.submit_control(
            RuntimeEventType.EMERGENCY_STOP,
            reason=str((payload or {}).get("reason") or "control plane emergency stop"),
        )

    @app.post("/v1/stop", dependencies=[Depends(authenticate)])
    async def stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.submit_control(
            RuntimeEventType.STOP,
            reason=str((payload or {}).get("reason") or "control plane stop"),
        )

    @app.post("/v1/checkpoint", dependencies=[Depends(authenticate)])
    async def checkpoint() -> dict[str, Any]:
        return control.supervisor.write_checkpoint().model_dump(mode="json")

    @app.post("/v1/agency:reset", dependencies=[Depends(authenticate)])
    async def reset_agency() -> dict[str, Any]:
        try:
            snapshot = await control.supervisor.reset_agency()
        except Exception as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        return {
            "agency_state": control.supervisor.status().agency_state.value,
            "body_state": snapshot.model_dump(mode="json"),
        }

    @app.post("/v1/actions/{action_id}/approve", dependencies=[Depends(authenticate)])
    async def approve(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = await control.supervisor.approve_action(
            action_id, approval=str(payload.get("approval") or "researcher")
        )
        result = action.model_dump(mode="json")
        await control.event_stream.publish("action.updated", {"action": result})
        return result

    @app.post("/v1/actions/{action_id}/reject", dependencies=[Depends(authenticate)])
    async def reject(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = await control.supervisor.reject_action(
            action_id, reason=str(payload.get("reason") or "rejected")
        )
        result = action.model_dump(mode="json")
        await control.event_stream.publish("action.updated", {"action": result})
        return result

    @app.post("/v1/deliveries/{prepare_id}/resolve", dependencies=[Depends(authenticate)])
    async def resolve_delivery(prepare_id: str, payload: dict[str, Any]) -> dict[str, bool]:
        try:
            control.authorize(ControlPermission.DEBUG_MUTATION, operation="delivery.resolve")
            control.supervisor.resolve_delivery(prepare_id, DeliveryResult.model_validate(payload))
            await control.event_stream.publish(
                "delivery.resolved", {"prepare_id": prepare_id, "result": payload}
            )
            return {"resolved": True}
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.post("/v1/actions/{action_id}/resolve", dependencies=[Depends(authenticate)])
    async def resolve_action(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        try:
            control.authorize(ControlPermission.DEBUG_MUTATION, operation="action.resolve")
            action = await control.supervisor.resolve_action(
                action_id,
                result=ActionBackendResult.model_validate(payload),
            )
            result = action.model_dump(mode="json")
            await control.event_stream.publish("action.updated", {"action": result})
            return result
        except ControlAuthorizationError as exc:
            raise HTTPException(status_code=403, detail=str(exc)) from exc

    @app.get("/v1/events/stream", dependencies=[Depends(authenticate)])
    async def events() -> StreamingResponse:
        queue = control.event_stream.subscribe()

        async def generate():
            try:
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), timeout=15.0)
                        yield f"event: {item['topic']}\ndata: {json.dumps(item['payload'], ensure_ascii=False)}\n\n"
                    except TimeoutError:
                        yield ": keepalive\n\n"
            finally:
                control.event_stream.unsubscribe(queue)

        return StreamingResponse(generate(), media_type="text/event-stream")

    return app
