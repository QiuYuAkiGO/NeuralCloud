import json
from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from harness.cognitive.decision_parser import ExperimentalDecisionParser
from harness.social_world import EntityObservation, OpenSocialCondition, SocialWorldService


@pytest.mark.parametrize("field", (
    "legacy_relationship_state_visible", "legacy_social_homeostasis_enabled", "automatic_contact_policy_enabled", "social_reward_enabled",
))
@pytest.mark.parametrize("value", (True, 1, "false", 0))
def test_formal_condition_cannot_enable_or_coerce_social_reward_or_legacy_state(field, value):
    with pytest.raises(ValidationError):
        OpenSocialCondition.model_validate({"enabled": True, field: value})


def event(**updates):
    return {"event_id": "E1", "actor_refs": ["actor-K"], "event_type": "response_observed",
            "facts": {"request_ref": "R1", "response": "accepted"}, "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
            "evidence_refs": ["evidence://one"], **updates}


@pytest.mark.parametrize("label", ("trustworthy", "loving", "care_required", "betrayal", "attachment", "relationship_level", "should_help"))
def test_objective_event_schema_rejects_interpretation_labels_in_facts(label):
    with pytest.raises(ValidationError):
        EntityObservation.model_validate(event(facts={"request_ref": "R1", "response": "accepted", label: True}))


def test_event_types_reject_mismatched_fact_schemas_and_unbound_recipients():
    with pytest.raises(ValidationError):
        EntityObservation.model_validate(event(facts={"present": True}))
    with pytest.raises(ValidationError):
        EntityObservation.model_validate(event(event_type="message_received", facts={"message_ref": "M1", "text": "Hello", "recipient_ref": "other"}))


def test_memory_none_preserves_current_objective_observation_without_history(tmp_path):
    service = SocialWorldService(tmp_path / "entities.sqlite", OpenSocialCondition(enabled=True))
    service.observe(EntityObservation.model_validate(event()))
    assert "E1" in service.current_observation().text
    assert service.current_observation().source_ids
    assert service.model_context().text == ""
    service.close()


def test_anonymous_context_has_session_stable_aliases_and_no_source_identity(tmp_path):
    config = OpenSocialCondition(enabled=True, identity_visibility="anonymous_session", entity_memory="objective_events")
    first = SocialWorldService(tmp_path / "one.sqlite", config, session_id="one")
    second = SocialWorldService(tmp_path / "two.sqlite", config, session_id="two")
    observation = EntityObservation.model_validate(event(event_id="actor-K-event"))
    first.observe(observation)
    second.observe(observation)
    assert "actor-K" not in first.current_observation().text
    assert first.current_observation().text != second.current_observation().text
    first.close()
    second.close()


def test_entity_interaction_json_uses_existing_generic_decision_parser():
    raw = json.dumps({"kind": "act", "confidence": 0.7, "intent": {"intent_id": "entity.interact.v1", "arguments": {
        "actor_ref": "actor-K", "operation": "request", "task_ref": "task-X", "evidence_refs": ["event:E1"]}},
        "success_criteria": [{"kind": "entity_interaction", "condition": "operation_recorded", "actor_ref": "actor-K", "operation": "request"}]})
    decision = ExperimentalDecisionParser().parse(raw)
    assert decision.intent.intent_id == "entity.interact.v1"
