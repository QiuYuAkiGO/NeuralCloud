import pytest

from harness.body_belief.consolidation import BodyBeliefConsolidator
from harness.body_belief.models import BodyExperienceEpisode
from tests.body_belief.conftest import candidate, experience


def test_three_independent_experiences_over_time_consolidate_and_duplicate_is_idempotent(store):
    engine = BodyBeliefConsolidator(store)
    episodes = []
    for index in range(3):
        item = experience(index)
        assert store.append_experience(item)
        assert not store.append_experience(item)
        episodes.append(item)
        belief = engine.apply(candidate(episodes))
        if index < 2:
            assert belief is None
    assert belief.confidence == 1 and belief.revision == 1
    assert belief.supporting_experience_refs == ("exp-0", "exp-1", "exp-2")
    assert engine.apply(candidate(episodes)) == belief


def test_single_window_and_unverified_or_missing_after_never_consolidate(store):
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        same_time = item.model_copy(update={"completed_at": episodes[0].completed_at})
        store.append_experience(same_time)
    assert BodyBeliefConsolidator(store).apply(candidate(episodes)) is None
    for update in ({"observation_after": None}, {"action_outcome_refs": ()}, {"outcome_verified": False}):
        invalid = BodyExperienceEpisode.model_validate({**experience(10).model_dump(), **update})
        assert not invalid.eligible


def test_context_action_and_declared_evidence_must_match(store):
    episodes = [experience(0), experience(1, action="station_b"), experience(2, context="room_b")]
    for item in episodes:
        store.append_experience(item)
    for second in episodes[1:]:
        with pytest.raises(ValueError, match="mismatch"):
            BodyBeliefConsolidator(store).apply(candidate([episodes[0], second]))
    missing = candidate([experience(11)])
    with pytest.raises(ValueError, match="verified outcome"):
        BodyBeliefConsolidator(store).apply(missing)


def test_same_decision_cannot_be_rebound_to_conflicting_experience(store):
    first = experience(0)
    store.append_experience(first)
    with pytest.raises(ValueError, match="conflicting"):
        store.append_experience(first.model_copy(update={"experience_id": "new-identity"}))
