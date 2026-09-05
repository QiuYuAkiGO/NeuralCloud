import asyncio
import json
from datetime import timedelta

from harness.agency.models import AgencyConfig, GoalTrigger
from harness.agency.planner import AgencyService
from harness.agency.scheduler import GoalScheduler
from harness.agency.store import GoalStore
from harness.llm.client import MockLLM


def test_body_tick_and_static_polling_never_call_model(tmp_path, goal):
    llm = MockLLM()
    service = AgencyService(tmp_path / "state.db", llm, config=AgencyConfig(enabled=True), clock=lambda: goal.created_at)
    async def run():
        for kind in ("body_tick", "static_observation"):
            for _ in range(20):
                result = await service.reconsider(GoalTrigger(kind=kind, evidence_refs=("same",), signature="same", observed_at=goal.created_at))
                assert not result.model_called
        assert not llm.chat_calls
        await service.close()
    asyncio.run(run())


def test_pending_triggers_survive_restart_and_coalesce(tmp_path, goal):
    path = tmp_path / "state.db"
    config = AgencyConfig(enabled=True, minimum_reconsideration_interval=60)
    store = GoalStore(path)
    scheduler = GoalScheduler(store, config)
    first = GoalTrigger(kind="environment_change", evidence_refs=("world-a",), signature="changed-a", observed_at=goal.created_at)
    assert scheduler.enqueue(first)
    assert scheduler.next_due(goal.created_at) == (first,)
    assert not scheduler.enqueue(first)
    second = first.model_copy(update={"signature": "changed-b", "evidence_refs": ("world-b",)})
    third = first.model_copy(update={"signature": "changed-c", "evidence_refs": ("world-c",)})
    scheduler.enqueue(second)
    scheduler.enqueue(third)
    assert not scheduler.next_due(goal.created_at + timedelta(seconds=1))
    store.close()
    reopened = GoalStore(path)
    ready = GoalScheduler(reopened, config).next_due(goal.created_at + timedelta(seconds=61))
    assert set(item.signature for item in ready) == {"changed-b", "changed-c"}
    reopened.close()


def test_main_model_selects_goal_without_cost_ranking(tmp_path, goal, plan):
    llm = MockLLM(chat_script=[json.dumps({"arbitration": {"selected_goal_id": "project-expensive", "operation": "activate", "confidence": 0.8}})])
    service = AgencyService(tmp_path / "state.db", llm, config=AgencyConfig(enabled=True), clock=lambda: goal.created_at,
        permitted_intents=("control.observe.v1",))
    service.store.create_goal(goal)
    service.store.save_plan(plan)
    expensive = goal.model_copy(update={"goal_id": "project-expensive", "expected_cost": goal.expected_cost.model_copy(update={"predicted_resource_units": 999.0})})
    service.store.create_goal(expensive)
    service.store.save_plan(plan.model_copy(update={"plan_id": "plan-expensive", "goal_id": expensive.goal_id}))
    async def run():
        result = await service.reconsider(GoalTrigger(kind="environment_change", evidence_refs=("world-a",), signature="new", observed_at=goal.created_at),
            provider_intents=("control.observe.v1",))
        assert result.applied, result.reason
        assert service.store.goal(expensive.goal_id).status == "active"
        assert service.store.goal(goal.goal_id).status == "candidate"
        assert llm.chat_calls
        assert service.traces[result.trace_ref]["raw_output_hash"]
        await service.close()
    asyncio.run(run())


def test_model_can_propose_plan_and_anticipate_before_shortfall(tmp_path, goal, plan):
    candidate = goal.model_dump(mode="json", exclude={"status", "created_at", "revision"})
    draft = plan.model_dump(mode="json", exclude={"created_at", "revision"})
    draft["prospective_requirements"] = [{"goal_id": goal.goal_id, "requirement_type": "resource",
        "predicted_shortfall": "M2 may follow the last step", "evidence_refs": ["world-a"]}]
    llm = MockLLM(chat_script=[json.dumps({"candidate": candidate, "plan": draft,
        "arbitration": {"selected_goal_id": goal.goal_id, "operation": "activate", "confidence": 0.8}})])
    service = AgencyService(tmp_path / "state.db", llm, config=AgencyConfig(enabled=True), clock=lambda: goal.created_at,
        permitted_intents=("control.observe.v1",))
    async def run():
        result = await service.reconsider(GoalTrigger(kind="deadline", evidence_refs=("world-a",), signature="future", observed_at=goal.created_at),
            provider_intents=("control.observe.v1",))
        assert result.applied, result.reason
        assert service.store.plan(goal.goal_id).prospective_requirements
        assert service.context()["goals"][0]["plan"]["prospective_requirements"]
        assert service.next_step(goal.goal_id)["step_id"] == "step-a"
        await service.close()
    asyncio.run(run())


def test_failed_arbitration_rolls_back_candidate_and_plan(tmp_path, goal, plan):
    output = {"candidate": goal.model_dump(mode="json", exclude={"status", "created_at", "revision"}),
        "plan": plan.model_dump(mode="json", exclude={"created_at", "revision"}),
        "arbitration": {"selected_goal_id": "nonexistent", "operation": "activate", "confidence": 0.8}}
    llm = MockLLM(chat_script=[json.dumps(output)])
    service = AgencyService(tmp_path / "state.db", llm, config=AgencyConfig(enabled=True), clock=lambda: goal.created_at,
        permitted_intents=("control.observe.v1",))
    async def run():
        result = await service.reconsider(GoalTrigger(kind="deadline", evidence_refs=("world-a",), signature="bad", observed_at=goal.created_at),
            provider_intents=("control.observe.v1",))
        assert not result.applied
        assert not service.store.goals()
        assert not service.store.all("plan")
        assert service.store.verify_integrity()
        await service.close()
    asyncio.run(run())


def test_pause_request_is_preserved_until_runtime_safe_point(tmp_path, goal, plan):
    llm = MockLLM(chat_script=[json.dumps({"arbitration": {"selected_goal_id": goal.goal_id, "operation": "pause", "confidence": 0.8}})])
    service = AgencyService(tmp_path / "state.db", llm, config=AgencyConfig(enabled=True), clock=lambda: goal.created_at,
        permitted_intents=("control.observe.v1",))
    service.store.create_goal(goal)
    service.store.save_plan(plan)
    service.store.transition(goal.goal_id, "active")
    async def run():
        result = await service.reconsider(GoalTrigger(kind="user_interrupt", evidence_refs=("world-a",), signature="interrupt", observed_at=goal.created_at))
        assert result.decision.operation == "pause"
        assert result.reason == "safe_point_required"
        assert service.store.goal(goal.goal_id).status == "active"
        await service.close()
    asyncio.run(run())


def test_review_is_accepted_from_the_main_model(tmp_path, goal, plan):
    output = {"arbitration": {"operation": "no_change", "confidence": 0.8}, "review": {
        "goal_id": goal.goal_id, "outcome": "abandon", "evidence_refs": ["world-b"], "reviewed_at": goal.created_at.isoformat(),
    }}
    llm = MockLLM(chat_script=[json.dumps(output)])
    service = AgencyService(tmp_path / "state.db", llm, config=AgencyConfig(enabled=True), clock=lambda: goal.created_at)
    service.store.create_goal(goal)
    service.store.save_plan(plan)
    async def run():
        result = await service.reconsider(GoalTrigger(kind="environment_change", evidence_refs=("world-b",), signature="gone", observed_at=goal.created_at))
        assert result.model_called
        assert service.store.goal(goal.goal_id).status == "abandoned", result.reason
        assert service.store.all("review")
        await service.close()
    asyncio.run(run())
