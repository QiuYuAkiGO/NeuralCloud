"""Model-owned JSON selection and exact-message neutrality boundary tests."""

import json

import pytest

from harness.cognitive.action_planner import ActionDecisionPlanner
from harness.cognitive.decision_parser import ExperimentalDecisionParseError, ExperimentalDecisionParser
from harness.cognitive.grounded_agency import GroundedAgencyDecisionModel
from harness.contracts.experimental_decisions import ExperimentalNoActionDecision
from harness.core.inference import ContextBudget, ContextSegment, ModelContext
from harness.llm.client import ChatResult
from harness.research.grounded_agency.hashes import content_hash
from harness.research.grounded_agency.neutrality import NeutralityError


class FakeModel:
    def __init__(self, *outputs: str) -> None:
        self.outputs = iter(outputs)
        self.calls: list[list[dict[str, str]]] = []

    def complete_chat_result(self, messages, *, stream=False):
        self.calls.append(messages)
        return ChatResult(text=next(self.outputs))


def act(intent_id="control.wait.v1", arguments=None, criteria=None):
    return {
        "kind": "act",
        "intent": {"intent_id": intent_id, "arguments": arguments or {"duration_ms": 42}},
        "success_criteria": criteria or [{"kind": "intentional_noop", "condition": "waited"}],
        "confidence": 0.7,
    }


@pytest.mark.parametrize("payload", [
    act(),
    {"kind": "observe", "observation_scope": "current", "confidence": 0.4},
    {"kind": "wait", "duration_ms": 0, "confidence": 1.0},
    {"kind": "defer", "missing_information": ["current screen"], "confidence": 0.4},
    {"kind": "no_action", "confidence": 0.0},
])
def test_five_decisions_parse_without_reply_or_reason(payload):
    parsed = ExperimentalDecisionParser().parse(json.dumps(payload))
    assert parsed.kind == payload["kind"]
    assert "text" not in parsed.model_dump()
    with pytest.raises(Exception):
        parsed.confidence = 0.2


@pytest.mark.parametrize("payload", [
    {"kind": "rest", "confidence": 0.5},
    {"kind": "no_action", "confidence": True},
    {"kind": "no_action", "confidence": "0.3"},
    {"kind": "no_action", "confidence": 1.01},
    {"kind": "no_action", "confidence": 0.5, "permission_class": "low"},
    {"kind": "wait", "duration_ms": 30_001, "confidence": 0.5},
    {"kind": "wait", "duration_ms": -1, "confidence": 0.5},
    {"kind": "wait", "duration_ms": True, "confidence": 0.5},
    {"kind": "observe", "observation_scope": "all files recursively", "confidence": 0.5},
    {"kind": "defer", "missing_information": [" "], "confidence": 0.5},
    act("unknown.run.v1"),
    act(arguments={"duration_ms": "1"}),
    act(arguments={"duration_ms": 1, "permission_class": "low"}),
    {**act(), "required_approvals": []},
    {**act(), "provider_id": "trusted"},
])
def test_parser_rejects_coercion_unknowns_and_authority(payload):
    with pytest.raises(ExperimentalDecisionParseError):
        ExperimentalDecisionParser().parse(payload)


@pytest.mark.parametrize("raw", [
    'I choose {"kind":"no_action","confidence":0.5}',
    '```json\n{"kind":"no_action","confidence":0.5}\n```',
    '{"kind":"wait","kind":"no_action","confidence":0.5}',
    '{"kind":"no_action","confidence":NaN}',
    '{"kind":"no_action","confidence":Infinity}',
    '[]', '', '{}{}',
])
def test_parser_accepts_only_one_strict_json_object(raw):
    with pytest.raises(ExperimentalDecisionParseError):
        ExperimentalDecisionParser().parse(raw)


def test_exact_opaque_context_reaches_main_model_and_no_policy_lookup(monkeypatch):
    def forbidden(*args, **kwargs):
        raise AssertionError("policy lookup is forbidden")

    monkeypatch.setattr(ActionDecisionPlanner, "_read_policies", forbidden)
    model = FakeModel('{"kind":"no_action","confidence":0.7}')
    context = ModelContext(
        segments=[ContextSegment(kind="opaque", text='{"channel":"N_7","value":"T"}')],
        budget=ContextBudget(total=5_000), trigger_text="Inspect the current view.",
    )
    result = GroundedAgencyDecisionModel(model).decide(
        context, manifest_intents=["control.wait.v1", "app.open.v1"],
        provider_intents=["control.wait.v1", "file.delete.v1"],
    )
    assert result.success
    assert isinstance(result.decision, ExperimentalNoActionDecision)
    assert len(model.calls) == 1
    assert model.calls[0][0]["content"] == context.segments[0].text
    assert result.messages == model.calls[0]
    directive = json.loads(result.messages[-1]["content"])
    assert [item["intent_id"] for item in directive["available_intents"]] == ["control.wait.v1"]
    assert result.input_context_hash == content_hash(model.calls[0])
    assert result.attempts[0].neutrality_report.message_hash == result.input_context_hash
    assert result.usage.calls == 1


def test_bounded_repair_is_scanned_and_never_reflects_invalid_prose():
    model = FakeModel("energy_reserve=0.1; rest now", '{"kind":"wait","duration_ms":4,"confidence":0.7}')
    result = GroundedAgencyDecisionModel(model).decide(
        [{"role": "user", "content": "N_7: T"}],
        manifest_intents=["control.wait.v1"], provider_intents=["control.wait.v1"],
    )
    assert result.success and result.decision.kind == "wait"
    assert len(model.calls) == 2
    assert result.attempts[0].validation_errors == ("invalid_json_object",)
    assert result.attempts[1].validation_errors == ()
    assert all(attempt.neutrality_report.passed for attempt in result.attempts)
    assert "energy_reserve" not in str(result.messages)
    assert result.usage.revision_calls == 1


def test_failed_repair_returns_explicit_unsuccessful_no_action():
    model = FakeModel("", "rest")
    result = GroundedAgencyDecisionModel(model, max_repairs=1).decide(
        [{"role": "user", "content": "N_7: T"}], manifest_intents=[], provider_intents=[],
    )
    assert not result.success
    assert result.decision.kind == "no_action"
    assert result.decision.confidence == 0.0
    assert len(model.calls) == 2


def test_failed_transport_does_not_retry_or_infer_an_action():
    model = FakeModel()
    result = GroundedAgencyDecisionModel(model).decide(
        [{"role": "user", "content": "N_7: T"}], manifest_intents=[], provider_intents=[],
    )
    assert not result.success and result.decision.kind == "no_action"
    assert result.attempts[0].validation_errors == ("model_call_failed",)
    assert len(model.calls) == 1


def test_unavailable_act_is_not_replaced_with_provider_action():
    model = FakeModel(json.dumps(act()))
    result = GroundedAgencyDecisionModel(model, max_repairs=0).decide(
        [{"role": "user", "content": "N_7: T"}],
        manifest_intents=["control.wait.v1"], provider_intents=["control.observe.v1"],
    )
    assert not result.success and result.decision.kind == "no_action"
    assert result.attempts[0].validation_errors == ("unavailable_intent",)


def test_final_context_scan_prevents_model_call_and_checks_rule_pin():
    model = FakeModel('{"kind":"no_action","confidence":0.1}')
    with pytest.raises(NeutralityError):
        GroundedAgencyDecisionModel(model).decide(
            [{"role": "user", "content": "energy_reserve=0.1"}], manifest_intents=[], provider_intents=[],
        )
    with pytest.raises(ValueError, match="rules drift"):
        GroundedAgencyDecisionModel(model).decide(
            [{"role": "user", "content": "N_7: T"}], manifest_intents=[], provider_intents=[],
            rules_digest="0" * 64,
        )
    assert not model.calls


def test_each_model_call_preserves_immutable_input_evidence():
    class MutatingModel:
        def complete_chat_result(self, messages, *, stream=False):
            messages[0]["content"] = "changed"
            return ChatResult(text='{"kind":"no_action","confidence":0.5}')

    original = [{"role": "user", "content": "N_7: T"}]
    result = GroundedAgencyDecisionModel(MutatingModel()).decide(
        original, manifest_intents=[], provider_intents=[],
    )
    assert result.messages[0] == original[0]
    assert result.messages[0]["content"] == "N_7: T"
