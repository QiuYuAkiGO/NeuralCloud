from harness.body_belief.consolidation import BodyBeliefConsolidator
from harness.body_belief.retrieval import retrieve_beliefs
from tests.body_belief.conftest import candidate, experience


def test_counterevidence_weakens_drops_and_revises_a_station_belief(store):
    engine = BodyBeliefConsolidator(store)
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        store.append_experience(item)
    initial = engine.apply(candidate(episodes))
    store.append_experience(experience(3, after="M2", success=False))
    weakened = engine.refresh(initial)
    assert weakened.confidence < initial.confidence and weakened.status == "weakened"
    assert store.reliabilities()[0].reliability < 1
    for index in (4, 5):
        store.append_experience(experience(index, after="M2", success=False))
    dropped = engine.refresh(weakened)
    assert dropped.confidence == 0 and dropped.status == "dropped"
    assert not retrieve_beliefs(store.beliefs(), context_signature=initial.context_signature)
    reversals = [experience(index, after="M1") for index in range(6, 20)]
    for item in reversals:
        store.append_experience(item)
    revised = engine.apply(candidate(reversals, transition="M2->M1", identifier="candidate-revised"))
    assert revised.belief_id == initial.belief_id
    assert revised.predicted_transition == "M2->M1" and revised.status == "revised"
    assert revised.revision > dropped.revision
    statuses = [entry["value"]["status"] for entry in store.audit() if entry["event"] == "belief"]
    assert statuses == ["consolidated", "weakened", "dropped", "revised"]


def test_mapping_identity_prevents_unconditional_belief_transfer(store):
    episodes = [experience(index) for index in range(3)]
    for item in episodes:
        store.append_experience(item)
    belief = BodyBeliefConsolidator(store).apply(candidate(episodes))
    changed = experience(4, mapping="b")
    assert changed.context_signature != belief.context_signature
    assert not retrieve_beliefs(store.beliefs(), context_signature=changed.context_signature)
