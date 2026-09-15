"""Production goals cross real Runtime/Cognitive/Homeostasis and body boundaries.

Only model answers and the body's HTTP transport are simulated. Goal decisions,
channel selection, Board writes, independent receipts and recovery use their
actual controllers and persistent stores.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import sys
import time

import pytest

# Preload both domains so Clock patches their imported datetime bindings.
from harness.cognitive import board as cognitive_board, goals as cognitive_goals  # noqa: F401
from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.memory.shared import SharedReadScope
from harness.regulation.config import RegulationConfig
from harness.regulation.store import STANDING_ID
from harness.runtime.goals import GoalController
from harness.runtime.models import RuntimeEventType, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeStateError
from tests.test_autonomous_capabilities import rig


GOAL_TEXT = "请在私人留言板写下 GOAL_NOTE，并核验实际写入。"
GOAL_CRITERION = "私人留言板包含 GOAL_NOTE"


class Clock:
    """Advance project wall clocks while leaving asyncio's monotonic clock real."""
    def __init__(self, monkeypatch):
        self.value = datetime.now(timezone.utc)
        owner = self
        actual_datetime = datetime

        class ClockMeta(type):
            def __instancecheck__(cls, instance):
                return isinstance(instance, actual_datetime)

        class ProjectDatetime(actual_datetime, metaclass=ClockMeta):
            @classmethod
            def now(cls, tz=None):
                return owner.value.astimezone(tz) if tz is not None else owner.value.replace(tzinfo=None)

        # All participating services run in-process here; the HTTP Board still
        # checks the same wall deadline as Runtime and Cognitive.
        for name, module in list(sys.modules.items()):
            if name.startswith("harness.") and getattr(module, "datetime", None) is actual_datetime:
                monkeypatch.setattr(module, "datetime", ProjectDatetime)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += timedelta(seconds=seconds)


class GoalModel:
    def __init__(self, activities=("expression.board.append.v1",)):
        self.activities = activities
        self.goal_inputs = []
        self.board_inputs = []
        self.chat_calls = []

    def complete_chat_result(self, messages, **kwargs):
        self.chat_calls.append(messages)
        payload = json.loads(messages[-1]["content"])
        if "goal_context" in payload:
            self.goal_inputs.append(payload)
            context = payload["goal_context"]
            episode = context.get("active_episode")
            known = context["known_evidence_refs"]
            if episode is None:
                index, operation = 0, "start"
            else:
                index = sum(step["status"] == "executed" for step in episode["steps"])
                operation = "continue"
            if index >= len(self.activities):
                proven = sorted({ref for item in context["outcomes"] if item["status"] in {"completed", "committed"}
                                 for ref in item["evidence_refs"]})
                criteria = sorted({*episode["success_criteria"],
                    *[criterion for step in episode["steps"] for criterion in step["completion_criteria"]]})
                answer = {"episode_operation": "complete", "choice": {"kind": "no_action"}, "evidence_refs": proven,
                    "criterion_results": [{"criterion": criterion, "satisfied": True, "evidence_refs": proven} for criterion in criteria]}
            else:
                capability = self.activities[index]
                offered = next(item for item in payload["available_offers"] if item["capability_id"] == capability)
                choice = {"kind": "observe" if capability in {"control.observe.v1", "expression.board.read.v1"} else "act",
                          "offer_id": offered["offer_id"]}
                if capability == "control.observe.v1":
                    choice["success_criteria"] = [{"kind": "intentional_noop", "condition": "observed"}]
                answer = {"episode_operation": operation, "choice": choice, "evidence_refs": known[:1]}
                if operation == "start":
                    answer.update(purpose="Write and verify the requested GOAL_NOTE", target_dimensions=[],
                        plan=[{"statement": "Use " + item, "completion_criteria": [
                            GOAL_CRITERION if item == "expression.board.append.v1" else "Current environment was independently observed"]}
                            for item in self.activities], completion_criteria=[GOAL_CRITERION])
        else:
            self.board_inputs.append(payload)
            answer = {"action": "write", "title": "目标记录", "text": "GOAL_NOTE"}
        return ChatResult(text=json.dumps(answer), usage=TokenUsage(prompt_tokens=80, completion_tokens=80, total_tokens=160))


@asynccontextmanager
async def goal_rig(tmp_path, monkeypatch, *, activities=("expression.board.append.v1",), desktop=False):
    async with rig(tmp_path, monkeypatch, [{"kind": "no_action"}], desktop=desktop) as values:
        sup, host, engine, _, legacy_event, requests, body = values
        assert (await sup.dispatch(legacy_event)).status == "completed"
        clock = Clock(monkeypatch)
        sup.goals.clock = clock
        configuration = RegulationConfig(enabled=True)
        sup.manifest = sup.manifest.model_copy(update={"goal_regulation": configuration})
        sup.goals.config = configuration
        if desktop:
            options = sup.capabilities.settings.model_dump()
            options["owners"]["board"] = "shared"
            options["allowed_capabilities"] = (*options["allowed_capabilities"], "expression.board.read.v1", "expression.board.append.v1")
            sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities": options})
        model = GoalModel(activities)
        host.chat.llm = model
        yield sup, host, engine, model, clock, requests, body


async def next_goal_event(sup):
    for _ in range(10):
        sup.capabilities.last_activity = time.monotonic() - 61
        sup.board.last_activity = time.monotonic() - 61
        await sup.goals.heartbeat()
        event = await sup.queue.get(timeout=0)
        assert event is not None, sup.goals.snapshot()
        if event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY:
            return event
        assert event.event_type in {RuntimeEventType.PERCEPTION, RuntimeEventType.BODY_STATE}, event
        result = await sup.dispatch(event)
        assert result.status == "completed", result.detail
    raise AssertionError("goal did not obtain a stable observed opportunity")


async def drain_effects(sup, *, expected_unknown=False):
    results = []
    for _ in range(15):
        if sup._action_tasks:
            await asyncio.gather(*list(sup._action_tasks.values()))
        event = await sup.queue.get(timeout=0)
        if event is None:
            break
        result = await sup.dispatch(event)
        if result.status != "completed":
            assert expected_unknown and result.status == "deferred" and (
                "unknown external result" in result.detail.get("error", "")
                or result.detail.get("lifecycle") == "PAUSED" and sup.lifecycle == RuntimeLifecycle.PAUSED
            ), result.detail
        results.append(result)
    await sup.capabilities.reconcile()
    sup.goals.reconcile()
    return results


async def submit(sup):
    return await sup.goals.submit({"request_id": "requested-note", "statement": GOAL_TEXT, "success_criteria": [GOAL_CRITERION]})


def test_user_goal_board_write_verified_review_completes_only_episode(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch) as (sup, host, engine, model, clock, requests, _):
            goal = await submit(sup)
            baseline = engine.snapshot.model_dump(mode="json")
            event = await next_goal_event(sup)
            result = await sup.dispatch(event)
            assert result.status == "completed", (result.detail, sup.capabilities.calls.all())
            await drain_effects(sup)
            state = sup.goals.snapshot()
            episode = state["active_episode"]
            assert episode["steps"][0]["status"] == "executed", state
            assert episode["results"][0]["evidence_refs"]
            assert requests.count(("POST", "/v1/board/append")) == 1
            assert GOAL_TEXT in model.board_inputs[0]["request_text"]
            assert "Write and verify" in model.board_inputs[0]["request_text"]
            assert sup.goals.store.get("goal", goal["id"])["status"] == "active"
            assert engine.snapshot.model_dump(mode="json") == baseline
            retained = host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",)))
            assert len(retained.records) == 1
            assert (await submit(sup))["id"] == goal["id"]
            calls_before = len(model.chat_calls)
            await sup.capabilities.dispatch(event, 999)
            assert len(model.chat_calls) == calls_before
            assert requests.count(("POST", "/v1/board/append")) == 1
            clock.advance(61)
            review_event = await next_goal_event(sup)
            review = await sup.dispatch(review_event)
            assert review.status == "completed", (review.detail, sup.capabilities.calls.all())
            state = sup.goals.snapshot()
            assert sup.goals.store.get("goal", goal["id"])["status"] == "completed", state
            assert state["episodes"][0]["status"] == "completed"
            assert state["standing_goal"]["id"] == STANDING_ID and state["standing_goal"]["status"] in {"active", "observing"}
            assert len(model.goal_inputs) == 2 and len(model.board_inputs) == 1
            assert requests.count(("POST", "/v1/board/append")) == 1
    asyncio.run(scenario())


def test_observe_then_board_uses_distinct_authorities_and_verified_steps(tmp_path, monkeypatch):
    async def scenario():
        activities = ("control.observe.v1", "expression.board.append.v1")
        async with goal_rig(tmp_path, monkeypatch, activities=activities, desktop=True) as (sup, _, _, model, clock, requests, body):
            goal = await submit(sup)
            observed_event = await next_goal_event(sup)
            first = await sup.dispatch(observed_event)
            assert first.status == "completed", first.detail
            await drain_effects(sup)
            assert body.executed == body.verified == ["control.observe.v1"]
            episode = sup.goals.snapshot()["active_episode"]
            assert [step["status"] for step in episode["steps"]] == ["executed", "planned"], episode
            assert requests.count(("POST", "/v1/board/append")) == 0
            clock.advance(61)
            write_event = await next_goal_event(sup)
            second = await sup.dispatch(write_event)
            assert second.status == "completed", (second.detail, sup.capabilities.calls.all())
            await drain_effects(sup)
            episode = sup.goals.snapshot()["active_episode"]
            assert [step["status"] for step in episode["steps"]] == ["executed", "executed"], episode
            assert [outcome["channel"] for outcome in episode["results"]] == ["desktop", "board"]
            assert all(outcome["evidence_refs"] for outcome in episode["results"])
            clock.advance(61)
            final = await sup.dispatch(await next_goal_event(sup))
            assert final.status == "completed", final.detail
            assert sup.goals.store.get("goal", goal["id"])["status"] == "completed"
            assert len(model.goal_inputs) == 3 and len(model.board_inputs) == 1
            assert body.executed == ["control.observe.v1"]
            assert requests.count(("POST", "/v1/board/append")) == 1
    asyncio.run(scenario())


def test_pause_reopen_resume_keeps_observed_progress_and_does_not_replay(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch, activities=("control.observe.v1", "expression.board.append.v1"), desktop=True) as (
                sup, _, _, model, clock, requests, body):
            # This case isolates pausing one user task. Maintenance has its own
            # independent pause control and can otherwise use the free slot.
            await sup.goals.operate(STANDING_ID, "pause", "pause-maintenance-for-user-recovery")
            goal = await submit(sup)
            event = await next_goal_event(sup)
            assert (await sup.dispatch(event)).status == "completed"
            await drain_effects(sup)
            episode = sup.goals.snapshot()["active_episode"]
            completed_step = episode["steps"][0]
            paused = await sup.goals.operate(goal["id"], "pause", "pause-one")
            assert paused["status"] == "paused"
            clock.advance(61)
            await sup.goals.heartbeat()
            assert sup.queue.empty() and len(model.goal_inputs) == 1
            old = sup.goals
            old.close()
            sup.goals = GoalController(sup, tmp_path / "runtime", clock=clock)
            recovered = sup.goals.store.get("episode", episode["id"])
            assert recovered["status"] == "paused" and recovered["steps"][0] == completed_step
            await sup.goals.operate(goal["id"], "resume", "resume-one")
            resumed = await sup.dispatch(await next_goal_event(sup))
            assert resumed.status == "completed", (resumed.detail, sup.goals.snapshot())
            await drain_effects(sup)
            assert body.executed == body.verified == ["control.observe.v1"]
            assert requests.count(("POST", "/v1/board/append")) == 1
            assert [step["status"] for step in sup.goals.snapshot()["active_episode"]["steps"]] == ["executed", "executed"]
            assert len(model.goal_inputs) == 2
    asyncio.run(scenario())


def test_unknown_body_outcome_survives_goal_reopen_without_new_dispatch(tmp_path, monkeypatch):
    async def scenario():
        async with goal_rig(tmp_path, monkeypatch, activities=("control.observe.v1", "expression.board.append.v1"), desktop=True) as (
                sup, _, _, model, clock, requests, body):
            body.config = body.config.model_copy(update={"scripted_outcomes": {"control.observe.v1": "unknown"}})
            await submit(sup)
            event = await next_goal_event(sup)
            first = await sup.dispatch(event)
            assert first.status == "completed", first.detail
            await drain_effects(sup, expected_unknown=True)
            call = sup.capabilities.calls.get(event.payload["opportunity_id"])
            assert call["status"] == "uncertain", call
            episode = sup.goals.snapshot()["active_episode"]
            assert episode["unresolved_call"] == call["opportunity_id"]
            assert episode["blocked_reason"] == "unknown_effect"
            assert body.executed == ["control.observe.v1"]
            sup.goals.close()
            sup.goals = GoalController(sup, tmp_path / "runtime", clock=clock)
            assert sup.lifecycle == RuntimeLifecycle.PAUSED
            with pytest.raises(RuntimeStateError, match="action reconciliation is unresolved"):
                await sup.resume(reason="acceptance: unresolved execution must remain blocked")
            for _ in range(3):
                clock.advance(61)
                await sup.capabilities.reconcile()
                await sup.goals.heartbeat()
                await sup.capabilities.dispatch(event, 999)
                assert sup.queue.empty()
            assert len(model.goal_inputs) == 1
            assert body.executed == ["control.observe.v1"]
            assert requests.count(("POST", "/v1/board/append")) == 0
            assert sup.goals.store.get("episode", episode["id"])["unresolved_call"] == call["opportunity_id"]
    asyncio.run(scenario())
