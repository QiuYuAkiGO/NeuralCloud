"""Regression cases from independent daily-revision contribution review."""
from copy import deepcopy

import pytest

from harness.config import Settings
from harness.core.models import SemanticFact
from harness.memory.revision.models import CleanedExperience, MemoryRevision, RelationshipRevision, RelationshipDeltas
from harness.memory.revision.reducer import build_revision, rollback_revision
from tests.test_daily_revision_reducer import NOW, preset, source, run, proposal, impression, relation


def semantic(*evidence, **changes):
    values = dict(kind="semantic", subject="local:main", predicate="likes", value="photography",
                  statement="对方喜欢摄影", evidence_ids=list(evidence))
    values.update(changes)
    return MemoryRevision(**values)


def test_rollback_recomputes_surviving_candidate_confidence_and_metadata():
    first = build_revision(preset(), run(), proposal(memories=[semantic("s1")]), Settings(), now=NOW)
    second_source = source("s2", days=0)
    second = build_revision(first, run("r2", [second_source]),
        proposal([second_source], memories=[semantic("s2")]), Settings(), now=NOW)
    reverted = rollback_revision(second, "r1", now=NOW)
    assert reverted.daily_revision_state["receipts"]["r2"]["status"] == "applied"
    candidate = reverted.candidates[0]
    assert candidate.confidence == pytest.approx(Settings().initial_candidate_confidence)
    assert set(candidate.revision_metadata["support"]) == {"message:s2"}
    assert candidate.evidence == ["s2"]


def group_source(identifier, member):
    return source(identifier, author_principal="qq:member:" + member, kind="qq_observation",
        relation_target=f"qq:100:{member}", scope={"owner_id": "misty", "channel": "qq_group",
            "conversation_id": "qq:group:100", "visibility": ["local:main", "qq:group:100"]})


def test_person_impression_cannot_use_another_members_utterance_as_independent_support():
    a, b = group_source("a", "200"), group_source("b", "201")
    with pytest.raises(ValueError):
        build_revision(preset(), run(sources=[a, b]), proposal([a, b], impressions=[
            impression(subject_refs=["qq:member:200"], evidence_ids=["b"])]), Settings(), now=NOW)


def test_member_relationship_cannot_mix_another_members_independent_contribution():
    a, b = group_source("a", "200"), group_source("b", "201")
    with pytest.raises(ValueError):
        build_revision(preset(), run(sources=[a, b]), proposal([a, b], relationships=[
            RelationshipRevision(target="qq:100:200", statement="对方帮助了我", evidence_ids=["a", "b"],
                                 deltas=RelationshipDeltas(trust=.05))]), Settings(), now=NOW)


def test_a_cleaned_merged_experience_contributes_one_impression_episode():
    a, b = source("a"), source("b")
    cleaned = [CleanedExperience(source_ids=["a", "b"], disposition="merge", summary="同一次讨论的两句说明",
                                 evidence_kind="speaker_claim")]
    proposed = proposal([a, b], impressions=[impression(evidence_ids=["a", "b"])]).model_copy(update={"cleaned": cleaned})
    result = build_revision(preset(), run(sources=[a, b]), proposed, Settings(), now=NOW)
    value = next(iter(result.qq_memory_state["impressions"].values()))
    assert len(value["supporting_episode_ids"]) == 1
    assert value["confidence"] == pytest.approx(Settings().qq_impression_initial_confidence)


def test_unrelated_revision_does_not_reactivate_dormant_baseline_impression():
    first = build_revision(preset(), run(), proposal(impressions=[impression()]), Settings(), now=NOW)
    qq = deepcopy(first.qq_memory_state)
    key = next(iter(qq["impressions"]))
    qq["impressions"][key]["status"] = "dormant"
    qq["impressions"][key]["strength"] = 0.0
    baseline = first.model_copy(update={"qq_memory_state": qq, "daily_revision_state": {}})
    new_source = source("unrelated", days=0)
    result = build_revision(baseline, run("later", [new_source]),
        proposal([new_source], relationships=[relation("unrelated")]), Settings(), now=NOW)
    assert result.qq_memory_state["impressions"][key]["status"] == "dormant"


def test_namespaced_local_source_can_revise_legacy_user_identity_without_duplicate():
    original = preset(semantic=[SemanticFact(fact_id="legacy-user-fact", subject="user", predicate="likes",
        value="photography", confidence=.8, supporting_events=["s1"], source_refs=["s1"], formed_at=NOW)])
    result = build_revision(original, run(), proposal(memories=[semantic("s1", target_id="legacy-user-fact")]),
                            Settings(), now=NOW)
    assert len(result.semantic) == 1
    assert result.semantic[0].fact_id == "legacy-user-fact"
    assert result.semantic[0].confidence == .8


def test_historical_hearsay_cannot_become_independent_when_cleaning_annotation_is_omitted():
    initial = proposal(memories=[MemoryRevision(kind="episodic", subject="local:main",
        statement="对方转述了他人的说法", evidence_ids=["s1"])])
    initial = initial.model_copy(update={"cleaned": [CleanedExperience(source_ids=["s1"], disposition="keep",
        summary="转述内容", evidence_kind="hearsay")]})
    first = build_revision(preset(), run(), initial, Settings(), now=NOW)
    later_source = source("s2", days=0)
    with pytest.raises(ValueError):
        build_revision(first, run("r2", [later_source]),
            proposal([later_source], memories=[semantic("s1")]), Settings(), now=NOW)


def test_removing_original_assessment_preserves_latest_same_evidence_relation_revision():
    first = build_revision(preset(), run(), proposal(relationships=[relation(amount=.05)]), Settings(), now=NOW)
    second = build_revision(first, run("r2"), proposal(relationships=[relation(amount=-.03)]), Settings(), now=NOW)
    reverted = rollback_revision(second, "r1", now=NOW)
    assert reverted.daily_revision_state["receipts"]["r2"]["status"] == "applied"
    assert len(reverted.relationships) == 1
    assert reverted.relationships[0].trust == pytest.approx(.47)
