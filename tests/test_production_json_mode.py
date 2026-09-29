"""Production JSON constraints stay on the main route and preserve physical-call accounting."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from harness.cognitive.production import ProductionAppraisal, ProductionChoice, _ask
from harness.config import Settings
from harness.llm.client import MockLLM, OpenAICompatClient, RoutingLLMClient
from harness.production.config import ProductionConfig
from harness.production.store import ProductionBudgetExceeded, ProductionStore
from tests.test_production_cognition import adapter_at, command, context


RAW_USAGE = {"prompt_tokens": 31, "completion_tokens": 11, "total_tokens": 42}
VALID_CHOICE = '{"kind":"wait","reason":"No current action is needed."}'


@pytest.fixture
def wire(tmp_path):
    state = SimpleNamespace(content=VALID_CHOICE, status=200, calls=[], finish_reason="stop",
                            tool_calls=None, refusal=None)

    def respond(request):
        state.calls.append({"method": request.method, "url": str(request.url),
                            "payload": json.loads(request.content)})
        return httpx.Response(state.status, json={"choices": [{"finish_reason": state.finish_reason,
            "message": {"content": state.content, "tool_calls": state.tool_calls,
                        "refusal": state.refusal}}], "usage": RAW_USAGE})

    main = OpenAICompatClient(Settings(_env_file=None, llm_model="synthetic-main",
        llm_base_url="https://main.invalid/v1", llm_api_key="synthetic-key", call_log_enabled=False))
    main._client.close()
    main._client = httpx.Client(transport=httpx.MockTransport(respond), follow_redirects=False)
    aux = MockLLM()
    state.llm = RoutingLLMClient(main, aux)
    state.aux = aux
    state.store = ProductionStore(tmp_path / "meter.sqlite3", ProductionConfig(autonomous_attempts_per_hour=1))
    yield state
    state.llm.close()


def ask(wire, schema=ProductionChoice, *, autonomous=True, command_id="production-json"):
    return _ask(wire.llm, {"source_refs": ["synthetic-event"]}, schema,
        context=context(autonomous=autonomous), command_id=command_id,
        autonomous=autonomous, meter=wire.store)


def assert_one_main_call(wire):
    assert len(wire.calls) == 1
    request = wire.calls[0]
    assert request["method"] == "POST" and request["url"] == "https://main.invalid/v1/chat/completions"
    assert request["payload"]["model"] == "synthetic-main"
    assert request["payload"]["response_format"] == {"type": "json_object"}
    assert request["payload"]["temperature"] == 0.3
    assert not wire.aux.chat_calls and not wire.aux.calls
    assert wire.store.budget()["used"] == 1
    pending = wire.store.pending_usage()
    assert len(pending) == 1
    assert pending[0]["usage"]["raw_usage"] == RAW_USAGE
    assert pending[0]["usage"]["model"] == "synthetic-main"
    assert pending[0]["usage"]["episode_id"] == "episode-1"
    reopened = ProductionStore(wire.store.path, wire.store.config)
    assert reopened.pending_usage() == pending
    assert reopened.budget()["used"] == 1


@pytest.mark.parametrize("schema,content", [(ProductionChoice, VALID_CHOICE), (ProductionAppraisal, '{}')])
def test_choice_and_qq_appraisal_use_json_mode_on_main_once(wire, schema, content):
    wire.content = content
    assert isinstance(ask(wire, schema), schema)
    assert_one_main_call(wire)


@pytest.mark.parametrize("content,status,error", [
    ('{"kind":"wait","reason":"unescaped\nnewline"}', 200, json.JSONDecodeError),
    ('{"kind":"invented","reason":"Invalid schema despite valid JSON."}', 200, ValidationError),
    (VALID_CHOICE, 400, httpx.HTTPStatusError),
])
def test_invalid_or_rejected_json_mode_retains_usage_without_retry(wire, content, status, error):
    wire.content, wire.status = content, status
    with pytest.raises(error):
        ask(wire)
    assert_one_main_call(wire)


@pytest.mark.parametrize("changes", [
    {"finish_reason": "length"},
    {"finish_reason": "tool_calls", "tool_calls": [{"type": "function", "function": {
        "name": "synthetic", "arguments": "{}"}}]},
    {"tool_calls": [{"type": "function", "function": {"name": "synthetic", "arguments": "{}"}}]},
    {"refusal": "Synthetic refusal despite accompanying valid JSON."},
    {"content": None},
    {"content": [{"type": "text", "text": VALID_CHOICE}]},
    {"content": {"text": VALID_CHOICE}},
    {"content": ""},
    {"content": " \n\t"},
    {"finish_reason": None},
])
def test_incomplete_or_nontext_response_is_rejected_before_json_and_retains_usage(wire, monkeypatch, changes):
    for name, value in changes.items():
        setattr(wire, name, value)

    def forbidden_parse(_text):
        pytest.fail("Response metadata must be checked before JSON parsing.")

    monkeypatch.setattr("harness.llm.client.parse_json_object", forbidden_parse)
    with pytest.raises(ValueError, match="^production_response_contract_invalid$"):
        ask(wire)
    assert_one_main_call(wire)


def test_complete_text_is_parsed_unchanged_after_response_gate(wire, monkeypatch):
    from harness.llm.client import parse_json_object
    parsed = []

    def record_parse(text):
        parsed.append(text)
        return parse_json_object(text)

    monkeypatch.setattr("harness.llm.client.parse_json_object", record_parse)
    assert ask(wire).kind == "wait"
    assert parsed == [VALID_CHOICE]
    assert_one_main_call(wire)


def test_budget_refusal_blocks_post_but_user_call_remains_metered(wire):
    ask(wire)
    with pytest.raises(ProductionBudgetExceeded):
        ask(wire, command_id="second-autonomous")
    assert_one_main_call(wire)
    ask(wire, autonomous=False, command_id="user-reply")
    assert len(wire.calls) == 2 and wire.store.budget()["used"] == 1
    pending = wire.store.pending_usage()
    assert len(pending) == 2 and len({row["attempt_id"] for row in pending}) == 2
    assert all(row["usage"]["raw_usage"] == RAW_USAGE for row in pending)
    assert not wire.aux.chat_calls and not wire.aux.calls


@pytest.mark.parametrize("valid", [True, False])
def test_prepare_replay_preserves_success_or_failure_without_another_call(tmp_path, wire, valid):
    if not valid:
        wire.content = '{"kind":"wait",}'

    async def run():
        adapter = adapter_at(tmp_path, wire.llm)
        adapter.production_meter = wire.store
        adapter.production_meter.context = lambda **kwargs: context()
        request = command()
        request.payload["production_context"]["autonomous"] = True
        first = await adapter.prepare_response(request)
        assert first.status == ("applied" if valid else "failed")
        again = await adapter.prepare_response(request)
        assert again.idempotency_hit
        assert again.status == ("idempotent_replay" if valid else "failed")
        assert again.result == first.result and again.error == first.error
        await adapter.close()
        reopened = adapter_at(tmp_path, wire.llm)
        reopened.production_meter = wire.store
        try:
            recovered = await reopened.prepare_response(request)
            assert recovered.model_dump(mode="json") == again.model_dump(mode="json")
        finally:
            await reopened.close()

    asyncio.run(run())
    assert_one_main_call(wire)


def test_complete_chat_only_legacy_fallback_keeps_original_signature(tmp_path):
    class Legacy:
        calls = 0

        def complete_chat(self, messages, *, stream):
            self.calls += 1
            assert stream is False
            return VALID_CHOICE

    llm = Legacy()
    result = _ask(llm, {}, ProductionChoice, context=context(), command_id="legacy", autonomous=False,
                  meter=ProductionStore(tmp_path / "legacy.sqlite3"))
    assert result.kind == "wait" and llm.calls == 1


def test_metadata_capable_adapter_cannot_downgrade_missing_metadata_to_legacy(tmp_path):
    class IncompleteAdapter:
        calls = 0

        def complete_chat_result(self, messages, **kwargs):
            self.calls += 1
            return SimpleNamespace(text=VALID_CHOICE)

        def complete_chat(self, messages, **kwargs):
            pytest.fail("A rejected structured response must not retry the legacy method.")

    llm = IncompleteAdapter()
    with pytest.raises(ValueError, match="^production_response_contract_invalid$"):
        _ask(llm, {}, ProductionChoice, context=context(), command_id="missing-metadata", autonomous=False,
             meter=ProductionStore(tmp_path / "missing-metadata.sqlite3"))
    assert llm.calls == 1
