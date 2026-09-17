from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from pathlib import Path

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import OpenAICompatClient, RoutingLLMClient
from harness.pipeline.pipeline import InferencePipeline
from infra.trace.call_log import CallLog, detached_call_log, list_call_logs
from infra.trace.request_spans import current_trace_context, trace_scope


def client_at(tmp_path: Path, handler, *, model: str = "main") -> OpenAICompatClient:
    settings = Settings(
        trace_dir=tmp_path, call_log_enabled=True, llm_model=model,
        llm_base_url="https://url-user:url-password@example.test/v1?query-secret=yes#fragment",
        llm_api_key="api-secret",
    )
    client = OpenAICompatClient(settings)
    client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def response(text: str = "reply", *, usage=True) -> httpx.Response:
    payload = {"choices": [{"message": {"content": text}}]}
    if usage:
        payload["usage"] = {"prompt_tokens": 12, "completion_tokens": 4, "total_tokens": 16}
    return httpx.Response(200, json=payload)


def rows(tmp_path: Path) -> list[dict]:
    return [json.loads(line) for line in (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8").splitlines()]


def test_main_aux_and_shared_endpoint_spans_preserve_usage_and_context(tmp_path):
    main = client_at(tmp_path, lambda _: response())
    aux = client_at(tmp_path, lambda _: response('{"ok":true}'), model="aux")
    routed = RoutingLLMClient(main, aux)
    try:
        with trace_scope(correlation_id="turn-1", event_id="event-1", command_id="cmd-1", phase="commit", span_id="cmd-1"):
            assert routed.complete_chat_result([{"role": "user", "content": "private story"}]).text == "reply"
            assert routed.complete_json(system="private instruction", user="private story") == {"ok": True}
        assert current_trace_context() == {}
        records = rows(tmp_path)
        assert len(records) == 4
        starts, ends = records[::2], records[1::2]
        assert [row["role"] for row in ends] == ["main", "aux"]
        for start, end in zip(starts, ends):
            assert start["span_id"] == end["span_id"]
            assert start["status"] == "running"
            assert end["status"] == "success"
            assert end["phase"] == "commit"
            assert end["correlation_id"] == "turn-1"
            assert end["parent_span_id"] == "cmd-1"
            assert end["usage"]["total_tokens"] == 16
            assert end["usage_source"] == "api"
            assert end["latency_ms"] >= 0
            assert end["completed_at"] >= end["started_at"]
            assert end["endpoint"] == "https://example.test/v1"
            assert end["attempt"] == 1 and end["retries"] == 0
        serialized = json.dumps(records)
        for private in ("url-user", "url-password", "query-secret", "api-secret", "private story", "private instruction"):
            assert private not in serialized
        # Helper usage is still captured when helpers share the character endpoint.
        shared = RoutingLLMClient(aux, aux)
        assert shared.complete_json(system="s", user="u") == {"ok": True}
        assert rows(tmp_path)[-1]["role"] == "aux"
    finally:
        routed.close()


@pytest.mark.parametrize("failure", ["http", "timeout", "json"])
def test_failed_requests_have_terminal_metadata_and_preserve_exception(tmp_path, failure):
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("api-secret url-password", request=request)
        if failure == "http":
            return httpx.Response(503, text="api-secret")
        return httpx.Response(200, text="invalid json api-secret")

    client = client_at(tmp_path, handler)
    try:
        expected = {"http": httpx.HTTPStatusError, "timeout": httpx.ReadTimeout, "json": ValueError}[failure]
        with pytest.raises(expected):
            client.complete_chat_result([{"role": "user", "content": "secret prompt"}])
        start, end = rows(tmp_path)
        assert start["status"] == "running" and end["status"] == "error"
        assert end["http_status"] == {"http": 503, "timeout": None, "json": 200}[failure]
        assert end["usage_source"] == "unavailable"
        assert end["error"]["type"]
        assert "api-secret" not in json.dumps(end)
    finally:
        client.close()


def test_usage_estimation_and_disabled_recording(tmp_path):
    client = client_at(tmp_path, lambda _: response(usage=False))
    try:
        client.complete_chat_result([{"role": "user", "content": "hi"}])
        assert rows(tmp_path)[-1]["usage_source"] == "estimate"
        client.settings.call_log_enabled = False
        client.complete_chat_result([{"role": "user", "content": "hi"}])
        assert len(rows(tmp_path)) == 4
        client.settings.model_request_log_enabled = False
        client.complete_chat_result([{"role": "user", "content": "hi"}])
        assert len(rows(tmp_path)) == 4
    finally:
        client.close()


def test_background_request_finishes_after_origin_scope_and_has_own_call_log(tmp_path):
    entered, release = threading.Event(), threading.Event()

    def handler(_request):
        entered.set()
        assert release.wait(3)
        return response()

    client = client_at(tmp_path, handler)
    log = CallLog(client.settings)

    def background():
        with detached_call_log(), log.scope("tier_expiry_review", {"event_id": "event-1"}):
            return client.complete_chat_result([{"role": "user", "content": "hi"}])

    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            try:
                with trace_scope(correlation_id="turn-1", event_id="event-1", command_id="cmd-1", phase="commit"):
                    with log.scope("record_event", {}, call_id="origin"):
                        job = pool.submit(copy_context().run, background)
                        assert entered.wait(3)
                assert len(rows(tmp_path)) == 1
                assert rows(tmp_path)[0]["status"] == "running"
            finally:
                release.set()
            assert job.result(timeout=3).text == "reply"
        assert rows(tmp_path)[-1]["correlation_id"] == "turn-1"
        assert rows(tmp_path)[-1]["phase"] == "commit"
        logs = [json.loads(path.read_text(encoding="utf-8")) for path in list_call_logs(tmp_path)]
        assert {row["kind"] for row in logs} == {"record_event", "tier_expiry_review"}
        assert all(row["command_id"] == "cmd-1" for row in logs)
        assert current_trace_context() == {}
    finally:
        release.set()
        client.close()


def test_failed_stage_has_partial_call_log_and_causal_span(service, tmp_path):
    service.settings.call_log_enabled = True
    pipeline = InferencePipeline(service, settings=service.settings)
    original = RuntimeError("private original error")

    def fail(_event):
        raise original

    pipeline.parser.parse = fail
    with trace_scope(correlation_id="turn-1", event_id="event-1", command_id="cmd-1", phase="prepare", span_id="cmd-1"):
        with pytest.raises(RuntimeError) as raised:
            pipeline.prepare("hello", session_id="session-1", call_id="turn-1")
    assert raised.value is original
    payload = json.loads(list_call_logs(tmp_path)[0].read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["phase"] == "prepare" and payload["command_id"] == "cmd-1"
    assert [step["kind"] for step in payload["steps"]] == ["ingest", "parse"]
    failed = payload["steps"][-1]
    assert failed["meta"]["status"] == "error"
    assert failed["meta"]["parent_span_id"] == "turn-1"
    assert failed["meta"]["span_id"]
    assert failed["latency_ms"] >= 0
    assert "private original error" not in json.dumps(payload)


def test_diagnostic_failure_never_masks_original_failure(service, tmp_path, monkeypatch):
    service.settings.call_log_enabled = True
    pipeline = InferencePipeline(service, settings=service.settings)
    original = ValueError("original")

    def fail():
        raise original

    def broken_diagnostic(*args, **kwargs):
        raise OSError("telemetry unavailable")

    monkeypatch.setattr(pipeline, "_emit", broken_diagnostic)
    monkeypatch.setattr(service.call_log, "flush", broken_diagnostic)
    with pytest.raises(ValueError) as raised:
        with service.call_log.scope("prepare_response", {}):
            pipeline._traced("parse", fail)
    assert raised.value is original
    client = client_at(tmp_path, lambda _: response())
    try:
        # A telemetry write failure also must not turn a successful request into a retry.
        monkeypatch.setattr("infra.trace.request_spans.Path.open", broken_diagnostic)
        assert client.complete_chat_result([{"role": "user", "content": "hello"}]).text == "reply"
    finally:
        client.close()
