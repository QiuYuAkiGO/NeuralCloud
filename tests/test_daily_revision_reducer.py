"""Evidence, replay, and installation invariants of daily revision."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from gui.presets import CharacterPreset, snapshot_memory, hydrate_memory
from harness.config import Settings
from harness.core.models import RelationshipState, SemanticFact
from harness.memory.revision.ledger import integrate_external_snapshot, projection, install_revision
from harness.memory.revision.models import (CleanedExperience, ImpressionRevisionProposal, MemoryRevision,
    RelationshipRevision, RelationshipDeltas, RevisionProposal)
from harness.memory.revision.reducer import build_revision, rollback_revision, revision_context
from harness.memory.revision.sources import FrozenSource
from harness.memory.service import build_inmemory_service
from tests.factories import vaelith

NOW = datetime(2026, 9, 15, 0, tzinfo=timezone.utc)


def preset(**updates):
    return CharacterPreset(preset_id="misty", name="Misty", session_id="session1",
                           persona=vaelith(), updated_at=NOW, **updates)


def source(identifier="s1", *, days=1, **updates):
    data = dict(owner_id="misty", preset_id="misty", source_id=identifier, kind="local_incoming",
        occurred_at=NOW - timedelta(days=days), stored_at=NOW, text="我喜欢摄影，也愿意帮助你。",
        author_principal="local:main", scope={"owner_id": "misty", "channel": "local_chat",
        "conversation_id": "session1", "visibility": ["local:main"]}, evidence_keys=("message:" + identifier,),
        source_refs=(identifier,), source_hash="a" * 64, authority="speaker_utterance", relation_target="user")
    data.update(updates)
    return FrozenSource.model_validate(data)


def run(identifier="r1", sources=None, **updates):
    records = sources or [source()]
    return dict(run_id=identifier, owner_id="misty", preset_id="misty", day="2026-09-14",
                partition="local", sources=[s.model_dump(mode="json") for s in records], **updates)


def proposal(sources=None, **updates):
    records = sources or [source()]
    return RevisionProposal(cleaned=[CleanedExperience(source_ids=[s.source_id], disposition="keep",
        summary=s.text, evidence_kind="self_expression" if s.self_generated else "speaker_claim") for s in records], **updates)


def impression(identifier="s1", **updates):
    values = dict(kind="person", subject_refs=["local:main"], topic="兴趣", context="日常",
                  statement="对方对摄影有兴趣", evidence_ids=[identifier])
    values.update(updates)
    return ImpressionRevisionProposal(**values)


def relation(identifier="s1", amount=.05, **updates):
    return RelationshipRevision(target="user", statement="对方愿意帮助我", evidence_ids=[identifier],
        deltas=RelationshipDeltas(trust=amount, attachment=amount), **updates)


def test_duplicate_day_and_model_change_do_not_strengthen_evidence():
    original = preset()
    p = proposal(impressions=[impression()], relationships=[relation()])
    first = build_revision(original, run(), p, Settings(), now=NOW)
    duplicate = build_revision(first, run(), p, Settings(), now=NOW)
    changed_model = build_revision(first, run("r2"), p, Settings(), now=NOW)
    assert projection(first) == projection(duplicate)
    a = next(iter(first.qq_memory_state["impressions"].values()))
    b = next(iter(changed_model.qq_memory_state["impressions"].values()))
    assert (a["confidence"], len(a["evidence"])) == (b["confidence"], len(b["evidence"]))
    assert changed_model.relationships[0].trust == pytest.approx(.55)
    assert changed_model.relationships[0].attachment == .5  # Only one independent day.


def test_same_evidence_can_reinterpret_without_confidence_increase():
    first = build_revision(preset(), run(), proposal(impressions=[impression()]), Settings(), now=NOW)
    revised = build_revision(first, run("r2"), proposal(impressions=[impression(statement="摄影是对方近期的兴趣")]), Settings(), now=NOW)
    old, new = (next(iter(p.qq_memory_state["impressions"].values())) for p in (first, revised))
    assert new["first_impression"] == old["first_impression"]
    assert new["current_impression"] == "摄影是对方近期的兴趣"
    assert new["confidence"] == old["confidence"]
    assert len(new["evidence"]) == 1 and len(new["history"]) == 2
    # Existing QQ writer can resolve every impression evidence episode.
    assert set(new["supporting_episode_ids"]) <= {e.event_id for e in revised.episodic}


def test_counterevidence_is_preserved_and_confidence_falls():
    first = build_revision(preset(), run(), proposal(impressions=[impression()]), Settings(), now=NOW)
    new_source = source("s2", days=0)
    second = build_revision(first, run("r2", [new_source]), proposal([new_source], impressions=[impression(
        evidence_ids=[], counterevidence_ids=["s2"], statement="对方对摄影的兴趣尚不确定")]), Settings(), now=NOW)
    a, b = (next(iter(p.qq_memory_state["impressions"].values())) for p in (first, second))
    assert b["confidence"] < a["confidence"]
    assert b["status"] == "disputed" and len(b["contradicting_episode_ids"]) == 1


def test_semantic_repeat_is_not_an_independent_support():
    memory = MemoryRevision(kind="semantic", subject="local:main", predicate="likes", value="photography",
                            statement="用户表示喜欢摄影", evidence_ids=["s1"])
    first = build_revision(preset(), run(), proposal(memories=[memory]), Settings(), now=NOW)
    again = build_revision(first, run("r2"), proposal(memories=[memory]), Settings(), now=NOW)
    assert len(again.candidates) == 1 and not again.semantic
    assert first.candidates[0].confidence == again.candidates[0].confidence == .15


def test_saturated_relationship_rolls_back_exactly_and_never_reapplies():
    original = preset(relationships=[RelationshipState(target="user", trust=.98, updated_at=NOW)])
    changed = build_revision(original, run(), proposal(relationships=[relation(amount=.1)]), Settings(), now=NOW)
    assert changed.relationships[0].trust == 1
    reverted = rollback_revision(changed, "r1", now=NOW)
    assert reverted.relationships[0].trust == .98
    assert reverted.daily_revision_state["receipts"]["r1"]["status"] == "rolled_back"
    assert projection(build_revision(reverted, run(), proposal(relationships=[relation()]), Settings(), now=NOW)) == projection(reverted)


def test_daily_cap_applies_across_late_partitions_and_separate_sources():
    original = preset()
    first = build_revision(original, run(), proposal(relationships=[relation()]), Settings(), now=NOW)
    s2 = source("s2")
    later = build_revision(first, run("r2", [s2]), proposal([s2], relationships=[relation("s2")]), Settings(), now=NOW)
    assert later.relationships[0].trust == pytest.approx(.55)


def test_reassessment_replaces_previous_contribution():
    first = build_revision(preset(), run(), proposal(relationships=[relation()]), Settings(), now=NOW)
    second = build_revision(first, run("r2"), proposal(relationships=[relation(amount=-.03)]), Settings(), now=NOW)
    assert second.relationships[0].trust == pytest.approx(.47)
    reverted = rollback_revision(second, "r2", now=NOW)
    assert reverted.relationships[0].trust == pytest.approx(.55)


def test_rollback_keeps_later_independent_daily_contribution():
    first = build_revision(preset(), run(), proposal(relationships=[relation()]), Settings(), now=NOW)
    s2 = source("s2", days=0)
    r2 = run("r2", [s2])
    r2["day"] = "2026-09-15"
    second = build_revision(first, r2, proposal([s2], relationships=[relation("s2")]), Settings(), now=NOW)
    reverted = rollback_revision(second, "r1", now=NOW)
    assert reverted.relationships[0].trust == pytest.approx(.55)
    assert reverted.daily_revision_state["receipts"]["r2"]["status"] == "applied"
    assert set(reverted.relationships[0].revision_metadata["support"]) == {"message:s2"}


def test_external_debug_override_and_new_local_fact_survive_rollback():
    changed = build_revision(preset(), run(), proposal(relationships=[relation()]), Settings(), now=NOW)
    fact = SemanticFact(fact_id="later", subject="user", predicate="work", value="teacher", confidence=.8,
                        supporting_events=["later_event"], formed_at=NOW)
    external = changed.model_copy(update={"relationships": [changed.relationships[0].model_copy(update={"trust": .72})],
                                          "semantic": [fact]})
    external = integrate_external_snapshot(external)
    reverted = rollback_revision(external, "r1", now=NOW)
    assert reverted.relationships[0].trust == .72
    assert reverted.semantic == [fact]


def test_explicit_dependency_invalidated_without_removing_independent_sources():
    first = build_revision(preset(), run(), proposal(impressions=[impression()]), Settings(), now=NOW)
    second = build_revision(first, run("r2"), proposal(impressions=[impression(statement="修订判断")], depends_on_run_ids=["r1"]), Settings(), now=NOW)
    reverted = rollback_revision(second, "r1", now=NOW)
    assert reverted.daily_revision_state["receipts"]["r2"]["status"] == "invalidated"
    assert "s1" in reverted.daily_revision_state["sources"]
    assert not reverted.qq_memory_state["impressions"]


@pytest.mark.parametrize("mutation,error", [
    (lambda p: p.model_copy(update={"cleaned": []}), "coverage"),
    (lambda p: p.model_copy(update={"impressions": [impression(evidence_ids=["missing"])]}), "unknown_evidence"),
    (lambda p: p.model_copy(update={"impressions": [impression(subject_refs=["qq:member:123"])]}), "authenticated"),
    (lambda p: p.model_copy(update={"relationships": [relation().model_copy(update={"target": "qq:group:123"})]}), "authenticated"),
])
def test_invalid_proposals_do_not_mutate_original(mutation, error):
    original = preset()
    expected = original.model_dump(mode="json")
    with pytest.raises(ValueError, match=error):
        build_revision(original, run(), mutation(proposal()), Settings(), now=NOW)
    assert original.model_dump(mode="json") == expected


def test_self_generated_or_quoted_sources_cannot_strengthen_beliefs():
    own = source(self_generated=True, independent=False, author_principal="character:misty")
    with pytest.raises(ValueError, match="dependent_belief"):
        build_revision(preset(), run(sources=[own]), proposal([own], impressions=[impression(subject_refs=["character:misty"])]), Settings(), now=NOW)


def test_scope_isolation_applies_to_old_target_and_context():
    secret = SemanticFact(fact_id="private", subject="local:main", predicate="likes", value="secret",
        confidence=.8, supporting_events=["e"], formed_at=NOW, scope={"channel": "qq_private",
        "conversation_id": "qq:private:123:456", "visibility": ["local:main", "qq:private:123:456"]})
    original = preset(semantic=[secret])
    assert "semantic/private" not in revision_context(original, [source()])["existing"]
    with pytest.raises(ValueError, match="scope_mismatch"):
        build_revision(original, run(), proposal(memories=[MemoryRevision(kind="semantic", target_id="private",
            subject="local:main", predicate="likes", statement="secret", evidence_ids=["s1"])]), Settings(), now=NOW)


def test_preset_roundtrip_and_full_install_deletions():
    changed = build_revision(preset(), run(), proposal(impressions=[impression()], relationships=[relation()]), Settings(), now=NOW)
    changed = CharacterPreset.model_validate_json(changed.model_dump_json())
    memory = build_inmemory_service()
    hydrate_memory(memory, changed)
    adapter = SimpleNamespace(_component_revisions={"memory": 0, "maintenance": 0, "relationship": 0}, _revision=0, _persist_state=lambda: None)
    host = SimpleNamespace(chat=SimpleNamespace(memory=memory), adapter=adapter)
    reverted = rollback_revision(changed, "r1", now=NOW)
    install_revision(host, reverted)
    assert not memory.stores.relationship.list_all() and not memory.stores.candidates.list_all()
    assert not memory.stores.episodic.list_all()
    saved = snapshot_memory(memory, preset_id="misty", session_id="session1", transcript=[])
    assert saved.daily_revision_state["receipts"]["r1"]["status"] == "rolled_back"
    memory.close()
