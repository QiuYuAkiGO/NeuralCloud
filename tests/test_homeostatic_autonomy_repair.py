"""Long-lived evidence, request faults, bounded episodes and plan effect windows."""
import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from harness.contracts.goals import GoalDecisionRequest
from harness.regulation.context import select_evidence, web_information_feedback
from harness.regulation.store import STANDING_ID
from harness.runtime.goals import GoalController, _outcome_projection, _review_outcomes
from tests.test_capability_cognition import command
from tests.test_goal_effect_review import review, snapshot, evaluation  # noqa: F401
from tests.test_goal_runtime import goal_rig, next_goal_event, submit
from tests.test_goal_runtime_acceptance import prepared_result
from tests.test_goal_runtime_review import controller


def test_evidence_overflow_preserves_appraisals_bindings_and_old_step_proof():
    episode = {"episode_id": "episode:old", "steps": [{"status": "executed", "evidence_refs": ["receipt:old"]}],
        "results": [{"status": "completed", "evidence_refs": [f"result:{i}" for i in range(250)]}]}
    known, refs = select_evidence(snapshot={"revision": 4}, goal={"id": STANDING_ID}, episode=episode,
        offers=[SimpleNamespace(observation_refs=[f"offer:{i}" for i in range(200)])],
        fresh_refs=[f"message:{i}" for i in range(64)], used_refs={"result:249"})
    assert len(known) == 128 and len(refs) == 64 and set(refs) <= set(known)
    assert "receipt:old" in known and "result:249" not in refs
    assert known[0].startswith("homeostasis-snapshot:") and known[1].startswith("goal-request:")
    payload = command().payload | {"snapshot_revision": 4, "homeostatic_summary": {"snapshot_revision": 4},
        "goal_context": {"episode_id": "episode:old", "active_episode": episode,
            "known_evidence_refs": known, "appraisal_evidence_refs": refs}}
    GoalDecisionRequest.model_validate(payload)
    assert len(episode["results"][0]["evidence_refs"]) == 250


def test_old_step_receipt_is_preserved_alongside_recent_outcomes():
    episode = {"steps": [{"status": "executed", "evidence_refs": ["receipt:old"]}],
        "results": [{"status": "completed", "evidence_refs": ["receipt:old"], "receipt": {"private": "omit"}}]
            + [{"status": "wait", "call_id": str(i)} for i in range(90)]}
    outcomes = _review_outcomes(episode)
    assert len(outcomes) == 17 and outcomes[0] == {"status": "completed", "evidence_refs": ["receipt:old"]}
    assert outcomes[-1]["call_id"] == "89"


def test_request_fault_survives_messages_and_restart_until_contract_or_configuration_changes(tmp_path):
    async def run():
        goals, sup, _ = controller(tmp_path)
        goal = goals.store.get("goal", STANDING_ID)
        goals._record_request_fault({"error_code": "goal_invalid_appraisal_evidence"}, goal)
        assert goals.health()["status"] == "degraded"
        await goals.on_event("event.accepted", {"event_type": "user_message", "event_id": "new-message"})
        assert goals.request_fault(goal)
        goals.close()
        recovered = GoalController(sup, tmp_path)
        try:
            assert recovered.request_fault(goal)
            recovered.config = recovered.config.model_copy(update={"review_seconds": 600})
            assert recovered.request_fault(goal) is None
            assert recovered.health()["status"] == "available"
        finally:
            recovered.close()
    asyncio.run(run())


def test_valid_waits_do_not_exhaust_no_progress_budget(tmp_path):
    goals, sup, _ = controller(tmp_path)
    try:
        episode = {"id": "episode:wait", "episode_id": "episode:wait", "goal_id": STANDING_ID,
            "created_at": datetime.now(timezone.utc).isoformat(), "status": "active", "seen_calls": [],
            "steps": [], "results": [], "no_progress": 2, "revision": 1}
        goals.store.put("episode", episode["id"], episode)
        for i in range(5):
            sup.capabilities.calls.put({"opportunity_id": f"wait:{i}", "status": "wait",
                "goal_binding": {"goal_id": STANDING_ID, "episode_id": episode["id"]}})
            goals.reconcile()
        saved = goals.store.get("episode", episode["id"])
        assert saved["no_progress"] == 0 and saved["blocked_reason"] == "model_wait"
        assert len(saved["results"]) == 5
    finally:
        goals.close()


@pytest.mark.parametrize("protected", [None, "paused", "unknown", "inflight", "user"])
def test_stale_autonomous_episode_ends_without_erasing_history_or_unresolved_effects(tmp_path, protected):
    goals, sup, _ = controller(tmp_path)
    now = datetime.now(timezone.utc)
    sup.capabilities.settings = SimpleNamespace(opportunity_seconds=1800)
    e = {"id": "episode:old", "goal_id": STANDING_ID, "status": "waiting",
         "created_at": (now - timedelta(days=20)).isoformat(), "results": [{"evidence_refs": ["receipt:old"]}]}
    if protected == "paused":
        e["status"] = "paused"
    elif protected == "unknown":
        e["unresolved_call"] = "call:unknown"
    elif protected == "inflight":
        sup.capabilities.calls.put({"opportunity_id": "call:pending", "status": "selecting",
                                   "goal_binding": {"episode_id": e["id"]}})
    elif protected == "user":
        e["goal_id"] = "user-task"
    try:
        goals.store.put("episode", e["id"], e)
        assert goals._expire_autonomous_episode(e, now) is (protected is None)
        stored = goals.store.get("episode", e["id"])
        assert stored["results"] == [{"evidence_refs": ["receipt:old"]}]
        if protected is None:
            assert stored["status"] == "abandoned" and stored["end_reason"] == "episode_time_limit"
            assert datetime.fromisoformat(goals.store.get("goal", STANDING_ID)["next_review_at"]) == now + timedelta(seconds=1800)
    finally:
        goals.close()


def test_replan_updates_target_baseline_and_archives_previous_window(tmp_path, monkeypatch):
    async def run():
        async with goal_rig(tmp_path, monkeypatch) as (sup, _, _, _, clock, _, _):
            goal = await submit(sup)
            event = await next_goal_event(sup)
            row = sup.capabilities.calls.get(event.payload["opportunity_id"])
            result = prepared_result(row, clock)
            # Start a valid waiting plan to avoid channel side effects in this test.
            result.result["decision"]["choice"] = {"kind": "wait"}
            result.result["prepared_capability_choice"]["choice"] = {"kind": "wait"}
            await sup.goals.accept(row, result, event)
            original = sup.goals._active(goal["id"])
            clock.advance(61)
            result = deepcopy(result)
            raw = result.result["decision"]
            raw.update(decision_id="goal:replan", episode_operation="replan", target_dimensions=["novelty"])
            raw["prepared_at"] = clock().isoformat()
            raw["deadline_at"] = (clock() + timedelta(seconds=30)).isoformat()
            from harness.contracts.goals import PreparedGoalDecision
            result.result["prepared_capability_choice"] = PreparedGoalDecision.model_validate(raw).capability_choice.model_dump(mode="json")
            row["deadline_at"] = raw["deadline_at"]
            row["goal_request"]["goal_context"]["snapshot"]["dimensions"]["novelty"]["value"] = 0.18
            await sup.goals.accept(row, result, event)
            current = sup.goals._active(goal["id"])
            assert current["target_dimensions"] == ["novelty"]
            assert current["baseline"]["dimensions"]["novelty"]["value"] == 0.18
            assert current["evaluation_started_at"] == clock().isoformat()
            window = sup.goals.store.all("evaluation_window")[0]
            assert window["baseline"] == original["baseline"]
    asyncio.run(run())


def test_autonomous_abandon_observes_cooldown_before_another_episode(tmp_path, monkeypatch):
    async def run():
        async with goal_rig(tmp_path, monkeypatch) as (sup, _, _, _, clock, _, _):
            user = await submit(sup)
            event = await next_goal_event(sup)
            row = sup.capabilities.calls.get(event.payload["opportunity_id"])
            await sup.goals.operate(user["id"], "pause", "pause-user")
            standing = sup.goals.store.get("goal", STANDING_ID)
            row["goal_binding"].update(goal_id=STANDING_ID, goal_revision=standing["revision"])
            row["activity_revision"] = sup.capabilities.activity_revision
            result = prepared_result(row, clock)
            result.result["decision"]["choice"] = {"kind": "wait"}
            result.result["prepared_capability_choice"]["choice"] = {"kind": "wait"}
            await sup.goals.accept(row, result, event)
            from harness.contracts.goals import PreparedGoalDecision
            raw = result.result["decision"]
            raw.update(decision_id="goal:abandon", episode_operation="abandon")
            result.result["prepared_capability_choice"] = PreparedGoalDecision.model_validate(raw).capability_choice.model_dump(mode="json")
            await sup.goals.accept(row, result, event)
            assert sup.goals._active(STANDING_ID) is None
            standing = sup.goals.store.get("goal", STANDING_ID)
            assert standing["status"] == "observing"
            assert datetime.fromisoformat(standing["next_review_at"]) == clock() + timedelta(seconds=sup.capabilities.settings.opportunity_seconds)
    asyncio.run(run())


def test_review_distinguishes_baseline_deficit_from_no_further_decline(review):  # noqa: F811
    goals, _, clock, episode = review
    goals.observe(snapshot(clock, 0.1, 1))
    assert evaluation(goals, episode)["interval_outcome"] == "worsened"
    clock.at(60)
    goals.observe(snapshot(clock, 0.1, 2))
    result = evaluation(goals, episode)
    assert result["outcome"] == "worsened" and result["interval_outcome"] == "unchanged"
    assert result["comparison_basis"] == "plan_baseline"


def test_web_feedback_distinguishes_repetition_and_empty_results_without_reward():
    a = {"url": "https://example.org/a", "snippet": "a", "origin": "search", "source_ref": "web:a"}
    b = a | {"source_ref": "web:b", "snippet": "changed"}
    feedback = web_information_feedback(SimpleNamespace(sources=[a, b]), [a])
    assert feedback["new_or_changed_sources"] == feedback["repeated_sources"] == 1
    assert feedback["novelty_assessment"] == "unassessed"
    empty = web_information_feedback(SimpleNamespace(sources=[]), [a])
    assert empty["empty_result"] and empty["new_or_changed_sources"] == 0
    row = {"status": "completed", "channel_result": {"information_feedback": feedback, "private": "omit"}}
    assert _outcome_projection(row) == {"status": "completed", "information_feedback": feedback}
