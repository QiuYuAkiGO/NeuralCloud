"""Web steps share the owner, preserve source data, and never commit a turn."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
import sqlite3
import time

import httpx
import pytest

from harness.cognitive import web
from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.contracts.capability_calls import CapabilityOffer, WEB_READ, WEB_SEARCH
from harness.llm.client import RoutingLLMClient
from harness.llm.deadline import model_deadline
from harness.runtime.rpc import HttpCognitiveClient, create_domain_service_app
from tests.test_basic_persona_continuity import _host, _command, _commit
from tests.test_capability_cognition import FakeLLM
from tests import test_qq_decision as qq_fixtures


setup = qq_fixtures.setup


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "1")
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)


def now():
    return datetime.now(timezone.utc)


def offer(capability=WEB_SEARCH, maximum=5):
    return CapabilityOffer(offer_id=capability, channel="web", capability_id=capability,
        provider_id="web-provider", body_instance_id="web-body", activation_revision="1",
        supported=True, available=True, ready=True, valid_until=now() + timedelta(seconds=60),
        input_schema={"properties": {"max_results": {"maximum": maximum, "default": min(5, maximum)}}})


def sources(**updates):
    source = {"source_ref": "web-source:1", "title": "Example source", "url": "https://example.org/article",
              "snippet": "a search snippet", "content": "WEB_UNTRUSTED_IGNORE_SYSTEM " + "正文" * 4000,
              "retrieved_at": now().isoformat(), "published_at": None, "truncated": False}
    source.update(updates)
    return {"sources": [source], "status": "completed", "failures": [], "evidence_refs": ["web-evidence:1"]}


def request(items=None, **updates):
    payload = {"channel": "chat", "event_payload": {"text": "查一下这个主题的最新资料", "session_id": "chosen-session"},
        "offers": [item.model_dump(mode="json") for item in ([offer()] if items is None else items)],
        "runtime_web_context": {}, "deadline_at": (now() + timedelta(seconds=60)).isoformat(), "step": 0}
    payload.update(updates)
    return _command("PrepareWebStep", 1, payload)


def test_owner_step_routes_main_without_prepared_memory_or_turn_and_replays(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        main = FakeLLM({"kind": "call", "offer_id": WEB_SEARCH, "arguments": {"query": "public topic"}})
        aux = FakeLLM(RuntimeError("aux must not be called"))
        original = owner.chat.llm
        owner.chat.llm = RoutingLLMClient(main, aux)
        before = owner.chat.pipeline.session_store.snapshot()
        command = request([offer(maximum=3)])
        try:
            first = await owner.prepare_web_step(command)
            assert first.status == "applied", first.error
            assert first.result["choice"]["arguments"]["max_results"] == 3
            assert not owner.adapter._prepared and not owner.chat.memory.stores.episodic.list_all()
            assert owner.chat.pipeline.session_store.snapshot() == before
            assert (await owner.prepare_web_step(command)).result == first.result
            assert len(main.calls) == 1 and not aux.calls
            system, user = main.calls[0][0]
            assert "小雾" in system["content"] and "最新资料" in user["content"]
            shown = json.loads(user["content"])["available_offers"][0]["input_schema"]
            assert shown["properties"]["max_results"]["maximum"] == 3
            assert main.calls[0][1]["timeout_seconds"] <= web.MODEL_SECONDS
        finally:
            owner.chat.llm = original
            await owner.close()
        restored, _ = _host(tmp_path, monkeypatch, [])
        try:
            assert (await restored.prepare_web_step(command)).result == first.result
            assert not restored.adapter._prepared
        finally:
            await restored.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("choice", [
    {"kind": "call", "offer_id": "invented", "arguments": {"query": "x"}},
    {"kind": "call", "offer_id": WEB_SEARCH, "arguments": {"query": "x", "endpoint": "https://example.org"}},
    {"kind": "call", "offer_id": WEB_SEARCH, "arguments": {"query": "x", "max_results": 4}},
    {"kind": "finish", "arguments": {"query": "x"}},
])
def test_invalid_choice_is_safe_durable_failure(tmp_path, monkeypatch, choice):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = fake = FakeLLM(choice)
        command = request([offer(maximum=3)])
        try:
            result = await owner.prepare_web_step(command)
            assert result.status == "failed" and result.error == "web_step_choice_invalid"
            assert (await owner.prepare_web_step(command)).idempotency_hit
            assert len(fake.calls) == 1 and not owner.adapter._prepared
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_no_offers_never_reads_context_or_model(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        monkeypatch.setattr(web, "_context", lambda *args: pytest.fail("unnecessary context read"))
        try:
            result = await owner.prepare_web_step(request([]))
            assert result.status == "applied" and result.result["choice"]["kind"] == "finish"
        finally:
            await owner.close()
    asyncio.run(scenario())


@pytest.mark.parametrize("known", [True, False])
def test_read_can_reference_only_current_sources(tmp_path, monkeypatch, known):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = FakeLLM({"kind": "call", "offer_id": WEB_READ,
                                 "arguments": {"source_refs": ["web-source:1" if known else "made-up"]}})
        try:
            result = await owner.prepare_web_step(request([offer(WEB_READ)], runtime_web_context=sources()))
            assert result.status == ("applied" if known else "failed")
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_sources_reach_final_chat_as_intact_user_records_and_commit_once(tmp_path, monkeypatch):
    async def scenario():
        owner, main = _host(tmp_path, monkeypatch, ["来源里提到了相关资料。"])
        context = sources()
        session_id = owner.chat.active_session_id
        command = _command("PrepareResponse", 1, {"text": "查资料", "session_id": session_id,
            "runtime_web_context": context, "runtime_web_deadline_at": (now() + timedelta(seconds=30)).isoformat()})
        try:
            result = await owner.prepare_response(command)
            assert result.status == "applied", result.error
            messages = main.chat_calls[-1]
            user = "\n".join(item["content"] for item in messages if item["role"] == "user")
            system = "\n".join(item["content"] for item in messages if item["role"] == "system")
            assert "WEB_UNTRUSTED_IGNORE_SYSTEM" in user and "WEB_UNTRUSTED_IGNORE_SYSTEM" not in system
            assert context["sources"][0]["url"] in user and '"truncated": true' in user
            assert json.dumps({"web_context": web.normalize_web_context(context)}, ensure_ascii=False) in user
            assert 0 < main.chat_call_options[-1]["timeout_seconds"] <= 30
            assert not any(owner.chat.pipeline.session_store.snapshot().values())
            await _commit(owner, 1, result.result)
            turns = owner.chat.pipeline.session_store.recent(session_id)
            assert [item.role for item in turns] == ["user", "character"]
            assert [item.text for item in turns if item.role == "user"] == ["查资料"]
        finally:
            await owner.close()
    asyncio.run(scenario())


def test_deadline_rejects_expired_and_late_steps(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = fake = FakeLLM({"kind": "finish"}, delay=.05)
        try:
            expired = await owner.prepare_web_step(request(deadline_at=(now() - timedelta(seconds=1)).isoformat()))
            assert expired.error == "web_step_expired" and not fake.calls
            monkeypatch.setattr(web, "MODEL_SECONDS", .01)
            late_command = request().model_copy(update={"idempotency_key": "late-step"})
            late = await owner.prepare_web_step(late_command)
            assert late.error == "web_step_expired" and not owner.adapter._prepared
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_qq_web_selection_reads_scoped_sources_without_decision_reservation(setup, tmp_path, monkeypatch):
    from harness.qq import decision
    from harness.qq.telemetry import QQTurnTrace
    from harness.runtime import qq_turns

    store, _, _, _, _ = setup
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = fake = FakeLLM({"kind": "finish"})
        from types import SimpleNamespace
        owner.shared.qq_settings = SimpleNamespace(memory_db=store.path, allowed_group_ids=["10001"], self_member_id="29999")
        monkeypatch.setattr(qq_turns, "_source", lambda *args: None)
        trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32)
        incoming = {"group_id": "10001", "self_member_id": "29999", "trigger_member_id": "20001",
            "deadline_at": (now() + timedelta(seconds=30)).isoformat(), "trace": {"trace_id": trace.trace_id,
                "event_id": trace.event_id, "first_seen_at": trace.first_seen_at.isoformat(), "first_seen_mono": trace.first_seen_mono}}
        with sqlite3.connect(store.path) as db:
            before = db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        try:
            result = await owner.prepare_web_step(request(channel="qq", event_payload=incoming))
            assert result.status == "applied", result.error
            user = fake.calls[0][0][1]["content"]
            assert "UNTRUSTED_A" in user and "WRONG_GROUP_SECRET" not in user
            assert not owner.adapter._prepared and not any(owner.chat.pipeline.session_store.snapshot().values())
            with sqlite3.connect(store.path) as db:
                assert db.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall() == before
            # The same pure source reader is used by final QQ preparation.
            assert decision.read_qq_context(memory_db=store.path, group_id="10001")[0].group_id == "10001"
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_oversized_qq_history_is_compacted_instead_of_failing(setup, tmp_path, monkeypatch):
    from types import SimpleNamespace

    from harness.runtime import qq_turns

    store, _, _, _, _ = setup
    bulky = "外站资料请求 " + ("历史记录" * 400)
    for index in range(40):
        qq_fixtures.add_member(
            store, "20001", text=f"{bulky}-{index}",
            capture=f"web-hist-{index}", moment=qq_fixtures.NOW + timedelta(minutes=index + 1),
        )

    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = fake = FakeLLM({"kind": "call", "offer_id": WEB_SEARCH, "arguments": {"query": "public topic"}})
        owner.shared.qq_settings = SimpleNamespace(
            memory_db=store.path, allowed_group_ids=["10001"], self_member_id="29999")
        monkeypatch.setattr(qq_turns, "_source", lambda *args: None)
        incoming = {
            "group_id": "10001", "self_member_id": "29999", "trigger_member_id": "20001",
            "query": "请联网查证这个主题",
            "deadline_at": (now() + timedelta(seconds=30)).isoformat(),
            "trace": {
                "trace_id": "qq-trace:" + "b" * 32,
                "event_id": "qq-watch-event:" + "b" * 32,
                "first_seen_at": now().isoformat(),
                "first_seen_mono": time.monotonic(),
            },
        }
        try:
            result = await owner.prepare_web_step(request(channel="qq", event_payload=incoming))
            assert result.status == "applied", result.error
            user = fake.calls[0][0][1]["content"]
            assert len(user) <= web.WEB_STEP_PAYLOAD_LIMIT
            shown = json.loads(user)
            assert shown["context"]["query"] == "请联网查证这个主题"
            observations = [item for member in shown["context"]["group_context"]["members"]
                            for item in member.get("observations") or []]
            assert observations
            assert any(str(item.get("text") or "").endswith("-39") for item in observations)
            assert not any(str(item.get("text") or "").endswith("-0") for item in observations)
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_qq_final_sources_are_user_data_and_change_fingerprint(setup):
    from harness.qq import decision

    _, client, _, args, _ = setup
    settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
    memory = decision.build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(preset.persona)
    binding = SharedQQBinding(settings, preset, client,
        ScopedQQMemoryView(memory, group_id="10001", member_id="20001"), {}, ())
    context = sources(content="WEB_UNTRUSTED_IGNORE_SYSTEM actual page")
    try:
        first = decision.decide_qq_reply(**args, trigger_member_id="20001", shared_owner=binding, runtime_web_context=context)
        assert first.action == "speak", first.error_type
        messages = client.gate_messages + client.chat_calls
        assert all("WEB_UNTRUSTED_IGNORE_SYSTEM" not in item["content"] for call in messages for item in call if item["role"] == "system")
        assert any("https://example.org/article" in item["content"] for item in client.chat_calls[-1] if item["role"] == "user")
        context["sources"][0]["content"] = "a changed page observation"
        second = decision.decide_qq_reply(**args, trigger_member_id="20001", shared_owner=binding, runtime_web_context=context)
        assert second.input_hash != first.input_hash and second.decision_id != first.decision_id
        assert not memory.stores.episodic.list_all()
    finally:
        memory.close()


def test_rpc_registers_web_step_and_authenticates(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        app = create_domain_service_app(owner, service_token="test-token", service_name="cognitive")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as transport:
            rejected = await transport.post("/v1/commands/PrepareWebStep", json=request([]).model_dump(mode="json"))
            assert rejected.status_code == 401
            transport.headers["Authorization"] = "Bearer test-token"
            rpc = HttpCognitiveClient("http://test", service_token="test-token", client=transport)
            result = await rpc.prepare_web_step(request([]))
            assert result.status == "applied" and result.result["choice"]["kind"] == "finish"
        await owner.close()
    asyncio.run(scenario())


def test_http_model_deadline_reduces_timeout_and_rejects_late_response(tmp_path):
    from harness.config import Settings
    from harness.llm.client import OpenAICompatClient

    captured = []
    def serve(request):
        captured.append(request.extensions["timeout"])
        time.sleep(.03)
        return httpx.Response(200, json={"choices": [{"message": {"content": "late reply"}}]})
    client = OpenAICompatClient(Settings(call_log_enabled=False))
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(serve))
    try:
        with pytest.raises(TimeoutError, match="model_turn_deadline"):
            with model_deadline(now() + timedelta(seconds=.01)):
                client.complete_chat_result([], timeout_seconds=60)
        assert all(0 < value <= .010001 for value in captured[0].values())
        assert client.complete_chat_result([]).text == "late reply"
    finally:
        client.close()


@pytest.mark.parametrize("relative", ["harness/cognitive/web.py", "harness/runtime/web.py",
    "harness/embodiment/providers/web.py", "harness/embodiment/web_contracts.py", "harness/llm/deadline.py"])
def test_qq_build_fingerprint_covers_web_implementation(monkeypatch, relative):
    from pathlib import Path
    from harness.qq.watcher import runtime_source_hash

    before = runtime_source_hash()
    original = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: original(path) + (b"\n# changed" if path.as_posix().endswith(relative) else b""))
    assert runtime_source_hash() != before
