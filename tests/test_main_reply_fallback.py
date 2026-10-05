from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import ChatResult, RoutingLLMClient, build_llm_client
from harness.llm.deadline import model_deadline
from harness.llm.fallback import FallbackConfig, FallbackLLMClient, fallback_status


MESSAGES = [{"role": "system", "content": "冻结的人设"}, {"role": "user", "content": "你好"}]
PRIMARY = "http://127.0.0.1:8080/v1/chat/completions"


class Stub:
    def __init__(self, result=None):
        self.settings = Settings(_env_file=None, llm_base_url="http://127.0.0.1:8080/v1")
        self.result = result or ChatResult(text="主回复")
        self.calls = []
        self.closed = False

    def complete_chat_result(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        if isinstance(self.result, Exception):
            # A failed client cannot mutate the fallback's frozen input.
            messages[0]["content"] = "mutated"
            raise self.result
        return self.result

    def complete_json(self, **kwargs):
        return {"aux": True}

    def close(self):
        self.closed = True


def wrapper(result=None):
    main, alternate = Stub(result), Stub(ChatResult(text="备用回复"))
    client = FallbackLLMClient(main, alternate,
        FallbackConfig(base_url="https://api.deepseek.com/v1", model="deepseek-v4-flash", api_key="private"))
    return client, main, alternate


def status_error(code, url=PRIMARY):
    request = httpx.Request("POST", url)
    return httpx.HTTPStatusError("private provider details", request=request,
        response=httpx.Response(code, request=request))


def test_healthy_primary_never_calls_paid_fallback():
    client, main, alternate = wrapper()
    assert client.complete_chat(MESSAGES) == "主回复"
    assert len(main.calls) == 1 and not alternate.calls
    client.close()
    assert main.closed and alternate.closed


@pytest.mark.parametrize("failure", [status_error(503), status_error(429), status_error(401),
    httpx.ConnectError("private", request=httpx.Request("POST", PRIMARY)),
    httpx.ReadTimeout("private", request=httpx.Request("POST", PRIMARY)), ChatResult(text="  ")])
def test_unavailable_preserves_input_and_options_and_records_alternate(failure, caplog):
    client, main, alternate = wrapper(failure)
    result = client.complete_chat_result(MESSAGES, max_tokens=91, temperature=.3,
        timeout_seconds=12, response_format={"type": "json_object"})
    assert result.text == "备用回复"
    assert alternate.calls[0][0] == MESSAGES
    options = alternate.calls[0][1]
    assert options["max_tokens"] == 91 and options["temperature"] == .3
    assert options["response_format"] == {"type": "json_object"}
    assert main.calls[0][1]["timeout_seconds"] == 6
    assert 0 < options["timeout_seconds"] <= 12
    assert "private" not in caplog.text


@pytest.mark.parametrize("failure", [status_error(400), status_error(500, "http://127.0.0.1:8770/budget"),
    httpx.ConnectError("meter", request=httpx.Request("POST", "http://127.0.0.1:8770/budget")),
    ValueError("schema rejected"), TimeoutError("model_turn_deadline_exceeded")])
def test_programming_budget_and_deadline_errors_do_not_spend_again(failure):
    client, _, alternate = wrapper(failure)
    with pytest.raises(type(failure)):
        client.complete_chat_result(MESSAGES)
    assert not alternate.calls


def test_alternate_failure_is_not_retried():
    client, _, alternate = wrapper(status_error(503))
    alternate.result = status_error(503, "https://api.deepseek.com/v1/chat/completions")
    with pytest.raises(httpx.HTTPStatusError):
        client.complete_chat_result(MESSAGES)
    assert len(alternate.calls) == 1


def test_deadline_reserve_and_default_output_cap():
    client, main, alternate = wrapper(status_error(503))
    with model_deadline(datetime.now(timezone.utc) + timedelta(seconds=8)):
        client.complete_chat_result(MESSAGES)
    assert 0 < main.calls[0][1]["timeout_seconds"] <= 4
    assert 0 < alternate.calls[0][1]["timeout_seconds"] <= 8
    assert alternate.calls[0][1]["max_tokens"] == 2048


def test_configured_factory_keeps_aux_and_memory_routes(tmp_path):
    config = tmp_path / "private.json"
    config.write_text(json.dumps({"base_url": "https://api.deepseek.com/v1",
        "model": "deepseek-v4-flash", "api_key": "private"}))
    settings = Settings(_env_file=None, llm_fallback_config=config,
        llm_base_url="http://127.0.0.1:8080/v1", llm_model="qwen3.8-27b",
        aux_llm_base_url="http://127.0.0.1:11435/v1", aux_llm_model="qwen3:4b-instruct")
    client = build_llm_client(settings)
    assert isinstance(client, RoutingLLMClient)
    assert isinstance(client._main, FallbackLLMClient)
    assert client._aux.settings.llm_model == "qwen3:4b-instruct"
    assert fallback_status(client)["model"] == "deepseek-v4-flash"
    assert "api_key" not in fallback_status(client)
    client.close()


def test_aux_json_does_not_use_fallback():
    client, _, alternate = wrapper(status_error(503))
    assert client.complete_json(system="s", user="u") == {"aux": True}
    assert not alternate.calls


def test_each_physical_attempt_is_metered_and_traced(tmp_path):
    from harness.llm.client import OpenAICompatClient
    from harness.production.metering import production_call_scope
    from harness.production.store import ProductionStore

    settings = Settings(_env_file=None, llm_base_url="http://127.0.0.1:8080/v1",
        llm_model="qwen3.8-27b", trace_dir=tmp_path, model_request_log_enabled=True)
    main = OpenAICompatClient(settings)
    alternate = OpenAICompatClient(settings.model_copy(update={
        "llm_base_url": "https://api.deepseek.com/v1", "llm_model": "deepseek-v4-flash"}))
    sent = []

    def wire(request):
        sent.append(json.loads(request.content))
        return httpx.Response(503 if len(sent) == 1 else 200, json={
            "choices": [{"message": {"content": "备用回复"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 3}})

    for provider in [main, alternate]:
        provider._client.close()
        provider._client = httpx.Client(transport=httpx.MockTransport(wire))
    client = FallbackLLMClient(main, alternate, FallbackConfig(
        base_url="https://api.deepseek.com/v1", model="deepseek-v4-flash", api_key="private"))
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    context = {"schema_version": "production-context-v1", "configuration": {},
               "body": {"available": False, "snapshot": {}}}
    try:
        with production_call_scope(context, command_id="reply", meter=store):
            assert client.complete_chat(MESSAGES) == "备用回复"
        attempts = [row["usage"] for row in store.pending_usage()]
        assert [a["model"] for a in attempts] == ["qwen3.8-27b", "deepseek-v4-flash"]
        assert [a["outcome"] for a in attempts] == ["failed", "completed"]
        assert sent[0]["messages"] == sent[1]["messages"]
        trace = [json.loads(line) for line in (tmp_path / "model_requests.jsonl").read_text().splitlines()]
        finished = [t for t in trace if t["record_type"] == "model_request_finished"]
        assert [t["request_model"] for t in finished] == ["qwen3.8-27b", "deepseek-v4-flash"]
        assert [t["attempt"] for t in finished] == [1, 2]
    finally:
        client.close()


def test_transport_budget_reservation_failure_never_calls_fallback(tmp_path):
    from harness.llm.client import OpenAICompatClient
    from harness.production.metering import production_call_scope

    class FailedMeter:
        def reserve_attempt(self, *args, **kwargs):
            raise status_error(503, "http://127.0.0.1:8770/v1/production/reserve")

    client, _, alternate = wrapper()
    client._main = OpenAICompatClient(client.settings)
    context = {"schema_version": "production-context-v1", "configuration": {}}
    try:
        with production_call_scope(context, command_id="reply", meter=FailedMeter()):
            with pytest.raises(httpx.HTTPStatusError):
                client.complete_chat_result(MESSAGES)
        assert not alternate.calls
    finally:
        client.close()
