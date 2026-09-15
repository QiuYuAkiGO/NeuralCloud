"""Diagnostics prove final serialized requests without weakening normal telemetry."""
import hashlib
import json

import httpx
import pytest

from harness.config import Settings
from harness.llm.client import OpenAICompatClient
from infra.trace.diagnostics import diagnostic_scope, observation_scope
from infra.trace.request_spans import trace_scope


def make_client(tmp_path, callback, handler):
    client = OpenAICompatClient(Settings(llm_base_url="https://synthetic.invalid/v1?ignored=query",
        llm_model="selected-model", llm_api_key="CREDENTIAL_DO_NOT_CAPTURE", call_log_enabled=True,
        trace_dir=tmp_path), extra_payload={"thinking": {"type": "enabled"}, "top_p": 0.95},
        request_observer=callback)
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def test_final_bytes_modes_stage_context_and_visible_response(tmp_path):
    records, received = [], []
    def handler(request):
        received.append(request.content)
        return httpx.Response(200, json={"model": "actual-model", "id": "response-1",
            "choices": [{"finish_reason": "length", "message": {"content": "初稿内容",
                "reasoning_content": "HIDDEN_REASONING", "refusal": "PRIVATE_REFUSAL"}}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 9, "prompt_cache_hit_tokens": 2,
                "prompt_cache_miss_tokens": 10, "completion_tokens_details": {"reasoning_tokens": 3}}})
    client = make_client(tmp_path, records.append, handler)
    try:
        with diagnostic_scope(case_id="synthetic-1", revision=2), trace_scope(stage="generate", model_role="main"):
            result = client.complete_chat_result([{"role": "user", "content": "合成输入"}],
                max_tokens=99, reasoning_effort="low", response_format={"type": "json_object"})
    finally:
        client.close()
    before, after = records
    assert before["record_type"] == "http_request_prepared" and not before["payload_verified"]
    assert after["request_id"] == before["request_id"] and after["payload_verified"]
    assert after["request_body_utf8"].encode() == received[0]
    assert after["request_body_sha256"] == hashlib.sha256(received[0]).hexdigest()
    assert after["payload"]["thinking"] == {"type": "enabled"}
    assert after["payload"]["reasoning_effort"] == "low" and after["payload"]["max_tokens"] == 99
    assert after["trace_context"]["stage"] == "generate" and after["diagnostic_context"]["revision"] == 2
    assert after["response_model"] == "actual-model" and after["response_content"] == result.text == "初稿内容"
    assert after["finish_reason"] == "length" and after["has_refusal"]
    serialized = json.dumps(records, ensure_ascii=False)
    telemetry = (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8")
    for hidden in ["CREDENTIAL_DO_NOT_CAPTURE", "HIDDEN_REASONING", "PRIVATE_REFUSAL"]:
        assert hidden not in serialized + telemetry
    assert "合成输入" not in telemetry and "初稿内容" not in telemetry


def test_context_opt_in_is_scoped(tmp_path):
    records = []
    client = make_client(tmp_path, None, lambda req: httpx.Response(200,
        json={"choices": [{"message": {"content": "reply"}}]}))
    try:
        client.complete_chat_result([])
        assert not records
        with observation_scope(records.append):
            client.complete_chat_result([])
        client.complete_chat_result([])
    finally:
        client.close()
    assert len(records) == 2
    assert not records[-1]["request_changed_after_prepare"]
    assert records[-1]["payload"]["model"] == "selected-model"


@pytest.mark.parametrize("mutation_source", ["request", "response", "auth"])
def test_mutable_http_hooks_and_auth_fail_before_transmission(tmp_path, mutation_source):
    records, received = [], []
    def handler(request):
        received.append(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "reply"}}]})
    client = make_client(tmp_path, None, handler)
    def mutate(value):
        request = value.request if isinstance(value, httpx.Response) else value
        request._content = b'{"model":"falsified"}'
        request.stream = httpx.ByteStream(request._content)
    if mutation_source == "auth":
        client._client.auth = httpx.BasicAuth("synthetic", "synthetic")
    else:
        client._client.event_hooks[mutation_source].append(mutate)
    try:
        with observation_scope(records.append), pytest.raises(ValueError, match="hooks_or_auth"):
            client.complete_chat_result([])
        assert not received and not records
        # Normal production operation retains the existing client behavior.
        client.complete_chat_result([])
        assert len(received) == 1 and not records
    finally:
        client.close()


@pytest.mark.parametrize("failure", ["timeout", "http", "json", "empty"])
def test_failures_and_empty_content_are_retained_without_exception_text(tmp_path, failure):
    records = []
    def handler(request):
        if failure == "timeout":
            raise httpx.ReadTimeout("PRIVATE_EXCEPTION", request=request)
        if failure == "http":
            return httpx.Response(503, json={"error": "PRIVATE_ERROR"})
        if failure == "json":
            return httpx.Response(200, content=b"PRIVATE_INVALID_JSON")
        return httpx.Response(200, json={"choices": [{"message": {"content": None}}]})
    client = make_client(tmp_path, records.append, handler)
    try:
        if failure == "empty":
            assert client.complete_chat_result([]).text == ""
            assert records[-1]["response_content"] is None
        else:
            with pytest.raises((httpx.HTTPError, ValueError)):
                client.complete_chat_result([])
            assert records[-1]["record_type"] == "http_request_error"
    finally:
        client.close()
    assert "PRIVATE" not in json.dumps(records)


def test_sink_failure_and_hidden_reasoning_in_request_stop_before_network(tmp_path):
    received = []
    client = make_client(tmp_path, lambda row: (_ for _ in ()).throw(OSError("sink unavailable")),
        lambda req: received.append(req))
    try:
        with pytest.raises(OSError):
            client.complete_chat_result([])
        with pytest.raises(ValueError, match="sensitive_field"):
            client.complete_chat_result([{"role": "assistant", "reasoning_content": "PRIVATE"}])
    finally:
        client.close()
    assert not received


def test_full_pipeline_keeps_raw_initial_candidate_revision_and_final(tmp_path):
    from datetime import UTC, datetime

    from eval.homeostatic_expression.evaluation import (
        DEFAULT_PERSONA_PATH, DEFAULT_PROFILE_PATH, build_summary, default_scenarios,
    )
    from eval.homeostatic_expression.production import ProductionPairPipeline
    from harness.core.inference import ValidationResult
    from harness.homeostasis import load_profile

    replies = iter(["  我已经休息好了。  ", "  我想先缓一缓。  "])
    records = []
    client = make_client(tmp_path, None, lambda req: httpx.Response(200,
        json={"model": "synthetic", "choices": [{"message": {"content": next(replies)}}]}))
    pair = ProductionPairPipeline(llm=client, persona_path=DEFAULT_PERSONA_PATH,
        fixed_user_text="合成测试：接下来呢？", fixed_at=datetime(2026, 9, 14, tzinfo=UTC),
        generator_model="synthetic", runtime_model_version="synthetic", prompt_version="synthetic",
        temperature=0.7, max_tokens=256)
    class Validator:
        def should_run(self, path):
            return True

        def check(self, candidate, **kwargs):
            return ValidationResult(verdict="revise", violations=["synthetic_unperformed_action"],
                instruction="不得声称已经完成休息") if candidate.revision == 0 else ValidationResult()

    pair.pipeline.validator = Validator()
    summary = build_summary(default_scenarios()[0], profile_ref=load_profile(DEFAULT_PROFILE_PATH).profile_ref,
                            marked=True, snapshot_revision=3)
    try:
        with observation_scope(records.append), diagnostic_scope(case_id="synthetic-revision-chain"):
            prepared = pair.prepare(condition="synthetic", event_id="synthetic-event", summary=summary)
    finally:
        pair.close()
        client.close()
    responses = [r for r in records if r["record_type"] == "http_response_received"]
    stages = [r for r in records if r["record_type"] == "pipeline_stage"]
    assert [r["response_content"] for r in responses] == ["  我已经休息好了。  ", "  我想先缓一缓。  "]
    assert [r["diagnostic_context"]["revision"] for r in responses] == [0, 1]
    assert [r["candidate_text"] for r in stages if "candidate_text" in r] == ["我已经休息好了。", "我想先缓一缓。"]
    assert [r["validation"]["verdict"] for r in stages if "validation" in r] == ["revise", "pass"]
    assert stages[-1]["final_text"] == prepared.final.text == "我想先缓一缓。"
    assert stages[-1]["revisions"] == 1
    compiled = next(r["compiled_context"] for r in stages if "compiled_context" in r)
    assert any(s["kind"] == "homeostatic_interoception" for s in compiled["segments"])
    assert "合成测试" not in (tmp_path / "model_requests.jsonl").read_text(encoding="utf-8")
