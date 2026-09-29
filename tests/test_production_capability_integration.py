"""Production Runtime commands through the real Cognitive host and model transport."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import json
from types import SimpleNamespace

import httpx
import pytest

from harness.contracts.capability_calls import CapabilityOffer, QQ_READ
from harness.llm.client import OpenAICompatClient
from harness.production.config import ProductionConfig
from harness.research.manifest import AutonomousCapabilitiesCondition
from harness.runtime import PassiveHomeostasisAdapter, RuntimeSupervisor
from harness.runtime.models import RuntimeEventType, utc_now
from tests.test_basic_persona_continuity import _host
from tests.test_runtime_supervisor import _manifest


@pytest.mark.parametrize("invalidation", [None, "runtime_epoch", "budget_after_queue"])
def test_production_capability_uses_real_host_budget_and_runtime_fence(tmp_path, monkeypatch, invalidation):
    class Body:
        async def snapshot(self):
            return {"body_instance_id": "test-body", "episode_id": "episode-1", "sample_sequence": 1,
                "physiology_mode": "production_v1", "captured_at": utc_now().isoformat(),
                "resting": False, "exhausted": False, "energy": {"level": 65, "capacity": 100}}

    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path / "owner", monkeypatch, ["unused"])
        original_llm = host.chat.llm
        client = OpenAICompatClient(host.chat.settings.model_copy(update={
            "llm_model": "scripted-production-test", "llm_base_url": "https://example.test/v1"}))
        host.chat.llm = client
        manifest = _manifest().model_copy(update={"production": ProductionConfig(),
            "autonomous_capabilities": AutonomousCapabilitiesCondition(enabled=True,
                owners={"qq": "shared"}, allowed_capabilities=(QQ_READ,))})
        supervisor = RuntimeSupervisor(manifest=manifest, cognitive=host,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
        runtime = supervisor.production_runtime
        runtime.attach_body_provider(Body())
        fence_reads, requests = [], []

        def current_context(*args, **kwargs):
            fence_reads.append((args, kwargs))
            return runtime.context_snapshot(*args, **kwargs)

        host.adapter.production_meter = SimpleNamespace(reserve_attempt=runtime.store.reserve_attempt,
            record_usage=runtime.store.record_usage, context=current_context)

        def respond(request):
            requests.append(json.loads(request.content))
            if invalidation == "runtime_epoch":
                runtime.epoch += 1
            return httpx.Response(200, json={"choices": [{"message": {"content": '{"kind":"wait"}'},
                "finish_reason": "stop"}], "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})

        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(respond))
        supervisor.qq_capability_bridge = SimpleNamespace(catalog=lambda: [CapabilityOffer(
            offer_id="qq:read:test", channel="qq", capability_id=QQ_READ, provider_id="qq-onebot",
            body_instance_id="qq-test", activation_revision="test-revision", scope={"group_id": "123"},
            supported=True, available=True, ready=True, valid_until=utc_now() + timedelta(seconds=30)
        ).model_dump(mode="json")])
        try:
            await supervisor.boot_and_recover()
            supervisor.scheduler.suspend()
            await supervisor.capabilities.production_heartbeat()
            event = await supervisor.queue.get(timeout=0)
            assert event is not None and event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            assert row["homeostatic_summary"] == {}
            assert row["production_context"]["body"]["available"]
            if invalidation == "budget_after_queue":
                # Another autonomous operation can consume the last slots after
                # an opportunity is queued. The actual transport must reserve.
                for number in range(12):
                    runtime.store.reserve_attempt(f"other-operation-{number}", purpose="other autonomous operation")
            result = await supervisor.dispatch(event)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            if invalidation is None:
                assert result.status == "completed", (result.detail, row)
                assert row["status"] == "wait"
                assert row["decision"]["choice"]["kind"] == "wait"
            else:
                assert result.status == "failed", (result.detail, row)
                assert row["status"] == "error"
                assert "decision" not in row
            expected_calls = 0 if invalidation == "budget_after_queue" else 1
            assert len(requests) == expected_calls
            assert len(fence_reads) == expected_calls
            assert runtime.store.budget()["used"] == (12 if invalidation == "budget_after_queue" else 1)
            usage = runtime.store.pending_usage()
            assert len(usage) == expected_calls
            if requests:
                assert "Current production state" in requests[0]["messages"][0]["content"]
                assert "homeostatic_summary" not in json.dumps(requests[0]["messages"])
                assert usage[0]["usage"]["raw_usage"]["total_tokens"] == 7
                assert usage[0]["usage"]["episode_id"] == "episode-1"
            # Replaying terminal work cannot create another model attempt.
            await supervisor.capabilities.dispatch(event, 999)
            assert len(requests) == expected_calls
        finally:
            host.chat.llm = original_llm
            client.close()
            await supervisor.stop()

    asyncio.run(run())
