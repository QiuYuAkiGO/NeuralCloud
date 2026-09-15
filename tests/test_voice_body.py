"""Voice body behavior against the real HTTP wire shape, without GPU inference."""

import asyncio
from copy import deepcopy
import hashlib
import json

import httpx
from pydantic import ValidationError
import pytest

from harness.embodiment.capabilities import EmbodimentState, HealthStatus, VOICE_RENDER
from harness.embodiment.providers.voice import VoiceClient, VoiceProvider, VOICE_CAPABILITIES
from harness.embodiment.voice_contracts import VoiceError, VoiceRenderRequest, VoiceScope
from harness.research.manifest import EmbodimentCondition

TOKEN = "voice-test-token-0123456789"
DIGEST = "a" * 64
NOW = "2026-09-12T00:00:00Z"


def canonical(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def profile(profile_id="character", revision=2):
    return {"schema_version": "voice-contract-v1", "voice_profile_id": profile_id, "revision": revision,
        "description": "Character voice", "reference_audio_digest": DIGEST, "reference_text": "Hello.",
        "design_job_id": "design-1", "candidate_id": "candidate-1", "created_at": NOW,
        "provenance": {"backend": "test", "model_id": "test", "model_revision": "1", "weights_sha256": DIGEST,
            "code_revision": "1", "seed": 0, "parameters": {}, "metrics": {}}}


def render_request():
    return VoiceRenderRequest.model_validate({"idempotency_key": "decision-1", "seed": 42,
        "script": {"segments": [
            {"kind": "speech", "segment_id": "speech-1", "text": "Hello.", "style": "gentle", "strength": "low", "rate": 0.9,
             "pronunciations": [{"text": "Hello", "phonemes": ["h", "e"]}]},
            {"kind": "pause", "segment_id": "pause-1", "duration_ms": 20},
            {"kind": "vocalization", "segment_id": "sigh-1", "vocalization": "sigh", "approved_asset_id": "approved-1"},
        ]}})


class VoiceServer:
    def __init__(self):
        self.calls = []
        self.jobs = {}
        self.payload = None
        self.profile = profile()
        self.available = True
        self.bad_input = False
        self.timeout_render = False
        self.wrong_job_id = False
        self.assets = {}
        self.asset_etag = None
        self.asset_body = None

    def __call__(self, request):
        self.calls.append((request.method, request.url.path))
        assert request.headers["authorization"] == "Bearer " + TOKEN
        path = request.url.path
        if path == "/voice/v1/health":
            return httpx.Response(200 if self.available else 503, json={"schema_version": "voice-contract-v1", "status": "healthy"})
        if path == "/voice/v1/profiles":
            return httpx.Response(200, json=[self.profile, profile("other-character", 1)])
        if path.startswith("/voice/v1/profiles/"):
            return httpx.Response(200, json=self.profile)
        if path == "/voice/v1/render-jobs":
            if self.timeout_render:
                raise httpx.ReadTimeout("remote prompt and " + TOKEN, request=request)
            self.payload = json.loads(request.content)
            value = {"schema_version": "voice-contract-v1", "job_id": "render-1", "kind": "render", "state": "queued",
                "input_sha256": DIGEST if self.bad_input else canonical(self.payload), "created_at": NOW, "updated_at": NOW,
                "result": None, "completed_segments": [], "error_code": None, "error_message": None}
            self.jobs[value["job_id"]] = value
            return httpx.Response(202, json=value)
        if path.startswith("/voice/v1/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            if job_id.endswith(":cancel"):
                job_id = job_id.removesuffix(":cancel")
                self.jobs[job_id]["state"] = "cancelled"
            value = deepcopy(self.jobs[job_id])
            if self.wrong_job_id:
                value["job_id"] = "other-job"
            return httpx.Response(200, json=value)
        if path.startswith("/voice/v1/assets/"):
            digest = path.rsplit("/", 1)[-1]
            content, media_type = self.assets[digest]
            return httpx.Response(200, content=self.asset_body if self.asset_body is not None else content,
                headers={"content-type": media_type, "etag": self.asset_etag if self.asset_etag is not None else f'"{digest}"'})
        return httpx.Response(404, json={"error": {"message": TOKEN}})


def provider(server, *, mode="active", capabilities=VOICE_CAPABILITIES):
    scope = VoiceScope(body_instance_id="character-body", profile_id="character", profile_revision=2)
    condition = EmbodimentCondition(mode=mode, body_instance_id=scope.body_instance_id if mode != "off" else None,
        capability_requirements=[{"capability": capability, "required": True} for capability in capabilities] if mode != "off" else [])
    client = VoiceClient("http://127.0.0.1:8794", token=TOKEN, transport=httpx.MockTransport(server))
    return VoiceProvider(condition, client, scope)


def test_render_preserves_structured_performance_and_binds_profile():
    async def scenario():
        server = VoiceServer()
        body = provider(server)
        try:
            status = await body.activate()
            assert status.embodiment.state == EmbodimentState.DEGRADED
            assert status.embodiment.health.detail["worker_readiness"] == "unverified"
            assert status.embodiment.health.capability_health[VOICE_RENDER] == HealthStatus.DEGRADED
            assert status.embodiment.effective_capabilities == set(VOICE_CAPABILITIES)
            assert (await body.capabilities()).capabilities[0].limits["asset_production_only"] is True
            profiles = await body.profiles()
            assert [(value.voice_profile_id, value.revision) for value in profiles] == [("character", 2)]
            value = await body.render(render_request())
            assert value.input_sha256 == canonical(server.payload)
            assert server.payload["script"]["profile"] == {"voice_profile_id": "character", "revision": 2}
            assert server.payload["script"]["segments"] == render_request().model_dump(mode="json")["script"]["segments"]
            assert server.payload["seed"] == 42
            assert (await body.job(value.job_id)).state == "queued"
            assert (await body.cancel(value.job_id)).state == "cancelled"
            assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
            assert server.calls.count(("POST", "/voice/v1/jobs/render-1:cancel")) == 1
        finally:
            await body.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_off_and_observe_block_mutations(mode):
    async def scenario():
        server = VoiceServer()
        body = provider(server, mode=mode)
        try:
            await body.activate()
            with pytest.raises(VoiceError, match="capability_unavailable"):
                await body.render(render_request())
            with pytest.raises(VoiceError, match="capability_unavailable"):
                await body.cancel("render-1")
            assert not any(method == "POST" for method, _ in server.calls)
            if mode == "off":
                assert server.calls == []
            else:
                assert (await body.profile()).voice_profile_id == "character"
        finally:
            await body.close()
    asyncio.run(scenario())


def test_health_loss_blocks_dispatch_and_recovery_restores_negotiated_capability():
    async def scenario():
        server = VoiceServer()
        body = provider(server)
        try:
            await body.activate()
            server.available = False
            with pytest.raises(VoiceError, match="capability_unavailable"):
                await body.render(render_request())
            assert not any(method == "POST" for method, _ in server.calls)
            server.available = True
            assert (await body.render(render_request())).state == "queued"
        finally:
            await body.close()
    asyncio.run(scenario())


def test_initially_offline_service_recovers_without_expanding_permissions():
    async def scenario():
        server = VoiceServer()
        server.available = False
        body = provider(server)
        try:
            initial = await body.activate()
            assert initial.embodiment.state == EmbodimentState.UNAVAILABLE
            assert initial.embodiment.activated_capabilities == set(VOICE_CAPABILITIES)
            server.available = True
            recovered = await body.activate()
            assert recovered.embodiment.state == EmbodimentState.DEGRADED
            assert recovered.embodiment.effective_capabilities == initial.embodiment.activated_capabilities
            assert (await body.render(render_request())).state == "queued"
        finally:
            await body.close()
    asyncio.run(scenario())


def test_unnegotiated_render_is_unavailable():
    async def scenario():
        server = VoiceServer()
        body = provider(server, capabilities=tuple(value for value in VOICE_CAPABILITIES if value != VOICE_RENDER))
        try:
            await body.activate()
            with pytest.raises(VoiceError, match="capability_unavailable"):
                await body.render(render_request())
            assert not any(method == "POST" for method, _ in server.calls)
        finally:
            await body.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("mutation", ["scope", "condition", "profile"])
def test_frozen_activation_identity_cannot_change(mutation):
    async def scenario():
        server = VoiceServer()
        body = provider(server)
        try:
            await body.activate()
            if mutation == "scope":
                body.scope = body.scope.model_copy(update={"profile_id": "other-character"})
            elif mutation == "condition":
                body.manager.condition = body.manager.condition.model_copy(update={"mode": "observe"})
            else:
                server.profile["reference_audio_digest"] = "b" * 64
            status = await body.status()
            assert status.embodiment.state == EmbodimentState.TERMINAL
            with pytest.raises(VoiceError):
                await body.render(render_request())
            assert not any(method == "POST" for method, _ in server.calls)
        finally:
            await body.close()
    asyncio.run(scenario())


def test_timeout_does_not_retry_or_reflect_exception_details():
    async def scenario():
        server = VoiceServer()
        server.timeout_render = True
        body = provider(server)
        try:
            await body.activate()
            with pytest.raises(VoiceError) as raised:
                await body.render(render_request())
            assert str(raised.value) == "voice_transport_unavailable"
            assert raised.value.__suppress_context__
            assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
        finally:
            await body.close()
    asyncio.run(scenario())


def test_wrong_render_input_hash_rejected():
    async def scenario():
        server = VoiceServer()
        server.bad_input = True
        body = provider(server)
        try:
            await body.activate()
            with pytest.raises(VoiceError, match="voice_job_mismatch"):
                await body.render(render_request())
            assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
        finally:
            await body.close()
    asyncio.run(scenario())


def test_pending_job_requires_binding_and_restore_verifies_acknowledged_digest():
    async def scenario():
        server = VoiceServer()
        first = provider(server)
        second = provider(server)
        try:
            await first.activate()
            submitted = await first.render(render_request())
            await second.activate()
            with pytest.raises(VoiceError, match="voice_job_scope_unverified"):
                await second.job(submitted.job_id)
            with pytest.raises(VoiceError, match="voice_job_scope_unverified"):
                await second.cancel(submitted.job_id)
            assert not any(path.endswith(":cancel") for _, path in server.calls)
            second.restore_binding(submitted.job_id, submitted.input_sha256)
            assert (await second.job(submitted.job_id)).input_sha256 == submitted.input_sha256
            server.jobs[submitted.job_id]["input_sha256"] = DIGEST
            with pytest.raises(VoiceError, match="voice_job_mismatch"):
                await second.cancel(submitted.job_id)
            assert not any(path.endswith(":cancel") for _, path in server.calls)
        finally:
            await first.close()
            await second.close()
    asyncio.run(scenario())


def test_job_response_identity_and_diagnostics_are_checked():
    async def scenario():
        server = VoiceServer()
        body = provider(server)
        try:
            await body.activate()
            submitted = await body.render(render_request())
            server.wrong_job_id = True
            with pytest.raises(VoiceError, match="voice_job_mismatch"):
                await body.job(submitted.job_id)
            server.wrong_job_id = False
            server.jobs[submitted.job_id].update(state="failed", error_code=TOKEN, error_message="private prompt " + TOKEN)
            failed = await body.job(submitted.job_id)
            assert failed.error_code == "voice_job_failed"
            assert failed.error_message == "Voice generation failed."
            assert TOKEN not in failed.model_dump_json()
        finally:
            await body.close()
    asyncio.run(scenario())


def test_completed_job_with_other_profile_cannot_be_read_or_used_as_source():
    async def scenario():
        server = VoiceServer()
        body = provider(server)
        try:
            await body.activate()
            submitted = await body.render(render_request())
            server.jobs[submitted.job_id].update(state="succeeded", result={"kind": "render", "asset": {
                "profile": {"voice_profile_id": "other-character", "revision": 2}, "audio_digest": DIGEST,
                "timeline_digest": DIGEST, "script_digest": DIGEST, "manifest_digest": DIGEST,
                "sample_rate": 24000, "sample_count": 240, "segments": [], "quality_checks": {}}})
            with pytest.raises(VoiceError, match="voice_profile_mismatch"):
                await body.job(submitted.job_id)
            request = render_request().model_copy(update={"source_job_id": submitted.job_id})
            with pytest.raises(VoiceError, match="voice_profile_mismatch"):
                await body.render(request)
            assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
        finally:
            await body.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("damage,expected", [(None, None), ("body", "voice_asset_integrity_failed"),
    ("etag", "voice_asset_integrity_failed"), ("media", "invalid_voice_asset_media_type")])
def test_asset_digest_etag_and_media_type(damage, expected):
    async def scenario():
        server = VoiceServer()
        content = b'{"schema_version":"voice-contract-v1"}'
        digest = hashlib.sha256(content).hexdigest()
        server.assets[digest] = (content, "text/html" if damage == "media" else "application/json")
        if damage == "body":
            server.asset_body = b"corrupt"
        if damage == "etag":
            server.asset_etag = '"' + DIGEST + '"'
        body = provider(server)
        try:
            await body.activate()
            if expected:
                with pytest.raises(VoiceError, match=expected):
                    await body.asset(digest)
            else:
                assert await body.asset(digest) == (content, "application/json")
        finally:
            await body.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("endpoint", ["https://127.0.0.1", "http://example.com", "http://127.0.0.1/voice",
    "http://user:secret@localhost", "http://localhost?token=secret", "http://localhost#x", "http://localhost:invalid"])
def test_client_rejects_nonprivate_origins(endpoint):
    with pytest.raises(ValueError, match="private loopback HTTP origin"):
        VoiceClient(endpoint, TOKEN)


def test_client_requires_one_explicit_credential_source_and_safe_token_file(tmp_path):
    with pytest.raises(ValueError, match="exactly one credential source"):
        VoiceClient("http://localhost")
    with pytest.raises(ValueError, match="exactly one credential source"):
        VoiceClient("http://localhost", TOKEN, token_file=tmp_path / "token")
    with pytest.raises(ValueError, match="voice_token_file_unavailable"):
        VoiceClient("http://localhost", token_file=tmp_path / "does-not-exist")
    with pytest.raises(ValueError, match="secure_voice_token_required"):
        VoiceClient("http://localhost", "short")


def test_scripts_reject_voice_switch_markup_duplicate_ids_and_invalid_regeneration():
    value = render_request().model_dump(mode="json")
    bad = deepcopy(value)
    bad["script"]["profile"] = {"voice_profile_id": "other-character", "revision": 1}
    with pytest.raises(ValidationError):
        VoiceRenderRequest.model_validate(bad)
    bad = deepcopy(value)
    bad["script"]["segments"][0]["text"] = "<system>control</system>"
    with pytest.raises(ValidationError):
        VoiceRenderRequest.model_validate(bad)
    bad = deepcopy(value)
    bad["script"]["segments"][1]["segment_id"] = "speech-1"
    with pytest.raises(ValidationError):
        VoiceRenderRequest.model_validate(bad)
    bad = deepcopy(value)
    bad["regenerate_segment_ids"] = ["speech-1"]
    with pytest.raises(ValidationError):
        VoiceRenderRequest.model_validate(bad)


def test_cancelled_activation_can_negotiate_on_retry():
    async def scenario():
        server = VoiceServer()
        stalled = True

        async def handler(request):
            if stalled and request.url.path == "/voice/v1/health":
                await asyncio.Event().wait()
            return server(request)

        body = provider(handler)
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(body.activate(), 0.01)
            stalled = False
            status = await body.activate()
            assert status.embodiment.effective_capabilities == set(VOICE_CAPABILITIES)
            assert (await body.render(render_request())).state == "queued"
        finally:
            await body.close()
    asyncio.run(scenario())


def test_mutation_checkpoint_runs_after_awaited_health_guards():
    async def scenario():
        server = VoiceServer()
        paused = False

        def check_running():
            if paused:
                raise VoiceError("voice_runtime_not_running")

        async def handler(request):
            nonlocal paused
            if request.url.path == "/voice/v1/health":
                paused = True
            return server(request)

        body = provider(handler)
        try:
            await body.activate()
            paused = False
            with pytest.raises(VoiceError, match="voice_runtime_not_running"):
                await body.render(render_request(), before_mutation=check_running)
            assert not any(method == "POST" for method, _ in server.calls)
            submitted = await body.render(render_request())
            paused = False
            with pytest.raises(VoiceError, match="voice_runtime_not_running"):
                await body.cancel(submitted.job_id, before_mutation=check_running)
            assert not any(path.endswith(":cancel") for _, path in server.calls)
        finally:
            await body.close()
    asyncio.run(scenario())
