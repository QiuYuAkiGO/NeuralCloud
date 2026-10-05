"""Evidence integrity and bounded search tests for the isolated vision benchmark."""
import json

import httpx
import pytest
from PIL import Image

from tools.production_acceptance import qq_vision_reply_benchmark as bench


def event(value):
    return "data: " + json.dumps(value)


def test_stream_first_visible_excludes_reasoning_and_assembles_tool_arguments():
    ticks = iter([13.0])
    message, row = bench.parse_events([
        event({"choices": [{"delta": {"reasoning_content": "private reasoning"}}]}),
        event({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "call-1", "function": {
            "name": "web_search", "arguments": '{"query":"'}}]}}]}),
        event({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {
            "arguments": 'SUGAR RUSH"}'}}]}}]}),
        event({"choices": [{"delta": {"content": "reply"}, "finish_reason": "tool_calls"}],
               "usage": {"prompt_tokens": 10, "completion_tokens": 20}}),
        "data: [DONE]",
    ], 10.0, now=lambda: next(ticks))
    assert row["first_content_seconds"] == 3
    assert bench.validate_search(message["tool_calls"][0]) == "SUGAR RUSH"
    assert message["reasoning_content"] == "private reasoning"
    assert "private reasoning" not in json.dumps(row)
    assert row["stream_done"]


def test_incomplete_stream_is_not_silently_complete():
    _, row = bench.parse_events([event({"choices": [{"delta": {"content": "partial"}}]})], 0)
    assert not row["stream_done"]
    assert row["finish_reason"] is None


@pytest.mark.parametrize("function", [
    {"name": "arbitrary_tool", "arguments": '{"query":"cat"}'},
    {"name": "web_search", "arguments": '{"query":"cat","url":"file://secret"}'},
    {"name": "web_search", "arguments": '{"query":null}'},
    {"name": "web_search", "arguments": '{"query":""}'},
    {"name": "web_search", "arguments": '[]'},
    {"name": "web_search", "arguments": 'null'},
    {"name": "web_search", "arguments": json.dumps({"query": "x" * 241})},
])
def test_only_bounded_explicit_search_arguments_are_executable(function):
    with pytest.raises(ValueError):
        bench.validate_search({"type": "function", "function": function})


def test_answer_labels_and_source_names_never_enter_messages(tmp_path):
    Image.new("RGB", (32, 32), "red").save(tmp_path / "secret-answer.png")
    case = {"id": "answer-id", "user": "What color?", "images": ["secret-answer.png"],
            "expected": "ground-truth", "source": "https://example.com/label"}
    messages, hashes = bench.make_messages(case, tmp_path, "system")
    encoded = json.dumps(messages)
    assert "secret-answer" not in encoded and "ground-truth" not in encoded and "answer-id" not in encoded
    assert "data:image/jpeg;base64," in encoded and hashes[0]["width"] == 32


def test_search_failure_is_returned_to_same_main_model_and_not_fabricated(monkeypatch):
    requests = []
    first_message = {"role": "assistant", "content": "", "reasoning_content": "internal",
                     "tool_calls": [{"id": "call-1", "type": "function", "function": {
                         "name": "web_search", "arguments": '{"query":"visible clue"}'}}]}

    def dispatch(client, provider, payload):
        requests.append(payload)
        if len(requests) == 1:
            return first_message, {"seconds": 1, "finish_reason": "tool_calls"}
        assert payload["messages"][1] == first_message
        assert payload["messages"][2]["tool_call_id"] == "call-1"
        assert json.loads(payload["messages"][2]["content"])["status"] == "unavailable"
        assert payload["tool_choice"] == "none"
        return {"role": "assistant", "content": "还不能确认[END]"}, {
            "seconds": 1, "finish_reason": "stop", "stream_done": True, "first_content_seconds": 0.5}

    monkeypatch.setattr(bench, "dispatch", dispatch)
    monkeypatch.setattr(bench, "search_web", lambda client, query: {
        "status": "unavailable", "results": [], "seconds": 0.2})
    row = bench.run_case(None, None, "kimi3", {"id": "case"}, [{"role": "user", "content": "image"}], web=True)
    assert row["reply"] == "还不能确认" and row["success"]
    assert row["first_visible_seconds"] == 1.7
    assert len(row["calls"]) == 2 and len(row["searches"]) == 1
    assert "internal" not in json.dumps(row)


def test_search_http_error_is_explicit():
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(429))) as client:
        result = bench.search_web(client, "visible clue")
    assert result["status"] == "unavailable" and result["http_status"] == 429 and result["results"] == []


@pytest.mark.parametrize("body", [[], {"results": {}}, {"results": [42]}])
def test_invalid_search_response_is_not_evidence(body):
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=body))) as client:
        result = bench.search_web(client, "visible clue")
    assert result["status"] == "unavailable" and result["results"] == []


def test_required_search_cannot_be_satisfied_by_claiming_it_in_text(monkeypatch):
    monkeypatch.setattr(bench, "dispatch", lambda *args, **kwargs: (
        {"role": "assistant", "content": "我搜到了[END]"},
        {"seconds": 1, "finish_reason": "stop", "stream_done": True}))
    row = bench.run_case(None, None, "local", {"id": "case"}, [], web=True, require_search=True)
    assert not row["success"] and row["error"] == "required_search_not_executed"


def test_multiple_tool_calls_never_expand_search_budget(monkeypatch):
    monkeypatch.setattr(bench, "dispatch", lambda *args, **kwargs: (
        {"role": "assistant", "content": "", "tool_calls": [{}, {}]},
        {"seconds": 1, "finish_reason": "tool_calls", "stream_done": True}))
    monkeypatch.setattr(bench, "search_web", lambda *args: pytest.fail("search must not execute"))
    row = bench.run_case(None, None, "local", {"id": "case"}, [], web=True)
    assert not row["success"] and row["error"] == "unexpected_tool_calls"


@pytest.mark.parametrize("provider", ["deepseek", "deepseek_high"])
def test_deepseek_cost_uses_its_cache_usage_and_conservative_peak_prices(provider):
    row = {"provider": provider, "calls": [{"usage": {
        "prompt_tokens": 1000, "prompt_cache_hit_tokens": 800, "completion_tokens": 100}}]}
    assert bench.estimate_fee(row) == pytest.approx(0.001232)
    row["calls"][0]["usage"]["prompt_cache_hit_tokens"] = 2000
    assert bench.estimate_fee(row) == pytest.approx(0.0028)
    row["calls"].append({"http_status": 500})
    assert bench.estimate_fee(row) == pytest.approx(0.8028)


@pytest.mark.parametrize("changed", ["manifest", "system_prompt", "web", "require_search", "stream", "history_chars"])
def test_supplement_reference_rejects_changed_inputs_or_protocol(changed):
    manifest = {"cases": [{"id": "c01"}]}
    reference = {"manifest_sha256": bench.digest(manifest), "system_prompt": "frozen persona",
        "system_sha256": bench.digest("frozen persona"),
        "parameters": {"web": True, "require_search": True, "stream": True, "history_chars": 12000}}
    parameters = {"web": True, "require_search": True, "stream": True, "history_chars": 12000}
    assert bench.frozen_system(reference, manifest, **parameters) == "frozen persona"
    if changed == "manifest":
        manifest["cases"].append({"id": "c02"})
    elif changed == "system_prompt":
        reference["system_prompt"] = "later persona edit"
    else:
        parameters[changed] = 0 if changed == "history_chars" else False
    with pytest.raises(ValueError, match="reference_input_mismatch"):
        bench.frozen_system(reference, manifest, **parameters)
