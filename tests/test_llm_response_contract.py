"""Per-call JSON options and response diagnostics without a live endpoint."""
import json

import httpx
import pytest

from harness.config import Settings
from harness.core.inference import TokenUsage
from harness.llm.character import CharacterModel
from harness.llm.client import (
    ChatResult, MockLLM, OpenAICompatClient, RoutingLLMClient, deepseek_low_reasoning_options,
)
from infra.trace.request_spans import trace_scope


def _client(tmp_path, payload, captured):
    def serve(request):
        captured.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    client = OpenAICompatClient(Settings(llm_base_url="http://synthetic.invalid/v1", llm_model="unchanged-model",
        llm_api_key="DO_NOT_LOG_CREDENTIAL", call_log_enabled=True, trace_dir=tmp_path),
        extra_payload={"thinking": {"type": "enabled"}})
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(serve))
    return client


def test_json_mode_is_per_call_preserves_thinking_and_records_safe_finish_metadata(tmp_path):
    captured = []
    payload = {"id": "synthetic-response-1", "choices": [{"finish_reason": "length", "message": {
        "content": None, "reasoning_content": "PRIVATE_REASONING", "refusal": None}}],
        "usage": {"prompt_tokens": 5075, "completion_tokens": 500, "total_tokens": 5575,
                  "completion_tokens_details": {"reasoning_tokens": 500}}}
    client = _client(tmp_path, payload, captured)
    client.settings.json_repair_enabled = False  # Legacy contract metadata remains available with repair disabled.
    try:
        with trace_scope(event_id="event:board", command_id="command:board", stage="board.prepare.model",
                         model_attempt=2, model_budget_seconds=3.5):
            result = client.complete_chat_result([{"role": "user", "content": "PRIVATE_PROMPT"}],
                response_format={"type": "json_object"}, max_tokens=2048)
        client.complete_chat_result([{"role": "user", "content": "normal chat"}])
    finally:
        client.close()
    assert captured[0]["response_format"] == {"type": "json_object"}
    assert "response_format" not in captured[1]
    assert all(item["thinking"] == {"type": "enabled"} and item["model"] == "unchanged-model" for item in captured)
    assert result.text == "" and result.content_type == "null"
    assert result.finish_reason == "length" and result.reasoning_tokens == 500
    assert result.usage.completion_tokens == 500 and result.response_id == "synthetic-response-1"
    trace_text = (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8")
    records = [json.loads(line) for line in trace_text.splitlines()]
    finished = records[1]
    assert finished["event_id"] == "event:board" and finished["command_id"] == "command:board"
    assert finished["attempt"] == 2 and finished["retries"] == 1
    assert finished["finish_reason"] == "length" and finished["content_characters"] == 0
    for secret in ("PRIVATE_REASONING", "PRIVATE_PROMPT", "DO_NOT_LOG_CREDENTIAL"):
        assert secret not in trace_text


def test_thinking_override_is_per_call_and_does_not_change_later_chat(tmp_path):
    captured = []
    client = _client(tmp_path, {"choices": [{"message": {"content": "{}"}}]}, captured)
    try:
        client.complete_chat_result([], thinking={"type": "disabled"})
        client.complete_chat_result([])
    finally:
        client.close()
    assert captured[0]["thinking"] == {"type": "disabled"}
    assert captured[1]["thinking"] == {"type": "enabled"}


@pytest.mark.parametrize("content,kind", [(None, "null"), ([], "array"), ({"text": "PRIVATE"}, "object"),
                                         (123, "invalid"), ("a reply", "text")])
def test_content_types_are_preserved_without_stringifying_objects(tmp_path, content, kind):
    client = _client(tmp_path, {"choices": [{"message": {"content": content}}]}, [])
    try:
        result = client.complete_chat_result([])
    finally:
        client.close()
    assert result.content_type == kind
    assert result.text == (content if kind == "text" else "")
    assert result.finish_reason is None and result.reasoning_tokens is None


def test_missing_content_tools_and_refusal_metadata_do_not_record_their_text(tmp_path):
    payload = {"id": "PRIVATE RESPONSE ID WITH SPACES", "choices": [{"finish_reason": "PRIVATE_FINISH", "message": {
        "tool_calls": [{"id": "PRIVATE_TOOL", "function": {"arguments": "PRIVATE_ARGS"}}],
        "refusal": "PRIVATE_REFUSAL"}}]}
    client = _client(tmp_path, payload, [])
    try:
        result = client.complete_chat_result([])
    finally:
        client.close()
    assert result.text == "" and result.content_type == "missing"
    assert result.has_tool_calls and result.has_refusal
    assert result.finish_reason == "unknown" and result.response_id is None
    assert "PRIVATE" not in (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8")


def test_routing_omits_optional_argument_for_legacy_client_and_forwards_when_requested():
    class Legacy:
        def complete_chat_result(self, messages, *, temperature, stream, max_tokens):
            return ChatResult(text="legacy")

    assert RoutingLLMClient(Legacy(), Legacy()).complete_chat_result([]).text == "legacy"
    assert RoutingLLMClient(Legacy(), Legacy()).complete_chat_result([], reasoning_effort=None).text == "legacy"
    main, aux = MockLLM(chat_script=['{"action":"wait"}']), MockLLM()
    routed = RoutingLLMClient(main, aux)
    routed.complete_chat_result([], response_format={"type": "json_object"}, timeout_seconds=4,
                                reasoning_effort="low")
    assert main.chat_call_options[0]["response_format"] == {"type": "json_object"}
    assert main.chat_call_options[0]["timeout_seconds"] == 4 and not aux.chat_calls
    assert main.chat_call_options[0]["reasoning_effort"] == "low"
    routed.complete_chat_result([])
    assert "reasoning_effort" not in main.chat_call_options[1]


@pytest.mark.parametrize("default_effort", [None, "high"])
def test_reasoning_effort_is_per_call_preserves_thinking_and_client_defaults(tmp_path, default_effort):
    captured = []
    client = _client(tmp_path, {"choices": [{"message": {"content": "synthetic reply"}}]}, captured)
    if default_effort is not None:
        client._extra_payload["reasoning_effort"] = default_effort
    original_extras = dict(client._extra_payload)
    try:
        client.complete_chat_result([], reasoning_effort="low", max_tokens=4096)
        client.complete_chat_result([], reasoning_effort=None)
        client.complete_chat_result([])
    finally:
        client.close()
    assert captured[0]["reasoning_effort"] == "low" and captured[0]["max_tokens"] == 4096
    for payload in captured[1:]:
        if default_effort is None:
            assert "reasoning_effort" not in payload
        else:
            assert payload["reasoning_effort"] == default_effort
    assert all(payload["thinking"] == {"type": "enabled"} for payload in captured)
    assert client._extra_payload == original_extras


@pytest.mark.parametrize("model", ["deepseek-v4-flash", "deepseek-v4-pro"])
@pytest.mark.parametrize("endpoint", ["https://api.deepseek.com", "https://api.deepseek.com/",
    "https://api.deepseek.com/v1", "https://api.deepseek.com/v1/", "https://api.deepseek.com:443/v1"])
def test_low_reasoning_options_only_enable_supported_official_models(model, endpoint):
    settings = Settings(llm_model=model, llm_base_url=endpoint)
    assert deepseek_low_reasoning_options(settings) == {"reasoning_effort": "low"}


@pytest.mark.parametrize("model,endpoint", [
    ("qwen", "https://api.deepseek.com/v1"),
    ("deepseek-chat", "https://api.deepseek.com/v1"),
    ("deepseek-v4-flash-vision-exp", "https://api.deepseek.com/v1"),
    ("deepseek-v4-flash", "http://api.deepseek.com/v1"),
    ("deepseek-v4-flash", "http://127.0.0.1:8080/v1"),
    ("deepseek-v4-flash", "https://proxy.invalid/v1"),
    ("deepseek-v4-flash", "https://api.deepseek.com.proxy.invalid/v1"),
    ("deepseek-v4-flash", "https://api.deepseek.com@proxy.invalid/v1"),
    ("deepseek-v4-flash", "https://user@api.deepseek.com/v1"),
    ("deepseek-v4-flash", "https://api.deepseek.com:444/v1"),
    ("deepseek-v4-flash", "https://api.deepseek.com:invalid/v1"),
    ("deepseek-v4-flash", "https://api.deepseek.com/beta"),
    ("deepseek-v4-flash", "https://api.deepseek.com/v1/other"),
    ("deepseek-v4-flash", "https://api.deepseek.com/v1?provider=other"),
    ("deepseek-v4-flash", "https://api.deepseek.com/v1#other"),
    ("deepseek-v4-flash", " https://api.deepseek.com/v1"),
])
def test_low_reasoning_options_leave_other_models_and_endpoints_unchanged(model, endpoint):
    settings = Settings(llm_model=model, llm_base_url=endpoint)
    assert deepseek_low_reasoning_options(settings) == {}


def test_character_usage_fallback_retains_metadata_and_legacy_result_construction():
    class Model:
        def complete_chat_result(self, messages, *, stream):
            return ChatResult(text="reply", finish_reason="stop", response_id="response:1",
                              reasoning_tokens=42, usage=TokenUsage())

    result = CharacterModel(Model())._complete([{"role": "user", "content": "hello"}], stream=False)
    assert result.usage.total_tokens > 0 and result.finish_reason == "stop"
    assert result.response_id == "response:1" and result.reasoning_tokens == 42
    assert ChatResult(text="legacy").finish_reason is None
