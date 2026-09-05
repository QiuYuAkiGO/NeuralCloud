from datetime import timedelta

import pytest

from harness.agency.models import AgencyOutcome, GoalCheckpoint, GoalReview
from harness.agency.review import apply_goal_review
from harness.agency.store import GoalStore


def test_legal_state_revision_and_evidence_transitions(tmp_path, goal, plan):
    store = GoalStore(tmp_path / "goals.db")
    store.create_goal(goal)
    store.save_plan(plan)
    assert store.transition(goal.goal_id, "active").revision == 2
    with pytest.raises(ValueError, match="require evidence"):
        store.transition(goal.goal_id, "completed")
    assert store.transition(goal.goal_id, "completed", evidence_refs=("outcome-a",)).revision == 3
    with pytest.raises(ValueError, match="illegal"):
        store.transition(goal.goal_id, "active")
    assert store.verify_integrity()
    store.close()


def test_revision_matching_checkpoint_and_restore_without_transcript(tmp_path, goal, plan):
    path = tmp_path / "goals.db"
    store = GoalStore(path)
    store.create_goal(goal)
    store.save_plan(plan)
    store.transition(goal.goal_id, "active")
    checkpoint = GoalCheckpoint(goal_id=goal.goal_id, plan_revision=1, next_step_id="step-a", continuation={"cursor": 4},
        body_state_ref="body-a", world_state_refs=("world-a",), paused_at=goal.created_at, safe_point="before-next-step")
    with pytest.raises(ValueError, match="revision"):
        store.save_checkpoint(checkpoint.model_copy(update={"plan_revision": 2}))
    store.save_checkpoint(checkpoint)
    store.transition(goal.goal_id, "paused")
    store.close()
    reopened = GoalStore(path)
    assert reopened.goal(goal.goal_id).status == "paused"
    assert reopened.checkpoint(goal.goal_id).continuation == {"cursor": 4}
    assert reopened.verify_integrity()
    reopened.close()


def test_duplicate_outcome_is_idempotent_and_conflict_rejected(tmp_path, goal, plan):
    store = GoalStore(tmp_path / "goals.db")
    store.create_goal(goal)
    store.save_plan(plan)
    outcome = AgencyOutcome(outcome_id="outcome-a", goal_id=goal.goal_id, step_id="step-a", action_id="action-a",
        state="succeeded", evidence_refs=("verified-a",), observed_at=goal.created_at)
    assert store.outcome(outcome)
    assert not store.outcome(outcome)
    with pytest.raises(ValueError):
        store.outcome(outcome.model_copy(update={"state": "failed"}))
    with pytest.raises(ValueError, match="repeated side effects"):
        store.outcome(outcome.model_copy(update={"outcome_id": "different"}))
    store.close()


def test_model_review_can_abandon_with_verified_evidence(tmp_path, goal, plan):
    store = GoalStore(tmp_path / "goals.db")
    store.create_goal(goal)
    store.save_plan(plan)
    review = GoalReview(goal_id=goal.goal_id, outcome="abandon", evidence_refs=("gone",), reviewed_at=goal.created_at + timedelta(seconds=5))
    with pytest.raises(ValueError, match="unverified"):
        apply_goal_review(store, review, verified_evidence=set())
    assert apply_goal_review(store, review, verified_evidence={"gone"}).status == "abandoned"
    store.close()
