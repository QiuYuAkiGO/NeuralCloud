"""Negotiated voice asset adapter for the separate MistyAI voice service.

The HTTP health endpoint proves authenticated API/storage access only. Render
submission may be available while the inference worker is offline; job results
remain authoritative. This adapter owns no player, sender, GPU or retry worker.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime
import hashlib
import ipaddress
import json
import math
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

import httpx
from pydantic import TypeAdapter

from harness.embodiment.capabilities import (
    VOICE_ASSETS, VOICE_JOBS, VOICE_PROFILES, VOICE_RENDER,
    CapabilityDescriptor, CapabilityStatus, EmbodimentState, HealthStatus,
    ProviderCapabilities, ProviderHealth, StrictModel,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.voice_contracts import (
    Digest, Identifier, VoiceError, VoiceJob, VoiceProfile, VoiceRenderRequest, VoiceScope, VoiceStatus,
)
from harness.research.manifest import EmbodimentCondition

PROVIDER_ID = "mistyai-voice-assets"
VOICE_CAPABILITIES = (VOICE_PROFILES, VOICE_RENDER, VOICE_JOBS, VOICE_ASSETS)
_IDENTIFIER = TypeAdapter(Identifier)
_DIGEST = TypeAdapter(Digest)
_PROFILES = TypeAdapter(list[VoiceProfile])
_MAX_ASSET_BYTES = 64 * 1024 * 1024
_JOB_ERRORS = frozenset({
    "worker_interrupted", "generation_failed", "model_changed", "model_unavailable", "audio_too_large",
    "asset_storage_failed", "asset_corrupt", "reuse_mismatch", "reuse_model_mismatch",
    "vocalization_not_found", "vocalization_mismatch", "worker_unavailable", "invalid_worker_response", "out_of_memory",
})


def _digest(value: object) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _validate(adapter, value):
    try:
        return adapter.validate_python(value)
    except Exception:
        raise VoiceError("invalid_voice_request") from None


def _render_payload(request: VoiceRenderRequest, scope: VoiceScope) -> dict:
    request = VoiceRenderRequest.model_validate(request.model_dump())
    scope = VoiceScope.model_validate(scope.model_dump())
    payload = request.model_dump(mode="json")
    payload["script"]["profile"] = {"voice_profile_id": scope.profile_id, "revision": scope.profile_revision}
    return payload


def render_input_sha256(request: VoiceRenderRequest, scope: VoiceScope) -> str:
    """MistyAI's canonical digest of the full normalized render request."""
    return _digest(_render_payload(request, scope))


class _VoiceHealth(StrictModel):
    schema_version: Literal["voice-contract-v1"]
    status: Literal["healthy"]


class VoiceClient:
    """Authenticated loopback HTTP, explicit credentials and zero automatic retries."""

    def __init__(self, base_url: str, token: str | None = None, *, token_file: str | Path | None = None,
                 timeout_seconds: float = 5, transport: httpx.AsyncBaseTransport | None = None):
        try:
            parts = urlsplit(base_url)
            loopback = parts.hostname == "localhost" or ipaddress.ip_address(parts.hostname or "").is_loopback
            valid_origin = (loopback and parts.scheme == "http" and not parts.username and not parts.password
                            and not parts.query and not parts.fragment and parts.path in ("", "/"))
            parts.port  # Reject malformed ports without exposing the supplied endpoint.
        except (ValueError, TypeError):
            valid_origin = False
        if not valid_origin:
            raise ValueError("voice endpoint must be a private loopback HTTP origin")
        if (token is None) == (token_file is None):
            raise ValueError("voice client requires exactly one credential source")
        try:
            secret = Path(token_file).read_text(encoding="utf-8").strip() if token_file is not None else token
        except (OSError, UnicodeError):
            raise ValueError("voice_token_file_unavailable") from None
        if not isinstance(secret, str) or not 16 <= len(secret) <= 4096 or any(not 33 <= ord(char) <= 126 for char in secret):
            raise ValueError("secure_voice_token_required")
        if not isinstance(timeout_seconds, (float, int)) or not math.isfinite(timeout_seconds) or not 0 < timeout_seconds <= 60:
            raise ValueError("invalid_voice_timeout")
        self._client = httpx.AsyncClient(base_url=base_url.rstrip("/"), headers={"Authorization": "Bearer " + secret},
            timeout=timeout_seconds, follow_redirects=False, trust_env=False, transport=transport)

    @staticmethod
    def _check_status(response: httpx.Response, expected: int = 200):
        if response.status_code != expected:
            codes = {401: "voice_authentication_failed", 403: "voice_forbidden", 404: "voice_not_found",
                     409: "voice_conflict", 422: "invalid_voice_request", 408: "voice_request_expired",
                     429: "voice_rate_limited", 503: "voice_unavailable"}
            raise VoiceError(codes.get(response.status_code, "voice_http_error"))

    async def _request(self, method: str, route: str, model, payload=None, expected: int = 200):
        try:
            response = await self._client.request(method, route, json=payload)
        except httpx.HTTPError:
            raise VoiceError("voice_transport_unavailable") from None
        self._check_status(response, expected)
        if response.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
            raise VoiceError("invalid_voice_response")
        try:
            return model.validate_python(response.json()) if isinstance(model, TypeAdapter) else model.model_validate(response.json())
        except Exception:
            raise VoiceError("invalid_voice_response") from None

    async def health(self) -> None:
        await self._request("GET", "/voice/v1/health", _VoiceHealth)

    async def profiles(self) -> list[VoiceProfile]:
        profiles = await self._request("GET", "/voice/v1/profiles", _PROFILES)
        identities = [(profile.voice_profile_id, profile.revision) for profile in profiles]
        if len(identities) != len(set(identities)):
            raise VoiceError("invalid_voice_response")
        return profiles

    async def profile(self, profile_id: str, revision: int) -> VoiceProfile:
        profile_id = _validate(_IDENTIFIER, profile_id)
        if type(revision) is not int or revision < 1:
            raise VoiceError("invalid_voice_request")
        value = await self._request("GET", f"/voice/v1/profiles/{profile_id}/revisions/{revision}", VoiceProfile)
        if (value.voice_profile_id, value.revision) != (profile_id, revision):
            raise VoiceError("voice_profile_mismatch")
        return value

    @staticmethod
    def _safe_job(value: VoiceJob) -> VoiceJob:
        # Remote diagnostics can contain prompts or worker paths. Keep only
        # recognized codes and a fixed public message, never arbitrary bodies.
        return value.model_copy(update={
            "error_code": value.error_code if value.error_code in _JOB_ERRORS else "voice_job_failed" if value.error_code else None,
            "error_message": "Voice generation failed." if value.error_message else None,
        })

    async def render(self, request: VoiceRenderRequest, scope: VoiceScope) -> VoiceJob:
        payload = _render_payload(request, scope)
        value = await self._request("POST", "/voice/v1/render-jobs", VoiceJob, payload, expected=202)
        if value.input_sha256 != _digest(payload):
            raise VoiceError("voice_job_mismatch")
        return self._safe_job(value)

    async def job(self, job_id: str) -> VoiceJob:
        job_id = _validate(_IDENTIFIER, job_id)
        value = await self._request("GET", f"/voice/v1/jobs/{job_id}", VoiceJob)
        if value.job_id != job_id:
            raise VoiceError("voice_job_mismatch")
        return self._safe_job(value)

    async def cancel(self, job_id: str) -> VoiceJob:
        job_id = _validate(_IDENTIFIER, job_id)
        value = await self._request("POST", f"/voice/v1/jobs/{job_id}:cancel", VoiceJob)
        if value.job_id != job_id:
            raise VoiceError("voice_job_mismatch")
        return self._safe_job(value)

    async def asset(self, digest: str) -> tuple[bytes, str]:
        digest = _validate(_DIGEST, digest)
        try:
            async with self._client.stream("GET", f"/voice/v1/assets/{digest}") as response:
                self._check_status(response)
                media_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
                if media_type not in {"audio/wav", "application/json"}:
                    raise VoiceError("invalid_voice_asset_media_type")
                if response.headers.get("etag") != f'"{digest}"':
                    raise VoiceError("voice_asset_integrity_failed")
                content = bytearray()
                async for chunk in response.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > _MAX_ASSET_BYTES:
                        raise VoiceError("voice_asset_too_large")
        except httpx.HTTPError:
            raise VoiceError("voice_transport_unavailable") from None
        value = bytes(content)
        if hashlib.sha256(value).hexdigest() != digest:
            raise VoiceError("voice_asset_integrity_failed")
        if media_type == "audio/wav" and (len(value) < 12 or value[:4] != b"RIFF" or value[8:12] != b"WAVE"):
            raise VoiceError("invalid_voice_asset")
        if media_type == "application/json":
            try:
                json.loads(value)
            except (ValueError, UnicodeError):
                raise VoiceError("invalid_voice_asset") from None
        return value, media_type

    async def close(self) -> None:
        await self._client.aclose()


class VoiceProvider:
    def __init__(self, condition: EmbodimentCondition, client: VoiceClient, scope: VoiceScope):
        self.scope = VoiceScope.model_validate(scope.model_dump())
        self.client = client
        self.manager = EmbodimentManager(condition.model_copy(deep=True), self)
        self._condition_hash = _digest(self.manager.condition.model_dump(mode="json"))
        self._scope_hash = _digest(self.scope.model_dump(mode="json"))
        self._client_identity = id(client)
        self._activated = False
        self._activation_lock = asyncio.Lock()
        self._profile_hash: str | None = None
        self._job_bindings: dict[str, str] = {}

    async def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(provider_id=PROVIDER_ID, provider_kind="mistyai_voice_assets",
            provider_revision="voice-contract-v1:" + self._scope_hash, body_instance_id=self.scope.body_instance_id,
            observed_at=datetime.now(UTC), capabilities=[CapabilityDescriptor(capability=capability,
                status=CapabilityStatus.AVAILABLE, implementation_version="1", limits={
                    "profile_id": self.scope.profile_id, "profile_revision": self.scope.profile_revision,
                    "scope_sha256": self._scope_hash, "max_segments": 100, "max_text_characters": 10000,
                    "sample_rate": 24000, "asset_production_only": True, "automatic_post_retries": False,
                }, detail="Authenticated voice asset API/storage; worker readiness is unverified. No playback or delivery.")
                for capability in VOICE_CAPABILITIES])

    def _check_profile(self, value: VoiceProfile):
        if (value.voice_profile_id, value.revision) != (self.scope.profile_id, self.scope.profile_revision):
            raise VoiceError("voice_profile_mismatch")
        digest = _digest(value.model_dump(mode="json"))
        if self._profile_hash is not None and self._profile_hash != digest:
            self.manager.set_terminal("voice_profile_revision_changed")
            raise VoiceError("voice_profile_revision_changed")
        self._profile_hash = digest

    async def health(self) -> ProviderHealth:
        try:
            await self.client.health()
            self._check_profile(await self.client.profile(self.scope.profile_id, self.scope.profile_revision))
            overall = HealthStatus.DEGRADED
            health = dict.fromkeys(VOICE_CAPABILITIES, HealthStatus.HEALTHY)
            health[VOICE_RENDER] = HealthStatus.DEGRADED
            reason = "voice_asset_api_ready_worker_unverified"
        except Exception:
            overall = HealthStatus.UNAVAILABLE
            health = dict.fromkeys(VOICE_CAPABILITIES, HealthStatus.UNAVAILABLE)
            reason = "voice_asset_api_or_profile_unavailable"
        return ProviderHealth(provider_id=PROVIDER_ID, body_instance_id=self.scope.body_instance_id,
            status=overall, capability_health=health, observed_at=datetime.now(UTC),
            detail={"reason_code": reason, "worker_readiness": "unverified", "asset_production_only": True})

    async def activate(self) -> VoiceStatus:
        async with self._activation_lock:
            if not self._activated or (self.manager.condition.mode != "off" and self.manager.status().negotiated is None):
                try:
                    await self.manager.activate()
                except BaseException:
                    self._activated = False
                    raise
                self._activated = True
        return await self.status()

    async def _refresh(self):
        if not self._activated:
            raise VoiceError("provider_not_activated")
        if (_digest(self.manager.condition.model_dump(mode="json")) != self._condition_hash
                or _digest(self.scope.model_dump(mode="json")) != self._scope_hash or id(self.client) != self._client_identity):
            self.manager.set_terminal("voice_provider_activation_changed")
            raise VoiceError("provider_activation_changed")
        return await self.manager.refresh()

    async def status(self) -> VoiceStatus:
        if self._activated:
            try:
                await self._refresh()
            except VoiceError:
                pass
        return VoiceStatus(scope=self.scope, embodiment=self.manager.status())

    async def _guard(self, capability: str, *, mutate: bool = False):
        if self.manager.condition.mode == "off" or (mutate and self.manager.condition.mode not in {"active", "grounded"}):
            raise VoiceError("capability_unavailable")
        state = await self._refresh()
        if (state.negotiated is None or not state.negotiated.required_satisfied
                or state.state in {EmbodimentState.DISABLED, EmbodimentState.TERMINAL, EmbodimentState.UNAVAILABLE}
                or not self.manager.supports(capability)):
            raise VoiceError("capability_unavailable")

    async def profiles(self) -> list[VoiceProfile]:
        await self._guard(VOICE_PROFILES)
        values = await self.client.profiles()
        # This body exposes only its configured character revision.
        selected = [value for value in values if (value.voice_profile_id, value.revision) == (self.scope.profile_id, self.scope.profile_revision)]
        for value in selected:
            self._check_profile(value)
        return selected

    async def profile(self) -> VoiceProfile:
        await self._guard(VOICE_PROFILES)
        value = await self.client.profile(self.scope.profile_id, self.scope.profile_revision)
        self._check_profile(value)
        return value

    def restore_binding(self, job_id: str, input_sha256: str) -> None:
        """Restore an acknowledged request from a trusted, same-scope local ledger.

        This is not a runtime HTTP operation. The controller must compare its
        persisted scope before using this association after restart.
        """
        job_id = _validate(_IDENTIFIER, job_id)
        input_sha256 = _validate(_DIGEST, input_sha256)
        existing = self._job_bindings.get(job_id)
        if existing is not None and existing != input_sha256:
            raise VoiceError("voice_job_mismatch")
        self._job_bindings[job_id] = input_sha256

    def _check_job(self, value: VoiceJob, expected_input: str | None = None):
        expected_input = expected_input or self._job_bindings.get(value.job_id)
        if expected_input is not None and value.input_sha256 != expected_input:
            raise VoiceError("voice_job_mismatch")
        if value.result is not None:
            profile = value.result.asset.profile
            if (profile.voice_profile_id, profile.revision) != (self.scope.profile_id, self.scope.profile_revision):
                raise VoiceError("voice_profile_mismatch")
        elif expected_input is None:
            # Unfinished wire jobs do not include their request/profile. Without
            # a known request digest, their ownership cannot be established.
            raise VoiceError("voice_job_scope_unverified")
        self.restore_binding(value.job_id, value.input_sha256)

    async def render(self, request: VoiceRenderRequest, *, before_mutation: Callable[[], None] | None = None) -> VoiceJob:
        request = VoiceRenderRequest.model_validate(request.model_dump())
        await self._guard(VOICE_RENDER, mutate=True)
        if request.source_job_id is not None:
            await self.job(request.source_job_id)
            await self._guard(VOICE_RENDER, mutate=True)
        if before_mutation is not None:
            before_mutation()
        value = await self.client.render(request, self.scope)
        self._check_job(value, render_input_sha256(request, self.scope))
        return value

    async def job(self, job_id: str) -> VoiceJob:
        job_id = _validate(_IDENTIFIER, job_id)
        await self._guard(VOICE_JOBS)
        value = await self.client.job(job_id)
        self._check_job(value)
        return value

    async def cancel(self, job_id: str, *, before_mutation: Callable[[], None] | None = None) -> VoiceJob:
        job_id = _validate(_IDENTIFIER, job_id)
        await self._guard(VOICE_JOBS, mutate=True)
        self._check_job(await self.client.job(job_id))
        await self._guard(VOICE_JOBS, mutate=True)
        if before_mutation is not None:
            before_mutation()
        value = await self.client.cancel(job_id)
        self._check_job(value)
        return value

    async def asset(self, digest: str) -> tuple[bytes, str]:
        await self._guard(VOICE_ASSETS)
        # Digest identity is verified by the client. Audio bytes themselves do
        # not prove a speaker identity, playback or delivery to an audience.
        return await self.client.asset(digest)

    async def close(self) -> None:
        await self.client.close()
