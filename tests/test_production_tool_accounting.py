"""NC-owned operations have durable costs separate from models and delivery."""

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harness.production.runtime import ProductionRuntime
from harness.production.store import ProductionStore
from harness.production.tool_accounting import ToolAccounting
from harness.research.manifest import load_experiment_manifest
from harness.runtime.models import utc_now
from tests.test_server_runtime import FakeProvider, offer as server_offer, rig as server_rig
from tests.test_web_runtime import offer as web_offer, rig as web_rig


class Body:
    def __init__(self, identity):
        self.identity = identity
        self.receipts = []

    async def snapshot(self):
        return {"body_instance_id": self.identity, "episode_id": "original-episode", "sample_sequence": 1,
            "captured_at": utc_now().isoformat(), "physiology_mode": "production_v1", "energy": {},
            "exhausted": False, "resting": False}

    async def record_usage(self, request):
        self.receipts.append(request)
        return SimpleNamespace(model_dump=lambda **_: {"inference_id": request.inference_id, "source": request.source})


def activate(supervisor, path, channel):
    manifest = load_experiment_manifest(Path("presets/runtime/production-embodied-v1.example.json"))
    supervisor.manifest = manifest.model_copy(update={
        channel + "_access": getattr(supervisor.manifest, channel + "_access"),
        "autonomous_capabilities": supervisor.manifest.autonomous_capabilities})
    supervisor.state_dir = path / "production"
    supervisor.embodiment = SimpleNamespace(provider=None)
    runtime = supervisor.production_runtime = ProductionRuntime(supervisor)
    body = Body(supervisor.manifest.embodiment.body_instance_id)
    runtime.attach_body_provider(body)
    return runtime, body


@pytest.mark.parametrize("channel", ["web", "server"])
def test_actual_tool_call_and_replay_bill_once_without_model_budget(tmp_path, channel):
    async def run():
        supervisor, wire = web_rig(tmp_path) if channel == "web" else server_rig(tmp_path)
        runtime, body = activate(supervisor, tmp_path, channel)
        controller = getattr(supervisor, channel)
        try:
            offer = await (web_offer(supervisor) if channel == "web" else server_offer(supervisor))
            arguments = {"query": "fixture"} if channel == "web" else {"command": "uptime"}
            first = await controller.execute("operation-1", offer, arguments, deadline=utc_now() + timedelta(seconds=10))
            replay = await controller.execute("operation-1", offer, arguments, deadline=utc_now() + timedelta(seconds=10))
            assert first == replay and len(wire if channel == "web" else wire.runs) == 1
            assert runtime.store.budget()["used"] == 0
            usage = runtime.store.pending_usage()
            assert len(usage) == 1 and usage[0]["usage"]["source"] == "tool"
            assert usage[0]["usage"]["episode_id"] == "original-episode"
            assert runtime.tool_accounting.get(channel, "operation-1")["outcome"] == "completed"
            await runtime.flush_usage()
            await runtime.flush_usage()
            assert len(body.receipts) == 1
            assert (body.receipts[0].source, body.receipts[0].metering, body.receipts[0].quantity) == ("tool", "operation", 1)
            assert body.receipts[0].cost_class == "tool_execution"
        finally:
            await controller.close()

    asyncio.run(run())


@pytest.mark.parametrize("channel", ["web", "server"])
def test_lost_tool_result_records_dispatch_without_inventing_success(tmp_path, channel):
    async def run():
        async def timeout(request):
            raise httpx.ReadTimeout("response lost", request=request)

        class LostServer(FakeProvider):
            async def run(self, request, **kwargs):
                self.runs.append(request)
                raise TimeoutError("response lost")

        supervisor, _ = web_rig(tmp_path, handler=timeout) if channel == "web" else server_rig(tmp_path, provider=LostServer())
        runtime, _ = activate(supervisor, tmp_path, channel)
        controller = getattr(supervisor, channel)
        try:
            offer = await (web_offer(supervisor) if channel == "web" else server_offer(supervisor))
            with pytest.raises(Exception):
                await controller.execute("unknown", offer, {"query": "fixture"} if channel == "web" else {"command": "uptime"},
                    deadline=utc_now() + timedelta(seconds=10))
            row = runtime.tool_accounting.get(channel, "unknown")
            assert row["outcome"] == "unknown" and row["evidence_refs"] == "[]"
            assert runtime.store.pending_usage_count() == 1 and runtime.store.budget()["used"] == 0
        finally:
            await controller.close()

    asyncio.run(run())


def test_web_provider_final_guard_rejection_does_not_create_operation_cost(tmp_path, monkeypatch):
    async def run():
        from harness.embodiment.web_contracts import WebError

        supervisor, wire = web_rig(tmp_path)
        runtime, _ = activate(supervisor, tmp_path, "web")
        try:
            offer = await web_offer(supervisor)
            async def reject(capability):
                raise WebError("web_capability_unavailable")
            monkeypatch.setattr(supervisor.web.provider, "_guard", reject)
            with pytest.raises(WebError):
                await supervisor.web.execute("not-sent", offer, {"query": "fixture"}, deadline=utc_now() + timedelta(seconds=10))
            assert wire == [] and runtime.store.pending_usage_count() == 0
            assert runtime.tool_accounting.get("web", "not-sent")["status"] == "cancelled"
        finally:
            await supervisor.web.close()

    asyncio.run(run())


def test_crash_recovery_preserves_one_dispatch_and_later_evidence_without_rebilling(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    tools = ToolAccounting(store)
    binding = {"body_instance_id": "body", "episode_id": "original"}
    tools.begin("web", "one", "web.search.v1", binding=binding)
    tools.dispatched("web", "one")
    tools.begin("web", "not-sent", "web.search.v1", binding=binding)
    assert store.pending_usage_count() == 1
    reopened = ToolAccounting(ProductionStore(store.path))
    assert reopened.get("web", "one")["outcome"] == "unknown"
    assert reopened.get("web", "not-sent")["status"] == "prepared"
    original = store.pending_usage()[0]
    reopened.finish("web", "one", outcome="completed", evidence_refs=["verified-result"])
    assert store.pending_usage() == [original] and store.budget()["used"] == 0
    assert reopened.get("web", "one")["outcome"] == "completed"
    with pytest.raises(ValueError, match="identity_conflict"):
        reopened.begin("web", "one", "web.read.v1", binding=binding)
    for excluded in ("qq", "desktop", "mistyai"):
        with pytest.raises(ValueError, match="identity_invalid"):
            reopened.begin(excluded, "one", "send", binding=binding)


def test_cancelled_before_dispatch_can_retry_with_new_verified_body(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    tools = ToolAccounting(store)
    tools.begin("voice", "same-key", "voice.render", binding={"body_instance_id": "body", "episode_id": "old"})
    tools.cancel("voice", "same-key")
    assert store.pending_usage_count() == 0
    tools.begin("voice", "same-key", "voice.render", binding={"body_instance_id": "body", "episode_id": "new"})
    tools.dispatched("voice", "same-key")
    tools.finish("voice", "same-key", outcome="completed", evidence_refs=["verified-job"])
    assert store.pending_usage()[0]["usage"]["episode_id"] == "new"
    tools.begin("voice", "same-key", "voice.render", binding={"body_instance_id": "body", "episode_id": "other"})
    tools.finish("voice", "same-key", outcome="completed", evidence_refs=["verified-job"])
    assert len(store.pending_usage()) == 1 and store.pending_usage()[0]["usage"]["episode_id"] == "new"
