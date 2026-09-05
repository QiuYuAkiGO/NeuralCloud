from datetime import timedelta

import pytest

from harness.agency.arbitration import validate_resume
from harness.agency.models import AgencyOutcome, GoalCheckpoint, ResumeObservation
from harness.agency.store import GoalStore


def setup_pause(tmp_path, goal, plan, *, unknown=False):
    store = GoalStore(tmp_path / "goals.db")
    store.create_goal(goal)
    store.save_plan(plan)
    store.transition(goal.goal_id, "active")
    store.save_checkpoint(GoalCheckpoint(goal_id=goal.goal_id, plan_revision=1, next_step_id="step-a", continuation={"cursor": 1},
        body_state_ref="body-a", world_state_refs=("world-a",), paused_at=goal.created_at, safe_point="safe-a",
        action_id="action-a" if unknown else None, action_status="unknown" if unknown else "none"))
    store.transition(goal.goal_id, "paused")
    observation = ResumeObservation(observed_at=goal.created_at + timedelta(seconds=1), world_evidence_refs=("world-b",),
        body_state_ref="body-b", provider_intents=("control.observe.v1",), assumptions_hold=True,
        continuation_usable=True, body_allows_next_step=True, belief_revision_matches=True)
    return store, observation


def test_fresh_observation_is_mandatory(tmp_path, goal, plan):
    store, observation = setup_pause(tmp_path, goal, plan)
    with pytest.raises(ValueError, match="fresh observation"):
        validate_resume(store, goal.goal_id, None)
    with pytest.raises(ValueError, match="fresh observation"):
        validate_resume(store, goal.goal_id, observation.model_copy(update={"observed_at": goal.created_at}))
    validate_resume(store, goal.goal_id, observation)
    store.close()


@pytest.mark.parametrize("changed", ["assumptions_hold", "continuation_usable", "body_allows_next_step", "belief_revision_matches"])
def test_changed_evidence_blocks_blind_resume(tmp_path, goal, plan, changed):
    store, observation = setup_pause(tmp_path, goal, plan)
    with pytest.raises(ValueError, match="facts changed"):
        validate_resume(store, goal.goal_id, observation.model_copy(update={changed: False}))
    store.close()


def test_capability_loss_requires_replan(tmp_path, goal, plan):
    store, observation = setup_pause(tmp_path, goal, plan)
    with pytest.raises(ValueError):
        validate_resume(store, goal.goal_id, observation.model_copy(update={"provider_intents": ()}))
    store.close()


def test_unknown_action_is_never_automatically_replayed(tmp_path, goal, plan):
    store, observation = setup_pause(tmp_path, goal, plan, unknown=True)
    unknown = AgencyOutcome(outcome_id="pending-a", goal_id=goal.goal_id, step_id="step-a", action_id="action-a", state="unknown",
        evidence_refs=("pending-ref",), observed_at=goal.created_at)
    store.outcome(unknown)
    with pytest.raises(ValueError, match="unknown action"):
        validate_resume(store, goal.goal_id, observation.model_copy(update={"action_reconciled": True}))
    store.outcome(unknown.model_copy(update={"outcome_id": "reconciled-a", "state": "succeeded", "evidence_refs": ("verified-a",)}))
    validate_resume(store, goal.goal_id, observation.model_copy(update={"action_reconciled": True, "reconciliation_evidence_ref": "verified-a"}))
    store.close()
