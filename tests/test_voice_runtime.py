"""Voice registration, fixed-scope dispatch, and read-only job recovery."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from harness.cognitive.capabilities import CapabilitySelectionError, _bound_choice
from harness.contracts.capability_calls import CapabilityChoice, VOICE_PROFILES, VOICE_RENDER
from harness.embodiment.capabilities import EmbodimentStatus, VOICE_ASSETS, VOICE_JOBS
from harness.embodiment.voice_contracts import VoiceJob, VoiceProfile, VoiceStatus
from harness.research.manifest import AutonomousCapabilitiesCondition
from harness.runtime.capabilities import CapabilityController
from harness.runtime.control import ControlPlane, create_control_app
from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.voice import VoiceController, VoiceRuntimeError
from harness.runtime.voice_config import VoiceSettings


CAPABILITIES = {VOICE_PROFILES, VOICE_RENDER, VOICE_ASSETS, VOICE_JOBS}
SCRIPT = {"segments": [{"kind": "speech", "segment_id": "sentence-1", "text": "你好，今天想听听雨声。"}]}


def voice_job(state="queued"):
    value = dict(job_id="render-1", state=state, input_sha256="a" * 64, created_at=utc_now(), updated_at=utc_now())
    if state == "succeeded":
        value["result"] = {"asset": {"profile": {"voice_profile_id": "misty", "revision": 1},
            **{name: "b" * 64 for name in ("audio_digest", "timeline_digest", "script_digest", "manifest_digest")},
            "sample_count": 24, "segments": []}}
    return VoiceJob.model_validate(value)


class Provider:
    def __init__(self, scope):
        self.scope, self.render_calls, self.job_reads, self.restored = scope, [], [], []
        self.current = voice_job()
        self.failure = None
        self.delay = 0
        self.activated = False
        self.asset_failure = False
        self.before_post = None
        self.manifest_bytes = b""

    async def activate(self):
        self.activated = True
        return VoiceStatus(scope=self.scope, embodiment=EmbodimentStatus(state="available", provider_id="voice",
            body_instance_id=self.scope.body_instance_id, activated_capabilities=CAPABILITIES,
            effective_capabilities=CAPABILITIES))

    async def status(self):
        return await self.activate()

    async def profiles(self):
        assert self.activated
        return [VoiceProfile(voice_profile_id="misty", revision=1, description="fixed voice",
            reference_audio_digest="c" * 64, reference_text="sample", design_job_id="design-1", candidate_id="candidate-1",
            provenance={"backend": "test", "model_id": "test", "model_revision": "1", "weights_sha256": "d" * 64,
                "code_revision": "1", "seed": 0}, created_at=utc_now())]

    async def render(self, request, *, before_mutation=None):
        assert self.activated
        if self.before_post:
            self.before_post()
        if before_mutation:
            before_mutation()
        self.render_calls.append(request)
        await asyncio.sleep(self.delay)
        if self.failure:
            raise self.failure
        wire = request.model_dump(mode="json")
        wire["script"]["profile"] = {"voice_profile_id": self.scope.profile_id, "revision": self.scope.profile_revision}
        self.wire = wire
        self.current = self.current.model_copy(update={"input_sha256": hashlib.sha256(json.dumps(wire,
            sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()})
        return self.current

    def finish(self, state):
        self.current = voice_job(state).model_copy(update={"input_sha256": self.current.input_sha256})
        if state == "succeeded":
            asset = self.current.result.asset.model_copy(update={"audio_digest": hashlib.sha256(b"isolated-wav").hexdigest()})
            manifest = {"schema_version": "voice-contract-v1", "job_id": self.current.job_id, "request": self.wire,
                **{name: getattr(asset, name) for name in ("audio_digest", "script_digest", "timeline_digest")}}
            self.manifest_bytes = json.dumps(manifest, ensure_ascii=False).encode()
            asset = asset.model_copy(update={"manifest_digest": hashlib.sha256(self.manifest_bytes).hexdigest()})
            self.current = self.current.model_copy(update={"result": self.current.result.model_copy(update={"asset": asset})})

    async def job(self, identity):
        self.job_reads.append(identity)
        return self.current

    async def cancel(self, identity, *, before_mutation=None):
        if self.before_post:
            self.before_post()
        if before_mutation:
            before_mutation()
        self.current = voice_job("cancelled")
        return self.current

    async def asset(self, digest):
        if self.asset_failure:
            raise RuntimeError("PRIVATE_REMOTE_ERROR")
        if self.current.result and digest == self.current.result.asset.manifest_digest:
            return self.manifest_bytes, "application/json"
        return b"isolated-wav", "audio/wav"

    def restore_binding(self, job_id, input_sha256):
        self.restored.append((job_id, input_sha256))

    async def close(self):
        pass


def rig(tmp_path, *, shared=False):
    settings = VoiceSettings(token_file=tmp_path / "token", profile_id="misty", profile_revision=1, timeout_seconds=0.05)
    supervisor = SimpleNamespace(lifecycle=RuntimeLifecycle.RUNNING, runtime_instance_id="runtime-1", add_listener=lambda _: None,
        manifest=SimpleNamespace(control_mode="study_runtime", embodiment=SimpleNamespace(mode="off"),
            autonomous_capabilities=AutonomousCapabilitiesCondition(enabled=shared,
                owners={"board": "off", "qq": "off", "desktop": "off", "voice": "shared" if shared else "off"},
                allowed_capabilities=(VOICE_PROFILES, VOICE_RENDER))))
    provider = Provider(settings.scope)
    supervisor.voice = VoiceController(supervisor, settings, tmp_path, provider=provider)
    supervisor.capabilities = CapabilityController(supervisor, tmp_path)
    return supervisor, provider


async def close(supervisor):
    await supervisor.voice.close()
    supervisor.capabilities.close()


def opportunity(controller, offer, identity):
    return {"opportunity_id": identity, "deadline_at": (utc_now() + timedelta(seconds=10)).isoformat(),
        "offer": offer.model_dump(mode="json"), "generation": controller.generation,
        "runtime_instance_id": controller.supervisor.runtime_instance_id, "activity_revision": controller.activity_revision}


def test_authenticated_routes_validate_without_echoing_inputs_and_gate_mutation(tmp_path):
    async def scenario():
        supervisor, provider = rig(tmp_path)
        app = create_control_app(ControlPlane(supervisor, token="local-test-token"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime") as client:
            for method, path in [("GET", "/v1/voice"), ("GET", "/v1/voice/profiles"), ("POST", "/v1/voice/render-jobs"),
                    ("GET", "/v1/voice/jobs/render-1"), ("POST", "/v1/voice/jobs/render-1:cancel"),
                    ("GET", "/v1/voice/assets/" + "a" * 64)]:
                assert (await client.request(method, path)).status_code == 401
            client.headers["Authorization"] = "Bearer local-test-token"
            assert (await client.get("/v1/voice")).json()["body"]["state"] == "available"
            assert (await client.get("/v1/voice/profiles")).json()["profiles"][0]["voice_profile_id"] == "misty"
            invalid = await client.post("/v1/voice/render-jobs", json={"script": SCRIPT, "profile_id": "PRIVATE_SENTINEL"})
            assert invalid.status_code == 422 and "PRIVATE_SENTINEL" not in invalid.text
            supervisor.lifecycle = RuntimeLifecycle.PAUSED
            request = {"script": SCRIPT, "idempotency_key": "request-1"}
            assert (await client.post("/v1/voice/render-jobs", json=request)).json()["detail"] == "runtime_not_running"
            assert not provider.render_calls
            supervisor.lifecycle = RuntimeLifecycle.RUNNING
            assert (await client.post("/v1/voice/render-jobs", json=request)).status_code == 202
            assert (await client.get("/v1/voice/jobs/render-1")).json()["state"] == "queued"
            assert (await client.get("/v1/voice/assets/" + "a" * 64)).content == b"isolated-wav"
            assert (await client.post("/v1/voice/jobs/render-1:cancel")).json()["state"] == "cancelled"
        await close(supervisor)
    asyncio.run(scenario())


def test_disabled_autonomy_exposes_dedicated_voice_body_without_offers(tmp_path):
    async def scenario():
        supervisor, _ = rig(tmp_path)
        snapshot = await supervisor.capabilities.snapshot()
        assert not snapshot["enabled"] and snapshot["catalog"] == []
        assert set(snapshot["bodies"]["voice"]["body"]["effective_capabilities"]) == CAPABILITIES
        await close(supervisor)
    asyncio.run(scenario())


@pytest.mark.parametrize("extra", [{"profile_id": "another-voice"}, {"idempotency_key": "model-owned"},
    {"runtime_instance_id": "forged"}, {"seed": True}, {"seed": "1"}, {"seed": -1},
    {"script": {"segments": [{"kind": "speech", "segment_id": "one", "text": "<unsafe>"}]}}])
def test_shared_choice_rejects_scope_ids_and_invalid_typed_arguments(tmp_path, extra):
    async def scenario():
        supervisor, _ = rig(tmp_path, shared=True)
        offers = await supervisor.capabilities.catalog()
        offer = next(item for item in offers if item.capability_id == VOICE_RENDER)
        with pytest.raises(CapabilitySelectionError, match="capability_invalid_voice_arguments"):
            _bound_choice(CapabilityChoice(kind="act", offer_id=offer.offer_id, arguments={"script": SCRIPT, **extra}), offer, now=utc_now())
        await close(supervisor)
    asyncio.run(scenario())


@pytest.mark.parametrize("final_state, expected", [("succeeded", "completed"), ("failed", "error"), ("cancelled", "cancelled")])
def test_shared_dispatch_reconciles_job_without_rerender_or_playback_claim(tmp_path, final_state, expected):
    async def scenario():
        supervisor, provider = rig(tmp_path, shared=True)
        controller = supervisor.capabilities
        offer = next(item for item in await controller.catalog() if item.capability_id == VOICE_RENDER)
        row = opportunity(controller, offer, "one-opportunity")
        selected = SimpleNamespace(choice=CapabilityChoice(kind="act", offer_id=offer.offer_id, arguments={"script": SCRIPT, "seed": 12}))
        await controller.dispatch_voice(row, selected, offer)
        assert row["status"] == "pending"
        assert provider.render_calls[0].seed == 12
        assert provider.render_calls[0].idempotency_key.startswith("voice-capability:")
        # Reading an already-issued job remains valid beyond the opportunity's lifetime.
        row["deadline_at"] = (utc_now() - timedelta(seconds=10)).isoformat()
        controller.calls.put(row)
        supervisor.lifecycle = RuntimeLifecycle.PAUSED
        provider.finish(final_state)
        await controller.reconcile()
        result = controller.calls.get(row["opportunity_id"])
        assert result["status"] == expected and len(provider.render_calls) == 1
        assert "reward" not in result and "memory" not in result and "delivered" not in result
        if final_state == "succeeded":
            assert result["effect"] == "audio_asset_only" and result["evidence_refs"]
        await close(supervisor)
    asyncio.run(scenario())


def test_lost_ack_is_durable_uncertain_and_cannot_rerender(tmp_path):
    async def scenario():
        supervisor, provider = rig(tmp_path)
        provider.delay = 1
        payload = {"script": SCRIPT, "idempotency_key": "lost-ack"}
        with pytest.raises(TimeoutError):
            await supervisor.voice.render(payload)
        assert supervisor.voice.attempt("lost-ack")["status"] == "uncertain"
        await close(supervisor)
        restored, second_provider = rig(tmp_path)
        with pytest.raises(VoiceRuntimeError, match="voice_dispatch_uncertain"):
            await restored.voice.render(payload)
        assert len(provider.render_calls) == 1 and second_provider.render_calls == []
        await close(restored)
    asyncio.run(scenario())


def test_acknowledged_job_restores_hash_binding_and_replay_is_readonly(tmp_path):
    async def scenario():
        supervisor, _ = rig(tmp_path)
        payload = {"script": SCRIPT, "idempotency_key": "durable"}
        first = await supervisor.voice.render(payload)
        await close(supervisor)
        restored, provider = rig(tmp_path)
        assert provider.restored == [("render-1", first.input_sha256)]
        assert (await restored.voice.render(payload)).job_id == "render-1"
        assert provider.render_calls == [] and provider.job_reads == ["render-1"]
        with pytest.raises(VoiceRuntimeError, match="voice_request_conflict"):
            await restored.voice.render({**payload, "seed": 2})
        await close(restored)
    asyncio.run(scenario())


def test_observe_mode_has_no_render_offer_or_poisoned_dispatch_key(tmp_path):
    async def scenario():
        supervisor, provider = rig(tmp_path, shared=True)
        supervisor.voice.settings = supervisor.voice.settings.model_copy(update={"mode": "observe"})
        offers = await supervisor.capabilities.catalog()
        assert next(item for item in offers if item.capability_id == VOICE_PROFILES).ready
        assert not next(item for item in offers if item.capability_id == VOICE_RENDER).available
        with pytest.raises(VoiceRuntimeError, match="capability_unavailable"):
            await supervisor.voice.render({"script": SCRIPT, "idempotency_key": "not-dispatched"})
        assert supervisor.voice.attempt("not-dispatched") is None and provider.render_calls == []
        await close(supervisor)
    asyncio.run(scenario())


def test_pause_during_provider_preflight_stops_actual_post(tmp_path):
    async def scenario():
        supervisor, provider = rig(tmp_path)
        provider.before_post = lambda: setattr(supervisor, "lifecycle", RuntimeLifecycle.PAUSED)
        with pytest.raises(VoiceRuntimeError, match="runtime_not_running"):
            await supervisor.voice.render({"script": SCRIPT, "idempotency_key": "pause-race"})
        assert provider.render_calls == []
        assert supervisor.voice.attempt("pause-race")["status"] == "not_dispatched"
        provider.before_post = None
        supervisor.lifecycle = RuntimeLifecycle.RUNNING
        assert (await supervisor.voice.render({"script": SCRIPT, "idempotency_key": "pause-race"})).state == "queued"
        await close(supervisor)
    asyncio.run(scenario())


def test_shared_pause_before_post_finishes_rejected_without_blocking_future_calls(tmp_path):
    async def scenario():
        supervisor, provider = rig(tmp_path, shared=True)
        controller = supervisor.capabilities
        offer = next(item for item in await controller.catalog() if item.capability_id == VOICE_RENDER)
        row = opportunity(controller, offer, "pause-before-post")
        choice = SimpleNamespace(choice=CapabilityChoice(kind="act", offer_id=offer.offer_id, arguments={"script": SCRIPT}))
        provider.before_post = lambda: setattr(supervisor, "lifecycle", RuntimeLifecycle.PAUSED)
        await controller.dispatch_voice(row, choice, offer)
        assert controller.calls.get(row["opportunity_id"])["status"] == "rejected"
        await controller.reconcile()
        assert provider.render_calls == []
        await close(supervisor)
    asyncio.run(scenario())


def test_explicit_first_render_lazily_activates_real_provider_and_restores_scope(tmp_path):
    from tests.test_voice_body import VoiceServer, provider as http_provider

    async def scenario():
        supervisor, _ = rig(tmp_path, shared=True)
        await supervisor.voice.close()
        server = VoiceServer()
        body = http_provider(server, mode="grounded")
        settings = VoiceSettings(token_file=tmp_path / "token", body_instance_id="character-body", profile_id="character", profile_revision=2)
        supervisor.voice = VoiceController(supervisor, settings, tmp_path, provider=body)
        app = create_control_app(ControlPlane(supervisor, token="runtime-token"))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://runtime",
                headers={"Authorization": "Bearer runtime-token"}) as client:
            # Render is the first request; startup/status/catalog need not have activated anything.
            result = await client.post("/v1/voice/render-jobs", json={"script": SCRIPT, "idempotency_key": "lazy-first"})
            assert result.status_code == 202, result.text
            assert server.payload["script"]["profile"] == {"voice_profile_id": "character", "revision": 2}
            snapshot = (await client.get("/v1/capabilities")).json()
            assert snapshot["bodies"]["voice"]["body"]["state"] == "degraded"
            assert next(item for item in snapshot["catalog"] if item["capability_id"] == VOICE_RENDER)["ready"]
        await supervisor.voice.close()
        restored_body = http_provider(server, mode="grounded")
        supervisor.voice = VoiceController(supervisor, settings, tmp_path, provider=restored_body)
        assert (await supervisor.voice.job("render-1")).state == "queued"
        assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
        await close(supervisor)
    asyncio.run(scenario())


def test_shared_artifact_verification_failure_recovers_readonly(tmp_path):
    async def scenario():
        supervisor, provider = rig(tmp_path, shared=True)
        controller = supervisor.capabilities
        offer = next(item for item in await controller.catalog() if item.capability_id == VOICE_RENDER)
        row = opportunity(controller, offer, "verify-artifact")
        choice = SimpleNamespace(choice=CapabilityChoice(kind="act", offer_id=offer.offer_id, arguments={"script": SCRIPT}))
        await controller.dispatch_voice(row, choice, offer)
        provider.finish("succeeded")
        provider.asset_failure = True
        await controller.reconcile()
        assert controller.calls.get(row["opportunity_id"])["status"] == "uncertain"
        provider.asset_failure = False
        original = provider.manifest_bytes
        bad_manifest = json.loads(original)
        bad_manifest["job_id"] = "unrelated-job"
        provider.manifest_bytes = json.dumps(bad_manifest).encode()
        await controller.reconcile()
        assert controller.calls.get(row["opportunity_id"])["status"] == "uncertain"
        provider.manifest_bytes = original
        await controller.reconcile()
        assert controller.calls.get(row["opportunity_id"])["status"] == "completed"
        assert len(provider.render_calls) == 1
        await close(supervisor)
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["user_activity", "generation"])
def test_shared_supersession_during_provider_preflight_blocks_post(tmp_path, change):
    async def scenario():
        supervisor, provider = rig(tmp_path, shared=True)
        controller = supervisor.capabilities
        offer = next(item for item in await controller.catalog() if item.capability_id == VOICE_RENDER)
        row = opportunity(controller, offer, "superseded-before-post")
        choice = SimpleNamespace(choice=CapabilityChoice(kind="act", offer_id=offer.offer_id, arguments={"script": SCRIPT}))

        def supersede():
            if change == "user_activity":
                controller.activity_revision += 1
            else:
                supervisor.manifest.autonomous_capabilities = supervisor.manifest.autonomous_capabilities.model_copy(
                    update={"quiet_seconds": 90})

        provider.before_post = supersede
        await controller.dispatch_voice(row, choice, offer)
        result = controller.calls.get(row["opportunity_id"])
        assert result["status"] == "rejected" and result["error_type"] == "voice_not_dispatched"
        assert supervisor.voice.attempt(row["voice_idempotency_key"])["status"] == "not_dispatched"
        await controller.reconcile()
        assert provider.render_calls == []
        await close(supervisor)
    asyncio.run(scenario())
