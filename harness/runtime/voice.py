"""Runtime-owned voice dispatch and bounded, authenticated render-job access.

The body renders audio; a successful job does not prove playback or delivery.
Attempt records prevent a lost HTTP response from causing another render.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import re
import sqlite3

from fastapi import HTTPException, Request, Response
from pydantic import ValidationError

from harness.embodiment.capabilities import VOICE_ASSETS, VOICE_JOBS, VOICE_PROFILES, VOICE_RENDER
from harness.runtime.models import RuntimeLifecycle


class VoiceRuntimeError(ValueError):
    def __init__(self, code):
        self.code = code
        super().__init__(code)


def _error_code(exc):
    code = getattr(exc, "code", None)
    if isinstance(code, str) and re.fullmatch(r"[a-z][a-z0-9_]{0,95}", code):
        return code
    return "voice_request_timeout" if isinstance(exc, TimeoutError) else "voice_operation_failed"


class VoiceController:
    def __init__(self, supervisor, settings, state_dir, *, provider):
        self.supervisor, self.settings, self.provider = supervisor, settings, provider
        state_dir.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(state_dir / "voice-attempts.sqlite3")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.execute("CREATE TABLE IF NOT EXISTS attempts(idempotency_key TEXT PRIMARY KEY, record_json TEXT NOT NULL)")
        self.db.commit()
        self._lock = asyncio.Lock()
        self._body_status = None
        self.last_error = None
        scope = self.settings.scope.model_dump(mode="json")
        for row in self.db.execute("SELECT record_json FROM attempts"):
            record = json.loads(row[0])
            if record.get("scope") == scope and record.get("job_id") and record.get("input_sha256"):
                self.provider.restore_binding(record["job_id"], record["input_sha256"])

    @property
    def timeout_seconds(self):
        return min(10.0, max(0.05, float(getattr(self.settings, "timeout_seconds", 5.0))))

    async def _bounded(self, awaitable):
        return await asyncio.wait_for(awaitable, timeout=self.timeout_seconds)

    async def ready(self):
        self._body_status = await self._bounded(self.provider.activate())
        return self._body_status

    async def status(self):
        try:
            result = await self.ready()
            self._body_status, self.last_error = result, None
            body = result.embodiment.model_dump(mode="json")
        except Exception as exc:
            self.last_error, body = _error_code(exc), None
        return {"enabled": True, "owner": "shared_runtime", "scope": self.settings.scope.model_dump(mode="json"),
            "body": body, "error_code": self.last_error}

    def attempt(self, idempotency_key):
        row = self.db.execute("SELECT record_json FROM attempts WHERE idempotency_key=?", (idempotency_key,)).fetchone()
        return json.loads(row[0]) if row else None

    def _save(self, record):
        with self.db:
            self.db.execute("INSERT INTO attempts VALUES (?,?) ON CONFLICT(idempotency_key) DO UPDATE SET record_json=excluded.record_json",
                (record["idempotency_key"], json.dumps(record, ensure_ascii=False)))

    async def profiles(self):
        await self._ensure_ready(VOICE_PROFILES)
        return await self._bounded(self.provider.profiles())

    async def _ensure_ready(self, capability, *, mutate=False):
        if mutate and self.settings.mode != "grounded":
            raise VoiceRuntimeError("capability_unavailable")
        status = await self.ready()
        body = status.embodiment
        if body.state.value not in {"available", "degraded"} or capability not in body.effective_capabilities:
            raise VoiceRuntimeError("capability_unavailable")

    def _require_running(self):
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            raise VoiceRuntimeError("runtime_not_running")

    async def render(self, payload, *, before_mutation=None):
        from harness.embodiment.voice_contracts import VoiceRenderRequest
        try:
            request = VoiceRenderRequest.model_validate(payload)
        except (ValidationError, TypeError, ValueError):
            raise VoiceRuntimeError("voice_invalid_request") from None
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            raise VoiceRuntimeError("runtime_not_running")
        scope = self.settings.scope.model_dump(mode="json")
        request_hash = hashlib.sha256(json.dumps({"request": request.model_dump(mode="json"), "scope": scope},
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()
        async with self._lock:
            prior = self.attempt(request.idempotency_key)
            if prior:
                if prior["request_hash"] != request_hash:
                    raise VoiceRuntimeError("voice_request_conflict")
                if prior.get("job_id"):
                    return await self.job(prior["job_id"])
                if prior["status"] != "not_dispatched":
                    raise VoiceRuntimeError("voice_dispatch_uncertain")
            # Clear offline/mode/scope failures precede the durable POST attempt.
            await self._ensure_ready(VOICE_RENDER, mutate=True)
            if request.source_job_id is not None:
                await self.job(request.source_job_id)
            # Recheck after acquiring the lock, immediately before mutation.
            if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
                raise VoiceRuntimeError("runtime_not_running")
            record = {"idempotency_key": request.idempotency_key, "request_hash": request_hash,
                "scope": scope, "status": "dispatching"}
            self._save(record)
            mutation_started = False

            def guard_mutation():
                nonlocal mutation_started
                self._require_running()
                if before_mutation is not None:
                    before_mutation()
                mutation_started = True

            try:
                job = await self._bounded(self.provider.render(request, before_mutation=guard_mutation))
                record.update(status="accepted", job_id=job.job_id, input_sha256=job.input_sha256)
                self._save(record)
                return job
            except BaseException as exc:
                record.update(status="uncertain" if mutation_started else "not_dispatched", error_code=_error_code(exc))
                self._save(record)
                raise

    async def job(self, job_id):
        await self._ensure_ready(VOICE_JOBS)
        return await self._bounded(self.provider.job(job_id))

    async def cancel(self, job_id):
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            raise VoiceRuntimeError("runtime_not_running")
        await self._ensure_ready(VOICE_JOBS, mutate=True)
        if self.supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            raise VoiceRuntimeError("runtime_not_running")
        return await self._bounded(self.provider.cancel(job_id, before_mutation=self._require_running))

    async def asset(self, digest):
        await self._ensure_ready(VOICE_ASSETS)
        return await self._bounded(self.provider.asset(digest))

    async def verify_job_artifacts(self, job):
        """Confirm reported output using independently fetched, digest-checked assets."""
        asset = job.result.asset
        _, audio_type = await self.asset(asset.audio_digest)
        raw, manifest_type = await self.asset(asset.manifest_digest)
        try:
            manifest = json.loads(raw)
            request = manifest["request"]
            profile = request["script"]["profile"]
            request_hash = hashlib.sha256(json.dumps(request, sort_keys=True, ensure_ascii=False,
                separators=(",", ":")).encode()).hexdigest()
            valid = (audio_type == "audio/wav" and manifest_type == "application/json"
                and manifest["schema_version"] == "voice-contract-v1" and manifest["job_id"] == job.job_id
                and request_hash == job.input_sha256
                and profile == {"voice_profile_id": self.settings.profile_id, "revision": self.settings.profile_revision}
                and all(manifest[name] == getattr(asset, name) for name in ("audio_digest", "script_digest", "timeline_digest")))
        except (ValueError, TypeError, KeyError, UnicodeError):
            valid = False
        if not valid:
            raise VoiceRuntimeError("voice_artifact_binding_invalid")

    async def close(self):
        try:
            await self._bounded(self.provider.close())
        finally:
            self.db.close()


def add_voice_routes(router, supervisor):
    def voice():
        controller = getattr(supervisor, "voice", None)
        if controller is None:
            raise HTTPException(status_code=503, detail="voice_not_configured")
        return controller

    async def invoke(awaitable):
        try:
            return await awaitable
        except HTTPException:
            raise
        except Exception as exc:
            code = _error_code(exc)
            status = 422 if code in {"voice_invalid_request", "invalid_voice_request", "voice_invalid_identifier", "voice_invalid_digest"} else (
                409 if code in {"runtime_not_running", "voice_request_conflict", "voice_conflict", "voice_dispatch_uncertain"} else
                404 if code in {"voice_job_not_found", "voice_asset_not_found", "voice_not_found"} else 503)
            raise HTTPException(status_code=status, detail=code) from None

    @router.get("/v1/voice")
    async def status():
        return await voice().status()

    @router.get("/v1/voice/profiles")
    async def profiles():
        return {"profiles": [item.model_dump(mode="json") for item in await invoke(voice().profiles())]}

    @router.post("/v1/voice/render-jobs", status_code=202)
    async def render(request: Request):
        # Validate explicitly so Pydantic errors never echo scripts or inputs.
        controller = voice()
        try:
            payload = await request.json()
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(status_code=422, detail="voice_invalid_request") from None
        return (await invoke(controller.render(payload))).model_dump(mode="json")

    @router.get("/v1/voice/jobs/{job_id}")
    async def job(job_id: str):
        return (await invoke(voice().job(job_id))).model_dump(mode="json")

    @router.post("/v1/voice/jobs/{job_id}:cancel")
    async def cancel(job_id: str):
        return (await invoke(voice().cancel(job_id))).model_dump(mode="json")

    @router.get("/v1/voice/assets/{digest}")
    async def asset(digest: str):
        content, media_type = await invoke(voice().asset(digest))
        return Response(content=content, media_type=media_type, headers={"Cache-Control": "private, no-store"})


__all__ = ["VoiceController", "VoiceRuntimeError", "add_voice_routes"]
