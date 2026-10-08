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


def test_schema_diagnostics_preserve_required_field_without_private_content(wire):
    wire.body = body('{"PRIVATE_UNKNOWN_FIELD":"PRIVATE_VALUE"}')
    with pytest.raises(StructuredResponseError) as caught:
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    assert caught.value.error_code == "deepseek_structured_schema_invalid"
    assert any(item["missing_fields"] == ["decision"] for item in caught.value.diagnostics)
    trace = (wire.path / "model_requests.jsonl").read_text(encoding="utf-8")
    assert "PRIVATE" not in trace
    error = json.loads(trace.splitlines()[-1])["error"]
    assert error["code"] == "deepseek_structured_schema_invalid"
    assert error["diagnostics"] == caught.value.diagnostics


@pytest.mark.parametrize("text,code", [
    ('not JSON', 'deepseek_structured_json_invalid'),
    ('{"decision":"wait","decision":"wait"}', 'deepseek_structured_duplicate_key'),
    ('{"decision":NaN}', 'deepseek_structured_nonfinite_number'),
])
def test_invalid_json_diagnostics_keep_distinct_content_free_codes(wire, text, code):
    wire.body = body(text)
    with pytest.raises(StructuredResponseError) as caught:
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    assert caught.value.error_code == code
    if code == "deepseek_structured_json_invalid":
        assert caught.value.diagnostics[0]["reason"] == "expected_value"
    elif code == "deepseek_structured_duplicate_key":
        assert caught.value.diagnostics == [{"validator": "unique_keys", "field": "decision"}]
    else:
        assert caught.value.diagnostics == []


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("text,reason", [
    ('', 'expected_value'),
    ('```json\n{"decision":"PRIVATE_VALUE"}\n```', 'expected_value'),
    ('{"decision":"PRIVATE_VALUE",}', 'expected_property_name'),
    ('{"decision":"PRIVATE_VALUE"}{}', 'extra_data'),
    ('{"decision":"PRIVATE_VALUE\\q"}', 'invalid_escape'),
    ('{"decision":"PRIVATE_VALUE\n"}', 'invalid_control_character'),
])
def test_json_failure_keeps_safe_location_and_response_metadata(wire, observed, text, reason):
    if observed:
        wire.client._request_observer = wire.diagnostics.append
    wire.body = body(text)
    with pytest.raises(json.JSONDecodeError) as parser:
        json.loads(text)
    with pytest.raises(StructuredResponseError) as caught:
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    diag = caught.value.diagnostics[0]
    assert diag == {"validator": "json_decode", "reason": reason, "offset": parser.value.pos,
                    "line": parser.value.lineno, "column": parser.value.colno,
                    "content_characters": len(text), "empty": not text.strip(),
                    "leading_code_fence": text.startswith('```')}
    trace = (wire.path / "model_requests.jsonl").read_text(encoding="utf-8")
    assert "PRIVATE" not in trace and len(wire.calls) == 1
    terminal = json.loads(trace.splitlines()[-1])
    assert terminal["finish_reason"] == "stop" and terminal["content_characters"] == len(text)
    assert terminal["content_type"] == "text" and terminal["usage"]["completion_tokens"] == 11
    assert terminal["structured_response"] == {"status": "completed", "output_items": 2,
                                               "message_items": 1, "text_parts": 1}
    assert terminal["error"]["diagnostics"] == caught.value.diagnostics


def test_multiple_json_parts_are_reported_and_still_rejected(wire):
    wire.body = body('{"decision":"wait"}')
    wire.body['output'][-1]['content'].append({'type': 'output_text', 'text': '{"PRIVATE":"VALUE"}'})
    with pytest.raises(StructuredResponseError) as caught:
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    assert caught.value.diagnostics[0]["reason"] == "extra_data"
    terminal = json.loads((wire.path / "model_requests.jsonl").read_text().splitlines()[-1])
    assert terminal['structured_response']['text_parts'] == 2
    assert "PRIVATE" not in json.dumps(terminal)


@pytest.mark.parametrize("wrapped", [False, True])
def test_json_nesting_error_remains_safe(wire, wrapped):
    text = '[' * 3000 + '"PRIVATE"' + ']' * 3000
    wire.body = body('{"decision":' + text + '}' if wrapped else text)
    with pytest.raises(StructuredResponseError) as caught:
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    assert caught.value.diagnostics[0]['reason'] == 'nesting_limit'
    assert "PRIVATE" not in (wire.path / "model_requests.jsonl").read_text()


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

@pytest.mark.parametrize("key,expected", [("decision", "decision"), ("PRIVATE_KEY", "*")])
def test_nested_duplicate_diagnostic_redacts_unknown_names_and_never_retries(wire, key, expected):
    wire.body = body('{"decision":"wait","nested":{"'+key+'":"PRIVATE_VALUE","'+key+'":"PRIVATE_OTHER"}}')
    with pytest.raises(StructuredResponseError) as caught:
        wire.client.complete_chat_result([], response_format=decision_format(wire.client, SCHEMA, "decision"))
    assert caught.value.error_code == "deepseek_structured_duplicate_key"
    assert caught.value.diagnostics == [{"validator": "unique_keys", "field": expected}]
    assert len(wire.calls) == 1
    assert "PRIVATE" not in (wire.path / "model_requests.jsonl").read_text(encoding="utf-8")


def test_semantic_reason_limit_remains_strict():
    from harness.validator.semantic import SemanticReview
    from harness.llm.deepseek_structured import validate_response
    report = {"coverage_complete": True, "claims": [{"quote": "test", "kind": "non_factual",
        "status": "not_asserted", "basis": "none", "evidence_rows": [], "reason": "x" * 400}],
        "violations": [], "instruction": None}
    schema = SemanticReview.model_json_schema()
    validate_response(body(json.dumps(report)), schema)
    report["claims"][0]["reason"] += "x"
    with pytest.raises(StructuredResponseError) as caught:
        validate_response(body(json.dumps(report)), schema)
    assert caught.value.error_code == "deepseek_structured_schema_invalid"
    assert caught.value.diagnostics[0]["path"] == ["claims", 0, "reason"]
    assert caught.value.diagnostics[0]["validator"] == "maxLength"
