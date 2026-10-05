"""Social / Intent Interpreter — gated subagent, never speaks for the character (01 §6.2 / 02 §阶段 6)."""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from harness.core.inference import (
    MAX_RELEVANT_MEMORIES,
    IntentHypothesis,
    SituationState,
    StructuredInsight,
)
from harness.core.models import ScoredMemory
from harness.llm.client import LLMClient, MockLLM
from harness.llm.structured import InterpreterOutput, helper_options

_SYSTEM = """You are Social Interpreter for a character AI.
Input includes up to 5 retrieved memories: episodic events carry an objective
fact plus the character's own perspective (insight) from when it happened;
semantic facts carry subject/predicate/value with confidence. Ground your
judgment in them.
Output JSON only:
{"possible_intents":[{"intent":string,"confidence":number}],
 "relationship_delta_expected": string|null,
 "recommended_stance": string,
 "constraint": "avoid_explicit_psychological_explanation"}
You provide structured judgment. Never write the character's reply.
The constraint is a red line: the character must not say they detected the user's psychology.
"""

CONSTRAINT = "avoid_explicit_psychological_explanation"


class SocialInterpreter:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or MockLLM()

    def interpret(self, situation: SituationState) -> StructuredInsight:
        if isinstance(self.llm, MockLLM):
            return rule_interpret(situation)
        try:
            raw = self.llm.complete_json(
                **helper_options(self.llm, InterpreterOutput, "social_interpreter"),
                system=_SYSTEM,
                user=str(
                    {
                        "utterance": situation.utterance,
                        "hypotheses": [h.model_dump() for h in situation.hypotheses],
                        "memories": [
                            _memory_payload(m)
                            for m in situation.relevant_memories[:MAX_RELEVANT_MEMORIES]
                        ],
                    }
                ),
            )
            insight = _coerce_insight(raw)
            if not insight.constraint:
                insight = insight.model_copy(update={"constraint": CONSTRAINT})
            return insight
        except (ValueError, ValidationError, TypeError, KeyError):
            return rule_interpret(situation)


def rule_interpret(situation: SituationState) -> StructuredInsight:
    why = {tag for mem in situation.relevant_memories for tag in mem.why_retrieved}
    intent_values = {h.value for h in situation.hypotheses}
    parsed_intent = situation.parsed.possible_social_intent if situation.parsed else None
    if parsed_intent:
        intent_values.add(parsed_intent.value)

    if "commitment_conflict" in why or "seeking_reassurance" in intent_values:
        return StructuredInsight(
            possible_intents=[
                IntentHypothesis(intent="seeking_reassurance", confidence=0.58),
                IntentHypothesis(intent="resentment_check", confidence=0.3),
            ],
            relationship_delta_expected="positive_if_handled_well",
            recommended_stance="承认昨天的话，但用角色自己的方式，不过度解释",
            constraint=CONSTRAINT,
        )
    if "values_dilemma" in intent_values or situation.complexity.signals.has_value_conflict:
        return StructuredInsight(
            possible_intents=[IntentHypothesis(intent="values_dilemma", confidence=0.7)],
            relationship_delta_expected=None,
            recommended_stance="stay_in_character_values",
            constraint=CONSTRAINT,
        )
    if "memory_check" in intent_values:
        return StructuredInsight(
            possible_intents=[IntentHypothesis(intent="memory_check", confidence=0.6)],
            recommended_stance="answer_from_retrieved_memory_without_overexplaining",
            constraint=CONSTRAINT,
        )
    intents = [
        IntentHypothesis(intent=value, confidence=0.5) for value in sorted(intent_values)
    ] or [IntentHypothesis(intent="unspecified", confidence=0.4)]
    return StructuredInsight(
        possible_intents=intents,
        recommended_stance="acknowledge_without_overexplaining",
        constraint=CONSTRAINT,
    )


def _coerce_insight(raw: dict) -> StructuredInsight:
    payload = raw.get("social_inference", raw)
    return StructuredInsight.model_validate({**payload, "source": "social_interpreter"})


def _memory_payload(memory: ScoredMemory) -> dict[str, Any]:
    """Serialize the retrieved event/fact itself, not just the retrieval tags."""
    payload: dict[str, Any] = {
        "ref": memory.ref,
        "type": memory.memory_type,
        "relevance": round(memory.relevance, 4),
        "why_retrieved": memory.why_retrieved,
    }
    if memory.episodic is not None:
        event = memory.episodic
        payload["event"] = {
            "time": event.time.isoformat(),
            "objective_fact": event.objective_fact,
            "character_perspective": event.persona_conditioned_insight,
        }
    elif memory.semantic is not None:
        fact = memory.semantic
        payload["fact"] = {
            "subject": fact.subject,
            "predicate": fact.predicate,
            "value": fact.value,
            "confidence": fact.confidence,
        }
    return payload
