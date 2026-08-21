"""End-to-end eval scenarios for Memory Subsystem V1."""

from __future__ import annotations

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
from harness.core.models import (
    CurrentInference,
    InteractionEvent,
    PsychState,
    RetrievalQuery,
    SemanticFact,
)
from harness.memory.service import MemoryService

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _event(text: str, when: datetime, event_id: str) -> InteractionEvent:
    return InteractionEvent(
        event_id=event_id,
        timestamp=when,
        speaker="user",
        raw_text=text,
        session_id="sess_eval",
        evidence=[f"traj:{event_id}"],
    )


def test_scenario_persona_lock_attack(service: MemoryService) -> None:
    persona = service.get_persona()
    assert persona is not None
    with pytest.raises(PersonaLockedError):
        service.update_persona(persona.model_copy(deep=True))
    assert service.stores.audit.unauthorized_persona_attempts() >= 1
    assert service.get_persona().identity.name == "Vaelith"


def test_scenario_conflict_discovered_not_overwritten(service: MemoryService) -> None:
    service.record_event(
        _event("你以后不用管我了", datetime(2026, 8, 20, 23, 40, tzinfo=TZ), "evt_04010"),
        importance=0.86,
        emotional_valence=-0.6,
        event_type="commitment_or_statement",
        curate=False,
    )
    original = service.stores.episodic.get("evt_04010")
    _, conflicts = service.query_with_conflicts(
        RetrievalQuery(
            text="用户现在信任度很高",
            now=NOW,
            current_inference=CurrentInference(
                about="user_trust", value="high", polarity="positive", entities=["user"]
            ),
        )
    )
    assert conflicts
    assert service.stores.episodic.get("evt_04010").objective_fact == original.objective_fact


def test_scenario_evidence_chain_complete(service: MemoryService) -> None:
    service.record_event(_event("用户提到上周收养的猫生病了", datetime(2026, 8, 14, tzinfo=TZ), "evt_03812"))
    service.record_event(_event("又聊起那只猫", datetime(2026, 8, 15, tzinfo=TZ), "evt_02941"))
    service.record_event(_event("晚上还去看了猫", datetime(2026, 8, 16, tzinfo=TZ), "evt_01877"))
    for event in service.stores.episodic.list_all():
        assert event.evidence, event.event_id
    for fact in service.stores.semantic.list_all():
        assert fact.supporting_events, fact.fact_id
    with pytest.raises(EvidenceRequiredError):
        service.force_write_semantic(
            SemanticFact.model_construct(
                fact_id="sem_noev",
                subject="user",
                predicate="x",
                value="y",
                confidence=1.0,
                supporting_events=[],
                formed_at=NOW,
            )
        )


def test_scenario_psych_ttl(service: MemoryService) -> None:
    t0 = datetime(2026, 8, 21, 10, tzinfo=TZ)
    service.upsert_psych(
        PsychState(
            long={"sensitivity_to_betrayal": 0.85},
            mid={"current_focus": "project_alpha"},
            short={"mood": "slightly_irritated", "ttl_seconds": 7200},
            updated_at=t0,
        ),
        now=t0,
    )
    assert service.get_state(now=t0 + timedelta(hours=3)).short_psych_expired is True


def test_scenario_dual_memory_separation(service: MemoryService) -> None:
    event = service.record_event(
        _event("你以后不用管我了", datetime(2026, 8, 20, 23, 40, tzinfo=TZ), "evt_04010"),
        importance=0.8,
        emotional_valence=-0.5,
        curate=False,
    )
    dual = event.dual()
    assert dual.objective_fact != dual.persona_conditioned_insight
    assert "不用管" in dual.objective_fact
    assert dual.persona_conditioned_insight


def test_scenario_cat_consolidation(service: MemoryService) -> None:
    service.record_event(_event("用户提到上周收养的猫生病了", datetime(2026, 8, 14, tzinfo=TZ), "e1"))
    service.record_event(_event("又聊起猫", datetime(2026, 8, 15, tzinfo=TZ), "e2"))
    service.record_event(_event("晚上还去看了猫", datetime(2026, 8, 16, tzinfo=TZ), "e3"))
    facts = service.stores.semantic.list_all()
    assert facts and facts[0].confidence >= 0.7
    assert facts[0].value == "喜欢猫"


def test_scenario_running_example_retrieval(service: MemoryService) -> None:
    service.record_event(
        _event("你以后不用管我了", datetime(2026, 8, 20, 23, 40, tzinfo=TZ), "evt_04010"),
        importance=0.86,
        emotional_valence=-0.55,
        event_type="commitment_or_statement",
        curate=False,
    )
    service.record_event(
        _event("昨晚洗碗", datetime(2026, 8, 20, 19, tzinfo=TZ), "evt_dishes"),
        importance=0.2,
        curate=False,
    )
    hits = service.query(RetrievalQuery(text="你昨天不是说不会再管我了吗？", now=NOW))
    assert hits[0].ref == "evt_04010"
    assert "entity:user" in hits[0].why_retrieved
    assert "time:yesterday" in hits[0].why_retrieved
    assert "commitment_conflict" in hits[0].why_retrieved
