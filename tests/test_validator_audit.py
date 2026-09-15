"""Validator evidence stays in existing call logs, outside the model request."""

from __future__ import annotations

import hashlib
import json

import httpx
import pytest

from harness.config import Settings
from harness.validator.audit import validation_input_audit
from infra.trace.call_log import CallLog, list_call_logs
from infra.trace.request_spans import current_trace_context, trace_scope


def _settings(tmp_path, *, enabled=True):
    return Settings(call_log_enabled=enabled, trace_dir=tmp_path,
        llm_api_key="credential-must-never-be-logged",
        llm_base_url="https://user:password@example.invalid/v1?api_key=secret")


def _inputs():
    return {
        "original_evidence": {"audience": "qq:group:10001", "current_source_ids": ["qq:original:01"],
            "entries": [{"source_id": "qq:original:01", "speaker": "qq:member:20001",
                "text": "你觉得这张图怎么样？", "evidence_refs": ["qq-protocol://sha256/full-provenance"]}]},
        "projection": {"rows": [["p1", "你觉得这张图怎么样？"]],
            "speaker_aliases": {"p1": "qq:member:20001"},
            "source_map": {"0": {"source_id": "qq:original:01", "original_index": 0}}},
        "system": "只校验候选正文。\n不要回复用户。",
        "user": '{"candidate":"颜色很好看。","rows":[["p1","你觉得这张图怎么样？"]]}',
    }


def _records(settings):
    return [json.loads(path.read_text(encoding="utf-8")) for path in list_call_logs(settings)]


def _metrics(text):
    return {"sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
        "characters": len(text), "utf8_bytes": len(text.encode("utf-8"))}


def test_standalone_audit_records_exact_before_after_output_and_correlation(tmp_path):
    settings = _settings(tmp_path)
    inputs = _inputs()
    raw = {"verdict": "pass", "violations": [], "instruction": None}
    with trace_scope(correlation_id="qq-trace:fixture", event_id="qq-event:fixture",
            command_id="command:fixture", phase="qq_persona", stage="validate"):
        with validation_input_audit(settings, **inputs) as audit:
            audit.set_result(raw)

    record, = _records(settings)
    assert record["kind"] == "validator" and record["ok"] is True
    assert record["correlation_id"] == "qq-trace:fixture"
    assert record["event_id"] == "qq-event:fixture" and record["command_id"] == "command:fixture"
    before, after, output = record["steps"]
    assert [step["kind"] for step in record["steps"]] == [
        "validator_input_before", "validator_input_after", "validator_output"]
    assert before["data"]["original_evidence"] == inputs["original_evidence"]
    assert after["data"]["projection"] == inputs["projection"]
    assert after["data"]["model_input"] == {"system": inputs["system"], "user": inputs["user"]}
    assert after["data"]["system_metrics"] == _metrics(inputs["system"])
    assert after["data"]["user_metrics"] == _metrics(inputs["user"])
    for data, value_key, metric_key in [
        (before["data"], "original_evidence", "original_evidence_metrics"),
        (after["data"], "projection", "projection_metrics"),
        (after["data"], "model_input", "model_input_metrics"),
        (output["data"], "raw_validation", "raw_validation_metrics"),
    ]:
        canonical = json.dumps(data[value_key], ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        assert {key: data[metric_key][key] for key in _metrics(canonical)} == _metrics(canonical)
    assert output["data"]["raw_validation"] == raw
    assert output["data"]["result_recorded"] is True and output["data"]["status"] == "success"
    assert record["result"] == output["data"]
    assert len({step["meta"]["audit_id"] for step in record["steps"]}) == 1
    assert all(step["meta"]["stage"] == "validate" for step in record["steps"])
    serialized = json.dumps(record)
    assert "credential-must-never-be-logged" not in serialized
    assert "password" not in serialized and "api_key=secret" not in serialized


def test_model_exception_records_partial_audit_without_headers_or_exception_text(tmp_path):
    settings = _settings(tmp_path)
    request = httpx.Request("POST", "https://example.invalid/secret-route", headers={"Authorization": "Bearer SECRET"})
    response = httpx.Response(400, request=request, headers={"Set-Cookie": "private-cookie"})
    error = httpx.HTTPStatusError("SECRET private upstream body", request=request, response=response)
    with pytest.raises(httpx.HTTPStatusError) as raised:
        with validation_input_audit(settings, **_inputs()):
            raise error
    assert raised.value is error
    record, = _records(settings)
    assert record["ok"] is False
    assert record["error"] == {"type": "HTTPStatusError", "message": "call failed"}
    output = record["steps"][-1]["data"]
    assert output["status"] == "error" and output["error_type"] == "HTTPStatusError"
    assert output["result_recorded"] is False and output["raw_validation"] is None
    serialized = json.dumps(record)
    for secret in ("Authorization", "Set-Cookie", "SECRET", "private-cookie", "secret-route", "private upstream body"):
        assert secret not in serialized


def test_nested_audit_reuses_parent_call_and_preserves_parent_result(tmp_path):
    settings = _settings(tmp_path)
    log = CallLog(settings)
    with trace_scope(correlation_id="parent-correlation"):
        with log.scope("prepare_response", {"input": "original"}, call_id="parent-call") as parent:
            # Set this before nested audits: setting it afterward could conceal
            # an accidental overwrite of the already prepared parent result.
            parent.set_result({"text": "最终正文"})
            parent_context = current_trace_context()
            for verdict in ("revise", "pass"):
                with trace_scope(stage="validate"):
                    with validation_input_audit(settings, **_inputs()) as audit:
                        assert log.active().call_id == "parent-call"
                        audit.set_result({"verdict": verdict})
                assert current_trace_context() == parent_context
    record, = _records(settings)
    assert record["call_id"] == "parent-call" and record["kind"] == "prepare_response"
    assert record["result"] == {"text": "最终正文"}
    assert len(record["steps"]) == 6
    assert len({step["meta"]["audit_id"] for step in record["steps"]}) == 2
    assert all(step["meta"]["correlation_id"] == "parent-correlation" for step in record["steps"])


def test_nested_model_error_remains_visible_when_caller_handles_it(tmp_path):
    settings = _settings(tmp_path)
    with CallLog(settings).scope("prepare_response", {}) as parent:
        with pytest.raises(ValueError):
            with validation_input_audit(settings, **_inputs()) as audit:
                audit.set_result({"verdict": "invalid-response"})
                raise ValueError("private parse detail")
        parent.set_result({"fallback": True})
    record, = _records(settings)
    assert record["ok"] is True and record["result"] == {"fallback": True}
    output = record["steps"][-1]["data"]
    assert output["status"] == "error" and output["error_type"] == "ValueError"
    assert output["raw_validation"] == {"verdict": "invalid-response"}
    assert "private parse detail" not in json.dumps(record)


@pytest.mark.parametrize("nested", [False, True])
def test_disabled_audit_does_not_snapshot_or_append_to_an_enabled_parent(tmp_path, monkeypatch, nested):
    settings = _settings(tmp_path, enabled=False)
    inputs = _inputs()
    original_dumps = json.dumps(inputs, ensure_ascii=False)

    def forbidden(*args, **kwargs):
        pytest.fail("disabled audit serialized an input or result")

    monkeypatch.setattr("harness.validator.audit.dump_value", forbidden)

    def invoke():
        with validation_input_audit(settings, **inputs) as audit:
            audit.set_result({"verdict": "pass"})

    if nested:
        with CallLog(_settings(tmp_path)).scope("parent", {}) as parent:
            invoke()
            parent.set_result({"ok": True})
        record, = _records(settings)
        assert record["steps"] == []
    else:
        invoke()
        assert not list_call_logs(settings) and not tmp_path.joinpath("calls").exists()
    assert json.dumps(inputs, ensure_ascii=False) == original_dumps


def test_audit_snapshots_inputs_and_result_without_mutating_or_exposing_provenance(tmp_path):
    settings = _settings(tmp_path)
    inputs = _inputs()
    expected_inputs = json.loads(json.dumps(inputs))
    calls = []

    def model(*, system, user):
        calls.append({"system": system, "user": user})
        return {"verdict": "pass", "violations": []}

    with validation_input_audit(settings, **inputs) as audit:
        raw = model(system=inputs["system"], user=inputs["user"])
        audit.set_result(raw)
        inputs["original_evidence"]["entries"][0]["text"] = "changed after call"
        inputs["projection"]["source_map"]["0"]["original_index"] = 99
        raw["violations"].append("changed after result")
    record, = _records(settings)
    before, after, output = [step["data"] for step in record["steps"]]
    assert before["original_evidence"] == expected_inputs["original_evidence"]
    assert after["projection"] == expected_inputs["projection"]
    assert output["raw_validation"] == {"verdict": "pass", "violations": []}
    assert calls == [{"system": expected_inputs["system"], "user": expected_inputs["user"]}]
    actual_model_input = json.dumps(calls)
    for private_ref in ("qq:original:01", "qq:member:20001", "qq-protocol://", "source_map", "audit_id", "correlation_id"):
        assert private_ref not in actual_model_input
