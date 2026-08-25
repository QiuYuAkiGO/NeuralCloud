from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from pydantic import ValidationError

from harness.core.ids import new_id
from harness.core.models import (
    DualMemoryRecord,
    EpisodicEvent,
    InteractionEvent,
    PersonaCore,
    PersonaIdentity,
    RetrievalPlan,
    SemanticFact,
)

TZ = ZoneInfo("Asia/Shanghai")


def test_id_prefixes() -> None:
    assert new_id("event").startswith("evt_")
    assert new_id("fact").startswith("sem_")
    assert new_id("relation").startswith("rel_")
    assert new_id("candidate").startswith("cand_")
    assert new_id("situation").startswith("sit_")
    assert new_id("insight").startswith("ins_")
    assert new_id("context").startswith("ctx_")
    assert new_id("response").startswith("resp_")


def test_episodic_roundtrip() -> None:
    event = EpisodicEvent(
        event_id="evt_03812",
        time=datetime(2026, 8, 14, 21, 32, tzinfo=TZ),
        participants=["user", "character"],
        summary="用户提到上周收养的猫生病了",
        character_action="放下手里的事，主动询问细节",
        outcome="用户情绪缓和",
        importance=0.72,
        emotional_valence=-0.3,
        evidence=["traj_T928100_step_44", "traj_T928100_step_45"],
        objective_fact="用户提到上周收养的猫生病了",
    )
    restored = EpisodicEvent.model_validate_json(event.model_dump_json())
    assert restored.event_id == "evt_03812"
    assert restored.evidence == ["traj_T928100_step_44", "traj_T928100_step_45"]
    assert restored.dual().event_id == "evt_03812"
    assert restored.tier == "short"
    assert restored.hit_count == 0
    assert restored.uniqueness is None
    assert restored.expire_at is None


def test_episodic_rejects_empty_evidence() -> None:
    with pytest.raises(ValidationError):
        EpisodicEvent(
            time=datetime(2026, 8, 14, tzinfo=TZ),
            participants=["user"],
            summary="x",
            importance=0.1,
            emotional_valence=0.0,
            evidence=[],
            objective_fact="x",
        )


def test_semantic_roundtrip() -> None:
    fact = SemanticFact(
        fact_id="sem_0019",
        subject="user",
        predicate="preference",
        value="喜欢猫",
        confidence=0.86,
        supporting_events=["evt_03812", "evt_02941", "evt_01877"],
        formed_at=datetime(2026, 8, 15, 8, tzinfo=TZ),
    )
    restored = SemanticFact.model_validate_json(fact.model_dump_json())
    assert restored.fact_id == "sem_0019"
    assert restored.supporting_events[0] == "evt_03812"


def test_semantic_rejects_empty_support() -> None:
    with pytest.raises(ValidationError):
        SemanticFact(
            subject="user",
            predicate="preference",
            value="喜欢猫",
            confidence=0.9,
            supporting_events=[],
            formed_at=datetime(2026, 8, 15, tzinfo=TZ),
        )


def test_persona_and_interaction_roundtrip() -> None:
    persona = PersonaCore(
        persona_id="persona_v3",
        version="v3",
        identity=PersonaIdentity(name="Vaelith", core_traits=["傲慢"], locked=True),
        created_at=datetime(2026, 1, 1, tzinfo=TZ),
    )
    event = InteractionEvent(
        event_id="evt_04030",
        timestamp=datetime(2026, 8, 21, 13, 54, tzinfo=TZ),
        speaker="user",
        raw_text="你昨天不是说不会再管我了吗？",
        session_id="sess_0881",
    )
    plan = RetrievalPlan(time_scope="yesterday", entities=["user", "character"])
    dual = DualMemoryRecord(event_id="evt_04010", objective_fact="fact", insight_confidence=0.64)
    assert PersonaCore.model_validate_json(persona.model_dump_json()).identity.locked is True
    assert InteractionEvent.model_validate_json(event.model_dump_json()).raw_text.startswith("你昨天")
    assert RetrievalPlan.model_validate_json(plan.model_dump_json()).time_scope == "yesterday"
    assert dual.insight_confidence == 0.64


def test_custom_speaker_keeps_role_aliases() -> None:
    event = InteractionEvent(
        timestamp=datetime(2026, 8, 21, 13, 54, tzinfo=TZ),
        speaker="阿宁",
        raw_text="早上好。",
        session_id="sess_0881",
    )
    assert event.participants == ["阿宁", "user", "character"]
    named = InteractionEvent(
        timestamp=datetime(2026, 8, 21, 13, 54, tzinfo=TZ),
        speaker="user",
        raw_text="早上好。",
        session_id="sess_0881",
    )
    assert named.participants == ["user", "character"]
