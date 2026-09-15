from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harness.config import Settings
from harness.core.models import EpisodicEvent, MemoryScope, ScoreBreakdown, ScoredMemory
from harness.llm.client import MockLLM
from harness.memory.impressions import (
    ImpressionEvidence,
    ImpressionMemory,
    ImpressionProposal,
    apply_impression,
    impression_key,
)
from harness.memory.store.inmemory import InMemoryStores
from harness.memory.tier import (
    TierClassifier,
    TierVerdict,
    apply_verdict,
    maintain_qq_events,
    qq_event_strength,
    register_hits,
    reinforce_qq_event,
    review_expiries,
)

NOW = datetime(2026, 9, 14, 5, tzinfo=UTC)
SCOPE = MemoryScope(channel="qq_group", conversation_id="group_1", visibility=["local:main", "qq:group_1"])


def _proposal(index: int = 0, **changes) -> ImpressionProposal:
    stance = changes.pop("stance", "support")
    moment = changes.pop("time", NOW + timedelta(days=index))
    statement = changes.pop("statement", "面包在讨论技术时愿意解释细节")
    evidence_changes = changes.pop("evidence_changes", {})
    return ImpressionProposal(
        kind="person", subject_refs=["qq:person_1"], statement=statement,
        evidence=ImpressionEvidence(**{
            "episode_id": f"episode_{index}", "source_ids": [f"source_{index}"],
            "time": moment, "stance": stance, "rationale": "该次讨论中提供了具体步骤", "statement": statement,
            **evidence_changes,
        }), **changes,
    )


def _apply(memory=None, index=0, **changes):
    return apply_impression(memory, _proposal(index, **changes), scope=SCOPE, now=NOW + timedelta(days=30))


def _stable():
    value = None
    for index in range(4):
        value = _apply(value, index).memory
    return value


def _event(tier="mid", **changes):
    return EpisodicEvent(**{
        "event_id": "episode_0", "time": NOW, "participants": ["qq:person_1"],
        "summary": "讨论游戏攻略", "objective_fact": "群友解释了游戏攻略", "importance": 0.8,
        "emotional_valence": 0, "evidence": ["source_0"], "scope": SCOPE,
        "tier": tier, "uniqueness": 0.1, "tier_updated_at": NOW,
        "strength_updated_at": NOW, "strength": 1.0, **changes,
    })


def test_first_impression_is_tentative_and_evidence_bound():
    update = _apply()
    assert update.changed
    assert update.memory.confidence == 0.15
    assert update.memory.status == "provisional"
    assert update.memory.first_impression == update.memory.current_impression
    assert update.memory.first_episode_id == "episode_0"


def test_four_independent_experiences_across_days_consolidate():
    memory = _stable()
    assert memory.status == "stable"
    assert memory.confidence == pytest.approx(0.76656875)
    assert len(memory.history) == memory.revision == 4


def test_four_experiences_on_one_shanghai_day_stay_provisional():
    memory = None
    for index in range(4):
        memory = _apply(memory, index, time=NOW + timedelta(hours=2 * index)).memory
    assert memory.confidence >= 0.7
    assert memory.status == "provisional"


def test_shanghai_day_boundary_is_used_not_utc_day():
    memory = None
    start = datetime(2026, 9, 14, 10, tzinfo=UTC)
    for index in range(4):
        memory = _apply(memory, index, time=start + timedelta(hours=2 * index)).memory
    assert memory.status == "stable"


def test_continuous_round_across_windows_does_not_accumulate_confidence():
    memory = None
    # Sanitized timing of the reviewed live teasing/mention sequence. The
    # overall round exceeds an hour, but every adjacent gap stays below it.
    for index, minute in enumerate((0, 35, 42, 79, 81, 83, 85, 89, 97, 99, 102)):
        memory = _apply(memory, index, time=NOW + timedelta(minutes=minute)).memory
    assert len(memory.evidence) == 1
    assert len(memory.continuation_evidence) == 10
    assert all(not entry.independent for entry in memory.continuation_evidence)
    assert memory.confidence == 0.15
    assert memory.revision == len(memory.history) == 1
    assert memory.current_impression == memory.first_impression
    # A new round after a quiet gap is a genuinely independent experience.
    independent = _apply(memory, 11, time=NOW + timedelta(minutes=163)).memory
    assert len(independent.evidence) == 2
    assert independent.confidence == pytest.approx(0.4475)


def test_first_counter_changes_belief_but_repeating_it_does_not():
    first = _apply().memory
    counter = _apply(first, 1, time=NOW + timedelta(minutes=2), stance="contradict").memory
    assert counter.confidence == pytest.approx(0.15 * 0.65)
    repeated = _apply(counter, 2, time=NOW + timedelta(minutes=4), stance="contradict")
    assert repeated.memory.confidence == counter.confidence
    assert repeated.memory.history == counter.history
    assert len(repeated.memory.continuation_evidence) == 1
    assert repeated.reinforce_episode_ids == []


def test_explicit_old_experience_continuation_is_not_new_support_and_cannot_repeat():
    first = _apply().memory
    continuation = _apply(first, 1, continues_episode_id="episode_0")
    assert continuation.memory.confidence == first.confidence
    assert len(continuation.memory.continuation_evidence) == 1
    duplicate = _apply(continuation.memory, 2, evidence_changes={"source_ids": ["source_1"]})
    assert not duplicate.changed
    same_episode = _apply(continuation.memory, 3, evidence_changes={"episode_id": "episode_1"})
    assert not same_episode.changed
    next_continuation = _apply(continuation.memory, 2, continues_episode_id="episode_1")
    assert next_continuation.memory.confidence == first.confidence
    assert len(next_continuation.memory.continuation_evidence) == 2


@pytest.mark.parametrize("anchor,stance", [("missing", "support"), ("episode_0", "contradict")])
def test_continuation_requires_existing_same_stance_evidence(anchor, stance):
    with pytest.raises(ValueError, match="same stance"):
        _apply(_apply().memory, 1, continues_episode_id=anchor, stance=stance)


def test_continuation_gap_is_a_configurable_engineering_policy():
    first = _apply().memory
    proposal = _proposal(1, time=NOW + timedelta(minutes=20))
    result = apply_impression(first, proposal, scope=SCOPE, now=NOW + timedelta(days=1),
                              settings=Settings(qq_impression_continuation_gap_minutes=10))
    assert result.memory.confidence == pytest.approx(0.4475)


@pytest.mark.parametrize("flag", ["independent", "quoted", "self_generated"])
def test_dependent_quote_and_self_synthesis_cannot_create_impressions(flag):
    value = False if flag == "independent" else True
    update = _apply(evidence_changes={flag: value})
    assert not update.changed
    assert update.memory is None


def test_uncertain_model_output_is_no_op():
    assert _apply(uncertain=True).memory is None


def test_same_episode_cannot_strengthen_a_belief_again():
    first = _apply().memory
    again = _apply(first, 1, evidence_changes={"episode_id": "episode_0"})
    assert not again.changed
    assert again.memory == first


def test_recaptured_or_quoted_source_cannot_count_as_new_episode():
    first = _apply().memory
    again = _apply(first, 1, evidence_changes={"source_ids": ["source_0", "source_1"]})
    assert not again.changed
    assert again.memory.confidence == 0.15


def test_replay_is_idempotent_even_for_an_opposite_proposal():
    first = _apply().memory
    again = _apply(first, stance="contradict", statement="面包这次没有解释")
    assert not again.changed
    assert again.memory.revision == 1


def test_contrast_preserves_first_and_lowers_old_belief_confidence():
    old = _stable()
    update = _apply(old, 4, stance="contradict", statement="他也会拒绝解释，需要结合当时状态判断")
    memory = update.memory
    assert memory.first_impression == old.first_impression
    assert memory.current_impression != old.current_impression
    assert memory.status == "disputed"
    assert memory.confidence == pytest.approx(old.confidence * 0.65)
    assert set(update.reinforce_episode_ids) == {"episode_0", "episode_3", "episode_4"}
    assert memory.history[:-1] == old.history
    assert memory.contradicting_episode_ids == ["episode_4"]


def test_new_consistent_evidence_can_resolve_dispute_without_erasing_it():
    memory = _apply(_stable(), 4, stance="contradict", statement="他偶尔也会拒绝解释").memory
    for index in range(5, 9):
        memory = _apply(memory, index, statement="他通常愿意解释，但也会明确拒绝").memory
    assert memory.status == "stable"
    assert memory.contradicting_episode_ids == ["episode_4"]


def test_no_prior_impression_means_no_contradiction():
    assert _apply(stance="contradict").memory is None


def test_new_source_is_required_after_dormancy():
    old = _apply().memory.model_copy(update={"status": "dormant", "strength": 0.0})
    assert _apply(old).memory.status == "dormant"
    assert _apply(old, 1).memory.status == "provisional"


def test_impression_round_trips_complete_history():
    memory = _stable()
    assert ImpressionMemory.model_validate_json(memory.model_dump_json()) == memory


@pytest.mark.parametrize("kind,subjects", [("person", ["a"]), ("relationship", ["a", "b"]), ("topic", []), ("group", [])])
def test_all_requested_impression_kinds_have_scoped_identities(kind, subjects):
    one = impression_key(SCOPE, kind, subjects, "游戏", "攻略讨论")
    another = impression_key(SCOPE.model_copy(update={"conversation_id": "group_2"}), kind, subjects, "游戏", "攻略讨论")
    assert one != another
    assert one == impression_key(SCOPE, kind, list(reversed(subjects)), "游戏", "攻略讨论")


def test_different_people_with_same_preference_do_not_merge():
    assert impression_key(SCOPE, "person", ["a"]) != impression_key(SCOPE, "person", ["b"])


def test_other_scope_cannot_update_an_existing_impression():
    with pytest.raises(ValueError, match="scope or subject"):
        apply_impression(_apply().memory, _proposal(1), scope=SCOPE.model_copy(update={"conversation_id": "elsewhere"}), now=NOW + timedelta(days=2))


def test_out_of_order_backfill_is_rejected_instead_of_rewriting_first():
    with pytest.raises(ValueError, match="chronological"):
        _apply(_apply(index=1).memory, 0)


def test_future_evidence_is_rejected():
    with pytest.raises(ValueError, match="future"):
        apply_impression(None, _proposal(1), scope=SCOPE, now=NOW)


def test_empty_or_mismatched_evidence_is_rejected():
    with pytest.raises(ValidationError):
        _proposal(evidence_changes={"source_ids": []})
    with pytest.raises(ValidationError):
        _proposal(evidence_changes={"statement": "另一个没有依据的观点"})


@pytest.mark.parametrize("tier,days", [("mid", 14), ("long", 90)])
def test_qq_strength_has_configured_half_life(tier, days):
    event = _event(tier)
    assert qq_event_strength(event, Settings(), now=NOW + timedelta(days=days)) == pytest.approx(0.5)
    assert event.strength == 1.0


def test_mid_and_long_are_reviewed_at_most_daily():
    event = _event("mid")
    settings = Settings()
    assert maintain_qq_events([event], settings, now=NOW + timedelta(hours=23))[0] is event
    reviewed = maintain_qq_events([event], settings, now=NOW + timedelta(days=1))[0]
    assert reviewed.strength < 1.0
    assert maintain_qq_events([reviewed], settings, now=NOW + timedelta(days=1, hours=23))[0] is reviewed


@pytest.mark.parametrize("tier,days,next_tier", [("mid", 34, "short"), ("long", 210, "mid")])
def test_decay_demotes_one_layer_and_resets_strength(tier, days, next_tier):
    updated = maintain_qq_events([_event(tier)], Settings(), now=NOW + timedelta(days=days))[0]
    assert updated.tier == next_tier
    assert updated.strength == 0.5
    assert updated.decay_demoted
    assert updated.tier_reasons[-1] == f"qq_decay:{tier}_to_{next_tier}"


def test_demoted_salient_short_is_forgotten_instead_of_bouncing_back():
    settings = Settings()
    short = maintain_qq_events([_event("mid")], settings, now=NOW + timedelta(days=34))[0]
    assert short.importance >= 0.75
    forgotten = maintain_qq_events([short], settings, now=short.expire_at + timedelta(seconds=1))[0]
    assert forgotten.tier == "forgotten"
    assert forgotten.evidence == ["source_0"]


def test_short_reuses_existing_ttl_and_initial_salience_policy():
    expiry = NOW + timedelta(minutes=30)
    event = _event("short", expire_at=expiry)
    assert maintain_qq_events([event], Settings(), now=expiry - timedelta(seconds=1))[0] is event
    assert maintain_qq_events([event], Settings(), now=expiry)[0].tier == "mid"


@pytest.mark.parametrize("scope", [None, MemoryScope(channel="chat"), MemoryScope(channel="qq_private")])
def test_legacy_and_private_memory_are_not_changed_by_qq_decay(scope):
    event = _event("mid", scope=scope)
    assert maintain_qq_events([event], Settings(), now=NOW + timedelta(days=1000))[0] is event
    assert reinforce_qq_event(event, now=NOW + timedelta(days=1)) is event


def test_actual_adoption_strengthens_without_modifying_old_confidence():
    event = _event("mid")
    moment = NOW + timedelta(days=14)
    updated = reinforce_qq_event(event, now=moment)
    assert updated.strength == pytest.approx(0.65)
    assert updated.hit_count == 1
    assert event.hit_count == 0


def test_contrast_strengthens_new_episode_without_faking_adoption():
    updated = reinforce_qq_event(_event("short"), now=NOW, adopted=False)
    assert updated.strength == 1.15
    assert updated.hit_count == 0
    assert updated.last_hit_at is None


def test_adoption_receipts_can_promote_but_not_undo_decay():
    moment = NOW + timedelta(hours=1)
    regular = _event("short", hit_count=2)
    assert reinforce_qq_event(regular, now=moment).tier == "mid"
    demoted = regular.model_copy(update={"decay_demoted": True})
    assert reinforce_qq_event(demoted, now=moment).tier == "short"
    assert apply_verdict(demoted, TierVerdict(tier="long"), now=moment).tier == "short"


def test_archived_memories_do_not_revive_from_retrieval_alone():
    event = _event("forgotten")
    assert reinforce_qq_event(event, now=NOW) is event


def test_legacy_async_maintenance_cannot_mutate_qq_projection():
    stores = InMemoryStores()
    event = _event("short", expire_at=NOW - timedelta(seconds=1), hit_count=2)
    stores.episodic.insert(event)
    hit = ScoredMemory(memory_type="episodic", ref=event.event_id, relevance=1, score_breakdown=ScoreBreakdown())
    assert register_hits(stores, [hit], Settings(), now=NOW) == []
    assert review_expiries(stores, Settings(), TierClassifier(MockLLM()), None, now=NOW) == []
    assert stores.episodic.get(event.event_id) is event


@pytest.mark.parametrize("field", ["qq_memory_poll_seconds", "qq_memory_decay_mid_days", "qq_memory_review_hours"])
def test_qq_settings_reject_nonpositive_durations(field):
    with pytest.raises(ValidationError):
        Settings(**{field: 0})


def test_settings_default_to_disabled():
    settings = Settings(_env_file=None)
    assert settings.qq_memory_enabled is False
    assert settings.qq_memory_recall_enabled is False


def test_impression_confidence_and_stability_policy_is_configurable():
    settings = Settings(qq_impression_initial_confidence=0.3, qq_impression_support_increment=0.5,
                        qq_impression_counter_multiplier=0.2, qq_impression_stable_confidence=0.6,
                        qq_impression_min_support_episodes=2, qq_impression_min_distinct_days=1)
    first = apply_impression(None, _proposal(), scope=SCOPE, now=NOW, settings=settings).memory
    assert first.confidence == 0.3
    second = apply_impression(first, _proposal(1, time=NOW + timedelta(hours=2)),
                              scope=SCOPE, now=NOW + timedelta(hours=2), settings=settings).memory
    assert second.confidence == pytest.approx(0.65)
    assert second.status == "stable"
    counter = apply_impression(second, _proposal(2, stance="contradict"),
                               scope=SCOPE, now=NOW + timedelta(days=2), settings=settings).memory
    assert counter.confidence == pytest.approx(0.13)
    assert counter.status == "disputed"


def test_configured_distinct_day_gate_cannot_be_bypassed_by_high_confidence():
    settings = Settings(qq_impression_initial_confidence=0.9, qq_impression_min_support_episodes=2,
                        qq_impression_min_distinct_days=3)
    first = apply_impression(None, _proposal(), scope=SCOPE, now=NOW, settings=settings).memory
    assert first.status == "provisional"
    second = apply_impression(first, _proposal(1), scope=SCOPE, now=NOW + timedelta(days=1), settings=settings).memory
    assert second.status == "provisional"
    third = apply_impression(second, _proposal(2), scope=SCOPE, now=NOW + timedelta(days=2), settings=settings).memory
    assert third.status == "stable"


@pytest.mark.parametrize("updates", [
    {"qq_impression_initial_confidence": -0.1}, {"qq_impression_initial_confidence": 1.1},
    {"qq_impression_support_increment": 0}, {"qq_impression_support_increment": 1.1},
    {"qq_impression_counter_multiplier": -0.1}, {"qq_impression_counter_multiplier": 1},
    {"qq_impression_stable_confidence": 0}, {"qq_impression_stable_confidence": 1.1},
    {"qq_impression_min_support_episodes": 1}, {"qq_impression_min_distinct_days": 0},
])
def test_impression_policy_settings_have_bounds(updates):
    with pytest.raises(ValidationError):
        Settings(**updates)
