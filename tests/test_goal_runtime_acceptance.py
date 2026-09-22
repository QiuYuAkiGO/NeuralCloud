"""Reject crossed Cognitive results before durable goal progress is accepted."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import time
from types import SimpleNamespace

import pytest

from harness.cognitive.capabilities import catalog_hash
from harness.contracts.capability_calls import CapabilityOffer
from harness.contracts.goals import GoalDecisionRequest, PreparedGoalDecision
from harness.regulation.store import PSYCHOLOGICAL_DIMENSIONS, STANDING_ID
from harness.runtime.capabilities import goal_error_code
from harness.runtime.goals import GoalController, _episode_context, _outcome_projection, deviation
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType
from harness.runtime.supervisor import DomainCommandError
from tests.test_capability_cognition import command as capability_command
from tests.test_goal_runtime import GOAL_CRITERION, drain_effects, goal_rig, next_goal_event, submit
from tests.test_goal_runtime_review import controller


def prepared_result(row, clock):
    offer = next(item for item in row["offers"] if item["capability_id"] == "expression.board.append.v1")
    decision = PreparedGoalDecision.model_validate({
        "decision_id": "goal-decision:acceptance",
        "opportunity_id": row["opportunity_id"],
        "snapshot_revision": row["goal_request"]["snapshot_revision"],
        "choice": {"kind": "act", "offer_id": offer["offer_id"]},
        "episode_operation": "start",
        "purpose": "Write and independently verify the requested note",
        "target_dimensions": [],
        "plan": [{"statement": "Write the note", "completion_criteria": [GOAL_CRITERION]}],
        "completion_criteria": [GOAL_CRITERION],
        "evidence_refs": row["goal_request"]["goal_context"]["known_evidence_refs"][:1],
        "prepared_at": clock(),
        "deadline_at": clock() + timedelta(seconds=30),
        "catalog_hash": catalog_hash([CapabilityOffer.model_validate(item) for item in row["offers"]]),
    })
    return SimpleNamespace(result={
        "decision": decision.model_dump(mode="json"),
        "prepared_capability_choice": decision.capability_choice.model_dump(mode="json"),
    })


@pytest.mark.parametrize("fresh_state", ["same_binding", "expired", "changed_binding"])
def test_goal_selection_can_outlive_catalog_but_execution_requires_a_fresh_bound_offer(
        tmp_path, monkeypatch, fresh_state):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, clock, requests, _):
            await submit(supervisor)
            event = await next_goal_event(supervisor)
            original = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            offered = next(item for item in original["offers"]
                           if item["capability_id"] == "expression.board.append.v1")
            complete = model.complete_chat_result

            def slow_goal_model(messages, **kwargs):
                response = complete(messages, **kwargs)
                # Advance the wall clock only after the proposal used the
                # original ready catalogue. No real sleep or model is needed.
                if len(model.goal_inputs) == 1 and not model.board_inputs:
                    clock.advance(20)
                return response

            monkeypatch.setattr(model, "complete_chat_result", slow_goal_model)
            catalog = supervisor.capabilities.catalog
            refreshed = []

            async def current_catalog(*args, **kwargs):
                offers = await catalog(*args, **kwargs)
                assert datetime.fromisoformat(offered["valid_until"]) < clock()
                refreshed.append(True)
                if fresh_state == "expired":
                    return [item.model_copy(update={"valid_until": clock() - timedelta(seconds=1)})
                            for item in offers]
                if fresh_state == "changed_binding":
                    return [item.model_copy(update={"activation_revision": "changed-after-selection"})
                            for item in offers]
                return offers

            monkeypatch.setattr(supervisor.capabilities, "catalog", current_catalog)
            result = await supervisor.dispatch(event)
            assert result.status == "completed", result.detail
            await drain_effects(supervisor)
            row = supervisor.capabilities.calls.get(original["opportunity_id"])
            assert len(model.goal_inputs) == 1 and len(refreshed) == 1
            assert row.get("error_type") is None
            if fresh_state == "same_binding":
                assert requests.count(("POST", "/v1/board/append")) == 1
                assert len(model.board_inputs) == 1
                assert row["status"] == "committed"
                assert supervisor.goals.snapshot()["active_episode"]["steps"][0]["status"] == "executed"
            else:
                assert requests.count(("POST", "/v1/board/append")) == 0
                assert model.board_inputs == []
                assert row["status"] == "superseded"
                assert supervisor.goals.snapshot()["active_episode"]["steps"][0]["status"] == "failed"

    asyncio.run(scenario())


@pytest.mark.parametrize("guard_state", ["fresh", "expired", "unsupported"])
def test_goal_desktop_final_guard_rechecks_offer_after_foreground_wait(tmp_path, monkeypatch, guard_state):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch, activities=("control.observe.v1",), desktop=True) as (
                supervisor, _, _, model, clock, requests, body):
            await submit(supervisor)
            event = await next_goal_event(supervisor)
            await supervisor._foreground_lock.acquire()
            try:
                result = await supervisor.dispatch(event)
                assert result.detail["status"] == "pending", result.detail
                assert body.executed == []
                clock.advance(11)
                catalog = supervisor.capabilities.catalog
                final_guards = []

                async def catalog_after_wait(*args, **kwargs):
                    offers = await catalog(*args, **kwargs)
                    assert kwargs.get("executing_action_id")
                    final_guards.append(kwargs["executing_action_id"])
                    if guard_state == "expired":
                        return [item.model_copy(update={"valid_until": clock() - timedelta(seconds=1)})
                                for item in offers]
                    if guard_state == "unsupported":
                        return [item.model_copy(update={"supported": False}) for item in offers]
                    return offers

                monkeypatch.setattr(supervisor.capabilities, "catalog", catalog_after_wait)
            finally:
                supervisor._foreground_lock.release()
            await drain_effects(supervisor)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            assert final_guards and len(model.goal_inputs) == 1
            assert requests.count(("POST", "/v1/board/append")) == 0
            if guard_state == "fresh":
                assert body.executed == body.verified == ["control.observe.v1"]
                assert row["status"] == "completed"
            else:
                assert body.executed == body.verified == []
                assert row["status"] == "rejected"

    asyncio.run(scenario())


@pytest.mark.parametrize("exc,expected", [
    (ValueError("necessary goal steps are unfinished"), "goal_complete_steps_unfinished"),
    (DomainCommandError("cognitive PrepareGoalDecision returned failed: goal_context_too_large"),
     "goal_context_too_large"),
    (DomainCommandError("cognitive PrepareGoalDecision returned conflict: expected revision 14, current 21"),
     "goal_revision_conflict"),
    (DomainCommandError("cognitive PrepareGoalDecision returned failed: private-model-prose"),
     "goal_runtime_error"),
])
def test_goal_error_code_maps_domain_failures_without_leaking_text(exc, expected):
    assert goal_error_code(exc) == expected
    assert "private-model-prose" not in goal_error_code(
        DomainCommandError("cognitive PrepareGoalDecision returned failed: private-model-prose"))


def test_episode_context_omits_receipt_blobs_and_stays_under_goal_limit():
    episode = {
        "id": "episode:1", "episode_id": "episode:1", "status": "active",
        "seen_calls": [f"call:{i}" for i in range(40)],
        "baseline": {"trace": "B" * 8000},
        "last_evaluation": {"prose": "E" * 8000},
        "results": [
            {"call_id": f"call:{i}", "status": "error", "evidence_refs": [f"receipt:{i}"],
             "receipt": {"body": "R" * 4000}, "channel_result": {"page": "P" * 4000},
             "outcome": {"observation": "O" * 4000}}
            for i in range(80)
        ],
        "steps": [{"status": "planned", "completion_criteria": ["A verified expression receipt exists"]}],
        "success_criteria": ["A verified expression receipt exists"],
        "appraisals": [{"id": f"a{i}", "evidence_refs": [f"fact:{i}"]} for i in range(20)],
        "predictions": [{"purpose": "keep going"} for _ in range(20)],
    }
    projected = _episode_context(episode)
    assert projected["episode_id"] == "episode:1"
    assert "seen_calls" not in projected and "baseline" not in projected and "last_evaluation" not in projected
    assert len(projected["results"]) == 16
    assert len(projected["appraisals"]) == 8 and len(projected["predictions"]) == 8
    assert all(set(item) <= {"call_id", "status", "evidence_refs", "at", "channel", "error_code", "step_id"}
               for item in projected["results"])
    assert "receipt" in episode["results"][0]
    payload = capability_command().payload
    payload.update(snapshot_revision=4, homeostatic_summary={"snapshot_revision": 4}, goal_context={
        "episode_id": "episode:1", "active_episode": projected,
        "outcomes": [_outcome_projection(item) for item in episode["results"][-16:]],
        "known_evidence_refs": ["receipt:1"], "requested_goal": {"origin": "maintenance"},
    })
    GoalDecisionRequest.model_validate(payload)
    assert len(json.dumps(payload["goal_context"]).encode()) < 128_000
    fat = {"active_episode": episode, "outcomes": episode["results"]}
    assert len(json.dumps(fat).encode()) > 128_000


@pytest.mark.parametrize("message,expected_code", [
    ("necessary goal steps are unfinished", "goal_complete_steps_unfinished"),
    ("goal completion criterion lacks independent outcome evidence", "goal_complete_outcome_evidence_required"),
    ("private-model-prose-and-secret-value", "goal_runtime_error"),
])
def test_goal_runtime_failure_records_only_allowlisted_diagnostic_codes(tmp_path, monkeypatch, message, expected_code):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, _, _, requests, _):
            await submit(supervisor)
            event = await next_goal_event(supervisor)

            async def rejected_decision(*args, **kwargs):
                raise ValueError(message)

            monkeypatch.setattr(supervisor.goals, "accept", rejected_decision)
            result = await supervisor.dispatch(event)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            assert result.status == "failed" and row["status"] == "error"
            assert row["error_type"] == "ValueError"
            assert row["error_code"] == result.detail["error_code"] == expected_code
            assert message not in str(result.detail) and message not in str(row)
            assert requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


def test_goal_prepare_domain_conflict_records_revision_code(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, _, _, requests, _):
            await submit(supervisor)
            event = await next_goal_event(supervisor)

            async def conflict(*args, **kwargs):
                raise DomainCommandError(
                    "cognitive PrepareGoalDecision returned conflict: expected revision 1, current 2")

            monkeypatch.setattr(supervisor, "_invoke_domain", conflict)
            result = await supervisor.dispatch(event)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            assert result.status == "failed" and row["status"] == "error"
            assert row["error_code"] == result.detail["error_code"] == "goal_revision_conflict"
            assert "expected revision" not in str(row) and "expected revision" not in str(result.detail)
            assert requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


def test_stale_cached_cognitive_revision_is_refreshed_before_goal_prepare(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, _, requests, _):
            await submit(supervisor)
            event = await next_goal_event(supervisor)
            supervisor._cognitive_snapshot = supervisor._cognitive_snapshot.model_copy(update={"revision": 0})
            result = await supervisor.dispatch(event)
            assert result.status == "completed", result.detail
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            assert row.get("error_code") is None and row["status"] in {"committed", "completed", "pending", "wait", "no_action", "defer"}
            assert model.goal_inputs
            assert requests.count(("POST", "/v1/board/append")) >= 0

    asyncio.run(scenario())


@pytest.mark.parametrize("code", [
    "goal_complete_steps_unfinished", "goal_runtime_error", "goal_context_too_large", "private-model-prose",
])
def test_goal_progress_projects_only_allowlisted_runtime_error_codes(tmp_path, code):
    goals, supervisor, _ = controller(tmp_path)
    try:
        episode = {"id": "episode-one", "episode_id": "episode-one", "goal_id": "goal-one",
            "created_at": datetime.now(UTC).isoformat(), "status": "active", "seen_calls": [],
            "steps": [], "results": [], "no_progress": 0, "revision": 1}
        goals.store.put("episode", episode["id"], episode)
        supervisor.capabilities.calls.put({"opportunity_id": "failed-review", "status": "error",
            "goal_binding": {"goal_id": "goal-one", "episode_id": episode["id"]}, "error_code": code})
        goals.reconcile()
        goals.reconcile()
        outcomes = goals.store.get("episode", episode["id"])["results"]
        assert len(outcomes) == 1
        if code == "private-model-prose":
            assert "error_code" not in outcomes[0] and code not in str(outcomes)
        else:
            assert outcomes[0]["error_code"] == code
    finally:
        goals.close()


@pytest.mark.parametrize("mismatch", [
    "choice", "decision_id", "catalog_hash", "prepared_at", "snapshot_revision", "opportunity_id",
])
def test_crossed_prepared_result_cannot_create_episode_or_advance_goal(tmp_path, monkeypatch, mismatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, clock, requests, _):
            goal = await submit(supervisor)
            event = await next_goal_event(supervisor)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            result = prepared_result(row, clock)
            decision, capability = result.result["decision"], result.result["prepared_capability_choice"]
            if mismatch == "choice":
                capability["choice"] = {"kind": "no_action"}
            elif mismatch == "decision_id":
                capability["decision_id"] = "goal-decision:another"
            elif mismatch == "catalog_hash":
                capability["catalog_hash"] = "f" * 64
            elif mismatch == "prepared_at":
                capability["prepared_at"] = (clock() + timedelta(seconds=1)).isoformat()
            elif mismatch == "snapshot_revision":
                decision["snapshot_revision"] += 1
            elif mismatch == "opportunity_id":
                # Keep both Cognitive projections mutually consistent while
                # binding them to a different Runtime reservation.
                decision["opportunity_id"] = capability["opportunity_id"] = "capability:another"
            before = deepcopy(supervisor.goals.snapshot())
            with pytest.raises(ValueError, match="goal decision capability or snapshot binding mismatch"):
                await supervisor.goals.accept(row, result, event)
            assert supervisor.goals.snapshot() == before
            assert supervisor.goals.store.get("goal", goal["id"])["status"] == "queued"
            assert supervisor.goals.store.all("decision") == []
            assert supervisor.goals.store.all("episode") == []
            assert supervisor.capabilities.calls.get(row["opportunity_id"])["status"] == "queued"
            assert model.goal_inputs == [] and requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


def test_ungrounded_plan_evidence_is_rejected_before_any_goal_write(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, _, clock, requests, _):
            await submit(supervisor)
            event = await next_goal_event(supervisor)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            result = prepared_result(row, clock)
            result.result["decision"]["evidence_refs"] = ["external-approval:invented-by-model"]
            with pytest.raises(ValueError, match="goal decision contains ungrounded evidence"):
                await supervisor.goals.accept(row, result, event)
            assert supervisor.goals.store.all("decision") == []
            assert supervisor.goals.store.all("episode") == []
            assert requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


def test_consistently_bound_result_is_durable_once_and_still_requires_channel_dispatch(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, _, clock, requests, _):
            goal = await submit(supervisor)
            event = await next_goal_event(supervisor)
            row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
            result = prepared_result(row, clock)
            first = await supervisor.goals.accept(row, result, event)
            episode_before = supervisor.goals.store.get("episode", row["goal_binding"]["episode_id"])
            replay = await supervisor.goals.accept(row, result, event)
            assert replay == first == result.result["prepared_capability_choice"]
            assert supervisor.goals.store.get("episode", episode_before["id"]) == episode_before
            assert len(supervisor.goals.store.all("decision")) == 1
            assert episode_before["steps"][0]["status"] == "pending"
            assert episode_before["results"] == []
            assert supervisor.goals.store.get("goal", goal["id"])["status"] == "active"
            assert requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("arriving_event", [RuntimeEventType.HEARTBEAT, RuntimeEventType.USER_MESSAGE])
@pytest.mark.parametrize("arrival_phase", ["before_discovery", "during_discovery"])
def test_heartbeat_during_discovery_cannot_starve_goal_but_user_input_still_has_priority(
        tmp_path, monkeypatch, arriving_event, arrival_phase):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, _, requests, _):
            goal = await submit(supervisor)
            original_catalog = supervisor.capabilities.catalog
            catalog_calls = []

            async def queue_arrival():
                if arriving_event == RuntimeEventType.USER_MESSAGE:
                    incoming = await supervisor.submit_user_message("先处理我的新请求。")
                else:
                    incoming = await supervisor.submit_event(
                        event_type=RuntimeEventType.HEARTBEAT, priority=RuntimeEventPriority.HEARTBEAT,
                        source="scheduler", payload={},
                    )
                assert incoming is not None
                # Keep quiet-time eligibility constant so the user case proves
                # queued work itself blocks scheduling, independently of the
                # usual activity timestamp guard.
                supervisor.capabilities.last_activity = time.monotonic() - 61

            async def catalog_with_arrival(*args, **kwargs):
                catalog_calls.append(True)
                offers = await original_catalog(*args, **kwargs)
                if arrival_phase == "during_discovery":
                    # A provider can finish after the next routine tick is
                    # queued; no real delay or external model call is needed.
                    await queue_arrival()
                return offers

            monkeypatch.setattr(supervisor.capabilities, "catalog", catalog_with_arrival)
            supervisor.capabilities.last_activity = time.monotonic() - 61
            if arrival_phase == "before_discovery":
                await queue_arrival()
            await supervisor.goals.heartbeat()
            queued = supervisor.queue.snapshot()
            opportunities = [event for event in queued if event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY]
            assert any(event.event_type == arriving_event for event in queued)
            if arriving_event == RuntimeEventType.HEARTBEAT:
                assert len(opportunities) == 1
                reserved = supervisor.capabilities.calls.get(opportunities[0].payload["opportunity_id"])
                assert reserved["goal_binding"]["goal_id"] == goal["id"]
                assert reserved["status"] == "queued"
                assert supervisor.goals.budget()["calls_last_hour"] == 1
                assert len(catalog_calls) == 1
            else:
                assert opportunities == []
                assert supervisor.goals.budget()["calls_last_hour"] == 0
                assert len(catalog_calls) == (1 if arrival_phase == "during_discovery" else 0)
            assert model.goal_inputs == [] and requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("user_requested", [False, True])
def test_real_heartbeat_result_keeps_psychological_snapshot_available_to_goal_scheduler(
        tmp_path, monkeypatch, user_requested):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, engine, model, clock, requests, _):
            expected_goal = (await submit(supervisor))["id"] if user_requested else STANDING_ID
            clock.advance(61)
            supervisor.capabilities.last_activity = time.monotonic() - 61
            before_revision = supervisor._homeostasis_snapshot.revision
            event = await supervisor.submit_event(
                event_type=RuntimeEventType.HEARTBEAT, priority=RuntimeEventPriority.HEARTBEAT,
                source="scheduler", payload={}, source_time=clock(),
            )
            assert (await supervisor.queue.get(timeout=0)).event_id == event.event_id
            result = await supervisor.dispatch(event)
            assert result.status == "completed", result.detail
            assert supervisor._homeostasis_snapshot.revision > before_revision
            # ApplyHeartbeat returns a result envelope whereas boot GetSnapshot
            # returns the bare state. Keep the real command/cache path intact.
            wrapped = supervisor._homeostasis_snapshot.payload
            assert "snapshot" in wrapped and "dimensions" in wrapped["snapshot"]
            assert deviation(wrapped, PSYCHOLOGICAL_DIMENSIONS) > 0
            opportunities = [queued for queued in supervisor.queue.snapshot()
                if queued.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY]
            assert len(opportunities) == 1, supervisor.goals.snapshot()
            row = supervisor.capabilities.calls.get(opportunities[0].payload["opportunity_id"])
            assert row["goal_binding"]["goal_id"] == expected_goal
            request = row["goal_request"]
            assert request["snapshot_revision"] == supervisor._homeostasis_snapshot.revision
            actual_snapshot = request["goal_context"]["snapshot"]
            assert actual_snapshot == engine.snapshot.model_dump(mode="json")
            assert "snapshot" not in actual_snapshot
            assert all(name in actual_snapshot["dimensions"] for name in PSYCHOLOGICAL_DIMENSIONS)
            assert supervisor.goals.budget()["calls_last_hour"] == 1
            assert model.goal_inputs == [] and requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("previous_evaluation", [False, True])
def test_advancing_clock_does_not_make_available_goal_budget_perpetually_future(
        tmp_path, monkeypatch, previous_evaluation):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, clock, requests, _):
            goal = await submit(supervisor)
            started = clock()
            ticks = []

            def advancing_clock():
                value = started + timedelta(milliseconds=len(ticks))
                ticks.append(value)
                return value

            supervisor.goals.clock = advancing_clock
            if previous_evaluation:
                state = supervisor.goals._state()
                state["calls"] = [(started - timedelta(seconds=61)).isoformat()]
                supervisor.goals._put_state(state)
            supervisor.capabilities.last_activity = time.monotonic() - 61
            await supervisor.goals.heartbeat()
            opportunities = [event for event in supervisor.queue.snapshot()
                if event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY]
            assert len(opportunities) == 1
            row = supervisor.capabilities.calls.get(opportunities[0].payload["opportunity_id"])
            assert row["goal_binding"]["goal_id"] == goal["id"]
            assert supervisor.goals.budget()["calls_last_hour"] == (2 if previous_evaluation else 1)
            assert len(ticks) > 1 and ticks[-1] > ticks[0]
            assert model.goal_inputs == [] and requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


def test_goal_budget_preserves_60_second_spacing_and_12_per_rolling_hour(tmp_path):
    goals, _, _ = controller(tmp_path)
    started = datetime.now(UTC)
    try:
        state = goals._state()
        state["calls"] = [started.isoformat()]
        goals._put_state(state)
        assert goals.budget(now=started + timedelta(seconds=59, milliseconds=999))["next_available_at"] == (
            started + timedelta(seconds=60)).isoformat()
        assert goals.budget(now=started + timedelta(seconds=60))["next_available_at"] == (
            started + timedelta(seconds=60)).isoformat()
        state["calls"] = [(started + timedelta(seconds=60 * index)).isoformat() for index in range(12)]
        goals._put_state(state)
        exhausted = goals.budget(now=started + timedelta(seconds=720))
        assert exhausted["calls_last_hour"] == exhausted["maximum_calls_per_hour"] == 12
        assert exhausted["next_available_at"] == (started + timedelta(hours=1)).isoformat()
        assert goals.budget(now=started + timedelta(seconds=3599, milliseconds=999))["calls_last_hour"] == 12
        reopened = goals.budget(now=started + timedelta(hours=1))
        assert reopened["calls_last_hour"] == 11
        assert reopened["next_available_at"] == (started + timedelta(hours=1)).isoformat()
    finally:
        goals.close()


@pytest.mark.parametrize("need", ["psychological", "nominal", "energy_only"])
def test_pausing_user_goal_leaves_standing_goal_eligible_only_for_salient_psychological_need(
        tmp_path, monkeypatch, need):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, _, requests, _):
            goal = await submit(supervisor)
            await supervisor.goals.operate(goal["id"], "pause", "pause-user-only")
            summary = deepcopy(supervisor._response_homeostatic_interoception())
            if need == "nominal":
                summary["salience"] = "nominal"
                monkeypatch.setattr(supervisor, "_response_homeostatic_interoception", lambda: summary)
            elif need == "energy_only":
                # A salient energy signal must not start psychological work.
                payload = deepcopy(supervisor._homeostasis_snapshot.payload)
                state = payload.get("snapshot", payload)
                for name in PSYCHOLOGICAL_DIMENSIONS:
                    factor = state["dimensions"][name]
                    factor["value"] = sum(factor["target_band"]) / 2
                summary["salience"] = "marked"
                summary["dominant_needs"] = [{"dimension": "energy", "direction": "increase"}]
                supervisor._homeostasis_snapshot = supervisor._homeostasis_snapshot.model_copy(update={"payload": payload})
                monkeypatch.setattr(supervisor, "_response_homeostatic_interoception", lambda: summary)
            supervisor.capabilities.last_activity = time.monotonic() - 61
            await supervisor.goals.heartbeat()
            opportunities = [event for event in supervisor.queue.snapshot()
                if event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY]
            if need == "psychological":
                assert len(opportunities) == 1
                row = supervisor.capabilities.calls.get(opportunities[0].payload["opportunity_id"])
                assert row["goal_binding"]["goal_id"] == STANDING_ID
                assert supervisor.goals.budget()["calls_last_hour"] == 1
            else:
                assert opportunities == []
                assert supervisor.goals.budget()["calls_last_hour"] == 0
            assert supervisor.goals.store.get("goal", goal["id"])["status"] == "paused"
            assert supervisor.goals.store.get("goal", STANDING_ID)["status"] == "observing"
            assert model.goal_inputs == [] and requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())


@pytest.mark.parametrize("wake", ["resume", "user_evidence"])
def test_three_failed_preparations_persist_wait_without_recounting_and_require_new_signal(
        tmp_path, monkeypatch, wake):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (supervisor, _, _, model, clock, requests, _):
            goal = await submit(supervisor)
            failed_calls = []
            for expected_count in (1, 2, 3):
                supervisor.capabilities.last_activity = time.monotonic() - 61
                await supervisor.goals.heartbeat()
                event = await supervisor.queue.get(timeout=0)
                assert event is not None and event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY
                row = supervisor.capabilities.calls.get(event.payload["opportunity_id"])
                failed_calls.append(row["opportunity_id"])
                # Exercise recovery of a failed model preparation before any
                # episode/step can be accepted or any channel dispatched.
                supervisor.capabilities._finish(row, "error", error_type="model_preparation_failed")
                supervisor.goals.reconcile()
                value = supervisor.goals.store.get("goal", goal["id"])
                assert value["no_progress"] == expected_count
                clock.advance(61)
            assert value["blocked_reason"] == "no_progress"
            assert set(value["seen_preparations"]) == set(failed_calls)
            assert supervisor.goals.store.all("episode") == []
            supervisor.goals.close()
            supervisor.goals = GoalController(supervisor, tmp_path / "runtime", clock=clock)
            for _ in range(3):
                supervisor.goals.reconcile()
                supervisor.capabilities.last_activity = time.monotonic() - 61
                await supervisor.goals.heartbeat()
                assert supervisor.queue.empty()
                assert supervisor.goals.store.get("goal", goal["id"])["no_progress"] == 3
                assert supervisor.goals.budget()["calls_last_hour"] == 3
                clock.advance(61)
            if wake == "resume":
                await supervisor.goals.operate(goal["id"], "resume", "resume-after-failed-preparations")
                assert supervisor.goals.store.get("goal", goal["id"])["no_progress"] == 0
            else:
                previous_signal = supervisor.goals._state()["signal_revision"]
                # The accepted-event projection runs after ingress, separately
                # from the higher-priority conversation dispatch. Exercise that
                # source-authorized signal once the foreground queue is clear.
                await supervisor.goals.on_event("event.accepted", {
                    "event_type": RuntimeEventType.USER_MESSAGE.value,
                    "event_id": "user:new-evidence", "payload": {"text": "目标的依据发生了变化。"},
                })
                assert supervisor.goals._state()["signal_revision"] == previous_signal + 1
            supervisor.capabilities.last_activity = time.monotonic() - 61
            await supervisor.goals.heartbeat()
            queued = supervisor.queue.snapshot()
            assert len(queued) == 1 and queued[0].event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY
            reserved = supervisor.capabilities.calls.get(queued[0].payload["opportunity_id"])
            assert reserved["goal_binding"]["goal_id"] == goal["id"]
            assert reserved["opportunity_id"] not in failed_calls
            assert supervisor.goals.budget()["calls_last_hour"] == 4
            await supervisor.goals.heartbeat()
            assert len(supervisor.queue.snapshot()) == 1
            assert supervisor.goals.budget()["calls_last_hour"] == 4
            assert set(supervisor.goals.store.get("goal", goal["id"])["seen_preparations"]) == set(failed_calls)
            assert model.goal_inputs == [] and requests.count(("POST", "/v1/board/append")) == 0

    asyncio.run(scenario())
