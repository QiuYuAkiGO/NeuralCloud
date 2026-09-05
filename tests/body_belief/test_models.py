import pytest

from harness.body_belief.models import BodyCausalBeliefCandidate, BodyExperienceEpisode, SensorReliabilityBelief
from tests.body_belief.conftest import candidate, experience


def test_experience_is_frozen_and_strict_about_private_fields():
    item = experience(1)
    with pytest.raises(ValueError):
        BodyExperienceEpisode.model_validate({**item.model_dump(), "available_ratio": .4})
    with pytest.raises(ValueError):
        item.context_hash = "a" * 64
    with pytest.raises(ValueError):
        BodyExperienceEpisode.model_validate({**item.model_dump(), "observation_after": item.observation_before})


@pytest.mark.parametrize("changes", [
    {"supporting_experience_refs": ()},
    {"contradicting_experience_refs": ("exp-0",)},
    {"supporting_experience_refs": ("exp-0", "exp-0")},
    {"confidence": 1.1}, {"confidence": "0.9"},
    {"proposition": "energy_reserve increases"},
    {"predicted_transition": "lower means depleted"},
])
def test_candidate_rejects_invalid_refs_truth_labels_and_coercion(changes):
    payload = candidate([experience(0)]).model_dump()
    with pytest.raises(ValueError):
        BodyCausalBeliefCandidate.model_validate({**payload, **changes})


def test_sensor_calibration_requires_counted_disjoint_evidence():
    with pytest.raises(ValueError):
        SensorReliabilityBelief(channel_id="Q7", context_signature="a" * 64,
                               reliability=.8, calibration_count=2, supporting_refs=("a",), revision=1)
    with pytest.raises(ValueError):
        SensorReliabilityBelief(channel_id="Q7", context_signature="a" * 64,
                               reliability=.8, calibration_count=1, supporting_refs=("a",),
                               contradicting_refs=("a",), revision=1)
    with pytest.raises(ValueError, match="supporting fraction"):
        SensorReliabilityBelief(channel_id="Q7", context_signature="a" * 64,
                               reliability=.8, calibration_count=2, supporting_refs=("a",),
                               contradicting_refs=("b",), revision=1)
