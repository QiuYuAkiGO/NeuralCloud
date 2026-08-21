from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from harness.core.exceptions import EvidenceRequiredError, InvalidCuratorOpError
from harness.core.models import CuratorOp, CuratorOutput, InteractionEvent, SemanticFact
from harness.memory.writer.pipeline import WriterPipeline

TZ = ZoneInfo("Asia/Shanghai")


def _chat(text: str, when: datetime, event_id: str | None = None) -> InteractionEvent:
    kwargs = {
        "timestamp": when,
        "speaker": "user",
        "raw_text": text,
        "session_id": "sess_cat",
        "evidence": [f"traj_{event_id or text}"],
    }
    if event_id:
        kwargs["event_id"] = event_id
    return InteractionEvent(**kwargs)


def test_cat_preference_consolidates(service) -> None:
    t0 = datetime(2026, 8, 14, 21, 32, tzinfo=TZ)
    events = [
        _chat("用户提到上周收养的猫生病了", t0, "evt_03812"),
        _chat("用户又说起那只猫，晚上还去看了它", t0.replace(day=15), "evt_02941"),
        _chat("角色主动问起猫的情况，用户很开心", t0.replace(day=16), "evt_01877"),
    ]
    recorded = [
        service.record_event(item, importance=0.6, emotional_valence=-0.1 if i == 0 else 0.2)
        for i, item in enumerate(events)
    ]
    assert recorded[0].persona_conditioned_insight
    assert "猫" in (recorded[0].persona_conditioned_insight or "")
    facts = service.stores.semantic.list_all()
    assert len(facts) == 1
    fact = facts[0]
    assert fact.subject == "user"
    assert fact.predicate == "preference"
    assert fact.value == "喜欢猫"
    assert fact.confidence >= 0.7
    assert set(fact.supporting_events) >= {recorded[0].event_id, recorded[1].event_id, recorded[2].event_id}
    candidates = service.stores.candidates.list_all()
    assert any(row.status == "consolidated" for row in candidates)


def test_direct_semantic_write_without_evidence_rejected(service) -> None:
    with pytest.raises(EvidenceRequiredError):
        service.force_write_semantic(
            SemanticFact.model_construct(
                fact_id="sem_bad",
                subject="user",
                predicate="preference",
                value="喜欢被幻觉污染",
                confidence=0.99,
                supporting_events=[],
                formed_at=datetime(2026, 8, 21, tzinfo=TZ),
            )
        )


def test_curator_cannot_add_semantic_directly(service) -> None:
    event = service.record_event(
        _chat("今天天气不错", datetime(2026, 8, 21, 9, tzinfo=TZ), "evt_weather"),
        curate=False,
    )
    pipeline = WriterPipeline(service.stores, service.embeddings, service.settings)
    with pytest.raises(EvidenceRequiredError):
        pipeline.apply(
            CuratorOutput(
                ops=[
                    CuratorOp(
                        op="ADD",
                        type="semantic",
                        claim="用户是虚构的神",
                        subject="user",
                        predicate="identity",
                        value="神",
                        evidence=[],
                    )
                ]
            ),
            event=event,
        )
    assert service.stores.semantic.list_all() == []


def test_dual_memory_commitment_insight(service) -> None:
    event = service.record_event(
        InteractionEvent(
            event_id="evt_04010",
            timestamp=datetime(2026, 8, 20, 23, 40, tzinfo=TZ),
            speaker="user",
            raw_text="你以后不用管我了",
            session_id="sess_0881",
            evidence=["chat:sess_0881:evt_04010"],
        ),
        importance=0.86,
        emotional_valence=-0.6,
        event_type="commitment_or_statement",
        curate=False,
    )
    dual = event.dual()
    assert dual.objective_fact
    assert dual.persona_conditioned_insight
    assert "气话" in dual.persona_conditioned_insight or "寻求关注" in dual.persona_conditioned_insight
    assert dual.insight_confidence == pytest.approx(0.64)


def test_update_candidate_without_ref_rejected_in_strict_apply(service) -> None:
    event = service.record_event(
        _chat("今天天气不错", datetime(2026, 8, 21, 9, tzinfo=TZ), "evt_weather2"),
        curate=False,
    )
    pipeline = WriterPipeline(service.stores, service.embeddings, service.settings)
    with pytest.raises(InvalidCuratorOpError, match="requires ref"):
        pipeline.apply(
            CuratorOutput(ops=[CuratorOp(op="UPDATE", type="candidate")]),
            event=event,
        )


def test_record_event_survives_update_without_ref(service) -> None:
    class BadCurator:
        def propose(self, event, *, persona, open_candidates):
            return CuratorOutput(ops=[CuratorOp(op="UPDATE", type="candidate")])

    service.curator = BadCurator()
    event = service.record_event(
        _chat("你好呀", datetime(2026, 8, 21, 11, tzinfo=TZ), "evt_hello_curator"),
    )
    assert service.stores.episodic.get(event.event_id) is not None


def test_llm_curator_update_without_ref_becomes_noop(service) -> None:
    from harness.llm.client import MockLLM
    from harness.memory.writer.curator import LLMCurator

    event = service.record_event(
        _chat("随便说一句", datetime(2026, 8, 21, 10, tzinfo=TZ), "evt_curator_bad"),
        curate=False,
    )
    curator = LLMCurator(
        MockLLM(routes={"evt_curator_bad": {"ops": [{"op": "UPDATE", "type": "candidate"}]}})
    )
    output = curator.propose(event, persona=service.get_persona(), open_candidates=[])
    assert output.ops[0].op == "NO_OP"
    assert "missing_ref" in (output.ops[0].reason or "")
