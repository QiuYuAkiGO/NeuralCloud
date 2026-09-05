import pytest

from harness.agency.models import AgencyConfig, AgencyModelOutput, GoalArbitrationDecision, PlannedStep, ProspectiveRequirement


def test_no_active_goal_can_choose_no_change():
    assert GoalArbitrationDecision(operation="no_change", confidence=0.5).selected_goal_id is None


@pytest.mark.parametrize("operation", ["activate", "continue", "pause", "resume", "replan", "abandon"])
def test_other_operations_require_goal(operation):
    with pytest.raises(ValueError):
        GoalArbitrationDecision(operation=operation, confidence=0.7)


@pytest.mark.parametrize("field", ["capability", "approval", "required_approvals", "risk_class", "policy_id"])
def test_model_cannot_assign_execution_authority(field):
    with pytest.raises(ValueError):
        AgencyModelOutput.model_validate({field: "bypass", "arbitration": {"operation": "no_change", "confidence": 1.0}})


def test_planned_actions_use_the_production_safe_profiles():
    with pytest.raises(ValueError):
        PlannedStep(step_id="unsafe", statement="Do work", evidence_refs=("world-a",),
            decision={"kind": "act", "intent": {"intent_id": "arbitrary.run.v1", "arguments": {}},
                "success_criteria": [{"kind": "intentional_noop", "condition": "observed"}], "confidence": 1.0})


def test_prospective_requirements_are_predictions_with_evidence():
    requirement = ProspectiveRequirement(goal_id="project-a", requirement_type="resource", predicted_shortfall="M3 after step B",
        evidence_refs=("belief-a",))
    assert requirement.predicted_shortfall
    assert not hasattr(requirement, "recommended_action")


def test_agency_is_opt_in():
    assert not AgencyConfig().enabled
