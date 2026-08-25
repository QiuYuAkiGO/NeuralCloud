from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from harness.core.inference import (
    Hypothesis,
    SituationActor,
    SituationState,
    SituationTrigger,
)
from harness.core.models import EpisodicEvent, ScoreBreakdown, ScoredMemory, SemanticFact
from harness.subagents.interpreter import CONSTRAINT, SocialInterpreter, rule_interpret


def test_running_example_constraint_is_red_line() -> None:
    situation = SituationState(
        trigger=SituationTrigger(ref="evt_04030"),
        utterance="你昨天不是说不会再管我了吗？",
        actor=SituationActor(),
        hypotheses=[Hypothesis(about="user_intent", value="seeking_reassurance", confidence=0.58)],
        relevant_memories=[
            ScoredMemory(
                memory_type="episodic",
                ref="evt_04010",
                relevance=0.91,
                why_retrieved=["commitment_conflict"],
                score_breakdown=ScoreBreakdown(),
            )
        ],
    )
    insight = rule_interpret(situation)
    assert insight.constraint == CONSTRAINT
    assert insight.possible_intents[0].intent == "seeking_reassurance"
    assert "不过度解释" in (insight.recommended_stance or "")
    assert insight.source == "social_interpreter"


class _RecordingLLM:
    """Not a MockLLM, so SocialInterpreter takes the LLM branch."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def complete_json(self, *, system: str, user: str) -> dict[str, Any]:
        self.calls.append((system, user))
        return {
            "possible_intents": [{"intent": "resentment_check", "confidence": 0.66}],
            "recommended_stance": "acknowledge",
            "constraint": "",
        }


def test_llm_prompt_carries_memory_events() -> None:
    llm = _RecordingLLM()
    situation = SituationState(
        trigger=SituationTrigger(ref="evt_04030"),
        utterance="你昨天不是说不会再管我了吗？",
        actor=SituationActor(),
        relevant_memories=[
            ScoredMemory(
                memory_type="episodic",
                ref="evt_04010",
                relevance=0.91,
                why_retrieved=["commitment_conflict"],
                score_breakdown=ScoreBreakdown(),
                episodic=EpisodicEvent(
                    event_id="evt_04010",
                    time=datetime(2026, 8, 20, 23, 40, tzinfo=UTC),
                    participants=["user", "character"],
                    summary="你以后不用管我了",
                    importance=0.86,
                    emotional_valence=-0.55,
                    evidence=["chat:sess_0881:evt_04010"],
                    objective_fact="用户昨晚 23:40 说'你以后不用管我了'，随后关闭对话窗口",
                    persona_conditioned_insight="角色当时处于愤怒状态；该表述属于气话",
                ),
            ),
            ScoredMemory(
                memory_type="semantic",
                ref="fact_01",
                relevance=0.5,
                why_retrieved=["entity:user"],
                score_breakdown=ScoreBreakdown(),
                semantic=SemanticFact(
                    fact_id="fact_01",
                    subject="user",
                    predicate="讨厌",
                    value="被过度管束",
                    confidence=0.8,
                    supporting_events=["evt_04010"],
                    formed_at=datetime(2026, 8, 21, tzinfo=UTC),
                ),
            ),
        ],
    )
    insight = SocialInterpreter(llm).interpret(situation)

    assert llm.calls, "non-mock client must take the LLM branch"
    _, user_prompt = llm.calls[0]
    assert "用户昨晚 23:40" in user_prompt
    assert "气话" in user_prompt
    assert "被过度管束" in user_prompt
    assert "commitment_conflict" in user_prompt
    assert insight.constraint == CONSTRAINT
    assert insight.possible_intents[0].intent == "resentment_check"
