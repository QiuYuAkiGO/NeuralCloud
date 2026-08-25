from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from harness.compiler.compiler import ContextCompiler, estimate_tokens
from harness.config import Settings
from harness.core.inference import (
    MAX_RELEVANT_MEMORIES,
    SessionTurn,
    SituationActor,
    SituationState,
    SituationTrigger,
    StructuredInsight,
)
from harness.core.models import (
    CharacterSnapshot,
    EpisodicEvent,
    ScoreBreakdown,
    ScoredMemory,
)
from tests.factories import vaelith

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _situation(*, memories: int = 3) -> SituationState:
    persona = vaelith()
    hits = []
    for i in range(memories):
        event = EpisodicEvent(
            event_id=f"evt_mem{i}",
            time=NOW,
            participants=["user", "character"],
            summary=f"记忆摘要{i}" * 20,
            importance=0.5,
            emotional_valence=0.0,
            evidence=[f"traj:{i}"],
            objective_fact=f"客观事实{i}" * 20,
            persona_conditioned_insight="角色视角解读" * 10,
        )
        hits.append(
            ScoredMemory(
                memory_type="episodic",
                ref=event.event_id,
                relevance=0.9 - i * 0.1,
                why_retrieved=["entity:user"],
                score_breakdown=ScoreBreakdown(total=0.9),
                episodic=event,
            )
        )
    return SituationState(
        trigger=SituationTrigger(ref="evt_04030"),
        utterance="你昨天不是说不会再管我了吗？",
        actor=SituationActor(persona_core_ref=persona.persona_id, psych_state={"short": {"mood": "guarded"}}),
        relevant_memories=hits,
        snapshot=CharacterSnapshot(persona=persona),
    )


def test_compiler_records_provenance() -> None:
    compiler = ContextCompiler(Settings(context_token_budget=3000, compiler_top_memories=5))
    insight = StructuredInsight(
        constraint="avoid_explicit_psychological_explanation",
        recommended_stance="acknowledge_without_overexplaining",
    )
    context = compiler.compile(
        _situation(),
        insights=[insight],
        session=[SessionTurn(role="user", text="上一句")],
    )
    kinds = {entry.segment_kind for entry in context.provenance}
    assert "persona" in kinds
    assert "memory" in kinds
    assert "insight" in kinds
    assert "user" in kinds
    persona_entry = next(entry for entry in context.provenance if entry.segment_kind == "persona")
    assert persona_entry.source_ids == ["persona_v3"]
    messages = context.render_messages()
    assert messages[0]["role"] == "system"
    assert "Vaelith" in messages[0]["content"]
    assert "avoid_explicit_psychological_explanation" in messages[0]["content"]
    assert "昨天" in messages[1]["content"]


def test_session_text_uses_speaker_names() -> None:
    compiler = ContextCompiler(Settings(context_token_budget=3000, compiler_top_memories=5))
    context = compiler.compile(
        _situation(memories=0),
        session=[
            SessionTurn(role="user", text="上一句", speaker="阿宁"),
            SessionTurn(role="character", text="嗯。", speaker="薇莉丝"),
        ],
    )
    session_seg = next(seg for seg in context.segments if seg.kind == "session")
    assert "阿宁: 上一句" in session_seg.text
    assert "薇莉丝: 嗯。" in session_seg.text


def test_compiler_respects_token_budget() -> None:
    compiler = ContextCompiler(Settings(context_token_budget=80, compiler_top_memories=5))
    context = compiler.compile(_situation(memories=8))
    assert context.budget.used <= context.budget.total
    rendered = "\n".join(seg.text for seg in context.segments)
    assert estimate_tokens(rendered) <= 80 + 5  # per-segment rounding slack
    assert any(seg.kind == "persona" for seg in context.segments)
    assert context.provenance


def test_empty_memory_adds_no_fabrication_constraint() -> None:
    compiler = ContextCompiler(Settings(context_token_budget=3000, compiler_top_memories=5))
    context = compiler.compile(_situation(memories=0))
    constraints = [seg for seg in context.segments if seg.kind == "constraint"]
    assert constraints, "empty memory store must inject a no-fabrication constraint"
    assert "no_fabricated_memory" in constraints[0].text
    assert "禁止编造" in constraints[0].text
    assert not any(seg.kind == "memory" for seg in context.segments)


def test_memories_present_skip_no_fabrication_constraint() -> None:
    compiler = ContextCompiler(Settings(context_token_budget=3000, compiler_top_memories=5))
    context = compiler.compile(_situation(memories=2))
    constraint_text = "".join(seg.text for seg in context.segments if seg.kind == "constraint")
    assert "no_fabricated_memory" not in constraint_text


def test_situation_state_lists_at_most_five_memories() -> None:
    situation = _situation(memories=8)
    assert len(situation.relevant_memories) == MAX_RELEVANT_MEMORIES
    assert [memory.ref for memory in situation.relevant_memories] == [
        f"evt_mem{i}" for i in range(MAX_RELEVANT_MEMORIES)
    ]
