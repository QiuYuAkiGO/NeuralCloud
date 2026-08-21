"""Dual Memory: objective fact + persona-conditioned insight (01 §3)."""

from __future__ import annotations

import httpx
from pydantic import ValidationError

from harness.core.models import DualMemoryRecord, EpisodicEvent, PersonaCore
from harness.llm.client import LLMClient, MockLLM

_SYSTEM = """You produce Dual Memory for a character.
Return JSON: {"persona_conditioned_insight": string|null, "insight_confidence": number 0-1}
Insight is what THIS persona thinks the event means, not a generic summary.
If unsure, set insight to null and confidence 0.
Never invent facts that contradict objective_fact.
"""


class DualMemoryWriter:
    def __init__(self, llm: LLMClient) -> None:
        self.llm = llm

    def enrich(
        self,
        event: EpisodicEvent,
        persona: PersonaCore | None,
        *,
        raise_on_error: bool = False,
    ) -> DualMemoryRecord:
        if persona is None or isinstance(self.llm, MockLLM):
            return _rule_insight(event, persona)
        user = {
            "persona": persona.identity.model_dump(),
            "objective_fact": event.objective_fact,
            "summary": event.summary,
            "emotional_valence": event.emotional_valence,
        }
        try:
            raw = self.llm.complete_json(system=_SYSTEM, user=str(user))
            insight = raw.get("persona_conditioned_insight")
            confidence = raw.get("insight_confidence")
            record = DualMemoryRecord(
                event_id=event.event_id,
                objective_fact=event.objective_fact,
                persona_conditioned_insight=insight,
                insight_confidence=confidence,
                supporting_evidence=list(event.evidence),
            )
            if not record.persona_conditioned_insight:
                return _rule_insight(event, persona)
            return record
        except (ValueError, ValidationError, TypeError, httpx.HTTPError):
            if raise_on_error:
                raise
            return DualMemoryRecord(
                event_id=event.event_id,
                objective_fact=event.objective_fact,
                persona_conditioned_insight=None,
                insight_confidence=0.0,
                supporting_evidence=list(event.evidence),
            )


def _rule_insight(event: EpisodicEvent, persona: PersonaCore | None) -> DualMemoryRecord:
    fact = event.objective_fact
    insight = None
    confidence = 0.0
    if "不用管" in fact or "不管" in fact:
        traits = " ".join(persona.identity.core_traits) if persona else ""
        insight = (
            "对方说「不用管」时处于情绪激动状态；以该人物的表达习惯，这更像气话或寻求关注，而非真实拒绝。"
            + (f"（人格锚点：{traits}）" if traits else "")
        )
        confidence = 0.64
    elif "猫" in fact or "cat" in fact.lower():
        insight = "这件事与对方的宠物/偏好有关，值得记住具体经过，而不是抽象成一句「喜欢猫」。"
        confidence = 0.55
    return DualMemoryRecord(
        event_id=event.event_id,
        objective_fact=fact,
        persona_conditioned_insight=insight,
        insight_confidence=confidence if insight else 0.0,
        supporting_evidence=list(event.evidence),
    )
