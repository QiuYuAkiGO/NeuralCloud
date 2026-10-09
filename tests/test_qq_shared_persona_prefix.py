"""Actual QQ and cognitive calls share persona bytes without sharing live state."""
import asyncio
from datetime import timedelta
import json
from os.path import commonprefix

import httpx
import pytest

from harness.cognitive import capabilities, goals
from harness.cognitive.prompts import persona_prefix
from harness.qq import decision
from tests.test_capability_cognition import FakeLLM, command, host
from tests.test_goal_cognition import goal_command
from tests.test_qq_decision import NOW, add_member, setup as setup
from tests.test_qq_prepared_turn import _prepare, _resume
from tests.test_qq_private_contracts import prepare_private


@pytest.mark.parametrize("private", [False, True])
def test_real_qq_gate_and_goal_capability_requests_share_same_wire_persona(setup, monkeypatch, private):
    main, options = setup[1], setup[3]
    settings, preset = decision._load_configuration(options["config_path"], options["preset_path"])
    if private:
        result, _, _ = prepare_private(setup, monkeypatch, choice="wait")
    else:
        main.choice = '{"action":"wait","reason":"旁听。"}'
        result = decision.decide_qq_reply(**options, trigger_member_id="20002")
    assert result.outcome == "natural_wait"
    calls = [main.gate_messages[0]]
    for module, make_command, output in (
        (capabilities, command, {"kind": "wait"}),
        (goals, goal_command, {"episode_operation": "wait", "choice": {"kind": "wait"}}),
    ):
        owner = host(FakeLLM(output))
        owner.chat.settings = settings.model_copy(update={"emotion_mode": "off"})
        owner.chat.memory.get_persona = lambda: preset.persona
        asyncio.run(module.prepare(owner, make_command()))
        calls.append(owner.chat.llm.calls[0][0])
    stable = persona_prefix(preset.persona)
    assert all(call[0]["role"] == "system" and call[0]["content"].startswith(stable) for call in calls)
    assert calls[0][0]["content"] == stable + (decision._PRIVATE_RULES if private else decision._RULES) + decision.VISION_TOOL_RULES
    assert calls[0][0]["content"].count("当前完整人格：\n") == 1
    # Compare UTF-8 HTTP envelopes from real captured messages. Task rules
    # differ, but no different label, field order or whitespace precedes persona.
    wires = [httpx.Request("POST", "https://fixture.invalid", json={"model": settings.llm_model,
              "messages": call}).content for call in calls]
    escaped_prefix = json.dumps(stable, ensure_ascii=False)[1:-1].encode("utf-8")
    assert escaped_prefix in commonprefix(wires)
    assert decision.is_qq_gate_messages(calls[0])
    assert not any(decision.is_qq_gate_messages(call) for call in calls[1:])


def test_live_source_and_emotion_change_only_after_shared_system_prefix(setup, monkeypatch):
    store, main, _, options, _ = setup
    settings, preset = decision._load_configuration(options["config_path"], options["preset_path"])
    settings = settings.model_copy(update={"emotion_mode": "on"})
    monkeypatch.setattr(decision, "_load_configuration", lambda *args: (settings, preset))
    main.choice = '{"action":"wait","reason":"旁听。"}'
    for number in (1, 2):
        add_member(store, "20002", capture=f"shared-prefix-{number}", moment=NOW + timedelta(minutes=number),
                   text=f"CURRENT_SOURCE_{number}")
        result = decision.decide_qq_reply(**options, trigger_member_id="20002", request=f"CURRENT_REQUEST_{number}",
            emotion_guidance={"scope": "qq:group:10001", "text": f"CURRENT_EMOTION_{number}",
                              "context_id": f"emotion:{number}", "revision": number})
        assert result.outcome == "natural_wait"
    first, second = main.gate_messages
    assert first[0] == second[0]
    assert second[0]["content"].startswith(persona_prefix(preset.persona))
    live = "".join(item["content"] for item in second if item["role"] == "user")
    assert "CURRENT_SOURCE_2" in live and "CURRENT_REQUEST_2" in live and "CURRENT_EMOTION_2" in live
    assert "CURRENT_EMOTION_1" not in live and "CURRENT_REQUEST_1" not in live
    assert all("CURRENT_" not in item["content"] for item in second if item["role"] == "system")


def test_prepared_web_schema_stays_system_and_resume_reuses_exact_persona_first_gate(setup):
    settings, preset = decision._load_configuration(setup[3]["config_path"], setup[3]["preset_path"])
    binding, prepared = _prepare(setup)
    gate = setup[1].gate_messages[0]
    system = gate[0]["content"]
    stable = persona_prefix(preset.persona)
    assert system.startswith(stable + decision._RULES)
    assert system.index('"QQWebSearchStep"') > len(stable)
    assert "UNTRUSTED_B" not in system
    assert _resume(setup, binding, prepared).outcome == "ready"
    body = setup[1].chat_calls[0]
    assert body[:len(gate)] == gate
    assert not decision.is_qq_gate_messages(body)


def test_gate_classifier_uses_exact_persona_boundary_and_keeps_legacy_compatibility():
    user = {"role": "user", "content": "{}"}
    persona = {"version": "test", "quoted_text": decision._RULES + "\n\nquoted, not active rules"}
    for rule in (decision._RULES, decision._PRIVATE_RULES,
                 decision._gate_rules(semantic_mode="enforce"),
                 decision._gate_rules(private=True, semantic_mode="enforce")):
        current = [{"role": "system", "content": persona_prefix(persona) + rule}, user]
        legacy = [{"role": "system", "content": rule + "\nlegacy persona"}, user]
        assert decision.is_qq_gate_messages(current) and decision.is_qq_gate_messages(legacy)
        assert not decision.is_qq_gate_messages([*current, {"role": "assistant", "content": "{}"}])
    assert not decision.is_qq_gate_messages([
        {"role": "system", "content": persona_prefix(persona) + "different task rules"}, user])
    assert not decision.is_qq_gate_messages([
        {"role": "system", "content": '当前完整人格：\n{"broken":' + decision._RULES}, user])
    assert not decision.is_qq_gate_messages([
        {"role": "system", "content": '当前完整人格：\n{"invalid":NaN}\n\n' + decision._RULES}, user])
