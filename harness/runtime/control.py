"""Authenticated local Runtime control plane and read-only event stream."""

from __future__ import annotations

import asyncio
import json
import secrets
from enum import StrEnum
from typing import Any

from harness.homeostasis.models import HomeostaticProfile
from harness.homeostasis.profiles import ProfileRevisionStore

from harness.runtime.models import ActionBackendResult, DeliveryResult, RuntimeEventPriority, RuntimeEventType
from harness.runtime.supervisor import RuntimeSupervisor


class ControlPermission(StrEnum):
    DIAGNOSTIC = "diagnostic"
    DEBUG_MUTATION = "debug_mutation"
    MANIFEST_CHANGE = "manifest_change"


class ControlAuthorizationError(PermissionError):
    pass


class RuntimeEventStream:
    def __init__(self, *, subscriber_capacity: int = 256) -> None:
        self.subscriber_capacity = subscriber_capacity
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    async def publish(self, topic: str, payload: dict[str, Any]) -> None:
        item = {"topic": topic, "payload": payload}
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
        diagnostic = operation in {"presets.list", "debug.state", "models", "snapshot"}
        try:
            control.authorize(
                ControlPermission.DIAGNOSTIC if diagnostic else ControlPermission.DEBUG_MUTATION,
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

    @app.post("/v1/stop", dependencies=[Depends(authenticate)])
    async def stop(payload: dict[str, Any] | None = None) -> dict[str, Any]:
        return await control.submit_control(
            RuntimeEventType.STOP,
            reason=str((payload or {}).get("reason") or "control plane stop"),
        )

    @app.post("/v1/checkpoint", dependencies=[Depends(authenticate)])
    async def checkpoint() -> dict[str, Any]:
        return control.supervisor.write_checkpoint().model_dump(mode="json")

    @app.post("/v1/actions/{action_id}/approve", dependencies=[Depends(authenticate)])
    async def approve(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = await control.supervisor.approve_action(
            action_id, approval=str(payload.get("approval") or "researcher")
        )
        return action.model_dump(mode="json")

    @app.post("/v1/actions/{action_id}/reject", dependencies=[Depends(authenticate)])
    async def reject(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = await control.supervisor.reject_action(
            action_id, reason=str(payload.get("reason") or "rejected")
        )
        return action.model_dump(mode="json")

    @app.post("/v1/deliveries/{prepare_id}/resolve", dependencies=[Depends(authenticate)])
    async def resolve_delivery(prepare_id: str, payload: dict[str, Any]) -> dict[str, bool]:
        control.supervisor.resolve_delivery(prepare_id, DeliveryResult.model_validate(payload))
        return {"resolved": True}

    @app.post("/v1/actions/{action_id}/resolve", dependencies=[Depends(authenticate)])
    async def resolve_action(action_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        action = await control.supervisor.resolve_action(
            action_id,
            result=ActionBackendResult.model_validate(payload),
        )
        return action.model_dump(mode="json")

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
