from datetime import UTC, datetime, timedelta

import pytest

from harness.body_belief.models import BodyCausalBeliefCandidate, BodyExperienceEpisode, context_signature
from harness.body_belief.store import BodyBeliefStore
from harness.cognitive.decision_parser import ExperimentalDecisionParser
from harness.research.grounded_agency.hashes import content_hash
from harness.research.interoception.contracts import OpaqueInteroceptiveObservation

NOW = datetime(2026, 9, 5, tzinfo=UTC)


def observation(sequence, reading="M2", *, seconds=0, mapping="a"):
    return OpaqueInteroceptiveObservation(
        channel_id="Q7", reading=reading, change="D2", confidence_band="C3", sample_sequence=sequence,
        observed_at=NOW + timedelta(seconds=seconds), transform_ref="T" + mapping * 64,
    )


def experience(index, *, after="M4", success=True, action="station_a", context="room_a", mapping="a", verified=True):
    before = observation(index * 2 + 1, seconds=index * 60, mapping=mapping)
    decision = ExperimentalDecisionParser().parse({
        "kind": "act", "confidence": .8,
        "intent": {"intent_id": "station.use.v1", "arguments": {"station_id": action}},
        "success_criteria": [{"kind": "scenario", "condition": "station_used", "target_id": action}],
    })
    return BodyExperienceEpisode(
        experience_id=f"exp-{index}", episode_id=f"episode-{index // 2}", decision_id=f"decision-{index}",
        context_hash=content_hash(("actual input", index)), context_signature=context_signature(context, before),
        observation_before=before, decision=decision, action_outcome_refs=(f"verified://outcome/{index}",),
        observation_after=observation(index * 2 + 2, reading=after, seconds=index * 60 + 1, mapping=mapping),
        outcome_success=success, outcome_verified=verified, completed_at=NOW + timedelta(seconds=index * 60 + 1),
    )


def candidate(episodes, *, transition="M2->M4", identifier="candidate-a"):
    matching = [item for item in episodes if item.transition == transition and item.outcome_success]
    opposite = [item for item in episodes if item not in matching]
    first = episodes[0]
    return BodyCausalBeliefCandidate(
        candidate_id=identifier, proposition=f"Observed Q7 transition {transition} after this action.",
        context_signature=first.context_signature, action_signature=first.action_signature,
        predicted_channel="Q7", predicted_transition=transition, confidence=.9,
        supporting_experience_refs=tuple(item.experience_id for item in matching),
        contradicting_experience_refs=tuple(item.experience_id for item in opposite),
        created_at=first.completed_at, updated_at=episodes[-1].completed_at,
    )


@pytest.fixture
def store(tmp_path):
    value = BodyBeliefStore(tmp_path / "belief.sqlite3")
    yield value
    value.close()
