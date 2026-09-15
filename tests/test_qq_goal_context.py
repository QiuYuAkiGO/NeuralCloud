"""Group goal continuations carry scoped typed intent, never private goal prose."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from harness.cognitive.shared_memory import ScopedQQMemoryView, SharedQQBinding
from harness.contracts.goals import QQGoalContext
from harness.qq import decision
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.telemetry import QQTurnTrace
from tests.test_basic_persona_continuity import _host, _command
from tests.test_qq_decision import setup as setup, isolated_static_parser_cache as isolated_static_parser_cache
from tests.test_qq_idle_decision import current_intent, payloads


def goal_context(**updates):
    return QQGoalContext(group_id="10001", self_member_id="29999", opportunity_id="goal-call:one",
        selection_ref="goal-decision:one", generation=2, episode_id="PRIVATE_EPISODE_ID", step_index=0,
        origin="user_task", episode_operation="continue", capability_id="communication.qq.send.v1",
        target_dimensions=["social_connection"], **updates)


def intent():
    return current_intent(opportunity_ref="goal-call:one", selection_ref="goal-decision:one", scheduler_generation=2)


def binding_for(client, args):
    settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
    memory = decision.build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(preset.persona)
    binding = SharedQQBinding(settings, preset, client, ScopedQQMemoryView(memory, group_id="10001", member_id=None), {}, ())
    return binding, memory


def test_goal_semantics_reach_group_stages_without_private_prose_or_other_group_context(setup):
    _, client, builds, args, _ = setup
    binding, memory = binding_for(client, args)
    try:
        result = decision.decide_qq_reply(**args, idle_intent=intent(), shared_owner=binding, goal_context=goal_context())
        assert result.action == "speak", result.error_type
        assert not builds
        gate = json.loads(client.gate_messages[0][1]["content"])
        assert gate["goal_continuation"] == goal_context().model_context()
        assert gate["goal_continuation"]["purpose_kind"] == "user_requested_channel_activity"
        visible = "\n".join(payloads(client))
        assert "goal_continuation" in visible and "user_requested_channel_activity" in visible
        assert "PRIVATE_EPISODE_ID" not in visible and "WRONG_GROUP_SECRET" not in visible
    finally:
        memory.close()


@pytest.mark.parametrize("change", [
    {"group_id": "10002"}, {"self_member_id": "28888"}, {"opportunity_id": "other"},
    {"selection_ref": "other"}, {"generation": 3}, {"capability_id": "communication.qq.read.v1"},
])
def test_goal_scope_and_selection_mismatch_rejected_before_model(setup, change):
    _, client, _, args, _ = setup
    binding, memory = binding_for(client, args)
    try:
        with pytest.raises(ValueError, match="goal continuation"):
            decision.decide_qq_reply(**args, idle_intent=intent(), shared_owner=binding,
                goal_context=goal_context().model_copy(update=change))
        assert not client.gate_messages
    finally:
        memory.close()


def test_private_free_text_cannot_enter_group_goal_dto():
    with pytest.raises(ValueError):
        QQGoalContext.model_validate({**goal_context().model_dump(), "purpose": "PRIVATE_USER_REQUEST"})
    with pytest.raises(ValueError):
        QQGoalContext.model_validate({**goal_context().model_dump(), "current_step": "PRIVATE_PLAN"})


def test_episode_and_step_are_part_of_decision_fingerprint(setup):
    _, client, _, args, _ = setup
    client.choice = '{"action":"wait","reason":"等待合适的群上下文。"}'
    binding, memory = binding_for(client, args)
    same_intent = intent()
    try:
        first = decision.decide_qq_reply(**args, idle_intent=same_intent, shared_owner=binding, goal_context=goal_context())
        second = decision.decide_qq_reply(**args, idle_intent=same_intent, shared_owner=binding,
            goal_context=goal_context().model_copy(update={"episode_id": "other", "step_index": 1}))
        assert first.input_hash != second.input_hash
        assert first.action == second.action == "wait"
    finally:
        memory.close()


def test_public_qq_dto_cannot_inject_runtime_goal_context():
    trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32)
    payload = {"group_id": "10001", "self_member_id": "29999", "idle_intent": intent().model_dump(mode="json"),
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat(),
        "trace": {"trace_id": trace.trace_id, "event_id": trace.event_id, "first_seen_at": trace.first_seen_at.isoformat(),
                  "first_seen_mono": trace.first_seen_mono}, "runtime_goal_context": goal_context().model_dump(mode="json")}
    with pytest.raises(ValueError):
        QQSharedPrepareRequest.model_validate(payload)


def test_cognitive_host_accepts_only_internal_goal_projection_and_keeps_qq_source_check(setup, tmp_path, monkeypatch):
    store, client, _, args, _ = setup
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, ["unused"])
        settings, preset = decision._load_configuration(args["config_path"], args["preset_path"])
        owner.shared.qq_settings = SimpleNamespace(memory_db=store.path)
        binding = SharedQQBinding(settings, preset, client,
            ScopedQQMemoryView(owner.chat.memory, group_id="10001", member_id=None), {}, ())
        monkeypatch.setattr(owner.shared, "qq_binding", lambda **kwargs: binding)
        sources = []
        monkeypatch.setattr("harness.runtime.qq_turns._source", lambda *args: sources.append(args))
        trace = QQTurnTrace(event_id="qq-watch-event:" + "a" * 32)
        payload = {"group_id": "10001", "self_member_id": "29999", "idle_intent": intent().model_dump(mode="json"),
            "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=40)).isoformat(),
            "trace": {"trace_id": trace.trace_id, "event_id": trace.event_id, "first_seen_at": trace.first_seen_at.isoformat(),
                      "first_seen_mono": trace.first_seen_mono}, "runtime_goal_context": goal_context().model_dump(mode="json")}
        try:
            result = await owner.prepare_qq_response(_command("PrepareQQResponse", 1, payload))
            assert result.status == "applied", result.error
            assert sources
            assert json.loads(client.gate_messages[0][1]["content"])["goal_continuation"] == goal_context().model_context()
        finally:
            await owner.close()
    asyncio.run(scenario())
