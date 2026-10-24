"""Internal authenticated bridge: Runtime alone saves private diary artifacts."""
from __future__ import annotations

import asyncio
import hashlib
from datetime import date

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field

from harness.diary.models import DiaryOriginal
from harness.runtime.models import RuntimeLifecycle
from harness.runtime.native_regulation import NativeFeedbackBatch, NativeRestRequest, NativeModelAdmission
from harness.agentic.web import NativeWebRequest, execute_native_web


class ArtifactCommit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    original: DiaryOriginal
    day: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")


async def commit(supervisor, request):
    date.fromisoformat(request.day)
    controller = supervisor.diary
    original = request.original
    if not original.entry_id.startswith("diary:") or not original.decision_id.startswith("diary-decision:"):
        raise ValueError("agentic_diary_identity_invalid")
    if hashlib.sha256(original.text.encode()).hexdigest() != original.body_hash:
        raise ValueError("agentic_diary_digest_invalid")
    prior = controller.store.get(original.entry_id)
    if prior is not None:
        if prior["body_hash"] != original.body_hash or prior["decision_id"] != original.decision_id:
            raise ValueError("agentic_diary_identity_conflict")
        return {"entry_id": original.entry_id, "body_hash": original.body_hash, "status": "saved"}
    if controller.store.paused or supervisor.lifecycle != RuntimeLifecycle.RUNNING:
        raise PermissionError("diary_paused")
    if controller.store.count(request.day) >= controller.settings.max_entries_per_day:
        raise PermissionError("diary_day_limit")
    production = getattr(supervisor, "production_runtime", None)
    if production is not None:
        await production.begin_tool_operation("diary", original.entry_id, "expression.diary.write.v1")
        if controller.store.paused or supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            production.cancel_tool_operation("diary", original.entry_id)
            raise PermissionError("diary_paused")
        if controller.store.count(request.day) >= controller.settings.max_entries_per_day:
            production.cancel_tool_operation("diary", original.entry_id)
            raise PermissionError("diary_day_limit")
        production.mark_tool_dispatched("diary", original.entry_id)
    try:
        # No await between permission/day-limit checks and the atomic write.
        controller.store.put(original, day=request.day, revision=supervisor._cognitive_snapshot.revision + 1)
    except Exception:
        if production is not None:
            production.finish_tool_operation("diary", original.entry_id, outcome="failed")
        raise
    if production is not None:
        production.finish_tool_operation("diary", original.entry_id, outcome="completed",
            evidence_refs=[original.entry_id, "diary-body:sha256:" + original.body_hash])
    return {"entry_id": original.entry_id, "body_hash": original.body_hash, "status": "saved"}


def install_routes(app, supervisor, *, token):
    lock = asyncio.Lock()

    @app.post('/v1/internal/agentic/web')
    async def native_web(request: NativeWebRequest, authorization: str | None = Header(default=None)):
        if authorization != 'Bearer ' + token:
            raise HTTPException(status_code=401, detail='invalid service token')
        from harness.embodiment.web_contracts import WebError
        try:
            async with asyncio.timeout(request.timeout_seconds):
                return await execute_native_web(supervisor, request)
        except WebError as exc:
            raise HTTPException(status_code=503, detail=exc.code) from None
        except (ValueError, TimeoutError):
            raise HTTPException(status_code=503, detail='web_unavailable') from None

    @app.get("/v1/internal/agentic/status")
    async def status(authorization: str | None = Header(default=None)):
        if authorization != "Bearer " + token:
            raise HTTPException(status_code=401, detail="invalid service token")
        native = getattr(supervisor, 'native_regulation', None)
        resting = bool(native and native.settings.mode == 'on' and native.rest()['resting'])
        return {"running": supervisor.lifecycle == RuntimeLifecycle.RUNNING and not resting, 'resting': resting}

    @app.get('/v1/internal/agentic/state')
    async def internal_state(authorization: str | None = Header(default=None)):
        if authorization != 'Bearer ' + token:
            raise HTTPException(status_code=401, detail='invalid service token')
        return supervisor.native_regulation.snapshot()

    @app.post('/v1/internal/agentic/affect')
    async def affect(request: NativeFeedbackBatch, authorization: str | None = Header(default=None)):
        if authorization != 'Bearer ' + token:
            raise HTTPException(status_code=401, detail='invalid service token')
        try:
            async with lock:
                return supervisor.native_regulation.queue_feedback(request)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post('/v1/internal/agentic/rest')
    async def rest(request: NativeRestRequest, authorization: str | None = Header(default=None)):
        if authorization != 'Bearer ' + token:
            raise HTTPException(status_code=401, detail='invalid service token')
        try:
            async with lock:
                return supervisor.native_regulation.set_rest(request)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post('/v1/internal/agentic/model-admission')
    async def admit(request: NativeModelAdmission, authorization: str | None = Header(default=None)):
        if authorization != 'Bearer ' + token:
            raise HTTPException(status_code=401, detail='invalid service token')
        try:
            async with lock:
                return supervisor.native_regulation.admit_model(request)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get('/v1/internal/agentic/rest/{request_id}')
    async def rest_receipt(request_id: str, authorization: str | None = Header(default=None)):
        if authorization != 'Bearer ' + token:
            raise HTTPException(status_code=401, detail='invalid service token')
        stored = supervisor.native_regulation.store.get('native_control', request_id)
        if stored is None:
            raise HTTPException(status_code=404, detail='unknown rest request')
        return stored['receipt']

    @app.post("/v1/internal/agentic/diary")
    async def save(request: ArtifactCommit, authorization: str | None = Header(default=None)):
        if authorization != "Bearer " + token:
            raise HTTPException(status_code=401, detail="invalid service token")
        try:
            async with lock:
                return await commit(supervisor, request)
        except PermissionError as exc:
            raise HTTPException(status_code=409, detail=str(exc)) from None
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None
