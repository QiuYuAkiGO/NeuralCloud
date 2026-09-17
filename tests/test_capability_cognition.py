"""Shared cognition never converts model prose into execution authority."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from types import SimpleNamespace

import pytest
from pydantic import BaseModel, ValidationError

from harness.cognitive.capabilities import CapabilitySelectionError, catalog_hash, compile_desktop_choice, prepare
from harness.contracts.capability_calls import (
    BOARD_EXPRESS, BOARD_READ, QQ_EXPRESS, QQ_READ, CapabilityChoice, CapabilityOffer,
    CapabilitySelectionRequest, PreparedCapabilityChoice,
)
from harness.contracts.envelope import DomainCommandEnvelope
from harness.core.inference import ContextSegment, TokenUsage
from harness.llm.client import ChatResult
from infra.trace.request_spans import current_trace_context


def moment():
    return datetime.now(timezone.utc)


def offer(**updates):
    value = dict(offer_id="board:append:private", channel="board", capability_id=BOARD_EXPRESS,
        provider_id="existing-body", body_instance_id="private", scope={"board_id": "misty"},
        activation_revision="revision-1", input_schema={"type": "object", "additionalProperties": False},
        observation_refs=["observation-1"], supported=True, available=True, ready=True,
        valid_until=moment() + timedelta(seconds=60))
    value.update(updates)
    return CapabilityOffer(**value)


def command(offers=None, **updates):
    issued_at = moment()
    payload = CapabilitySelectionRequest(opportunity_id="opportunity-1", generation=1,
        deadline_at=issued_at + timedelta(seconds=60), homeostatic_summary={"level": "marked"},
        offers=[offer()] if offers is None else offers).model_dump(mode="json")
    payload.update(updates)
    return DomainCommandEnvelope(command_type="PrepareCapabilityCall", event_id="internal-opportunity-1",
        receive_seq=1, correlation_id="correlation-1", runtime_instance_id="runtime-1", manifest_revision=1,
        idempotency_key="capability-opportunity-1", deadline=issued_at + timedelta(seconds=60),
        issued_at=issued_at, payload=payload)


class Persona(BaseModel):
    name: str = "same-current-persona"
    style: str = "current-user-personality-setting"


class FakeLLM:
    def __init__(self, output=None, *, delay=0, **metadata):
        self.output = output if output is not None else {"kind": "act", "offer_id": "board:append:private"}
        self.metadata = metadata
        self.delay = delay
        self.calls = []
        self.started = threading.Event()

    def complete_chat_result(self, messages, **kwargs):
        self.calls.append((messages, kwargs, current_trace_context()))
        self.started.set()
        time.sleep(self.delay)
        if isinstance(self.output, BaseException):
            raise self.output
        text = self.output if isinstance(self.output, str) else json.dumps(self.output)
        return ChatResult(text=text, usage=TokenUsage(prompt_tokens=50, completion_tokens=20, total_tokens=70),
            **self.metadata)


def host(llm=None):
    queries = []

    def local_context(event):
        queries.append(event)
        return [ContextSegment(kind="user", text="verified-shared-recall", source_ids=["memory-1"])]

    return SimpleNamespace(chat=SimpleNamespace(
        memory=SimpleNamespace(get_persona=lambda: Persona()), active_session_id="current-session",
        pipeline=SimpleNamespace(session_store=SimpleNamespace(recent=lambda session: [])),
        llm=llm or FakeLLM()), shared=SimpleNamespace(local_context=local_context), queries=queries)


def run(owner, request):
    return asyncio.run(prepare(owner, request))


def test_shared_persona_memory_model_and_causal_trace_are_reused():
    owner = host()
    request = command()
    result = run(owner, request)
    decision = PreparedCapabilityChoice.model_validate(result["decision"])
    assert decision.choice.kind == "act"
    assert decision.opportunity_id == "opportunity-1"
    assert decision.catalog_hash == catalog_hash(CapabilitySelectionRequest.model_validate(request.payload).offers)
    assert decision.deadline_at == request.deadline
    assert owner.queries[0].session_id == "current-session"
    assert owner.queries[0].raw_text == ""
    messages, kwargs, trace = owner.chat.llm.calls[0]
    assert "same-current-persona" in messages[0]["content"]
    assert "verified-shared-recall" in messages[1]["content"]
    assert kwargs["max_tokens"] == 2048
    assert kwargs["response_format"] == {"type": "json_object"}
    assert 0 < kwargs["timeout_seconds"] <= 12
    assert trace["event_id"] == request.event_id
    assert trace["command_id"] == request.command_id
    assert result["timing"]["usage"]["total_tokens"] == 70
    assert "verified-shared-recall" not in json.dumps(result)


@pytest.mark.parametrize("kind", ["wait", "defer", "no_action"])
def test_nonaction_valid_and_no_reason_durable(kind):
    owner = host(FakeLLM({"kind": kind, "reason": "private-freeform-not-to-be-persisted"}))
    result = run(owner, command())
    assert result["decision"]["choice"]["kind"] == kind
    assert result["decision"]["choice"]["reason"] is None
    assert "private-freeform" not in json.dumps(result)
    assert len(owner.chat.llm.calls) == 1


@pytest.mark.parametrize(("channel", "capability", "kind"), [
    ("board", BOARD_READ, "observe"), ("board", BOARD_EXPRESS, "act"),
    ("qq", QQ_READ, "observe"), ("qq", QQ_EXPRESS, "act"),
])
def test_board_and_qq_choose_existing_high_level_offer(channel, capability, kind):
    item = offer(channel=channel, capability_id=capability)
    result = run(host(FakeLLM({"kind": kind, "offer_id": item.offer_id})), command([item]))
    assert result["decision"]["choice"]["arguments"] == {}


@pytest.mark.parametrize("items", [[], [offer(ready=False)], [offer(available=False, ready=False)],
    [offer(supported=False, available=False, ready=False)], [offer(valid_until=moment() - timedelta(seconds=1))]])
def test_no_live_ready_offer_requires_no_model_or_memory_query(items):
    owner = host()
    result = run(owner, command(items))
    assert result["decision"]["choice"]["kind"] == "no_action"
    assert result["timing"]["model_calls"] == 0
    assert owner.chat.llm.calls == []
    assert owner.queries == []


@pytest.mark.parametrize("output,code", [
    ("", "capability_empty_content"), ("not-json-secret", "capability_invalid_json"),
    ('{"kind":"wait","kind":"act"}', "capability_invalid_json"),
    ('{"kind":"wait","arguments":{"number":NaN}}', "capability_invalid_json"),
    ('```json\n{"kind":"wait"}\n```', "capability_invalid_json"),
    ([], "capability_invalid_schema"),
    ({"kind": "act", "offer_id": "invented"}, "capability_unknown_offer"),
    ({"kind": "act", "offer_id": "board:append:private", "authorized": True}, "capability_invalid_schema"),
    ({"kind": "act", "offer_id": "board:append:private", "scope": {}}, "capability_invalid_schema"),
    ({"kind": "act", "offer_id": "board:append:private", "arguments": {"text": "forged draft"}}, "capability_invalid_channel_choice"),
    ({"kind": "observe", "offer_id": "board:append:private"}, "capability_invalid_channel_choice"),
    ({"kind": "wait", "offer_id": "board:append:private"}, "capability_invalid_schema"),
])
def test_bad_output_is_rejected_without_retry_or_private_error(output, code):
    owner = host(FakeLLM(output))
    with pytest.raises(CapabilitySelectionError) as error:
        run(owner, command())
    assert str(error.value) == code
    assert len(owner.chat.llm.calls) == 1


@pytest.mark.parametrize("metadata,code", [
    ({"finish_reason": "length"}, "capability_output_truncated"),
    ({"finish_reason": "content_filter"}, "capability_invalid_finish_reason"),
    ({"content_type": "null"}, "capability_invalid_content_type"),
    ({"content_type": "array"}, "capability_invalid_content_type"),
    ({"has_tool_calls": True}, "capability_invalid_content_type"),
    ({"has_refusal": True}, "capability_invalid_content_type"),
])
def test_completion_metadata_is_checked_before_valid_json(metadata, code):
    with pytest.raises(CapabilitySelectionError, match=code):
        run(host(FakeLLM({"kind": "wait"}, **metadata)), command())


def test_request_deadline_cannot_reset_sixty_second_opportunity():
    with pytest.raises(CapabilitySelectionError, match="capability_deadline_unbounded"):
        run(host(), command(deadline_at=(moment() + timedelta(seconds=90)).isoformat()))


def test_expired_request_has_no_model_call():
    owner = host()
    with pytest.raises(TimeoutError):
        run(owner, command(deadline_at=(moment() - timedelta(seconds=1)).isoformat()))
    assert owner.chat.llm.calls == []


def test_late_response_does_not_prepare_candidate():
    owner = host(FakeLLM(delay=0.2))
    with pytest.raises(TimeoutError):
        run(owner, command(deadline_at=(moment() + timedelta(seconds=0.1)).isoformat()))
    assert len(owner.chat.llm.calls) == 1


def test_offer_expiring_during_model_call_is_rejected():
    item = offer(valid_until=moment() + timedelta(seconds=0.1))
    with pytest.raises(CapabilitySelectionError, match="capability_offer_unavailable"):
        run(host(FakeLLM(delay=0.2)), command([item]))


def test_cancellation_does_not_prepare_candidate():
    async def scenario():
        owner = host(FakeLLM(delay=0.06))
        task = asyncio.create_task(prepare(owner, command()))
        assert await asyncio.to_thread(owner.chat.llm.started.wait, 1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(owner.chat.llm.calls) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("updates", [{"ready": "true"}, {"available": False}, {"supported": False},
    {"valid_until": "2026-01-01T00:00:00"}, {"observation_refs": ["same", "same"]}])
def test_offer_strictness_and_readiness_invariants(updates):
    with pytest.raises(ValidationError):
        offer(**updates)


def test_duplicate_offer_and_generation_bool_rejected():
    with pytest.raises(ValidationError):
        command([offer(), offer()])
    request = command()
    with pytest.raises(ValidationError):
        CapabilitySelectionRequest.model_validate({**request.payload, "generation": True})


def desktop_offer(capability="app.open.v1"):
    return offer(offer_id="desktop:app:body", channel="desktop", capability_id=capability,
        input_schema={}, scope={"app_ids": ["scratchpad"]})


def test_desktop_compilation_keeps_security_and_binds_typed_criteria():
    item = desktop_offer("file.create_text.v1")
    choice = CapabilityChoice(kind="act", offer_id=item.offer_id,
        arguments={"root_id": "scratch", "relative_path": "proof.txt", "text": "hello"},
        success_criteria=[{"kind": "file", "condition": "exists", "root_id": "scratch", "relative_path": "proof.txt"}])
    goal = compile_desktop_choice(choice, item)
    assert goal.permission_class == "medium"
    assert goal.required_approvals == ["researcher"]
    assert goal.reversible is False
    assert goal.embodied_action.intent.arguments["relative_path"] == "proof.txt"
    assert goal.verification_method == "provider_typed_criteria.v1"
    assert goal.goal_id == compile_desktop_choice(choice, item).goal_id


def test_desktop_model_receives_registry_schema_and_produces_goal():
    item = desktop_offer()
    output = {"kind": "act", "offer_id": item.offer_id, "arguments": {"app_id": "scratchpad"},
        "success_criteria": [{"kind": "window", "condition": "present", "app_id": "scratchpad"}]}
    owner = host(FakeLLM(output))
    result = run(owner, command([item]))
    payload = json.loads(owner.chat.llm.calls[0][0][1]["content"])
    definition = next(value for value in payload["available_offer_definitions"]
                      if value["definition_id"] == payload["available_offers"][0]["definition_id"])
    assert "app_id" in definition["input_schema"]["properties"]
    assert definition["criterion_kinds"] == ["process", "window"]
    assert result["decision"]["choice"]["arguments"] == {"app_id": "scratchpad"}


@pytest.mark.parametrize("output", [
    {"arguments": {"app_id": "scratchpad", "permission_class": "low"}},
    {"arguments": {"app_id": 1}},
    {"success_criteria": [{"kind": "window", "condition": "present", "app_id": "different-app"}]},
    {"success_criteria": []},
])
def test_desktop_rejects_arguments_authority_and_unbound_criteria(output):
    item = desktop_offer()
    value = {"kind": "act", "offer_id": item.offer_id, "arguments": {"app_id": "scratchpad"},
        "success_criteria": [{"kind": "window", "condition": "present", "app_id": "scratchpad"}], **output}
    with pytest.raises(CapabilitySelectionError, match="capability_desktop_validation_failed"):
        compile_desktop_choice(CapabilityChoice(**value), item)


def test_desktop_observation_requires_matching_kind_and_verified_criterion():
    item = desktop_offer("control.observe.v1")
    choice = CapabilityChoice(kind="observe", offer_id=item.offer_id,
        success_criteria=[{"kind": "intentional_noop", "condition": "observed"}])
    assert compile_desktop_choice(choice, item).embodied_action.intent.intent_id == "control.observe.v1"
    with pytest.raises(CapabilitySelectionError, match="capability_observation_kind_mismatch"):
        compile_desktop_choice(choice.model_copy(update={"kind": "act"}), item)


def test_research_only_intents_are_not_available_in_product_selector():
    item = desktop_offer("station.use.v1")
    owner = host(FakeLLM({"kind": "wait"}))
    with pytest.raises(CapabilitySelectionError, match="capability_desktop_intent_unavailable"):
        run(owner, command([item]))
    assert owner.chat.llm.calls == []


def ui_offer_and_choice(*, native_tree=True):
    observed = {"observation_id": "actual-provider-observation", "windows": [
        {"window_id": "window-1", "app_id": "scratchpad", "focused": True}],
        "active_window": {"window_id": "window-1", "app_id": "scratchpad", "focused": True}}
    node = {"window_id": "window-1", "app_id": "scratchpad", "role": "button", "name": "Save"}
    observed.update({"a11y_tree": {"nodes": [node]}} if native_tree else {"a11y_nodes": [node]})
    item = offer(channel="desktop", capability_id="ui.move_pointer.v1",
        observation_refs=["evidence://sha256/" + "a" * 64],
        observation={"provider_id": "existing-body", "body_instance_id": "private",
            "extensions": {"mistyai.desktop.v1": observed}})
    choice = CapabilityChoice(kind="act", offer_id=item.offer_id,
        arguments={"selector": {"observation_id": "actual-provider-observation", **node}},
        success_criteria=[{"kind": "pointer", "app_id": "scratchpad", "role": "button", "name": "Save"}])
    return item, choice


@pytest.mark.parametrize("native_tree", [True, False])
def test_selector_binds_to_verified_provider_snapshot_not_evidence_uri(native_tree):
    item, choice = ui_offer_and_choice(native_tree=native_tree)
    compiled = compile_desktop_choice(choice, item)
    assert compiled.embodied_action.intent.arguments["selector"]["observation_id"] == "actual-provider-observation"
    assert "actual-provider-observation" not in item.observation_refs


@pytest.mark.parametrize("bad_target", ["forged_observation", "missing", "ambiguous", "occurrence", "protected", "body", "provider"])
def test_selector_cannot_invent_or_ambiguously_bind_ui_target(bad_target):
    item, choice = ui_offer_and_choice()
    observed = deepcopy(item.observation)
    arguments = deepcopy(choice.arguments)
    nodes = observed["extensions"]["mistyai.desktop.v1"]["a11y_tree"]["nodes"]
    if bad_target == "forged_observation":
        arguments["selector"]["observation_id"] = "invented"
    elif bad_target == "missing":
        nodes.clear()
    elif bad_target == "ambiguous":
        nodes.append(dict(nodes[0]))
    elif bad_target == "occurrence":
        arguments["selector"]["occurrence"] = 1
    elif bad_target == "protected":
        nodes[0]["protected"] = True
    elif bad_target == "body":
        observed["body_instance_id"] = "different"
    elif bad_target == "provider":
        observed["provider_id"] = "different"
    with pytest.raises(CapabilitySelectionError):
        compile_desktop_choice(choice.model_copy(update={"arguments": arguments}), item.model_copy(update={"observation": observed}))


def test_drag_validates_both_source_and_target_snapshot_identity():
    item, initial = ui_offer_and_choice()
    arguments = {"source": initial.arguments["selector"], "target": {**initial.arguments["selector"], "observation_id": "invented"}}
    choice = CapabilityChoice(kind="act", offer_id=item.offer_id, arguments=arguments,
        success_criteria=initial.success_criteria)
    with pytest.raises(CapabilitySelectionError, match="capability_desktop_selector_observation_mismatch"):
        compile_desktop_choice(choice, item.model_copy(update={"capability_id": "ui.drag.v1"}))


@pytest.mark.parametrize("intent_id", ["window.focus.v1", "ui.key_chord.v1"])
def test_window_or_keyboard_target_must_exist_in_current_snapshot(intent_id):
    item, _ = ui_offer_and_choice()
    arguments = {"window_id": "window-1"} if intent_id == "window.focus.v1" else {"keys": ["enter"]}
    choice = CapabilityChoice(kind="act", offer_id=item.offer_id, arguments=arguments,
        success_criteria=[{"kind": "window", "window_id": "window-1", "condition": "present_and_focused"}])
    item = item.model_copy(update={"capability_id": intent_id})
    assert compile_desktop_choice(choice, item).embodied_action.intent.intent_id == intent_id
    bad = deepcopy(item.observation)
    bad["extensions"]["mistyai.desktop.v1"]["windows"] = []
    with pytest.raises(CapabilitySelectionError, match="capability_desktop_target_missing_or_ambiguous"):
        compile_desktop_choice(choice, item.model_copy(update={"observation": bad}))
