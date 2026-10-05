"""Production transport, schema enforcement and accounting under real call paths."""
import json
from types import SimpleNamespace

import httpx
import pytest

from harness.cognitive.production import ProductionChoice, _ask
from harness.config import Settings
from harness.llm.client import MockLLM, OpenAICompatClient, RoutingLLMClient
from harness.llm.deepseek_structured import StructuredResponseError, decision_format, supported
from harness.production.config import ProductionConfig
from harness.production.store import ProductionStore
from tests.test_production_cognition import context

SCHEMA = {"type": "object", "properties": {"decision": {"type": "string", "enum": ["wait"]}},
          "required": ["decision"], "additionalProperties": False}
RAW_USAGE = {"input_tokens": 31, "output_tokens": 11, "total_tokens": 42,
             "input_tokens_details": {"cached_tokens": 10}, "output_tokens_details": {"reasoning_tokens": 0}}


def body(text='{"decision":"wait"}', status="completed"):
    return {"id": "response-synthetic", "model": "deepseek-flash", "status": status, "usage": RAW_USAGE,
            "output": [{"type": "reasoning", "content": [{"type": "reasoning_text", "text": "PRIVATE_REASONING"}]},
                       {"type": "message", "content": [{"type": "output_text", "text": text}]}]}


@pytest.fixture
def wire(tmp_path):
    state = SimpleNamespace(body=body(), calls=[], status=200, diagnostics=[])
    settings = Settings(_env_file=None, llm_base_url="https://api.deepseek.com/v1", llm_model="deepseek-v4-flash",
                        llm_api_key="PRIVATE_API_KEY", trace_dir=tmp_path, model_request_log_enabled=True)
    client = OpenAICompatClient(settings)
    client._client.close()

    def serve(request):
        state.calls.append(request)
        return httpx.Response(state.status, json=state.body)

    client._client = httpx.Client(transport=httpx.MockTransport(serve))
    state.client = client
    state.path = tmp_path
    yield state
    client.close()


@pytest.mark.parametrize("observed", [False, True])
def test_wire_schema_none_usage_deadline_and_diagnostics(wire, observed):
    if observed:
        wire.client._request_observer = wire.diagnostics.append
    format_ = decision_format(wire.client, SCHEMA, "decision")
    result = wire.client.complete_chat_result([{"role": "user", "content": "synthetic"}],
        response_format=format_, reasoning_effort="low", thinking={"type": "enabled"}, max_tokens=128, timeout_seconds=2.5)
    request = wire.calls[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://api.deepseek.com/responses"
    assert payload["model"] == "deepseek-flash" and payload["reasoning"] == {"effort": "none"}
    assert payload["temperature"] == 0 and payload["max_output_tokens"] == 128
    assert payload["text"]["format"]["schema"] == SCHEMA
    assert "thinking" not in payload and "messages" not in payload
    assert request.extensions["timeout"]["read"] == 2.5
    assert result.finish_reason == "stop" and result.content_type == "text"
    assert result.usage.prompt_tokens == 31 and result.usage.completion_tokens == 11
    assert result.usage.cache_hit_tokens == 10 and result.reasoning_tokens == 0
    assert "PRIVATE_API_KEY" not in json.dumps(wire.diagnostics)
    assert "PRIVATE_REASONING" not in json.dumps(wire.diagnostics)
    trace = (wire.path / "model_requests.jsonl").read_text(encoding="utf-8")
    assert "PRIVATE" not in trace
    if observed:
        assert wire.diagnostics[-1]["response_content"] == result.text


@pytest.mark.parametrize("text,status", [('{"decision":"wait"}', "incomplete"),
    ('{"decision":"wait"}', "failed"), ('{"decision":"other"}', "completed"),
    ('{"decision":"wait","extra":1}', "completed"), ('{"decision":"wait","decision":"wait"}', "completed"),
    ('```json\n{"decision":"wait"}\n```', "completed"), ('[]', "completed")])
def test_invalid_outputs_do_not_get_repaired_or_retried(wire, text, status):
    wire.body = body(text, status)
    with pytest.raises(StructuredResponseError):
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    assert len(wire.calls) == 1
    records = [json.loads(x) for x in (wire.path / "model_requests.jsonl").read_text().splitlines()]
    assert records[-1]["usage"]["completion_tokens"] == 11


@pytest.mark.parametrize("part", [{"type": "refusal", "refusal": "private refusal"},
                                  {"type": "output_text", "text": ""}])
def test_refusal_or_empty_output_is_not_a_decision(wire, part):
    wire.body["output"][-1]["content"] = [part]
    with pytest.raises(StructuredResponseError):
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))


def test_actual_production_choice_stays_main_and_preserves_raw_metering(wire):
    wire.body = body('{"kind":"wait","reason":"no current action"}')
    aux = MockLLM()
    routed = RoutingLLMClient(wire.client, aux)
    store = ProductionStore(wire.path / "production.sqlite3", ProductionConfig())
    decision = _ask(routed, {"source_refs": ["synthetic"]}, ProductionChoice, context=context(),
                    command_id="structured-production", autonomous=False, meter=store)
    assert decision.kind == "wait" and not aux.chat_calls and not aux.calls
    payload = json.loads(wire.calls[0].content)
    assert payload["text"]["format"]["schema"] == ProductionChoice.model_json_schema()
    usage = store.pending_usage()[0]["usage"]["raw_usage"]
    assert usage["input_tokens"] == 31 and usage["output_tokens"] == 11
    assert usage["input_tokens_details.cached_tokens"] == 10


@pytest.mark.parametrize("url", ["https://proxy.invalid/v1", "https://api.deepseek.com.evil/v1",
    "https://api.deepseek.com@evil/v1", "http://api.deepseek.com", "https://user@api.deepseek.com",
    "https://api.deepseek.com/v1?key=x", "https://api.deepseek.com:444/v1"])
def test_other_endpoints_keep_legacy_format(url):
    settings = Settings(_env_file=None, llm_model="deepseek-flash", llm_base_url=url)
    assert not supported(settings)
    assert decision_format(SimpleNamespace(settings=settings), SCHEMA, "decision") == {"type": "json_object"}


def test_opt_out_and_plain_chat_preserve_legacy_transport(wire):
    wire.body = {"choices": [{"message": {"content": "hello"}, "finish_reason": "stop"}]}
    wire.client.complete_chat_result([{"role": "user", "content": "hello"}])
    assert wire.calls[0].url.path == "/v1/chat/completions"
    wire.client.settings.deepseek_structured_outputs = False
    assert decision_format(wire.client, SCHEMA, "decision") == {"type": "json_object"}
