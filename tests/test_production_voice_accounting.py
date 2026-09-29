"""Voice render dispatch is billed once without claiming playback or delivery."""
from __future__ import annotations

import asyncio

import pytest

from harness.production.tool_accounting import ToolAccounting
from harness.runtime.models import RuntimeLifecycle
from harness.runtime.voice import VoiceController, VoiceRuntimeError
from tests.test_production_board_diary_accounting import attach_production, assert_one_cost
from tests.test_voice_runtime import SCRIPT, close, rig


@pytest.mark.parametrize("state,outcome", [("succeeded", "completed"), ("failed", "failed"), ("cancelled", "failed")])
def test_voice_pending_reopen_and_terminal_read_keep_one_cost(tmp_path, state, outcome):
    async def run():
        supervisor, provider = rig(tmp_path)
        production = attach_production(supervisor, tmp_path)
        request = {"script": SCRIPT, "idempotency_key": "production-render"}
        await supervisor.voice.profiles()
        assert not production.store.pending_usage()
        await supervisor.voice.render(request)
        assert_one_cost(production, "voice", "production-render", "unknown")
        original_usage = production.store.pending_usage()
        await supervisor.voice.close()
        production.tool_accounting = ToolAccounting(production.store)
        supervisor.voice = VoiceController(supervisor, supervisor.voice.settings, tmp_path, provider=provider)
        provider.finish(state)
        job = await supervisor.voice.render(request)
        assert job.state == state and len(provider.render_calls) == 1
        assert_one_cost(production, "voice", "production-render", outcome)
        assert production.store.pending_usage() == original_usage
        await close(supervisor)
    asyncio.run(run())


def test_voice_lost_ack_reopen_has_one_unknown_cost_and_never_rerenders(tmp_path):
    async def run():
        supervisor, provider = rig(tmp_path)
        production = attach_production(supervisor, tmp_path)
        provider.delay = 1
        request = {"script": SCRIPT, "idempotency_key": "lost-production-render"}
        with pytest.raises(TimeoutError):
            await supervisor.voice.render(request)
        assert_one_cost(production, "voice", "lost-production-render", "unknown")
        await supervisor.voice.close()
        production.tool_accounting = ToolAccounting(production.store)
        supervisor.voice = VoiceController(supervisor, supervisor.voice.settings, tmp_path, provider=provider)
        with pytest.raises(VoiceRuntimeError, match="voice_dispatch_uncertain"):
            await supervisor.voice.render(request)
        assert len(provider.render_calls) == 1
        assert_one_cost(production, "voice", "lost-production-render", "unknown")
        await close(supervisor)
    asyncio.run(run())


def test_real_voice_provider_http_dispatch_and_cancel_keep_one_render_cost(tmp_path):
    from harness.runtime.voice_config import VoiceSettings
    from tests.test_voice_body import VoiceServer, provider as make_provider, render_request
    async def run():
        supervisor, _ = rig(tmp_path)
        await supervisor.voice.close()
        production = attach_production(supervisor, tmp_path)
        server = VoiceServer()
        provider = make_provider(server)
        settings = VoiceSettings(body_instance_id="character-body", profile_id="character", profile_revision=2,
            token_file=tmp_path / "token", timeout_seconds=1)
        supervisor.voice = VoiceController(supervisor, settings, tmp_path / "wire", provider=provider)
        request = render_request()
        job = await supervisor.voice.render(request)
        assert_one_cost(production, "voice", request.idempotency_key, "unknown")
        assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
        await supervisor.voice.render(request)
        await supervisor.voice.cancel(job.job_id)
        assert_one_cost(production, "voice", request.idempotency_key, "failed")
        assert server.calls.count(("POST", "/voice/v1/render-jobs")) == 1
        await close(supervisor)
    asyncio.run(run())


@pytest.mark.parametrize("change", ["pause", "rest", "exhausted", "disconnect", "epoch"])
def test_voice_late_guard_has_no_cost_and_not_dispatched_retry_is_allowed(tmp_path, change):
    async def run():
        supervisor, provider = rig(tmp_path)
        production = attach_production(supervisor, tmp_path)
        def changing():
            if change == "pause":
                supervisor.lifecycle = RuntimeLifecycle.PAUSED
            elif change == "rest":
                production._body["resting"] = True
            elif change == "exhausted":
                production._body["exhausted"] = True
            elif change == "disconnect":
                production._body_error = "unavailable"
            else:
                production.epoch += 1
        provider.before_post = changing
        request = {"script": SCRIPT, "idempotency_key": "cancelled-production-render"}
        with pytest.raises(VoiceRuntimeError):
            await supervisor.voice.render(request)
        assert not provider.render_calls and not production.store.pending_usage()
        assert production.tool_accounting.get("voice", request["idempotency_key"])["status"] == "cancelled"
        provider.before_post = None
        supervisor.lifecycle = RuntimeLifecycle.RUNNING
        production._body.update(resting=False, exhausted=False)
        production._body_error = None
        await supervisor.voice.render(request)
        assert len(provider.render_calls) == 1
        assert_one_cost(production, "voice", request["idempotency_key"], "unknown")
        await close(supervisor)
    asyncio.run(run())
