from __future__ import annotations

import pytest

from gui.debug import (
    DEBUG_ACTOR,
    CandidatePatch,
    EpisodicPatch,
    PersonaPatch,
    PsychPatch,
    RelationshipPatch,
    SemanticPatch,
    TuningPatch,
    apply_candidate,
    apply_episodic,
    apply_persona,
    apply_psych,
    apply_relationship,
    apply_semantic,
    apply_tuning,
    collect_debug_state,
)
from harness.core.exceptions import EvidenceRequiredError, MemoryNotFoundError, PersonaLockedError
from harness.core.models import (
    CandidateMemory,
    EpisodicEvent,
    InteractionEvent,
    RetrievalQuery,
    SemanticFact,
)
from tests.factories import NOW, seed_running_example


def _event(event_id: str, text: str) -> InteractionEvent:
    return InteractionEvent(
        event_id=event_id,
        timestamp=NOW,
        speaker="user",
        raw_text=text,
        session_id="sess_debug",
        evidence=[f"chat:sess_debug:{event_id}"],
    )


def test_relationship_absolute_set_and_clamp(service) -> None:
    updated = apply_relationship(
        service,
        RelationshipPatch(target="user", closeness=0.91, trust=0.0, attachment=1.0, recent_tension=0.33),
    )
    assert updated.closeness == pytest.approx(0.91)
    assert updated.trust == pytest.approx(0.0)
    assert updated.attachment == pytest.approx(1.0)
    assert updated.recent_tension == pytest.approx(0.33)
    logs = service.stores.audit.list_for("relationship", updated.relation_id)
    assert any(row["actor"] == DEBUG_ACTOR for row in logs)


def test_psych_short_restamps_set_at(service) -> None:
    seed_running_example(service)
    before = service.stores.psych.get()
    assert before is not None
    previous_set_at = before.short.get("set_at")
    updated = apply_psych(
        service,
        PsychPatch(short={"mood": "calm", "ttl_seconds": 3600, "set_at": previous_set_at, "expired": True}),
    )
    assert updated.short["mood"] == "calm"
    assert updated.short["ttl_seconds"] == 3600
    assert "expired" not in updated.short
    assert updated.short["set_at"] != previous_set_at
    assert any(row["actor"] == DEBUG_ACTOR for row in service.stores.audit.list_for("psych"))


def test_persona_debug_bypass_keeps_lock_and_audits(service) -> None:
    updated = apply_persona(
        service,
        PersonaPatch(name="Vaelith-debug", core_traits=["傲慢", "debug"]),
    )
    assert updated is not None
    assert updated.identity.name == "Vaelith-debug"
    assert updated.identity.core_traits == ["傲慢", "debug"]
    assert updated.identity.locked is True
    with pytest.raises(PersonaLockedError):
        service.update_persona(updated)
    logs = service.stores.audit.list_for("persona_core", updated.persona_id)
    assert any(
        row["actor"] == DEBUG_ACTOR and row["success"] is True and row["detail"].get("bypass") == "persona_lock"
        for row in logs
    )


def test_episodic_update_recomputes_embedding_and_rejects_empty_evidence(service) -> None:
    seed_running_example(service)
    event = service.stores.episodic.get("evt_04010")
    assert event is not None
    old_embedding = list(event.embedding or [])
    updated = apply_episodic(
        service,
        EpisodicPatch(
            event_id="evt_04010",
            importance=0.42,
            emotional_valence=0.2,
            summary="完全不同的摘要内容",
            objective_fact="客观事实也被改写了",
            insight="新的角色解读",
        ),
    )
    assert updated.importance == pytest.approx(0.42)
    assert updated.emotional_valence == pytest.approx(0.2)
    assert updated.summary == "完全不同的摘要内容"
    assert updated.persona_conditioned_insight == "新的角色解读"
    assert updated.embedding != old_embedding
    assert service.stores.episodic.get("evt_04010").embedding == updated.embedding

    with pytest.raises(MemoryNotFoundError):
        apply_episodic(service, EpisodicPatch(event_id="evt_missing", importance=0.1))

    constructed = EpisodicEvent.model_construct(**{**event.model_dump(), "evidence": []})
    with pytest.raises(EvidenceRequiredError):
        service.stores.episodic.update(constructed)


def test_episodic_regenerate_insight_create_and_curate(service) -> None:
    seed_running_example(service)
    regenerated = apply_episodic(
        service,
        EpisodicPatch(event_id="evt_04010", regenerate_insight=True),
    )
    assert regenerated.persona_conditioned_insight
    assert "气话" in regenerated.persona_conditioned_insight or "寻求关注" in regenerated.persona_conditioned_insight
    assert regenerated.insight_confidence == pytest.approx(0.64)
    logs = service.stores.audit.list_for("episodic", "evt_04010")
    assert any(row["actor"] == DEBUG_ACTOR and row["detail"].get("patch", {}).get("regenerate_insight") for row in logs)

    cat = apply_episodic(
        service,
        EpisodicPatch(
            event_id="evt_04010",
            summary="用户提到上周收养的猫生病了",
            objective_fact="用户提到上周收养的猫生病了",
            regenerate_insight=True,
            curate=True,
        ),
    )
    assert "宠物" in (cat.persona_conditioned_insight or "") or "猫" in (cat.persona_conditioned_insight or "")
    assert any("猫" in (row.claim or "") for row in service.stores.candidates.list_all())

    created = apply_episodic(
        service,
        EpisodicPatch(
            summary="别突然打扰我",
            character_action="停下当前工作",
            outcome="用户继续说话",
            event_type="interruption",
            importance=0.7,
            emotional_valence=-0.2,
            regenerate_insight=False,
            curate=True,
        ),
    )
    assert created.event_id.startswith("evt_")
    assert created.summary == "别突然打扰我"
    assert created.objective_fact == "别突然打扰我"
    assert created.character_action == "停下当前工作"
    assert created.outcome == "用户继续说话"
    assert created.event_type == "interruption"
    assert created.importance == pytest.approx(0.7)
    assert created.evidence[0].startswith("debug:")
    assert created.embedding
    assert any("打扰" in (row.claim or "") for row in service.stores.candidates.list_all())
    assert any(
        row["actor"] == DEBUG_ACTOR and row["op"] == "ADD" and row["target_id"] == created.event_id
        for row in service.stores.audit.list_for("episodic", created.event_id)
    )

    named = apply_episodic(
        service,
        EpisodicPatch(summary="阿宁打了招呼", speaker="阿宁", regenerate_insight=False, curate=False),
    )
    assert named.participants[0] == "阿宁"
    assert "user" in named.participants
    assert "character" in named.participants

    with pytest.raises(ValueError, match="summary"):
        apply_episodic(service, EpisodicPatch(regenerate_insight=True))


def test_semantic_and_candidate_updates(service) -> None:
    fact = service.force_write_semantic(
        SemanticFact(
            fact_id="sem_debug",
            subject="user",
            predicate="preference",
            value="喜欢猫",
            confidence=0.4,
            supporting_events=["evt_03812"],
            formed_at=NOW,
        )
    )
    old_embedding = list(fact.embedding or [])
    semantic = apply_semantic(service, SemanticPatch(fact_id="sem_debug", confidence=0.88, value="非常喜欢猫"))
    assert semantic.confidence == pytest.approx(0.88)
    assert semantic.value == "非常喜欢猫"
    assert semantic.embedding != old_embedding

    service.stores.candidates.insert(
        CandidateMemory(
            candidate_id="cand_debug",
            claim="用户不喜欢被突然打扰",
            source="debug",
            confidence=0.2,
            evidence=["evt_03955"],
            created_at=NOW,
            updated_at=NOW,
        )
    )
    candidate = apply_candidate(
        service,
        CandidatePatch(candidate_id="cand_debug", confidence=0.77, status="dropped"),
    )
    assert candidate.confidence == pytest.approx(0.77)
    assert candidate.status == "dropped"
    with pytest.raises(MemoryNotFoundError):
        apply_semantic(service, SemanticPatch(fact_id="sem_missing", confidence=0.1))


def test_tuning_weight_validation_and_retrieval_order(service) -> None:
    service.record_event(_event("evt_hi_imp", "完全不相关的天气闲聊"), importance=0.99, curate=False, now=NOW)
    service.record_event(
        _event("evt_lo_imp", "用户提到上周收养的猫生病了"),
        importance=0.01,
        curate=False,
        now=NOW,
    )
    with pytest.raises(ValueError, match="sum"):
        apply_tuning(
            service.settings,
            TuningPatch(
                retrieval_weights={
                    "semantic": 1.0,
                    "entity": 1.0,
                    "recency": 1.0,
                    "importance": 1.0,
                    "relationship": 1.0,
                    "persona": 1.0,
                }
            ),
        )
    payload = apply_tuning(
        service.settings,
        TuningPatch(
            retrieval_weights={
                "semantic": 0.0,
                "entity": 0.0,
                "recency": 0.0,
                "importance": 1.0,
                "relationship": 0.0,
                "persona": 0.0,
            },
            retrieval_top_k=2,
        ),
        memory=service,
    )
    assert payload["retrieval_weights"]["importance"] == pytest.approx(1.0)
    assert payload["weight_sum"] == pytest.approx(1.0)
    assert service.hybrid.settings.retrieval_weights["importance"] == pytest.approx(1.0)
    hits = service.query(RetrievalQuery(text="那只猫后来怎样了？", now=NOW, top_k=2))
    assert hits[0].ref == "evt_hi_imp"

    normalized = apply_tuning(
        service.settings,
        TuningPatch(
            retrieval_weights={
                "semantic": 1.0,
                "entity": 1.0,
                "recency": 1.0,
                "importance": 1.0,
                "relationship": 1.0,
                "persona": 1.0,
            },
            normalize=True,
        ),
        memory=service,
    )
    assert normalized["weight_sum"] == pytest.approx(1.0)
    assert all(value == pytest.approx(1.0 / 6.0) for value in normalized["retrieval_weights"].values())


def test_collect_debug_state_omits_embeddings(service) -> None:
    seed_running_example(service)
    payload = collect_debug_state(service)
    assert payload["persona"]["identity"]["name"] == "Vaelith"
    assert payload["episodic"]
    assert "embedding" not in payload["episodic"][0]
    assert "tuning" in payload
    assert set(payload["tuning"]["retrieval_weights"]) == {
        "semantic",
        "entity",
        "recency",
        "importance",
        "relationship",
        "persona",
    }
