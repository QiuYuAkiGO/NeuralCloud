"""Real provider transport with isolated Runtime authority, quota and evidence."""

import asyncio
from datetime import timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.embodiment.providers.web import TavilyWebProvider
from harness.embodiment.web_contracts import WebError
from harness.research.manifest import EmbodimentCondition, WebAccessCondition, load_experiment_manifest
from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.web import READ, SEARCH, WebController
from harness.runtime.web_config import WebSettings
from harness.runtime.web_dialogue import _preparation_failure_code, prepare_web_context


def rig(tmp_path, *, quota=100, handler=None):
    base = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    access = WebAccessCondition(enabled=True, conversation_enabled=True)
    auto = base.autonomous_capabilities.model_copy(update={"enabled": True,
        "owners": {"web": "shared"}, "allowed_capabilities": (SEARCH, READ)})
    manifest = base.model_copy(update={"web_access": access, "autonomous_capabilities": auto})
    sup = SimpleNamespace(manifest=manifest, lifecycle=RuntimeLifecycle.RUNNING, runtime_instance_id="runtime-1",
        capabilities=SimpleNamespace(activity_revision=1), _cognitive_snapshot=SimpleNamespace(revision=0))
    requests = []

    async def transport(request):
        requests.append(request)
        if handler:
            return await handler(request)
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "Evidence", "url": "https://example.com/article",
                "content": "Returned snippet"}], "request_id": "provider-1", "usage": {"credits": 1}})
        return httpx.Response(200, json={"results": [{"url": "https://example.com/article",
            "raw_content": "BODY_EVIDENCE_TEXT"}], "failed_results": [], "request_id": "provider-2"})

    settings = WebSettings(daily_requests=quota, autonomous_daily_requests=min(20, quota))
    provider = TavilyWebProvider(EmbodimentCondition(mode="observe", body_instance_id="web-01",
        capability_requirements=access.capability_requirements), settings.base_url, "synthetic-web-key",
        "web-01", transport=httpx.MockTransport(transport))
    sup.web = WebController(sup, settings, tmp_path, provider=provider)
    return sup, requests


async def offer(sup, capability=SEARCH, *, scope="conversation", autonomous=False):
    return next(item for item in await sup.web.catalog(scope, autonomous=autonomous) if item.capability_id == capability)


def test_search_read_bound_evidence_replay_and_no_status_requests(tmp_path):
    async def run():
        sup, requests = rig(tmp_path)
        web = sup.web
        try:
            await web.status()
            search = await offer(sup)
            assert requests == [] and search.ready
            result = await web.execute("first", search, {"query": "evidence"}, deadline=utc_now() + timedelta(seconds=10))
            assert result.sources[0].source_ref and result.evidence_refs
            assert len(requests) == 1
            replay = await web.execute("first", search, {"query": "evidence"}, deadline=utc_now() + timedelta(seconds=10))
            assert replay == result and len(requests) == 1
            with pytest.raises(WebError, match="web_call_conflict"):
                await web.execute("first", search, {"query": "different"}, deadline=utc_now() + timedelta(seconds=10))
            read = await offer(sup, READ)
            assert read.ready
            body = await web.execute("read", read, {"source_refs": [result.sources[0].source_ref]},
                                     deadline=utc_now() + timedelta(seconds=10))
            assert body.sources[0].content == "BODY_EVIDENCE_TEXT"
            assert web.context("conversation")["sources"][0]["title"] == "Evidence"
            assert (await web.status())["usage"]["requests"] == 2
            assert requests[1].url.path == "/extract"
        finally:
            await web.close()
    asyncio.run(run())


def test_foreign_sources_and_private_user_links_never_dispatched(tmp_path):
    async def run():
        sup, requests = rig(tmp_path)
        try:
            sup.web.register_urls("a", "读 https://example.com/article，以及 http://127.0.0.1/private")
            refs = [s.source_ref for s in sup.web.calls.sources("a")]
            assert len(refs) == 1
            context = sup.web.context("a")
            assert context["sources"][0]["origin"] == "user_link" and not context["evidence_refs"]
            other = await offer(sup, READ, scope="b")
            with pytest.raises(WebError, match="web_source_not_in_scope"):
                await sup.web.execute("cross", other, {"source_refs": refs}, deadline=utc_now() + timedelta(seconds=10))
            assert requests == []
        finally:
            await sup.web.close()
    asyncio.run(run())


def test_quota_is_reserved_before_concurrent_calls_and_survives_restart(tmp_path):
    async def run():
        sup, requests = rig(tmp_path, quota=1)
        item = await offer(sup)
        results = await asyncio.gather(*(sup.web.execute(str(i), item, {"query": "one"},
            deadline=utc_now() + timedelta(seconds=10)) for i in range(2)), return_exceptions=True)
        assert sum(isinstance(result, WebError) for result in results) == 1
        assert len(requests) == 1
        await sup.web.close()
        restored, requests2 = rig(tmp_path, quota=1)
        try:
            assert not (await offer(restored)).ready
            assert (await restored.web.status())["usage"]["requests"] == 1
            assert requests2 == []
        finally:
            await restored.web.close()
    asyncio.run(run())


def test_cancelled_post_is_uncertain_and_never_replayed(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        async def blocked(request):
            started.set()
            await release.wait()
            return httpx.Response(200, json={"results": []})

        sup, requests = rig(tmp_path, handler=blocked)
        item = await offer(sup)
        task = asyncio.create_task(sup.web.execute("cancelled", item, {"query": "x"}, deadline=utc_now() + timedelta(seconds=10)))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sup.web.calls.call("cancelled")["status"] == "uncertain"
        await sup.web.close()
        restored, requests2 = rig(tmp_path)
        try:
            with pytest.raises(WebError):
                await restored.web.execute("cancelled", await offer(restored), {"query": "x"}, deadline=utc_now() + timedelta(seconds=10))
            assert len(requests) == 1 and requests2 == []
        finally:
            await restored.web.close()
    asyncio.run(run())


def test_inflight_revocation_discards_evidence(tmp_path):
    async def run():
        changed = False

        async def handler(request):
            nonlocal changed
            changed = True
            return httpx.Response(200, json={"results": [{"title": "Late", "url": "https://example.com/late", "content": "LATE"}]})

        sup, _ = rig(tmp_path, handler=handler)
        try:
            with pytest.raises(WebError, match="web_late_result"):
                await sup.web.execute("late", await offer(sup), {"query": "x"}, deadline=utc_now() + timedelta(seconds=10),
                                      guard=lambda: changed)
            assert sup.web.calls.sources("conversation") == []
            assert (await sup.web.status())["usage"]["requests"] == 1
        finally:
            await sup.web.close()
    asyncio.run(run())


def dialogue_driver(sup):
    decisions = []

    async def prepare(command):
        decisions.append(command)
        sources = command.payload["runtime_web_context"].get("sources", [])
        if not sources:
            choice = {"kind": "call", "offer_id": "web:" + SEARCH, "arguments": {"query": "find evidence"}}
        elif not sources[0]["content"]:
            choice = {"kind": "call", "offer_id": "web:" + READ, "arguments": {"source_refs": [sources[0]["source_ref"]]}}
        else:
            choice = {"kind": "finish"}
        return SimpleNamespace(result={"choice": choice})

    sup.cognitive = SimpleNamespace(prepare_web_step=prepare)

    def command(event, *, command_type, idempotency_key, expected_revision, payload):
        now = utc_now()
        return DomainCommandEnvelope(command_id=idempotency_key, command_type=command_type,
            event_id=event.event_id, receive_seq=1, correlation_id=event.event_id, runtime_instance_id="runtime-1",
            manifest_revision=1, idempotency_key=idempotency_key, issued_at=now, deadline=now + timedelta(seconds=30), payload=payload)

    async def invoke(*, method, envelope, **kwargs):
        return await method(envelope)

    sup._make_command = command
    sup._invoke_domain = invoke
    return decisions


def test_web_preparation_failure_preserves_step_error_code():
    assert _preparation_failure_code(WebError("web_choice_invalid")) == "web_choice_invalid"
    assert _preparation_failure_code(RuntimeError(
        "cognitive PrepareWebStep returned failed: web_step_context_too_large"
    )) == "web_step_context_too_large"
    assert _preparation_failure_code(RuntimeError(
        "cognitive PrepareWebStep returned conflict: expected revision 1, current 2"
    )) == "web_step_revision_conflict"


def test_dialogue_search_read_finish_and_replay_does_not_repeat(tmp_path):
    async def run():
        sup, requests = rig(tmp_path)
        decisions = dialogue_driver(sup)
        event = SimpleNamespace(event_id="dialogue-1", payload={"text": "联网查证", "runtime_web_context": {"secret": "spoofed"}})
        try:
            context, deadline = await prepare_web_context(sup, event)
            assert context["sources"][0]["content"] == "BODY_EVIDENCE_TEXT"
            assert len(requests) == 2 and len(decisions) == 3
            assert "spoofed" not in json.dumps(decisions[0].payload)
            assert (deadline - utc_now()).total_seconds() <= 60
            again, same_deadline = await prepare_web_context(sup, event)
            assert again == context and same_deadline == deadline
            assert len(requests) == 2 and len(decisions) == 3
        finally:
            await sup.web.close()
    asyncio.run(run())


def test_short_qq_deadline_reserves_final_generation_and_no_network(tmp_path):
    async def run():
        sup, requests = rig(tmp_path)
        decisions = dialogue_driver(sup)
        event = SimpleNamespace(event_id="qq-1", payload={"query": "查资料", "group_id": "g"})
        end = utc_now() + timedelta(seconds=20)
        try:
            _, actual = await prepare_web_context(sup, event, channel="qq", deadline=end)
            assert actual == end and not requests and not decisions
        finally:
            await sup.web.close()
    asyncio.run(run())


def test_partial_extraction_is_visible_to_final_dialogue_and_connection_test(tmp_path):
    async def handler(request):
        if request.url.path == "/search":
            return httpx.Response(200, json={"results": [{"title": "Example", "url": "https://example.com/article", "content": "Snippet only"}]})
        return httpx.Response(200, json={"results": [], "failed_results": [{"url": "https://example.com/article", "error": "Cannot extract"}]})

    async def run():
        sup, requests = rig(tmp_path, handler=handler)
        decisions = dialogue_driver(sup)
        try:
            context, _ = await prepare_web_context(sup, SimpleNamespace(event_id="partial", payload={"text": "search"}))
            assert context["status"] == "partial" and context["failures"]
            assert context["sources"][0]["content"] == ""
            assert decisions[-1].payload["runtime_web_context"]["failures"]
            sup.web.test_provider_factory = lambda settings, key: TavilyWebProvider(
                EmbodimentCondition(mode="observe", body_instance_id="web-01", capability_requirements=sup.manifest.web_access.capability_requirements),
                settings.base_url, key, "web-01", transport=httpx.MockTransport(handler))
            result = await sup.web.test(sup.web.settings, "synthetic-test-key")
            assert result["status"] == "partial" and len(result["checks"]) == 2
            assert result["activated"] is False
            assert len(requests) == 2
        finally:
            await sup.web.close()
    asyncio.run(run())


def test_journal_failure_does_not_erase_completed_post_or_replay_network(tmp_path):
    async def run():
        sup, requests = rig(tmp_path)

        def unavailable(*args, **kwargs):
            raise OSError("test journal unavailable")

        sup._append_journal = unavailable
        try:
            selected = await offer(sup)
            with pytest.raises(OSError):
                await sup.web.execute("journal", selected, {"query": "x"}, deadline=utc_now() + timedelta(seconds=10))
            assert sup.web.calls.call("journal")["status"] == "completed"
            replay = await sup.web.execute("journal", selected, {"query": "x"}, deadline=utc_now() + timedelta(seconds=10))
            assert replay.sources and len(requests) == 1
        finally:
            await sup.web.close()
    asyncio.run(run())


def test_disabled_manifest_canonical_hash_compatible():
    manifest = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    assert "web_access" not in json.loads(manifest.canonical_json())
    assert "web" not in manifest.autonomous_capabilities.owners


def test_real_shared_cognition_search_read_answer_commits_once(tmp_path, monkeypatch):
    from harness.core.inference import TokenUsage
    from harness.llm.character import CharacterModel
    from harness.llm.client import ChatResult, MockLLM
    from harness.runtime import PassiveHomeostasisAdapter, RuntimeSupervisor
    from tests.test_basic_persona_continuity import _host
    from tests.test_runtime_supervisor import _manifest

    class OwnerModel(MockLLM):
        def complete_chat_result(self, messages, **kwargs):
            self.chat_calls.append(messages)
            if kwargs.get("response_format"):
                evidence = json.loads(messages[-1]["content"])["web_context"]["sources"]
                if not evidence:
                    choice = {"kind": "call", "offer_id": "web:" + SEARCH, "arguments": {"query": "evidence"}}
                elif not evidence[0]["content"]:
                    choice = {"kind": "call", "offer_id": "web:" + READ,
                              "arguments": {"source_refs": [evidence[0]["source_ref"]]}}
                else:
                    choice = {"kind": "finish"}
                text = json.dumps(choice)
            else:
                text = "查到了，资料在这里：[来源](https://example.com/article)。"
            return ChatResult(text=text, usage=TokenUsage(prompt_tokens=100, completion_tokens=30, total_tokens=130))

    async def run():
        monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
        monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "1")
        monkeypatch.delenv("NC_QQ_SESSION_CONFIG", raising=False)
        host, _ = _host(tmp_path / "owner", monkeypatch, [])
        original = host.chat.llm
        model = OwnerModel()
        host.chat.llm = model
        host.chat.pipeline.character = CharacterModel(model, settings=host.chat.settings)
        fake, requests = rig(tmp_path / "web-state")
        manifest = _manifest().model_copy(update={"web_access": fake.manifest.web_access,
            "autonomous_capabilities": fake.manifest.autonomous_capabilities, "autonomous_chat_enabled": False})
        sup = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=PassiveHomeostasisAdapter(),
                                state_dir=tmp_path / "runtime")
        sup.web = fake.web
        sup.web.supervisor = sup
        try:
            await sup.boot_and_recover()
            sup.scheduler.suspend()
            while (pending := await sup.queue.get(timeout=0)) is not None:
                await sup.dispatch(pending)
            event = await sup.submit_user_message("请联网查一下资料。", session_id=host.chat.active_session_id)
            queued = await sup.queue.get(timeout=0)
            assert queued.event_id == event.event_id
            result = await sup.dispatch(queued)
            assert result.status == "completed" and result.decision_kind == "reply", result.detail
            assert len(requests) == 2, sup.web.calls.round(event.event_id)
            character_input = model.chat_calls[-1]
            user_data = "\n".join(m["content"] for m in character_input if m["role"] == "user")
            system = "\n".join(m["content"] for m in character_input if m["role"] == "system")
            assert "BODY_EVIDENCE_TEXT" in user_data and "BODY_EVIDENCE_TEXT" not in system
            turns = host.chat.pipeline.session_store.recent(host.chat.active_session_id)
            assert sum(turn.role == "user" for turn in turns) == 1
            assert sum(turn.role == "character" for turn in turns) == 1
            assert sup.web.calls.round(event.event_id)["status"] == "completed"
        finally:
            host.chat.llm = original
            await sup.stop(reason="isolated test complete")
            await sup.web.close()
    asyncio.run(run())
