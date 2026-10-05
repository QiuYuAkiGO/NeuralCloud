"""Dedicated JSON provider must preserve text routing, evidence and budgets."""

from copy import deepcopy
import json
from types import SimpleNamespace

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import MockLLM, build_llm_client
from harness.llm.deepseek_structured import decision_format, StructuredResponseError
from harness.llm.structured import cache_messages, cache_user, helper_options, ParserOutput


def response(value, *, status="completed"):
    return {
        "status": status,
        "model": "deepseek-flash",
        "usage": {
            "input_tokens": 100,
            "output_tokens": 20,
            "total_tokens": 120,
            "input_tokens_details": {"cached_tokens": 80},
        },
        "output": [{"type": "message", "content": [{"type": "output_text", "text": json.dumps(value)}]}],
    }


@pytest.fixture
def routed(tmp_path):
    key = tmp_path / "structured.key"
    key.write_text("TEST-STRUCTURED-SECRET")
    config = tmp_path / "structured.json"
    config.write_text(json.dumps({"api_key_file": str(key)}))
    settings = Settings(
        _env_file=None,
        structured_llm_config=config,
        llm_fallback_config=None,
        llm_base_url="http://main.invalid/v1",
        llm_model="qwen",
        llm_api_key="TEST-MAIN",
        aux_llm_base_url="http://aux.invalid/v1",
        aux_llm_model="aux-qwen",
        aux_llm_api_key="TEST-AUX",
        model_request_log_enabled=True,
        trace_dir=tmp_path,
    )
    client = build_llm_client(settings)
    state = SimpleNamespace(client=client, calls=[], body=response({"action": "wait"}), code=200, settings=settings)

    def serve(request):
        state.calls.append(request)
        if request.url.host == "main.invalid":
            return httpx.Response(
                200, json={"choices": [{"finish_reason": "stop", "message": {"content": "QWEN_TEXT"}}]}
            )
        if request.url.host == "aux.invalid":
            raise AssertionError("JSON must not reach the old auxiliary provider")
        return httpx.Response(state.code, json=state.body)

    for child in (client._main, client._aux, client._structured):
        child._client.close()
        child._client = httpx.Client(transport=httpx.MockTransport(serve))
    yield state
    client.close()


SCHEMA = {
    "type": "object",
    "properties": {"action": {"enum": ["wait", "speak"]}},
    "required": ["action"],
    "additionalProperties": False,
}


def test_typed_decision_is_separate_from_character_and_aux(routed):
    client = routed.client
    format_ = decision_format(client, SCHEMA, "test_decision")
    assert format_["type"] == "json_schema"
    result = client.complete_chat_result(
        [{"role": "user", "content": '{"text":"now","persona":"fixed"}'}],
        response_format=format_,
        timeout_seconds=2,
        max_tokens=128,
    )
    assert json.loads(result.text) == {"action": "wait"}
    assert client.complete_chat([{"role": "user", "content": "ordinary text"}]) == "QWEN_TEXT"
    request = routed.calls[0]
    payload = json.loads(request.content)
    assert str(request.url) == "https://api.deepseek.com/responses"
    assert request.headers["authorization"] == "Bearer TEST-STRUCTURED-SECRET"
    assert request.extensions["timeout"]["read"] == 2
    assert payload["reasoning"] == {"effort": "none"} and payload["max_output_tokens"] == 128
    assert "options" not in payload and "keep_alive" not in payload
    assert payload["input"][0]["content"].startswith('{"persona":')
    assert routed.calls[1].url.host == "main.invalid"
    assert result.usage.cache_hit_tokens == 80
    log = (routed.settings.trace_dir / "model_requests.jsonl").read_text()
    assert '"structured"' in log and "SECRET" not in log


@pytest.mark.parametrize("bad", ["schema", "truncated", "http"])
def test_bad_decision_is_not_retried_on_main_or_aux(routed, bad):
    if bad == "schema":
        routed.body = response({"action": "invented"})
    if bad == "truncated":
        routed.body = response({"action": "wait"}, status="incomplete")
    if bad == "http":
        routed.code = 503
    with pytest.raises((StructuredResponseError, httpx.HTTPStatusError)):
        routed.client.complete_chat_result([], response_format=decision_format(routed.client, SCHEMA, "test"))
    assert len(routed.calls) == 1


def test_helper_schema_and_validator_use_dedicated_context(routed):
    routed.body = response({"speech_act": "question", "entities": ["user"], "gate": {"path": "deep"}})
    parsed = routed.client.complete_json(
        system="Parse JSON", user="{'text': 'hello'}", **helper_options(routed.client, ParserOutput, "context_parser")
    )
    assert parsed["gate"]["path"] == "deep"
    assert json.loads(routed.calls[0].content)["max_output_tokens"] == routed.settings.deepseek_structured_max_output_tokens
    routed.body = response({"verdict": "pass"})
    result = routed.client.complete_validation_json(system="Validate JSON", user="long evidence " * 10000)
    assert result["verdict"] == "pass"
    assert all(r.url.host == "api.deepseek.com" for r in routed.calls)
    assert json.loads(routed.calls[1].content)["text"]["format"]["name"] == "response_validation"


def test_unconfigured_helpers_keep_legacy_contract():
    assert helper_options(MockLLM(), ParserOutput, "parser") == {}


def test_cache_layout_preserves_roles_order_withdrawal_and_live_values():
    first = [
        {"role": "system", "content": "Fixed instruction"},
        {
            "role": "user",
            "content": json.dumps(
                {
                    "text": "first",
                    "persona": {"name": "Misty"},
                    "memory": [{"id": "m1", "text": "untrusted"}],
                    "unknown_field": {"x": [3, 1]},
                    "production": {"budget": 8},
                }
            ),
        },
        {"role": "system", "content": "current authority"},
    ]
    original = deepcopy(first)
    second = deepcopy(first)
    obj = json.loads(second[1]["content"])
    obj.update(text="second", production={"budget": 2})
    second[1]["content"] = json.dumps(obj)
    a, b = cache_messages(first), cache_messages(second)
    assert first == original and a[0] == b[0] and a[-1] == b[-1]
    assert a[1]["content"].split(',"production":')[0] == b[1]["content"].split(',"production":')[0]
    assert json.loads(a[1]["content"]) == json.loads(first[1]["content"])
    obj["memory"] = []
    second[1]["content"] = json.dumps(obj)
    assert "untrusted" not in cache_messages(second)[1]["content"]


@pytest.mark.parametrize("value", ['{"text":"a","text":"b"}', '{"a":NaN}', "{'a': (1, 2)}", "not json", "[1,2]"])
def test_unrepresentable_or_ambiguous_cache_inputs_are_not_rewritten(value):
    assert cache_user(value) == value


def test_emotion_state_does_not_interrupt_stable_history_prefix():
    value = {"persona": {"name": "Misty"}, "homeostasis": {"fatigue": 0.2},
             "shared_affect": {"joy": 0.1}, "messages": [{"text": "hello"}], "new_source_ids": ["a"]}
    first = cache_user(json.dumps(value))
    value.update(homeostasis={"fatigue": 0.8}, shared_affect={"joy": 0.7}, new_source_ids=["b"])
    second = cache_user(json.dumps(value))
    assert first.split(',"homeostasis":')[0] == second.split(',"homeostasis":')[0]
    assert json.loads(second) == value


def test_multimodal_structured_input_keeps_pixels_and_user_authority(routed):
    messages = [
        {"role": "system", "content": "Observe only"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "read"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,AAAA", "detail": "high"}},
            ],
        },
    ]
    before = deepcopy(messages)
    routed.client.complete_chat_result(messages, response_format=decision_format(routed.client, SCHEMA, "screen"))
    data = json.loads(routed.calls[0].content)
    assert messages == before
    assert data["input"][1]["content"][1] == {
        "type": "input_image",
        "image_url": "data:image/png;base64,AAAA",
        "detail": "high",
    }
    assert data["input"][1]["role"] == "user"


def test_emotion_reuses_schema_provider(routed):
    from harness.emotion.endpoint import build_emotion_client

    client = build_emotion_client(routed.settings)
    try:
        assert client.settings.llm_model == "deepseek-flash"
        assert client.settings.llm_api_key == "TEST-STRUCTURED-SECRET"
        assert client.settings.llm_fallback_config is None
    finally:
        client.close()
