"""Human approval delay and changing desktop targets in the real Runtime loop."""
from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from pydantic import ValidationError

from harness.research.manifest import BasicTerminalCondition
from harness.runtime import ActionExecutionState
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, utc_now
from tests.test_basic_terminal_runtime import APP, REQUEST, boot, drain, runtime


async def pending_type(supervisor):
    await boot(supervisor)
    await supervisor.submit_user_message(REQUEST, session_id="sess_approval")
    results = await drain(supervisor, approvals=False)
    return next(
        supervisor.action_coordinator.get(result.detail["action"]["action_id"])
        for result in results
        if result.detail.get("action", {}).get("state") == "AWAITING_APPROVAL"
    )


def test_approval_configuration_is_bounded():
    assert BasicTerminalCondition().approval_timeout_seconds == 180
    for seconds in (0, 601):
        with pytest.raises(ValidationError):
            BasicTerminalCondition(approval_timeout_seconds=seconds)


def test_approval_after_prepare_expiry_refreshes_observation_without_changing_command(tmp_path, monkeypatch):
    async def scenario():
        supervisor, desktop, _, pipeline = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            prepared = supervisor._action_context[pending.action_id]["prepared"]
            assert pending.deadline > prepared.expires_at + timedelta(seconds=140)
            approved_arguments = dict(pending.embodied_action.intent.arguments)
            later = utc_now() + timedelta(seconds=45)
            assert later > prepared.expires_at
            monkeypatch.setattr("harness.runtime.supervisor.utc_now", lambda: later)
            monkeypatch.setattr("harness.runtime.basic_terminal.utc_now", lambda: later)
            monkeypatch.setattr("harness.runtime.action._utc_now", lambda: later)
            monkeypatch.setattr(desktop, "clock", lambda: later)
            observe = desktop.observe

            async def current_observation(**kwargs):
                return await observe(now=later)

            monkeypatch.setattr(desktop, "observe", current_observation)
            approved = await supervisor.approve_action(pending.action_id, approval="researcher")
            assert approved.state == ActionExecutionState.RUNNING, approved.error
            await asyncio.gather(*list(supervisor._action_tasks.values()))
            executed = supervisor.action_coordinator.get(pending.action_id)
            assert executed.state == ActionExecutionState.SUCCEEDED
            assert desktop.executed == ["app.open.v1", "ui.type_text.v1"]
            refreshed = executed.embodied_action.intent.arguments
            assert refreshed["text"] == approved_arguments["text"] == desktop.typed
            assert refreshed["selector"]["observation_id"] != approved_arguments["selector"]["observation_id"]
            assert {k: v for k, v in refreshed["selector"].items() if k != "observation_id"} == {
                k: v for k, v in approved_arguments["selector"].items() if k != "observation_id"
            }
            assert executed.embodied_action.success_criteria == pending.embodied_action.success_criteria
            results = await drain(supervisor)
            final_reply = next(result for result in results if result.decision_kind == "reply" and result.detail["delivered"])
            assert "/home/misty-body" in "".join(final_reply.detail["delivery"]["delivered_segments"])
            assert sum(turn.text == REQUEST for turn in pipeline.session_store.recent("sess_approval")) == 1
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("elapsed", [181, 601])
def test_expired_approval_or_task_cannot_execute_input(tmp_path, monkeypatch, elapsed):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            later = utc_now() + timedelta(seconds=elapsed)
            monkeypatch.setattr("harness.runtime.supervisor.utc_now", lambda: later)
            monkeypatch.setattr("harness.runtime.basic_terminal.utc_now", lambda: later)
            rejected = await supervisor.approve_action(pending.action_id, approval="researcher")
            assert rejected.state == ActionExecutionState.CANCELLED
            assert desktop.executed == ["app.open.v1"]
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


def test_approval_deadline_is_capped_by_task_and_does_not_extend_ordinary_goals(tmp_path):
    async def scenario():
        supervisor, _, _, _ = runtime(tmp_path)
        config = supervisor.manifest.basic_terminal.model_copy(update={"task_timeout_seconds": 60})
        supervisor.manifest = supervisor.manifest.model_copy(update={"basic_terminal": config})
        supervisor._basic_terminal.config = config
        try:
            pending = await pending_type(supervisor)
            context = supervisor._action_context[pending.action_id]
            task = next(iter(supervisor._basic_terminal.tasks.values()))
            from datetime import datetime

            assert pending.deadline == datetime.fromisoformat(task["deadline"])
            prepared = context["prepared"]
            decision = prepared.decision.model_copy(update={"policy_id": "ordinary_goal", "goal_id": "ordinary_goal"})
            ordinary = prepared.model_copy(update={"prepare_id": "ordinary_prepare", "decision": decision})
            result = await supervisor._handle_decision(context["event"], ordinary, decision)
            other = supervisor.action_coordinator.get(result["action"]["action_id"])
            assert other.deadline == ordinary.expires_at
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["other_app", "other_window", "renamed_node", "ambiguous_node", "stale", "future", "unavailable"])
def test_changed_or_unavailable_target_cancels_approved_input(tmp_path, monkeypatch, change):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            observe = desktop.observe

            async def changed_observation(**kwargs):
                if change == "unavailable":
                    raise RuntimeError("desktop unavailable")
                snapshot = await observe(**kwargs)
                if change == "stale":
                    return snapshot.model_copy(update={"captured_at": utc_now() - timedelta(minutes=5)})
                if change == "future":
                    return snapshot.model_copy(update={"captured_at": utc_now() + timedelta(minutes=5)})
                observed = snapshot.extensions["mistyai.desktop.v1"]
                if change == "other_app":
                    observed["active_window"]["app_id"] = "org.example.Other"
                elif change == "other_window":
                    observed["active_window"]["window_id"] = "window-2"
                elif change == "renamed_node":
                    observed["a11y_nodes"][0]["name"] = "Other terminal"
                elif change == "ambiguous_node":
                    observed["a11y_nodes"].append(dict(observed["a11y_nodes"][0]))
                return snapshot

            monkeypatch.setattr(desktop, "observe", changed_observation)
            rejected = await supervisor.approve_action(pending.action_id, approval="researcher")
            assert rejected.state == ActionExecutionState.CANCELLED
            assert desktop.executed == ["app.open.v1"]
            assert not any(
                record.record_type == "action_execution_intent" and record.data["action"]["action_id"] == pending.action_id
                for record in supervisor.journal.iter_records()
            )
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


def test_future_initial_observation_prevents_terminal_planning(tmp_path, monkeypatch):
    async def scenario():
        supervisor, desktop, model, _ = runtime(tmp_path)
        try:
            await boot(supervisor)
            observe = desktop.observe

            async def future_observation(**kwargs):
                snapshot = await observe(**kwargs)
                return snapshot.model_copy(update={"captured_at": utc_now() + timedelta(minutes=5)})

            monkeypatch.setattr(desktop, "observe", future_observation)
            await supervisor.submit_user_message(REQUEST)
            results = await drain(supervisor)
            assert not desktop.executed and not model.calls
            assert any(result.decision_kind == "reply" and result.detail["delivered"] for result in results)
            assert not supervisor._basic_terminal.tasks
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("elapsed", [181, 601])
def test_heartbeat_finishes_unapproved_task_once_without_further_user_input(tmp_path, monkeypatch, elapsed):
    async def heartbeat(supervisor):
        await supervisor.submit_event(
            event_type=RuntimeEventType.HEARTBEAT, priority=RuntimeEventPriority.HEARTBEAT,
            source="scheduler", payload={},
        )
        return await drain(supervisor, approvals=False)

    async def scenario():
        supervisor, desktop, _, pipeline = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            # A normal heartbeat must preserve a still-valid approval request.
            await heartbeat(supervisor)
            assert supervisor.action_coordinator.get(pending.action_id).state == ActionExecutionState.AWAITING_APPROVAL
            later = utc_now() + timedelta(seconds=elapsed)
            monkeypatch.setattr("harness.runtime.supervisor.utc_now", lambda: later)
            monkeypatch.setattr("harness.runtime.basic_terminal.utc_now", lambda: later)
            monkeypatch.setattr("harness.runtime.action._utc_now", lambda: later)
            monkeypatch.setattr("harness.runtime.adapters._utc_now", lambda: later)
            monkeypatch.setattr(desktop, "clock", lambda: later)
            results = await heartbeat(supervisor)
            action = supervisor.action_coordinator.get(pending.action_id)
            assert action.state == ActionExecutionState.CANCELLED
            assert desktop.executed == ["app.open.v1"]
            assert not supervisor._basic_terminal.tasks
            assert any(result.decision_kind == "action_result" for result in results)
            replies = [result for result in results if result.decision_kind == "reply" and result.detail["delivered"]]
            assert len(replies) == 1
            assert "未完成" in "".join(replies[0].detail["delivery"]["delivered_segments"])
            assert sum(turn.text == REQUEST for turn in pipeline.session_store.recent("sess_approval")) == 1
            assert not any(result.decision_kind == "reply" for result in await heartbeat(supervisor))
            committed = [
                record for record in supervisor.journal.iter_records()
                if record.record_type == "action_interaction_committed" and record.data["action_id"] == pending.action_id
            ]
            assert len(committed) == 1
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("state", [ActionExecutionState.RUNNING, ActionExecutionState.VERIFYING, ActionExecutionState.UNKNOWN])
def test_terminal_expiration_never_cancels_submitted_or_unknown_effects(tmp_path, state):
    async def scenario():
        supervisor, _, _, _ = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            submitted = pending.model_copy(update={"state": state, "deadline": utc_now() - timedelta(seconds=1)})
            supervisor.action_coordinator.restore(submitted)
            await supervisor._basic_terminal.expire_pending_approvals()
            assert supervisor.action_coordinator.get(pending.action_id) == submitted
            assert supervisor.queue.empty()
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


def test_terminal_expiration_leaves_ordinary_approval_policy_unchanged(tmp_path):
    async def scenario():
        supervisor, _, _, _ = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            context = supervisor._action_context[pending.action_id]
            prepared = context["prepared"]
            context["prepared"] = prepared.model_copy(update={
                "decision": prepared.decision.model_copy(update={"policy_id": "ordinary_goal"}),
            })
            expired = pending.model_copy(update={"deadline": utc_now() - timedelta(seconds=1)})
            supervisor.action_coordinator.restore(expired)
            await supervisor._basic_terminal.expire_pending_approvals()
            assert supervisor.action_coordinator.get(pending.action_id) == expired
            assert supervisor.queue.empty()
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


def test_enter_cannot_submit_to_a_different_terminal_window(tmp_path, monkeypatch):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        try:
            pending = await pending_type(supervisor)
            await supervisor.approve_action(pending.action_id, approval="researcher")
            results = await drain(supervisor, approvals=False)
            enter = next(
                result.detail["action"] for result in results
                if result.detail.get("action", {}).get("state") == "AWAITING_APPROVAL"
            )
            assert enter["embodied_action"]["intent"]["arguments"] == {"keys": ["enter"]}
            observe = desktop.observe

            async def other_terminal(**kwargs):
                snapshot = await observe(**kwargs)
                snapshot.extensions["mistyai.desktop.v1"]["active_window"] = {
                    "app_id": APP, "window_id": "window-2", "focused": True,
                }
                return snapshot

            monkeypatch.setattr(desktop, "observe", other_terminal)
            rejected = await supervisor.approve_action(enter["action_id"], approval="researcher")
            assert rejected.state == ActionExecutionState.CANCELLED
            assert desktop.executed == ["app.open.v1", "ui.type_text.v1"]
        finally:
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


def test_user_interrupt_during_approval_observation_prevents_input(tmp_path, monkeypatch):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            pending = await pending_type(supervisor)
            observe = desktop.observe

            async def delayed_observation(**kwargs):
                entered.set()
                await release.wait()
                return await observe(**kwargs)

            monkeypatch.setattr(desktop, "observe", delayed_observation)
            approving = asyncio.create_task(supervisor.approve_action(pending.action_id, approval="researcher"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            await supervisor.submit_user_message("停止之前的操作。")
            release.set()
            rejected = await approving
            assert rejected.state == ActionExecutionState.CANCELLED
            assert desktop.executed == ["app.open.v1"]
        finally:
            release.set()
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())


@pytest.mark.parametrize("second_operation", ["approve", "reject"])
def test_concurrent_approval_or_rejection_does_not_repeat_or_revive_input(tmp_path, monkeypatch, second_operation):
    async def scenario():
        supervisor, desktop, _, _ = runtime(tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        try:
            pending = await pending_type(supervisor)
            observe = desktop.observe

            async def delayed_observation(**kwargs):
                entered.set()
                await release.wait()
                return await observe(**kwargs)

            monkeypatch.setattr(desktop, "observe", delayed_observation)
            first = asyncio.create_task(supervisor.approve_action(pending.action_id, approval="researcher"))
            await asyncio.wait_for(entered.wait(), timeout=2)
            if second_operation == "approve":
                second = asyncio.create_task(supervisor.approve_action(pending.action_id, approval="researcher"))
            else:
                await supervisor.reject_action(pending.action_id, reason="cancelled during observation")
                second = None
            release.set()
            await asyncio.gather(first, *([second] if second else []))
            await asyncio.gather(*list(supervisor._action_tasks.values()))
            assert desktop.executed.count("ui.type_text.v1") == (1 if second else 0)
        finally:
            release.set()
            await supervisor.stop(reason="test finished")

    asyncio.run(scenario())
