"""Server capability selection reuses shared cognition without tool_calls."""

from __future__ import annotations

import pytest

from harness.cognitive.capabilities import CapabilitySelectionError
from harness.contracts.capability_calls import SERVER_EXECUTE, SERVER_OBSERVE, PreparedCapabilityChoice
from tests.test_capability_cognition import FakeLLM, command, host, offer, run


def server_offer(capability=SERVER_EXECUTE, **updates):
    return offer(offer_id="server:" + capability, channel="server", capability_id=capability,
                 provider_id="ssh-server", body_instance_id="server-01", **updates)


def test_execute_choice_is_schema_bound_and_strips_reason():
    item = server_offer()
    owner = host(FakeLLM({"kind": "act", "offer_id": item.offer_id,
                          "arguments": {"command": "uptime"}, "reason": "private"}))
    result = run(owner, command([item]))
    decision = PreparedCapabilityChoice.model_validate(result["decision"])
    assert decision.choice.kind == "act"
    assert decision.choice.arguments == {"command": "uptime"}
    assert decision.choice.reason is None
    system = owner.chat.llm.calls[0][0][0]["content"]
    assert "高危删除类命令需要人工批准" in system


def test_observe_choice_uses_observe_kind():
    item = server_offer(SERVER_OBSERVE)
    result = run(host(FakeLLM({"kind": "observe", "offer_id": item.offer_id,
                               "arguments": {"command": "df -h"}})), command([item]))
    assert result["decision"]["choice"]["kind"] == "observe"


@pytest.mark.parametrize("output,code", [
    ({"kind": "act", "offer_id": "server:" + SERVER_EXECUTE, "arguments": {"command": "uptime", "host": "evil"}},
     "capability_invalid_server_arguments"),
    ({"kind": "observe", "offer_id": "server:" + SERVER_EXECUTE, "arguments": {"command": "uptime"}},
     "capability_invalid_server_choice"),
    ({"kind": "act", "offer_id": "server:" + SERVER_OBSERVE, "arguments": {"command": "uptime"}},
     "capability_invalid_server_choice"),
    ({"kind": "observe", "offer_id": "server:" + SERVER_OBSERVE, "arguments": {"command": "rm /tmp/x"}},
     "capability_invalid_server_arguments"),
    ({"kind": "act", "offer_id": "server:" + SERVER_EXECUTE, "arguments": {"command": "uptime", "success_criteria": []},
      "success_criteria": [{"kind": "done"}]}, "capability_invalid_server_choice"),
    ({"kind": "act", "offer_id": "invented", "arguments": {"command": "uptime"}}, "capability_unknown_offer"),
])
def test_invalid_server_choice_is_rejected(output, code):
    item = server_offer(SERVER_OBSERVE) if (output.get("offer_id") or "").endswith("observe.v1") else server_offer()
    owner = host(FakeLLM(output))
    with pytest.raises(CapabilitySelectionError, match=code):
        run(owner, command([item]))
    assert len(owner.chat.llm.calls) == 1


def test_tool_calls_are_rejected():
    item = server_offer()
    with pytest.raises(CapabilitySelectionError, match="capability_invalid_content_type"):
        run(host(FakeLLM({"kind": "act", "offer_id": item.offer_id, "arguments": {"command": "uptime"}},
                         has_tool_calls=True)), command([item]))
