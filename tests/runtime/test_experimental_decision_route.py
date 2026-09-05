"""The model's selection goes through the existing typed execution boundary."""

import json

import pytest

from harness.cognitive.action_planner import ActionDecisionPlanner
from harness.cognitive.decision_parser import ExperimentalDecisionParser
from harness.cognitive.grounded_agency import GroundedAgencyDecisionModel
from harness.contracts.decisions import DeferDecision, GoalProposal, NoActionDecision, ReplyDecision
from harness.llm.client import ChatResult
from harness.runtime.experimental_decision import ExperimentalDecisionCompiler


@pytest.mark.parametrize(("payload", "intent", "arguments", "condition"), [
    ({"kind": "wait", "duration_ms": 48, "confidence": 0.5}, "control.wait.v1", {"duration_ms": 48}, "waited"),
    ({"kind": "observe", "observation_scope": "current", "confidence": 0.5}, "control.observe.v1", {}, "observed"),
    ({"kind": "act", "confidence": 0.5, "intent": {"intent_id": "control.wait.v1", "arguments": {"duration_ms": 48}},
      "success_criteria": [{"kind": "intentional_noop", "condition": "waited"}]},
     "control.wait.v1", {"duration_ms": 48}, "waited"),
])
def test_fake_model_compiles_exact_selection_without_policy_or_text(monkeypatch, payload, intent, arguments, condition):
    def forbidden(*args, **kwargs):
        raise AssertionError("model-owned decisions cannot use policy ranking")

    monkeypatch.setattr(ActionDecisionPlanner, "_read_policies", forbidden)
    monkeypatch.setattr(ActionDecisionPlanner, "decide", forbidden)

    class FakeModel:
        def complete_chat_result(self, messages, *, stream=False):
            return ChatResult(text=json.dumps(payload))

    result = GroundedAgencyDecisionModel(FakeModel()).decide(
        [{"role": "user", "content": "N_7: T"}],
        manifest_intents=[intent], provider_intents=[intent],
    )
    compiler = ExperimentalDecisionCompiler()
    decision = compiler.compile(result.decision, manifest_intents=[intent], provider_intents=[intent])
    assert result.success
    assert isinstance(decision, GoalProposal)
    assert not isinstance(decision, ReplyDecision)
    assert decision.embodied_action.intent.intent_id == intent
    assert decision.embodied_action.intent.arguments == arguments
    assert decision.embodied_action.success_criteria[0].condition == condition
    assert decision.permission_class == "low"
    assert decision.reversible
    assert decision.required_approvals == []
    assert decision.verification_method == "provider_typed_criteria.v1"
    assert decision == compiler.compile(result.decision, manifest_intents=[intent], provider_intents=[intent])


@pytest.mark.parametrize("kind", ["no_action", "defer"])
def test_nonaction_decisions_are_completed_without_any_capability(kind):
    payload = {"kind": kind, "confidence": 0.5}
    if kind == "defer":
        payload["missing_information"] = ["view"]
    compiled = ExperimentalDecisionCompiler().compile(
        ExperimentalDecisionParser().parse(payload), manifest_intents=[], provider_intents=[],
    )
    assert isinstance(compiled, NoActionDecision if kind == "no_action" else DeferDecision)


@pytest.mark.parametrize(("manifest", "provider"), [([], ["control.wait.v1"]), (["control.wait.v1"], [])])
def test_wait_requires_both_manifest_and_provider_capability(manifest, provider):
    compiled = ExperimentalDecisionCompiler().compile(
        ExperimentalDecisionParser().parse({"kind": "wait", "duration_ms": 2, "confidence": 0.8}),
        manifest_intents=manifest, provider_intents=provider,
    )
    assert isinstance(compiled, DeferDecision)
    assert "unavailable" in compiled.reason


def test_file_action_requires_researcher_approval_assigned_by_profile():
    intent_id = "file.create_text.v1"
    parsed = ExperimentalDecisionParser().parse({
        "kind": "act", "confidence": 1.0,
        "intent": {"intent_id": intent_id, "arguments": {
            "root_id": "sandbox", "relative_path": "a.txt", "text": "hello",
        }},
        "success_criteria": [{"kind": "file", "condition": "exists", "root_id": "sandbox", "relative_path": "a.txt"}],
    })
    compiled = ExperimentalDecisionCompiler().compile(
        parsed, manifest_intents=[intent_id], provider_intents=[intent_id],
    )
    assert isinstance(compiled, GoalProposal)
    assert compiled.permission_class == "medium"
    assert not compiled.reversible
    assert compiled.required_approvals == ["researcher"]
    assert "expected_observation" not in compiled.embodied_action.intent.arguments


def test_unbound_criteria_and_constructed_instances_fail_closed():
    parsed = ExperimentalDecisionParser().parse({
        "kind": "act", "confidence": 1.0,
        "intent": {"intent_id": "app.open.v1", "arguments": {"app_id": "editor"}},
        "success_criteria": [{"kind": "window", "condition": "present", "app_id": "browser"}],
    })
    compiler = ExperimentalDecisionCompiler()
    compiled = compiler.compile(parsed, manifest_intents=["app.open.v1"], provider_intents=["app.open.v1"])
    assert isinstance(compiled, DeferDecision)
    copied = parsed.model_copy(update={"confidence": 10})
    compiled = compiler.compile(copied, manifest_intents=["app.open.v1"], provider_intents=["app.open.v1"])
    assert isinstance(compiled, DeferDecision)


@pytest.mark.parametrize(("intent_id", "argument_key", "condition"), [
    ("task.execute.v1", "option_id", "task_completed"),
    ("station.use.v1", "station_id", "station_used"),
])
def test_scenario_profiles_require_exact_typed_target_and_capability(intent_id, argument_key, condition):
    payload = {
        "kind": "act", "confidence": 0.9,
        "intent": {"intent_id": intent_id, "arguments": {argument_key: "option_a"}},
        "success_criteria": [{"kind": "scenario", "condition": condition, "target_id": "option_a"}],
    }
    compiler = ExperimentalDecisionCompiler()
    parsed = ExperimentalDecisionParser().parse(payload)
    compiled = compiler.compile(parsed, manifest_intents=[intent_id], provider_intents=[intent_id])
    assert isinstance(compiled, GoalProposal)
    assert compiled.embodied_action.intent.intent_id == intent_id
    assert compiled.permission_class == "low"
    assert isinstance(compiler.compile(parsed, manifest_intents=[intent_id], provider_intents=[]), DeferDecision)
    payload["success_criteria"][0]["target_id"] = "option_b"
    parsed = ExperimentalDecisionParser().parse(payload)
    assert isinstance(compiler.compile(parsed, manifest_intents=[intent_id], provider_intents=[intent_id]), DeferDecision)


@pytest.mark.parametrize("injected", ["required_approvals", "permission_class", "reversible", "provider_id"])
def test_scenario_model_cannot_supply_execution_authority(injected):
    payload = {
        "kind": "act", "confidence": 0.9,
        "intent": {"intent_id": "station.use.v1", "arguments": {"station_id": "station_a", injected: "override"}},
        "success_criteria": [{"kind": "scenario", "condition": "station_used", "target_id": "station_a"}],
    }
    with pytest.raises(ValueError):
        ExperimentalDecisionParser().parse(payload)
