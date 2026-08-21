from __future__ import annotations

from harness.core.inference import (
    Hypothesis,
    SituationActor,
    SituationState,
    SituationTrigger,
)
from harness.core.models import ScoreBreakdown, ScoredMemory
from harness.subagents.interpreter import CONSTRAINT, rule_interpret


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
