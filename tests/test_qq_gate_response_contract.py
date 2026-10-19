"""QQ gate output stays strict without treating a whole JSON fence as a failure."""
import json

import pytest
from pydantic import ValidationError

from harness.llm.client import ChatResult
from harness.qq import decision
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_decision import (
    isolated_static_parser_cache as isolated_static_parser_cache, setup as setup,
)


VALID = '{"action":"speak","reason":"有新问题可以回应。"}'


@pytest.mark.parametrize("envelope", ["{}", " \n```json\n{}\n```\n", "```\r\n{}\r\n```"])
def test_whole_gate_json_or_fence_validates_once(setup, monkeypatch, envelope):
    _, client, _, kwargs, _ = setup
    client.choice = envelope.format(VALID)
    delegate = client.complete_chat_result
    gate_options = []
    body_options = []

    def complete(messages, **options):
        if decision.is_qq_gate_messages(messages):
            gate_options.append(options)
        else:
            body_options.append((messages, options))
        return delegate(messages, **options)

    monkeypatch.setattr(client, "complete_chat_result", complete)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.outcome == "ready"
    assert len(gate_options) == 1
    assert gate_options[0]["response_format"] == {"type": "json_object"}
    # The exact original gate messages and completion form the next prefix.
    assert any(messages[:len(client.gate_messages[0])] == client.gate_messages[0]
        and messages[len(client.gate_messages[0])] == {"role": "assistant", "content": client.choice}
        for messages, _ in body_options)
    assert all("response_format" not in options for _, options in body_options)
    assert trace.snapshot()["counters"]["gate_response_validated"] == 1


@pytest.mark.parametrize("text", [
    "前言 " + VALID, VALID + " 后记", "[" + VALID + "]", "```json\n" + VALID,
    "```json\n" + VALID + "\n```\n尾注", VALID + VALID,
    '{"action":"send_now","reason":"x"}', '{"action":"speak","reason":"x","force_send":true}',
    '{"action":"speak","reason":"x","addressing":"owner"}',
])
# Strict rollback mode; default repair has separate positive and safety coverage.
def test_ambiguous_or_invalid_gate_is_not_salvaged(legacy_json_mode, text):
    with pytest.raises(ValidationError):
        decision._parse_gate_response(ChatResult(text=text), QQTurnTrace())


@pytest.mark.parametrize("metadata", [
    {"finish_reason": "length"}, {"finish_reason": "tool_calls"}, {"has_tool_calls": True},
    {"has_refusal": True}, {"content_type": "object"},
])
def test_valid_looking_json_cannot_hide_incomplete_or_nontext_response(metadata):
    trace = QQTurnTrace()
    with pytest.raises(ValueError, match="qq_gate_response_contract_invalid"):
        decision._parse_gate_response(ChatResult(text=VALID, **metadata), trace)
    assert trace.snapshot()["counters"]["gate_response_contract_invalid"] == 1


# Strict rollback mode; default repair has separate positive and safety coverage.
def test_validation_diagnostics_name_only_allowlisted_fields_and_error_types(legacy_json_mode):
    trace = QQTurnTrace()
    raw = {"action": "PRIVATE_BAD_ACTION", "reason": None, "PRIVATE_FIELD_NAME": "PRIVATE_VALUE"}
    with pytest.raises(ValidationError):
        decision._parse_gate_response(ChatResult(text=json.dumps(raw)), trace)
    counters = trace.snapshot()["counters"]
    assert counters["gate_invalid_field_action"] == 1
    assert counters["gate_invalid_field_reason"] == 1
    assert counters["gate_invalid_field_other"] == 1
    assert counters["gate_invalid_type_literal_error"] == 1
    assert counters["gate_invalid_type_string_type"] == 1
    assert counters["gate_invalid_type_extra_forbidden"] == 1
    assert "PRIVATE" not in json.dumps(trace.snapshot())


# Strict rollback mode; default repair has separate positive and safety coverage.
def test_invalid_gate_still_falls_back_without_another_model_call(legacy_json_mode, setup):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"speak","reason":"x","PRIVATE_EXTRA":null}'
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.outcome == "error_fallback" and result.error_type == "model_invalid"
    assert len(client.gate_messages) == 1 and not client.chat_calls
    assert trace.snapshot()["counters"]["gate_invalid_type_extra_forbidden"] == 1


# Strict rollback mode; default repair has separate positive and safety coverage.
def test_empty_gate_is_identified_without_inventing_a_wait_decision(legacy_json_mode):
    trace = QQTurnTrace()
    with pytest.raises(ValidationError):
        decision._parse_gate_response(ChatResult(text="  "), trace)
    assert trace.snapshot()["counters"]["gate_response_empty"] == 1
