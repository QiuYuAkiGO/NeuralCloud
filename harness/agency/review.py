"""Apply evidence-backed model reviews without hardcoding goal value."""

from __future__ import annotations

from harness.agency.models import GoalReview, PersistentGoal
from harness.agency.store import GoalStore


def apply_goal_review(store: GoalStore, review: GoalReview, *, verified_evidence: set[str]) -> PersistentGoal:
    references = set(review.evidence_refs) | {error.evidence_ref for error in review.prediction_errors}
    if review.updated_cost is not None:
        references.update(review.updated_cost.evidence_refs)
    if review.updated_value is not None:
        references.update(review.updated_value.evidence_refs)
    if not references <= verified_evidence:
        raise ValueError("review contains unverified evidence references")
    goal = store.goal(review.goal_id)
    if goal.status in {"completed", "abandoned"}:
        raise ValueError("cannot revise a terminal goal")
    if review.outcome == "complete" and any(
        item["goal_id"] == goal.goal_id and item["state"] == "unknown" for item in store.all("action_outcome")
    ):
        raise ValueError("unknown effects must be reconciled before completing the goal")
    changes = {"revision": goal.revision + 1}
    if review.updated_cost is not None:
        changes["expected_cost"] = review.updated_cost.model_dump(mode="json")
    if review.updated_value is not None:
        changes["expected_value"] = review.updated_value.model_dump(mode="json")
    updated = PersistentGoal.model_validate({**goal.model_dump(mode="json"), **changes})
    store.put("goal", goal.goal_id, updated)
    store.save_review(review)
    status = {"complete": "completed", "abandon": "abandoned", "defer": "blocked"}.get(review.outcome)
    if status is not None:
        updated = store.transition(goal.goal_id, status, evidence_refs=review.evidence_refs)
    return updated
