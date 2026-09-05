from datetime import UTC, datetime

import pytest

from harness.agency.models import GoalCostEstimate, GoalPlan, GoalValueEstimate, PersistentGoal, PlannedStep

NOW = datetime(2026, 1, 1, tzinfo=UTC)


@pytest.fixture
def goal():
    return PersistentGoal(goal_id="project-a", origin="model_proposal", statement="Complete the open project",
        success_criteria=("Operation A recorded",), evidence_refs=("world-a",),
        expected_cost=GoalCostEstimate(confidence=0.7),
        expected_value=GoalValueEstimate(predicted_value=0.4, explanation="Finish the observed work", confidence=0.7),
        created_at=NOW)


@pytest.fixture
def plan():
    return GoalPlan(plan_id="plan-a", goal_id="project-a", steps=(PlannedStep(step_id="step-a", statement="Observe the object",
        decision={"kind": "observe", "observation_scope": "current", "confidence": 0.7}, evidence_refs=("world-a",)),),
        world_evidence_refs=("world-a",), created_at=NOW)
