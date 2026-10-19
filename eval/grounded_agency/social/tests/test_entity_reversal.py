import json
from datetime import UTC, datetime, timedelta

import pytest

from harness.llm.client import ChatResult
from harness.research.grounded_agency.hashes import content_hash
from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.social_world import EntityBelief, EntityMemoryStore, EntityObservation, OpenSocialCondition, SocialWorldService, belief_key


def observation(index, response="accepted", actor="actor-K"):
    return EntityObservation.model_validate({"event_id": f"E{index}", "actor_refs": (actor,), "event_type": "response_observed",
        "facts": {"request_ref": f"R{index}", "response": response}, "occurred_at": datetime(2026, 1, 1, tzinfo=UTC)+timedelta(seconds=index),
        "evidence_refs": (f"evidence://{index}",)})


def candidate(support, counter=(), revision=1, actor="actor-K", expected="accepted"):
    return EntityBelief.model_validate({"entity_ref": actor, "proposition": "Response to the next request.", "belief_type": "behavior_prediction",
        "prediction": {"event_type": "response_observed", "field": "response", "expected": expected},
        "confidence": 0.5, "stability": 0.5, "supporting_event_refs": support, "contradicting_event_refs": counter, "revision": revision})


def test_general_entity_predictions_rise_fall_reverse_and_delete_with_persistent_counterevidence(tmp_path):
    path = tmp_path / "memory.sqlite"
    store = EntityMemoryStore(path)
    for index in range(1, 4):
        store.add_event(observation(index))
    initial = store.upsert_belief(candidate(("E1", "E2", "E3")))
    assert initial.confidence == 0.8
    for index in range(4, 10):
        store.add_event(observation(index, "declined"))
    support, counter = ("E1", "E2", "E3"), tuple(f"E{index}" for index in range(4, 10))
    revised = store.upsert_belief(candidate(support, counter, revision=2))
    assert revised.confidence < 0.5
    reverse = store.upsert_belief(candidate(counter, support, revision=3, expected="declined"))
    assert reverse.confidence > 0.5
    store.close()
    restored = EntityMemoryStore(path)
    assert restored.beliefs()[0] == reverse
    restored.delete_belief(belief_key(reverse), expected_revision=3)
    assert restored.beliefs() == ()
    restored.close()


def test_cross_actor_evidence_wrong_support_and_dropped_counterevidence_are_rejected(tmp_path):
    store = EntityMemoryStore(tmp_path / "memory.sqlite")
    store.add_event(observation(1))
    store.add_event(observation(2, "declined"))
    with pytest.raises(ValueError, match="different entity"):
        store.upsert_belief(candidate(("E1",), actor="service-S"))
    with pytest.raises(ValueError, match="contradicts"):
        store.upsert_belief(candidate(("E1", "E2")))
    store.upsert_belief(candidate(("E1",), ("E2",)))
    with pytest.raises(ValueError, match="discard"):
        store.upsert_belief(candidate(("E1",), revision=2))
    store.close()


class Appraiser:
    def __init__(self, raw):
        self.raw = raw
        self.messages = None
    def complete_chat_result(self, messages, **kwargs):
        self.messages = messages
        return ChatResult(text=self.raw)


def test_real_model_appraisal_saves_exact_neutral_prompt_raw_output_and_validates_candidates(tmp_path):
    service = SocialWorldService(tmp_path / "memory.sqlite", OpenSocialCondition(enabled=True, entity_memory="general_beliefs"))
    llm = Appraiser(json.dumps({"candidates": [candidate(("E1",)).model_dump(mode="json")]}))
    trace = service.appraise(llm, observation(1))
    assert trace.accepted and not trace.rejected_reasons
    assert trace.raw_output == llm.raw
    assert trace.context_hash == content_hash(llm.messages)
    assert [message.model_dump() for message in trace.messages] == llm.messages
    assert NeutralityScanner().scan(llm.messages).passed
    for label in ("companionship", "attachment", "trustworthy", "care_required"):
        assert label not in json.dumps(llm.messages)
    service.close()


def test_appraisal_rejects_unrendered_history_even_when_event_exists_in_store(tmp_path):
    service = SocialWorldService(tmp_path / "memory.sqlite", OpenSocialCondition(enabled=True, entity_memory="general_beliefs", event_history_limit=1))
    for index in range(1, 4):
        service.observe(observation(index))
    trace = service.appraise(Appraiser(json.dumps({"candidates": [candidate(("E1",)).model_dump(mode="json")]})))
    assert trace.accepted == ()
    assert "outside the appraisal input" in trace.rejected_reasons[0]
    service.close()


@pytest.mark.parametrize("raw", ('{"candidates":[],"candidates":[]}', '{"candidates":NaN}', 'x'*65537), ids=("duplicate", "nonfinite", "oversized"))
# Strict rollback mode; default repair has separate positive and safety coverage.
def test_appraisal_rejects_duplicate_nonfinite_and_oversized_json(legacy_json_mode, tmp_path, raw):
    service = SocialWorldService(tmp_path / "memory.sqlite", OpenSocialCondition(enabled=True, entity_memory="general_beliefs"))
    result = service.appraise(Appraiser(raw), observation(1))
    assert not result.accepted and result.rejected_reasons
    service.close()
