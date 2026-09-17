"""QQ accepts four times the input without extending generation or deadlines."""

from datetime import timedelta
import json
import shutil

import pytest

from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.config import Settings
from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.qq import decision
from harness.qq.conversation import private_scope_id
from harness.qq.memory import protocol_message_evidence_ref
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_decision import (
    NOW, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup,
)
from tests.test_qq_decision_context_budget import _gate_payload, _persona_payload
from tests.test_qq_memory_reply import _binding
from tests.test_qq_protocol_memory import EVIDENCE, protocol_observation, protocol_profile


SELF = "29999"
PEER = "2285027817"


@pytest.fixture(autouse=True)
def use_default_input_budget(monkeypatch):
    monkeypatch.delenv("NC_QQ_INPUT_TOKEN_BUDGET", raising=False)
    monkeypatch.delenv("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", raising=False)


def _observations(setup, monkeypatch, *, private, count, body_length):
    store, client, _, args, _ = setup
    group = private_scope_id(SELF, PEER) if private else "10001"
    member = PEER if private else "20003"
    _, profile = protocol_profile(store, group=group, member=member)
    for index in range(count):
        message_id = str(7000 + index)
        store.record_protocol_observation(protocol_observation(profile,
            text=f"INPUT_BUDGET_MARKER_{index}:" + "原" * body_length,
            capture_id=f"input-budget-{index}", capture_item_id=str(index),
            observed_at=NOW + timedelta(seconds=index), message_id=message_id,
            attribution_evidence=[EVIDENCE, protocol_message_evidence_ref(group, member, message_id)],
            image_analysis=[]))
    original = client.complete_chat_result

    def complete(messages, **options):
        if decision.is_qq_gate_messages(messages):
            client.gate_messages.append(messages)
            return ChatResult(text='{"action":"speak","reason":"私聊中有新的问题。"}',
                              usage=TokenUsage(prompt_tokens=30, completion_tokens=10, source="api"))
        return original(messages, **options)

    monkeypatch.setattr(client, "complete_chat_result", complete)
    return {**args, "group_id": group, "trigger_member_id": member,
            "recent_limit_per_member": 10, "history_limit_per_member": 0}


def _shared_binding(setup, args, *, large_memory):
    original, memory = _binding(setup)
    derived = original.derived_memory
    if large_memory:
        derived["entries"].extend({"record_id": f"qqepisode_large_{index}", "kind": "episode",
            "summary": "有来源的过往经历" * 160, "objective_fact": "成员实际说过的话" * 230,
            "source_refs": [f"source:large-{index}"], "authority": "source_attributed_experience"}
            for index in range(3))
        assert 4000 < len(json.dumps(derived, ensure_ascii=False)) < 16000
    binding = SharedQQBinding(original.settings, original.preset, original.llm,
        ScopedQQMemoryView(memory, group_id=args["group_id"], member_id=args["trigger_member_id"]),
        {}, (), derived)
    return binding, memory


def test_defaults_raise_only_qq_input_and_recall_budget_fourfold():
    settings = Settings()
    assert settings.qq_input_token_budget == 96_000
    assert settings.qq_memory_recall_max_characters == 16_000
    assert settings.qq_memory_recall_max_items == 8
    assert settings.llm_json_max_tokens == 512


@pytest.mark.parametrize("private", [False, True], ids=["group", "private"])
def test_input_above_old_limit_keeps_exact_memories_in_gate_and_draft(setup, monkeypatch, private):
    args = _observations(setup, monkeypatch, private=private, count=5, body_length=14_000)
    binding, memory = _shared_binding(setup, args, large_memory=True)
    trace = QQTurnTrace()
    try:
        result = decision.decide_qq_reply(**args, shared_owner=binding, trace=trace, deadline=QQDeadline.after(60))
        assert result.outcome == "ready", result.error_type
        gate, draft = _gate_payload(setup[1]), _persona_payload(setup[1])
        assert gate["derived_memory"] == draft["derived_memory"] == binding.derived_memory
        assert 48_000 < len(decision._canonical(gate)) < 192_000
        assert 48_000 < len(decision._canonical(draft)) < 192_000
        assert len(decision._canonical(gate)) + decision._assessment_reserve() <= 192_000
        assert "context_budget_exceeded" not in trace.snapshot()["counters"]
        assert set(result.recalled_memory_ids) == {entry["record_id"] for entry in binding.derived_memory["entries"]}
        for index in range(5):
            assert f"INPUT_BUDGET_MARKER_{index}:" in decision._canonical(gate)
        assert len(result.recalled_memory_ids) <= 8 and len(result.recalled_episode_ids) <= 40
    finally:
        memory.close()


@pytest.mark.parametrize("private", [False, True], ids=["group", "private"])
def test_required_input_above_new_limit_still_fails_before_model_calls(setup, monkeypatch, private):
    args = _observations(setup, monkeypatch, private=private, count=9, body_length=26_000)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**args, trace=trace, deadline=QQDeadline.after(60))
    assert result.outcome == "error_fallback", result.reason
    assert result.reason == decision._CONTEXT_BUDGET_REASON
    assert result.reply == decision.ERROR_FALLBACK_REPLY and result.usage.calls == 0
    assert result.source_record_ids
    assert result.seen_observation_record_ids == result.new_observation_record_ids
    assert not setup[1].gate_messages and not setup[2]
    counters = trace.snapshot()["counters"]
    assert counters["context_base_characters"] > 192_000
    assert counters["context_budget_exceeded_final"] == 1
    assert "INPUT_BUDGET_MARKER" not in result.reason + json.dumps(trace.snapshot())


@pytest.mark.parametrize("private", [False, True], ids=["group", "private"])
def test_fourfold_input_keeps_virtual_latency_calls_outputs_and_sixty_second_deadline(
        setup, tmp_path, monkeypatch, private):
    # Hold stochastic validator sampling constant across the two budget runs.
    monkeypatch.setenv("NC_VALIDATOR_SAMPLE_RATE", "0")
    args = _observations(setup, monkeypatch, private=private, count=1, body_length=30)
    client = setup[1]
    chat, complete_json = client.complete_chat_result, client.complete_json
    clock = [1000.0]
    calls = []

    def timed_chat(messages, **options):
        calls.append(("chat", options.get("max_tokens"), options.get("timeout_seconds")))
        clock[0] += 0.125
        return chat(messages, **options)

    def timed_json(**options):
        calls.append(("json", options.get("max_tokens"), options.get("timeout_seconds")))
        clock[0] += 0.125
        return complete_json(**options)

    monkeypatch.setattr(client, "complete_chat_result", timed_chat)
    monkeypatch.setattr(client, "complete_json", timed_json)
    runs = []
    for label, budget in (("legacy", "24000"), ("fourfold", "96000")):
        monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", budget)
        copy = tmp_path / f"{label}-input.sqlite3"
        shutil.copy2(setup[0].path, copy)
        with decision._STATIC_PARSE_LOCK:
            decision._STATIC_PARSE_CACHE.clear()
        clock[0] = 1000.0
        calls.clear()
        binding, memory = _shared_binding(setup, args, large_memory=False)
        deadline = QQDeadline.after(60, clock=lambda: clock[0])
        trace = QQTurnTrace(clock=lambda: clock[0])
        try:
            result = decision.decide_qq_reply(**{**args, "memory_db": copy}, shared_owner=binding,
                                              deadline=deadline, trace=trace)
            assert result.outcome == "ready", result.error_type
            gate, draft = _gate_payload(client), _persona_payload(client)
            assert gate["derived_memory"] == draft["derived_memory"]
            assert len(decision._canonical(gate)) < 48_000
            assert len(decision._canonical(draft)) < 48_000
            assert deadline.deadline_mono == 1060.0
            assert deadline.remaining() == 60.0 - len(calls) * 0.125
            runs.append({"calls": list(calls), "elapsed": clock[0] - 1000.0,
                         "usage_calls": result.usage.calls, "gate_size": len(decision._canonical(gate)),
                         "draft_size": len(decision._canonical(draft))})
        finally:
            memory.close()
    assert runs[0] == runs[1]
    assert len(runs[0]["calls"]) >= 2
    assert all(0 < timeout <= 60 for _, _, timeout in runs[0]["calls"])


@pytest.mark.parametrize("private", [False, True], ids=["group", "private"])
def test_input_and_recall_budget_change_reuses_warm_static_parse(setup, tmp_path, monkeypatch, private):
    monkeypatch.setenv("NC_QQ_BODY_PIPELINE", "legacy")
    monkeypatch.setenv("NC_VALIDATOR_SAMPLE_RATE", "0")
    args = _observations(setup, monkeypatch, private=private, count=1, body_length=30)
    client = setup[1]
    original_json = client.complete_json
    parser_calls = []

    def parsed_json(**options):
        if options["system"] == decision.parser_module._SYSTEM:
            parser_calls.append(options)
            return {"speech_act": "request", "entities": ["operator"],
                    "gate": {"path": "fast", "reason": "static instruction"}}
        return original_json(**options)

    monkeypatch.setattr(client, "complete_json", parsed_json)
    traces = []
    for index, (budget, recall) in enumerate((("24000", "4000"), ("96000", "16000"), ("96000", "16000"))):
        monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", budget)
        monkeypatch.setenv("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", recall)
        path = tmp_path / f"warm-cache-{index}.sqlite3"
        shutil.copy2(setup[0].path, path)
        binding, memory = _shared_binding(setup, args, large_memory=False)
        trace = QQTurnTrace()
        try:
            result = decision.decide_qq_reply(**{**args, "memory_db": path}, shared_owner=binding,
                                              trace=trace, deadline=QQDeadline.after(60))
            assert result.outcome == "ready", result.error_type
            traces.append(trace.snapshot()["counters"])
        finally:
            memory.close()
    assert len(parser_calls) == 1
    assert traces[0]["static_parse_cache_miss"] == 1
    assert traces[1]["static_parse_cache_hit"] == traces[2]["static_parse_cache_hit"] == 1
    assert traces[1].get("static_parse_cache_miss", 0) == traces[2].get("static_parse_cache_miss", 0) == 0
    assert traces[1]["model_calls"] == traces[2]["model_calls"] == traces[0]["model_calls"] - 1
