"""Shared goal decisions bind current evidence without acquiring action authority."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from harness.cognitive import goals
from harness.cognitive.capabilities import CapabilitySelectionError
from harness.contracts.goals import GoalDecisionRequest, PreparedGoalDecision
from tests.test_capability_cognition import FakeLLM, command, host, moment, offer


def goal_command(offers=None, *, active=False, user=False, **updates):
    request = command(offers)
    context = {"episode_id": "episode:runtime-owned", "known_evidence_refs": ["snapshot:4", "fact:1", "receipt:1"],
        "requested_goal": {"origin": "user_task" if user else "maintenance", "statement": "Stay engaged when useful"}}
    if active:
        context.update(active_episode={"episode_id": "episode:runtime-owned", "status": "active"},
                       outcomes=[{"status": "completed", "evidence_refs": ["receipt:1"]}])
    payload = {**request.payload, "snapshot_revision": 4, "homeostatic_summary": {"snapshot_revision": 4, "salience": "marked"},
               "goal_context": context, **updates}
    return request.model_copy(update={"command_type": "PrepareGoalDecision", "payload": payload})


def start_choice(*, action=False, user=False):
    return {"episode_operation": "start", "choice": {"kind": "act", "offer_id": offer().offer_id} if action else {"kind": "wait"},
        "purpose": "Make one considered expression", "target_dimensions": [] if user else ["social_connection"],
        "plan": [{"statement": "Consider expressing a thought", "completion_criteria": ["A verified expression receipt exists"]}],
        "completion_criteria": ["A verified expression receipt exists"], "evidence_refs": ["fact:1"]}


def run(output, request=None, **metadata):
    owner = host(FakeLLM(output, **metadata))
    result = asyncio.run(goals.prepare(owner, request or goal_command()))
    return owner, result


def test_same_persona_memory_one_model_call_and_same_capability_binding():
    request = goal_command()
    owner, result = run(start_choice(action=True), request)
    selected = PreparedGoalDecision.model_validate(result["decision"])
    assert selected.capability_choice.model_dump(mode="json") == result["prepared_capability_choice"]
    assert selected.choice.kind == "act" and selected.snapshot_revision == 4
    assert selected.deadline_at == request.deadline
    assert len(owner.chat.llm.calls) == 1
    messages, arguments, trace = owner.chat.llm.calls[0]
    assert "same-current-persona" in messages[0]["content"]
    assert "verified-shared-recall" in messages[1]["content"]
    assert arguments["max_tokens"] == 4096 and 0 < arguments["timeout_seconds"] <= 25
    assert trace["phase"] == "goal_decision"
    assert owner.queries[0].raw_text == ""
    assert "verified-shared-recall" not in json.dumps(result)


def test_no_offers_still_allows_review_with_one_model_call():
    output = {"episode_operation": "complete", "choice": {"kind": "no_action"}, "evidence_refs": ["receipt:1"],
        "criterion_results": [{"criterion": "An expression receipt exists", "satisfied": True, "evidence_refs": ["receipt:1"]}]}
    owner, result = run(output, goal_command([], active=True))
    assert result["decision"]["episode_operation"] == "complete"
    assert len(owner.chat.llm.calls) == 1
    assert json.loads(owner.chat.llm.calls[0][0][1]["content"])["available_offers"] == []


def test_wait_without_episode_or_offers_is_valid_and_private_reason_not_durable():
    owner, result = run({"episode_operation": "wait", "choice": {"kind": "wait", "reason": "PRIVATE_REASON"}}, goal_command([]))
    assert result["decision"]["choice"]["kind"] == "wait"
    assert "PRIVATE_REASON" not in json.dumps(result)
    assert len(owner.chat.llm.calls) == 1


def test_user_goal_may_start_without_psychological_targets():
    _, result = run(start_choice(user=True), goal_command(user=True))
    assert result["decision"]["target_dimensions"] == []


def test_appraisal_identity_revision_and_expiry_are_bound_by_host():
    output = start_choice()
    output["appraisal"] = {"appraisal_kind": "self_report", "observed_event_refs": ["fact:1"],
        "suggested_impacts": {"cognitive_load": -0.03}, "model_confidence": 0.4}
    request = goal_command()
    _, first = run(output, request)
    _, second = run(output, request)
    appraisal = PreparedGoalDecision.model_validate(first["decision"]).appraisal
    assert appraisal.episode_id == "episode:runtime-owned"
    assert appraisal.basis_snapshot_revision == 4 and appraisal.expires_at == request.deadline
    assert appraisal.appraisal_id == second["decision"]["appraisal"]["appraisal_id"]
    assert "source" not in appraisal.model_dump() and "precision" not in appraisal.model_dump()


@pytest.mark.parametrize("appraisal_refs", [[], ["receipt:1"]])
def test_known_snapshot_is_plan_evidence_but_not_new_appraisal_evidence(appraisal_refs):
    output = start_choice()
    output["evidence_refs"] = ["snapshot:4"]
    output["appraisal"] = {"appraisal_kind": "self_report", "observed_event_refs": ["snapshot:4"],
        "suggested_impacts": {"cognitive_load": -0.03}, "model_confidence": 0.4}
    request = goal_command()
    request.payload["goal_context"]["appraisal_evidence_refs"] = appraisal_refs
    with pytest.raises(goals.GoalDecisionError, match="goal_appraisal_unknown_evidence"):
        run(output, request)
    output.pop("appraisal")
    _, result = run(output, request)
    assert result["decision"]["evidence_refs"] == ["snapshot:4"]


def test_appraisal_accepts_only_runtime_event_subset():
    output = start_choice()
    output["appraisal"] = {"appraisal_kind": "event_interpretation", "observed_event_refs": ["fact:1"],
        "suggested_impacts": {"novelty": 0.03}, "model_confidence": 0.4}
    request = goal_command()
    request.payload["goal_context"]["appraisal_evidence_refs"] = ["fact:1"]
    _, result = run(output, request)
    assert result["decision"]["appraisal"]["observed_event_refs"] == ["fact:1"]


@pytest.mark.parametrize("field,value", [
    ("goal_id", "forged"), ("episode_id", "forged"), ("permission", "all"),
    ("target_dimensions", ["energy_reserve"]), ("evidence_refs", ["invented"]),
])
def test_model_cannot_invent_identity_authority_energy_or_evidence(field, value):
    output = start_choice()
    output[field] = value
    with pytest.raises(goals.GoalDecisionError):
        run(output)


@pytest.mark.parametrize("update", [
    {"source": "system_sensor"}, {"precision": 1.0}, {"appraisal_id": "forged"},
    {"observed_event_refs": ["invented"]}, {"suggested_impacts": {"energy_reserve": 0.1}},
    {"suggested_impacts": {"cognitive_load": float("nan")}},
])
def test_appraisal_cannot_self_certify_or_invent_evidence(update):
    output = start_choice()
    output["appraisal"] = {"appraisal_kind": "event_interpretation", "observed_event_refs": ["fact:1"],
        "suggested_impacts": {"cognitive_load": 0.01}, "model_confidence": 0.5, **update}
    with pytest.raises(goals.GoalDecisionError):
        run(output)


@pytest.mark.parametrize("output,active,code", [
    ({"episode_operation": "continue", "choice": {"kind": "wait"}}, False, "goal_episode_unavailable"),
    (start_choice(), True, "goal_episode_already_active"),
    (start_choice(user=True), False, "goal_autonomous_requires_psychological_target"),
    ({"episode_operation": "complete", "choice": {"kind": "no_action"}, "evidence_refs": ["fact:1"]}, True, "goal_invalid_schema"),
])
def test_episode_operation_requires_valid_state_and_completion_evidence(output, active, code):
    with pytest.raises(goals.GoalDecisionError, match=code):
        run(output, goal_command(active=active))


def test_capability_target_is_validated_without_second_selection():
    output = start_choice(action=True)
    output["choice"]["arguments"] = {"text": "forged-channel-content"}
    with pytest.raises(CapabilitySelectionError, match="capability_invalid_channel_choice"):
        run(output)


@pytest.mark.parametrize("output,code", [
    ("PRIVATE_BAD_JSON", "goal_invalid_json"), ("", "goal_empty_content"),
    ('{"episode_operation":"wait","episode_operation":"start","choice":{"kind":"wait"}}', "goal_invalid_json"),
])
def test_invalid_output_is_rejected_with_safe_error(output, code):
    with pytest.raises(goals.GoalDecisionError, match=code):
        run(output)


@pytest.mark.parametrize("metadata,code", [
    ({"finish_reason": "length"}, "goal_output_truncated"),
    ({"has_tool_calls": True}, "goal_invalid_content_type"),
    ({"content_type": "array"}, "goal_invalid_content_type"),
])
def test_completion_metadata_is_checked(metadata, code):
    with pytest.raises(goals.GoalDecisionError, match=code):
        run({"episode_operation": "wait", "choice": {"kind": "wait"}}, **metadata)


def test_expired_request_has_no_model_call():
    request = goal_command()
    request = request.model_copy(update={"deadline": moment() - timedelta(seconds=1)})
    owner = host(FakeLLM(start_choice()))
    with pytest.raises(TimeoutError, match="goal_decision_expired"):
        asyncio.run(goals.prepare(owner, request))
    assert not owner.chat.llm.calls


def test_offer_expiring_during_model_call_can_only_become_a_proposal():
    short_offer = offer(valid_until=moment() + timedelta(seconds=0.15))
    request = goal_command([short_offer])
    owner = host(FakeLLM(start_choice(action=True), delay=0.2))
    result = asyncio.run(goals.prepare(owner, request))
    selected = PreparedGoalDecision.model_validate(result["decision"])
    assert selected.prepared_at > short_offer.valid_until
    assert selected.choice.offer_id == short_offer.offer_id
    assert len(owner.chat.llm.calls) == 1


def test_offer_already_expired_before_thinking_is_not_selectable():
    request = goal_command([offer(valid_until=moment() - timedelta(seconds=1))])
    with pytest.raises(goals.GoalDecisionError, match="goal_unknown_offer"):
        run(start_choice(action=True), request)


def test_invalid_schema_diagnostics_do_not_leak_model_text_or_unknown_keys(caplog):
    output = start_choice()
    output["PRIVATE_FIELD"] = "PRIVATE_CONTENT"
    with pytest.raises(goals.GoalDecisionError, match="goal_invalid_schema"):
        run(output)
    assert "extra_forbidden" in caplog.text and "unknown_field" in caplog.text
    assert "PRIVATE_FIELD" not in caplog.text and "PRIVATE_CONTENT" not in caplog.text


@pytest.mark.parametrize("unfinished", ["planned", "pending", "failed"])
def test_complete_cannot_skip_unfinished_steps_even_with_other_receipts(unfinished):
    request = goal_command([], active=True)
    request.payload["goal_context"]["active_episode"]["steps"] = [{"status": unfinished}]
    output = {"episode_operation": "complete", "choice": {"kind": "no_action"}, "evidence_refs": ["receipt:1"],
        "criterion_results": [{"criterion": "Receipt exists", "satisfied": True, "evidence_refs": ["receipt:1"]}]}
    with pytest.raises(goals.GoalDecisionError, match="goal_complete_steps_unfinished"):
        run(output, request)


@pytest.mark.parametrize("status", ["wait", "error", "pending", "uncertain"])
def test_wait_and_failure_outcomes_cannot_prove_completion(status):
    request = goal_command([], active=True)
    request.payload["goal_context"]["outcomes"] = [{"status": status, "evidence_refs": ["receipt:1"]}]
    output = {"episode_operation": "complete", "choice": {"kind": "no_action"}, "evidence_refs": ["receipt:1"],
        "criterion_results": [{"criterion": "Receipt exists", "satisfied": True, "evidence_refs": ["receipt:1"]}]}
    with pytest.raises(goals.GoalDecisionError, match="goal_complete_requires_verified_outcomes"):
        run(output, request)


def test_snapshot_cannot_replace_independent_completion_evidence():
    output = {"episode_operation": "complete", "choice": {"kind": "no_action"}, "evidence_refs": ["snapshot:4"],
        "criterion_results": [{"criterion": "Receipt exists", "satisfied": True, "evidence_refs": ["snapshot:4"]}]}
    with pytest.raises(goals.GoalDecisionError, match="goal_criterion_requires_verified_outcome"):
        run(output, goal_command([], active=True))


def test_goal_disables_thinking_only_for_supported_endpoint():
    from harness.config import Settings
    owner = host(FakeLLM(start_choice()))
    owner.chat.settings = Settings(llm_base_url="https://api.deepseek.com", llm_model="deepseek-v4-flash")
    asyncio.run(goals.prepare(owner, goal_command()))
    assert owner.chat.llm.calls[0][1]["thinking"] == {"type": "disabled"}
    assert "reasoning_effort" not in owner.chat.llm.calls[0][1]
    assert owner.chat.llm.calls[0][1]["max_tokens"] == 4096


def test_late_model_output_does_not_prepare_decision(monkeypatch):
    monkeypatch.setattr(goals, "MODEL_SECONDS", 0.1)
    owner = host(FakeLLM(start_choice(), delay=0.2))
    with pytest.raises(TimeoutError, match="goal_decision_expired"):
        asyncio.run(goals.prepare(owner, goal_command()))
    assert len(owner.chat.llm.calls) == 1


def test_snapshot_revision_and_bounded_goal_context_are_validated_before_model():
    request = goal_command()
    payload = deepcopy(request.payload)
    payload["homeostatic_summary"]["snapshot_revision"] = 3
    with pytest.raises(goals.GoalDecisionError, match="goal_invalid_request"):
        run(start_choice(), request.model_copy(update={"payload": payload}))
    payload = deepcopy(request.payload)
    payload["goal_context"]["outcomes"] = ["x"] * 65
    with pytest.raises(ValueError):
        GoalDecisionRequest.model_validate(payload)


def test_oversized_goal_context_is_rejected_before_model_with_specific_code():
    payload = deepcopy(goal_command().payload)
    payload["goal_context"]["padding"] = "x" * 130_000
    owner = host(FakeLLM(start_choice()))
    with pytest.raises(goals.GoalDecisionError, match="goal_context_too_large"):
        asyncio.run(goals.prepare(owner, goal_command().model_copy(update={"payload": payload})))
    assert owner.chat.llm.calls == []
