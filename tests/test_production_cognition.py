import asyncio
from datetime import datetime, timedelta, timezone
import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.cognitive.production import augment_messages
from harness.config import Settings
from harness.contracts.envelope import DomainCommandEnvelope
from harness.llm.client import ChatResult, OpenAICompatClient
from harness.memory.service import build_inmemory_service
from harness.pipeline import InferencePipeline
from harness.production.config import ProductionConfig
from harness.production.metering import install_metering_routes, production_call_scope
from harness.production.store import ProductionBudgetExceeded, ProductionStore
from harness.runtime.adapters import PipelineCognitiveAdapter
from tests.factories import vaelith


def context(*, autonomous=False):
    return {"schema_version": "production-context-v1", "configuration": ProductionConfig().model_dump(mode="json"),
            "body": {"available": True, "snapshot": {"body_instance_id": "misty", "episode_id": "episode-1"}},
            "autonomous": autonomous, "scope": "local:main", "restricted_dialogue": False,
            "epoch": 0, "runtime_lifecycle": "RUNNING", "authorized_action_context": {"runtime_instance_id": "runtime"}}


def command(identifier="evt-1", session="session", text="hello"):
    now = datetime.now(timezone.utc)
    return DomainCommandEnvelope(command_type="PrepareResponse", event_id=identifier, receive_seq=1,
        correlation_id=identifier, runtime_instance_id="runtime", manifest_revision=1,
        idempotency_key=identifier, issued_at=now, deadline=now + timedelta(seconds=20),
        payload={"mode": "production", "production_context": context(), "session_id": session, "text": text, "channel": "chat"})


class ChoiceModel:
    def __init__(self, kind="reply", started=None, release=None):
        self.kind, self.started, self.release = kind, started, release
        self.calls = 0

    def complete_chat_result(self, messages, **kwargs):
        self.calls += 1
        payload = json.loads(messages[-1]["content"])
        if self.started:
            self.started.set()
            assert self.release.wait(3)
        return ChatResult(finish_reason="stop", content_type="text", text=json.dumps({"kind": self.kind, "reason": "my choice", "text": "hello again" if self.kind == "reply" else None,
            "affect": {"description": "a revisable interpretation", "valence": 0.2, "activation": 0.3,
                       "source_refs": [payload["current_event"]["event_id"]]}}))


def adapter_at(tmp_path, model):
    settings = Settings(cognitive_incremental_persistence_enabled=True, call_log_enabled=False,
                        parser_llm_enabled=False, planner_llm_enabled=False, tier_llm_enabled=False)
    memory = build_inmemory_service(settings=settings, llm=model)
    memory.bootstrap_persona(vaelith())
    adapter = PipelineCognitiveAdapter(InferencePipeline(memory, settings=settings, llm=model), state_path=tmp_path / "owner.json")
    adapter.production_meter = ProductionStore(tmp_path / "budget.sqlite3")
    adapter.production_meter.context = lambda **kwargs: context()
    return adapter


def test_model_can_choose_rest_and_replay_preserves_affect(tmp_path):
    async def run():
        model = ChoiceModel("rest_start")
        adapter = adapter_at(tmp_path, model)
        request = command()
        result = await adapter.prepare_response(request)
        assert result.status == "applied", result.error
        assert result.result.decision.policy_id == "production:rest:start"
        again = await adapter.prepare_response(request)
        assert again.result.prepare_id == result.result.prepare_id
        assert model.calls == 1
        assert adapter.production_affect["session"]["revision"] == 1
        await adapter.close()
        reopened = adapter_at(tmp_path, model)
        assert reopened.production_affect["session"]["source_refs"] == ["evt-1"]
        await reopened.close()
    asyncio.run(run())


def test_slow_model_does_not_hold_owner_lock(tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()
        adapter = adapter_at(tmp_path, ChoiceModel(started=started, release=release))
        work = asyncio.create_task(adapter.prepare_response(command()))
        assert await asyncio.to_thread(started.wait, 2)
        try:
            await asyncio.wait_for(adapter._lock.acquire(), 0.2)
            adapter._lock.release()
        finally:
            release.set()
        assert (await work).status == "applied"
        await adapter.close()
    asyncio.run(run())


def test_conversation_change_fences_late_result(tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()
        adapter = adapter_at(tmp_path, ChoiceModel(started=started, release=release))
        work = asyncio.create_task(adapter.prepare_response(command()))
        assert await asyncio.to_thread(started.wait, 2)
        adapter.pipeline.session_store.append("session", "user", "newer information", event_id="new")
        release.set()
        result = await work
        assert result.status == "failed"
        assert not adapter._prepared
        assert not adapter.production_affect
        await adapter.close()
    asyncio.run(run())


def test_attempt_budget_and_provider_usage_at_transport_boundary(tmp_path):
    store = ProductionStore(tmp_path / "meter.sqlite3", ProductionConfig(autonomous_attempts_per_hour=1))
    settings = Settings(llm_model="test", llm_base_url="https://example.test/v1")
    client = OpenAICompatClient(settings)
    captured = []
    def respond(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json={"choices": [{"message": {"content": "hello"}}],
                                        "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7}})
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(respond))
    with production_call_scope(context(autonomous=True), command_id="automatic", autonomous=True, meter=store):
        result = client.complete_chat_result([{"role": "user", "content": "hello"}])
        assert result.usage.total_tokens == 7
        with pytest.raises(ProductionBudgetExceeded):
            client.complete_chat_result([{"role": "user", "content": "retry"}])
    assert len(captured) == 1
    assert "Current production state" in captured[0]["messages"][0]["content"]
    pending = store.pending_usage()
    assert len(pending) == 1
    assert "episode-1" in json.dumps(pending)
    client.close()


def test_authenticated_rpc_and_user_calls_have_separate_budget(tmp_path):
    store = ProductionStore(tmp_path / "rpc.sqlite3", ProductionConfig(autonomous_attempts_per_hour=1))
    app = FastAPI()
    install_metering_routes(app, SimpleNamespace(production_runtime=SimpleNamespace(store=store), lifecycle="RUNNING"), token="test-token")
    client = TestClient(app)
    data = {"attempt_id": "a", "purpose": "goal", "autonomous": True}
    assert client.post("/v1/production/model-attempts", json=data).status_code == 401
    headers = {"Authorization": "Bearer test-token"}
    assert client.post("/v1/production/model-attempts", json=data, headers=headers).status_code == 200
    assert client.post("/v1/production/model-attempts", json={**data, "attempt_id": "b"}, headers=headers).status_code == 429
    assert client.post("/v1/production/model-attempts", json={**data, "attempt_id": "user", "autonomous": False}, headers=headers).status_code == 200


def test_prompt_projection_does_not_export_internal_configuration():
    value = {**context(), "token": "secret", "database_path": "private.sqlite3"}
    messages = augment_messages([], value)
    assert "secret" not in messages[0]["content"]
    assert "private.sqlite3" not in messages[0]["content"]


def test_real_host_local_production_ingests_before_choice(tmp_path, monkeypatch):
    from tests.test_basic_persona_continuity import _host
    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, ["unused"])
        host.adapter.production_meter = ProductionStore(tmp_path / "budget.sqlite3")
        host.adapter.production_meter.context = lambda **kwargs: context()
        host.chat.pipeline.llm = ChoiceModel()
        try:
            result = await host.prepare_response(command(session=host.chat.active_session_id))
            assert result.status == "applied", result.error
            recalled = host.shared.production_store.recall(audience="local:main", query="hello")
            assert recalled["entries"]
            assert host.adapter.production_affect[host.chat.active_session_id]["revision"] == 1
        finally:
            await host.close()
    asyncio.run(run())


def test_retracted_memory_fences_model_result(tmp_path):
    async def run():
        started, release = threading.Event(), threading.Event()
        adapter = adapter_at(tmp_path, ChoiceModel(started=started, release=release))
        adapter.production_validate_context = lambda segments: False
        work = asyncio.create_task(adapter.prepare_response(command()))
        assert await asyncio.to_thread(started.wait, 2)
        release.set()
        assert (await work).status == "failed"
        assert not adapter._prepared
        await adapter.close()
    asyncio.run(run())


def test_prompt_budget_preserves_persona_and_current_input():
    from harness.cognitive.production import bounded_payload, canonical
    value = {"persona": {"name": "Misty", "history": "continuous"}, "current_event": {"raw_text": "new message"},
        "recent_conversation": [{"text": "old" * 1000} for _ in range(20)],
        "memories": [{"text": "memory" * 1000, "source_ids": [str(i)]} for i in range(20)],
        "source_refs": [str(i) for i in range(20)]}
    selected = bounded_payload(value, maximum_characters=5000)
    assert selected["persona"] == value["persona"] and selected["current_event"] == value["current_event"]
    assert len(canonical(selected)) < 5100
    assert len(value["memories"]) == 20
    with pytest.raises(ValueError, match="exceeds_budget"):
        bounded_payload({"current_event": {"raw_text": "x" * 10001}}, maximum_characters=10000)


def test_action_catalog_requires_runtime_and_provider_authority():
    from harness.cognitive.production import action_catalog
    catalog = action_catalog({"provider_intents": ["control.wait.v1", "invented.root.v1"],
        "permitted_intents": ["control.wait.v1", "invented.root.v1", "process.start.v1"]})
    assert [row["intent_id"] for row in catalog] == ["control.wait.v1"]
    assert "duration_ms" in catalog[0]["arguments_schema"]["properties"]
