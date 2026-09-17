from __future__ import annotations

import json
import sqlite3

import httpx
import pytest

from harness.config import Settings
from harness.core.inference import TokenUsage
from harness.llm.client import OpenAICompatClient, usage_from_api_payload
from infra.trace.request_spans import trace_scope
from tools.qq_cache_report import summarize_monitor, summarize_requests, summarize_traces


@pytest.mark.parametrize("cache,expected", [
    ({}, (0, 0, 0, 100, "unknown")),
    ({"prompt_cache_hit_tokens": 0}, (0, 100, 100, 0, "known")),
    ({"prompt_cache_miss_tokens": 100}, (0, 100, 100, 0, "known")),
    ({"prompt_cache_hit_tokens": 100}, (100, 0, 100, 0, "known")),
    ({"prompt_tokens_details": {"cached_tokens": 0}}, (0, 100, 100, 0, "known")),
    ({"prompt_cache_hit_tokens": 0, "prompt_tokens_details": {"cached_tokens": 90}}, (0, 100, 100, 0, "known")),
    ({"prompt_cache_hit_tokens": 80, "prompt_cache_miss_tokens": 0}, (0, 0, 0, 100, "unknown")),
    ({"prompt_cache_hit_tokens": True}, (0, 0, 0, 100, "unknown")),
    ({"prompt_cache_hit_tokens": 101}, (0, 0, 0, 100, "unknown")),
])
def test_provider_cache_absence_zero_and_inconsistent_fields(cache, expected):
    usage = usage_from_api_payload({"prompt_tokens": 100, "completion_tokens": 2, **cache}, [], "")
    assert (usage.cache_hit_tokens, usage.cache_miss_tokens, usage.cache_known_prompt_tokens,
            usage.cache_unknown_prompt_tokens, usage.cache_status) == expected


def test_cache_coverage_survives_aggregation_and_legacy_serialization():
    unknown = TokenUsage.model_validate({"prompt_tokens": 900, "cache_hit_tokens": 0,
                                        "cache_miss_tokens": 0, "source": "api"}).for_call()
    known = usage_from_api_payload({"prompt_tokens": 100, "prompt_cache_hit_tokens": 50}, [], "").for_call()
    total = unknown.add(known)
    restored = TokenUsage.model_validate(total.model_dump())
    assert restored.prompt_tokens == 1000 and restored.calls == 2
    assert restored.cache_known_prompt_tokens == 100
    assert restored.cache_unknown_prompt_tokens == 900 and restored.cache_status == "partial"
    assert restored.cache_hit_tokens == restored.cache_miss_tokens == 50
    assert unknown.cache_status == "unknown"


def test_partition_can_supply_an_omitted_prompt_total():
    usage = usage_from_api_payload({"prompt_cache_hit_tokens": 20, "prompt_cache_miss_tokens": 5}, [], "")
    assert usage.prompt_tokens == usage.cache_known_prompt_tokens == 25
    assert usage.cache_status == "known"


def _write_requests(path, rows):
    path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")


def _request(span, usage, **extra):
    return {"record_type": "model_request_finished", "span_id": span,
            "started_at": "2026-09-16T10:00:00+08:00", "request_model": "deepseek-v4-flash",
            "response_model": "deepseek-flash", "role": "main", "phase": "qq_gate", "status": "success",
            "usage": usage, "usage_source": "api", "latency_ms": 100, **extra}


def test_request_report_uses_covered_denominator_and_preserves_unknown_same_hour(tmp_path):
    known = usage_from_api_payload({"prompt_tokens": 100, "completion_tokens": 20,
                                   "prompt_cache_hit_tokens": 50}, [], "").model_dump()
    unknown = {"prompt_tokens": 900, "completion_tokens": 80, "source": "api"}
    row = _request("known", known)
    path = tmp_path / "requests.jsonl"
    _write_requests(path, [{**row, "record_type": "model_request_started", "usage": None}, row, row,
                           _request("unknown", unknown, phase="qq_persona")])
    report = summarize_requests(path)
    totals = report["totals"]
    assert totals["calls"] == 2 and totals["hit_rate"] == .5
    assert totals["cache_coverage"] == .1 and totals["cache_unknown_prompt_tokens"] == 900
    assert totals["prompt_tokens"] == 1000 and totals["completion_tokens"] == 100
    assert totals["estimated_usd"] is None and totals["latency_ms"]["samples"] == 2
    assert set(report["hours"]) == {"2026-09-16T02"}
    assert {group["stage"] for group in report["groups"]} == {"qq_gate", "qq_persona"}
    assert all(group["request_model"] == "deepseek-v4-flash" for group in report["groups"])


def test_request_report_counts_interruptions_and_returned_error_usage(tmp_path):
    path = tmp_path / "requests.jsonl"
    _write_requests(path, [_request("late", {"prompt_tokens": 100, "cache_hit_tokens": 40,
                                            "cache_miss_tokens": 60}, status="error"),
                           _request("interrupted", None, record_type="model_request_started", status="running")])
    totals = summarize_requests(path)["totals"]
    assert totals["calls"] == 2 and totals["hit_rate"] == .4
    assert totals["cache_coverage"] is None and totals["cache_coverage_of_recorded_input"] == 1
    assert totals["input_usage_unknown_calls"] == 1 and not totals["prompt_tokens_complete"]


def test_legacy_trace_does_not_claim_complete_input_or_output():
    totals = summarize_traces([{"counters": {"gate_cache_hit_tokens": 80, "gate_cache_miss_tokens": 20}}])["totals"]
    assert totals["hit_rate"] == .8 and totals["cache_coverage"] is None
    assert not totals["prompt_tokens_complete"] and not totals["completion_tokens_complete"]


def test_monitor_report_is_read_only_and_legacy_zeroes_are_unknown(tmp_path):
    path = tmp_path / "monitor.sqlite3"
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE monitor_runs (started_at TEXT, prompt_tokens INTEGER, completion_tokens INTEGER)")
        connection.execute("INSERT INTO monitor_runs VALUES ('2026-09-16T00:00:00+00:00', 100, 10)")
    before = path.read_bytes()
    totals = summarize_monitor(path)["totals"]
    assert totals["hit_rate"] is None and totals["cache_coverage"] is None
    assert totals["cache_coverage_of_recorded_input"] == 0
    assert totals["cache_unknown_prompt_tokens"] == 100 and totals["cache_miss_tokens"] == 0
    assert path.read_bytes() == before
    with pytest.raises(sqlite3.OperationalError):
        summarize_monitor(tmp_path / "absent.sqlite3")
    assert not (tmp_path / "absent.sqlite3").exists()


def test_partial_provider_usage_and_estimates_do_not_claim_complete_api_coverage(tmp_path):
    partial = usage_from_api_payload({"completion_tokens": 0}, [{"role": "user", "content": "input"}], "")
    assert partial.prompt_tokens_source == "unknown" and partial.completion_tokens_source == "api"
    estimated = usage_from_api_payload({}, [{"role": "user", "content": "input"}], "answer")
    assert estimated.prompt_tokens_source == estimated.completion_tokens_source == "estimate"
    path = tmp_path / "requests.jsonl"
    _write_requests(path, [_request("partial", partial.model_dump()),
                           _request("estimated", estimated.model_dump(), usage_source="estimate")])
    totals = summarize_requests(path)["totals"]
    assert totals["api_usage_calls"] == 1 and totals["api_usage_call_coverage"] == .5
    assert totals["input_api_calls"] == 0 and totals["output_api_calls"] == 1
    assert not totals["prompt_tokens_complete"] and not totals["completion_tokens_complete"]
    assert totals["estimated_prompt_tokens"] == estimated.prompt_tokens
    assert totals["cache_coverage"] is None


def test_trace_keeps_provider_input_and_output_coverage_separate():
    from harness.qq.telemetry import QQTurnTrace

    trace = QQTurnTrace()
    trace.add_counter("gate_model_calls")
    trace.add_cache_usage("gate", usage_from_api_payload({"prompt_tokens": 100, "prompt_cache_hit_tokens": 50}, [], ""))
    totals = summarize_traces([trace.snapshot()])["totals"]
    assert totals["hit_rate"] == .5 and totals["cache_coverage"] == 1
    assert totals["prompt_tokens_complete"] and not totals["completion_tokens_complete"]


def _client(tmp_path, observer=None):
    settings = Settings(trace_dir=tmp_path, call_log_enabled=False, model_request_log_enabled=True,
                        llm_model="deepseek-v4-flash", llm_base_url="https://example.invalid", llm_api_key="private-key")
    client = OpenAICompatClient(settings, request_observer=observer)
    client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json={
        "model": "deepseek-flash", "choices": [{"message": {"content": "private-response"}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 10, "prompt_cache_hit_tokens": 0}})))
    return client


@pytest.mark.parametrize("observed", [False, True])
def test_returned_usage_survives_expired_deadline_in_both_http_paths(tmp_path, monkeypatch, observed):
    attempts = 0

    def deadline():
        nonlocal attempts
        attempts += 1
        if attempts > 1:
            raise TimeoutError("private deadline details")
        return 10

    monkeypatch.setattr("harness.llm.deadline.remaining_model_seconds", deadline)
    client = _client(tmp_path, observer=(lambda _: None) if observed else None)
    try:
        with pytest.raises(TimeoutError):
            client.complete_chat_result([{"role": "user", "content": "private-prompt"}])
    finally:
        client.close()
    text = (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8")
    end = json.loads(text.splitlines()[-1])
    assert end["status"] == "error" and end["http_status"] == 200
    assert end["usage"]["cache_miss_tokens"] == 100 and end["usage"]["cache_status"] == "known"
    assert end["request_model"] == "deepseek-v4-flash" and end["response_model"] == "deepseek-flash"
    assert all(secret not in text for secret in ("private-prompt", "private-response", "private-key", "private deadline"))


def test_declared_stable_prefix_hash_excludes_dynamic_tail_and_no_plaintext(tmp_path):
    client = _client(tmp_path)
    prefix = [{"role": "system", "content": "private-system"}, {"role": "user", "content": "private-fixed-scope"}]
    try:
        with trace_scope(phase="qq_gate", stable_prefix_message_count=2):
            client.complete_chat_result(prefix + [{"role": "user", "content": "private-turn-one"}])
            client.complete_chat_result(prefix + [{"role": "user", "content": "private-turn-two"}])
        client.complete_chat_result(prefix + [{"role": "user", "content": "private-turn-three"}])
    finally:
        client.close()
    text = (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8")
    ends = [json.loads(line) for line in text.splitlines()][1::2]
    assert ends[0]["stable_prefix"] == ends[1]["stable_prefix"]
    assert ends[0]["stable_prefix"]["message_count"] == 2
    assert ends[2]["stable_prefix"]["message_count"] == 1
    assert ends[0]["stable_prefix"]["characters"] > 0
    assert "private-system" not in text and "private-fixed-scope" not in text and "private-turn" not in text
