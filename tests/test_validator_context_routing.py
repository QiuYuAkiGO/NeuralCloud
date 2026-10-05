"""A large admitted QQ evidence set must not be rejected by an 8K helper."""
from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from harness.config import Settings
from harness.core.inference import CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry
from harness.llm.client import OpenAICompatClient, RoutingLLMClient
from harness.qq.decision import _QQScopedLLM
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from harness.validator.validator import ResponseValidator
from infra.trace.request_spans import trace_scope


def _evidence(*, large=False):
    audience = "qq:group:10001"
    return MemoryEvidenceContext(audience=audience, current_speaker_id="qq:member:20001", entries=[
        MemoryEvidenceEntry(source_id=f"source-{index}", text=f"记录{index}：" + "完整的历史记录。" * (2000 if large else 1),
            audience=audience, source_channel="qq_group", source_conversation_id=audience,
            speaker_id=f"qq:member:{20001 + index}", source_refs=[f"verified-source-{index}"],
            status="user_claim", segment_sha256="a" * 64)
        for index in range(3)
    ])


def _routed(tmp_path, *, main_status=200, model="deepseek-v4-flash", context=8192, qq_turn=False):
    captured = []
    settings = Settings(llm_base_url="https://api.deepseek.com/v1", llm_model=model,
        llm_api_key="PRIVATE_KEY", aux_llm_num_ctx=context, call_log_enabled=True, trace_dir=tmp_path)
    main = OpenAICompatClient(settings)
    aux = OpenAICompatClient(settings.model_copy(update={"llm_base_url": "http://helper.invalid/v1",
                                                       "llm_model": "small-helper"}))

    def serve(request):
        payload = json.loads(request.content)
        role = "aux" if request.url.host == "helper.invalid" else "main"
        captured.append((role, payload, request.extensions["timeout"]))
        # Reproduce the production helper's rejection rather than accepting
        # arbitrary-size prompts in a scripted model double.
        if role == "aux" and sum(len(item["content"]) for item in payload["messages"]) > context:
            return httpx.Response(400, json={"error": "input exceeds available context"})
        if role == "main" and main_status != 200:
            return httpx.Response(main_status, json={"error": "synthetic main failure"})
        content = json.dumps({"verdict": "pass", "violations": [], "severity": 0, "instruction": None})
        if qq_turn:
            from harness.qq.decision import is_qq_gate_messages
            from harness.validator.validator import _LLM_SYSTEM
            messages = payload.get("messages", payload.get("input"))
            system = messages[0]["content"]
            if is_qq_gate_messages(messages):
                content = json.dumps({"action": "speak", "reason": "synthetic direct question"})
            elif role == "aux":
                content = json.dumps({"path": "fast"})
            elif not system.startswith(_LLM_SYSTEM):
                content = "这个配色挺好看。[END]"
        if request.url.path == "/responses":
            return httpx.Response(200, json={"status": "completed", "model": "deepseek-flash", "output": [
                {"type": "message", "content": [{"type": "output_text", "text": content}]}]})
        return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})

    for client in (main, aux):
        client._client.close()
        client._client = httpx.Client(transport=httpx.MockTransport(serve))
    return RoutingLLMClient(main, aux), captured


@pytest.mark.parametrize("large,expected_role", [(False, "aux"), (True, "main")])
def test_critic_routes_complete_evidence_with_bounded_output_and_qq_deadline(tmp_path, large, expected_role):
    routed, captured = _routed(tmp_path)
    evidence = _evidence(large=large)
    trace = QQTurnTrace()
    scoped = _QQScopedLLM(routed, deadline=QQDeadline.after(10), trace=trace, timeout=60)
    scoped.stage_name = "persona"
    try:
        with trace_scope(phase="qq_persona", stage="validate", correlation_id="synthetic-qq"):
            result = ResponseValidator(llm=scoped).check(CandidateResponse(text="这个配色挺好看。"),
                persona=None, memory_evidence=evidence)
    finally:
        routed.close()

    assert result.verdict == "pass" and not result.skipped
    assert len(captured) == 1
    role, payload, timeout = captured[0]
    assert role == expected_role
    assert 0 < timeout["read"] <= 10
    assert payload["max_tokens"] == (4096 if large else 1024)
    assert payload.get("reasoning_effort") == ("low" if large else None)
    assert "response_format" not in payload
    actual = json.loads(payload["messages"][1]["content"])["memory_evidence"]
    assert [dict(zip(actual["columns"], row))["text"] for row in actual["rows"]] == [
        entry.text for entry in evidence.entries]
    assert "source_refs" not in json.dumps(actual) and "segment_sha256" not in json.dumps(actual)
    assert scoped.failed_error_type is None
    assert trace.snapshot()["counters"] == {"model_calls": 1, "persona_model_calls": 1}
    records = [json.loads(line) for line in (tmp_path / "model_requests.jsonl").read_text().splitlines()]
    finished = records[-1]
    assert finished["stage"] == "validate" and finished["role"] == expected_role
    assert finished["http_status"] == 200
    assert "PRIVATE_KEY" not in json.dumps(records)
    assert evidence.entries[-1].text not in json.dumps(records, ensure_ascii=False)


def test_utf8_input_and_output_reserve_trigger_route_before_aux_window_overflow(tmp_path):
    routed, captured = _routed(tmp_path, context=2000)
    try:
        # 900 code points fit 2000, but 2700 UTF-8 bytes plus the template and
        # completion reserve do not. A character-count check is insufficient.
        routed.complete_validation_json(system="critic", user="图" * 900)
    finally:
        routed.close()
    assert [item[0] for item in captured] == ["main"]


def test_main_http_failure_stays_visible_and_is_not_a_validation_pass(tmp_path):
    routed, captured = _routed(tmp_path, main_status=400)
    trace = QQTurnTrace()
    scoped = _QQScopedLLM(routed, deadline=QQDeadline.after(10), trace=trace, timeout=60)
    scoped.stage_name = "persona"
    try:
        with pytest.raises(httpx.HTTPStatusError):
            ResponseValidator(llm=scoped).check(CandidateResponse(text="配色挺好看。"),
                persona=None, memory_evidence=_evidence(large=True))
    finally:
        routed.close()
    assert [item[0] for item in captured] == ["main"]
    assert scoped.failed_error_type == "model_http"
    assert trace.snapshot()["counters"]["failed_model_calls"] == 1


def test_other_main_models_do_not_receive_deepseek_reasoning_option(tmp_path):
    routed, captured = _routed(tmp_path, model="another-model")
    try:
        ResponseValidator(llm=routed).check(CandidateResponse(text="配色挺好看。"),
            persona=None, memory_evidence=_evidence(large=True))
    finally:
        routed.close()
    assert captured[0][0] == "main"
    assert "reasoning_effort" not in captured[0][1]


def test_short_aux_validation_does_not_change_other_helper_payloads(tmp_path):
    routed, captured = _routed(tmp_path)
    schema = {"type": "json_schema", "json_schema": {"name": "emotion", "strict": True}}
    try:
        routed.complete_validation_json(system="critic", user="brief response")
        routed.complete_json(system="emotion", user="small input", max_tokens=384, response_format=schema)
    finally:
        routed.close()
    assert [item[0] for item in captured] == ["aux", "aux"]
    assert captured[0][1]["max_tokens"] == 1024
    assert captured[1][1]["max_tokens"] == 384 and captured[1][1]["response_format"] == schema


def test_scoped_legacy_client_keeps_existing_validation_entrypoint():
    class Legacy:
        def complete_json(self, *, system, user, timeout_seconds):
            assert 0 < timeout_seconds <= 10
            return {"verdict": "pass"}

    trace = QQTurnTrace()
    scoped = _QQScopedLLM(Legacy(), deadline=QQDeadline.after(10), trace=trace, timeout=60)
    result = ResponseValidator(llm=scoped).check(CandidateResponse(text="配色挺好看。"), persona=None)
    assert result.verdict == "pass" and trace.snapshot()["counters"]["model_calls"] == 1


def test_qq_turn_keeps_large_admitted_history_and_finishes_ready(tmp_path, monkeypatch):
    from harness.qq import decision
    from harness.qq.memory import QQVisualMemoryStore
    from tests.test_qq_decision import NOW, add_member

    store = QQVisualMemoryStore(tmp_path / "qq.sqlite3")
    _, observation = add_member(store, "20001", text="Misty，你觉得这个配色怎么样？")
    receipts = [decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        delivered_text=f"历史内容{index}：" + "配色讨论。" * 1000,
        evidence_refs=[f"test-evidence://sent-{index}"], source_record_ids=[observation.record_id],
        verification="qq_ui_verified", delivered_at=NOW) for index in range(3)]
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"mode": "live", "model": "deepseek-v4-flash",
        "base_url": "https://api.deepseek.com/v1", "api_key": "synthetic-key"}))
    preset_path = Path(__file__).resolve().parents[1] / "presets/builtin/misty.json"
    settings, preset = decision._load_configuration(config, preset_path)
    settings = settings.model_copy(update={"parser_llm_enabled": False, "validator_sample_rate": 1,
        "max_revisions": 0, "aux_llm_base_url": "http://helper.invalid/v1", "aux_llm_model": "small-helper"})
    routed, captured = _routed(tmp_path, qq_turn=True)
    monkeypatch.setattr(decision, "_load_configuration", lambda *args: (settings, preset))
    monkeypatch.setattr(decision, "build_llm_client", lambda settings: routed)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(memory_db=store.path, group_id="10001", self_member_id="29999",
        config_path=config, preset_path=preset_path, trigger_member_id="20001", request="评价一下配色。", trace=trace)

    assert result.outcome == "ready" and result.action == "speak"
    assert result.reply_style == "conversational"
    assert result.reply == "这个配色挺好看" and result.error_type is None
    critic = [(role, json.loads(payload["messages"][1]["content"])) for role, payload, _ in captured
              if payload.get("messages", [{}])[0].get("content", "").startswith("You are a persona consistency")]
    assert len(critic) == 1 and critic[0][0] == "main"
    projected = critic[0][1]["memory_evidence"]
    entries = [dict(zip(projected["columns"], row)) for row in projected["rows"]]
    assert {item["text"] for item in entries} == {receipt.delivered_text for receipt in receipts}
    assert set(result.delivery_refs) == {receipt.receipt_id for receipt in receipts}
    assert trace.snapshot()["counters"].get("failed_model_calls", 0) == 0
