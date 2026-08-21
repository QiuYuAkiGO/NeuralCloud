"""Complexity Gate: Fast Path vs Deep Path (02 §3 / §阶段 6)."""

from __future__ import annotations

import re

from harness.config import Settings, get_settings
from harness.core.inference import (
    ComplexityAssessment,
    ComplexitySignals,
    InferenceRequest,
    ParsedInteraction,
    SituationState,
)
from harness.core.models import ScoredMemory
from harness.llm.client import LLMClient, MockLLM
from harness.memory.retrieval.planner import _match_time_scope

_COMMITMENT_RE = re.compile(r"答应|承诺|不管|说好|不会再")
_MEMORY_PROBE_RE = re.compile(r"还记得|记得")
_VALUE_RE = re.compile(r"如果.{0,24}意味着|放弃.{0,12}目标|救我")
_GREETING_RE = re.compile(
    r"^(早上好|晚上好|早安|晚安|你好|嗨|哈喽|在吗|你在干嘛|在干嘛|一起吃饭吗)[。？?！!\s]*$"
)
_QUESTION_RE = re.compile(r"[?？]|吗|呢|是不是")

_LLM_GATE_SYSTEM = """You are the Complexity Gate for a character-AI inference pipeline.
Decide whether the utterance needs the deep path (Social Interpreter + full validation).
Deep path signals: emotionally loaded probes, references to shared past events
(user-stated dates are often imprecise — judge by meaning, not literal keywords),
commitment checks, value dilemmas, relationship tests, indirect accusation or hurt.
Fast path: greetings, small talk, simple factual questions, logistics.
The rule-based score and matched signals are a weak prior from a regex table;
override them when the table misreads the utterance.
Return JSON only: {"path": "fast"|"deep", "reason": string (short)}.
"""


class ComplexityGate:
    def __init__(self, settings: Settings | None = None, llm: LLMClient | None = None) -> None:
        self.settings = settings or get_settings()
        self.llm = llm

    def evaluate(self, situation: SituationState) -> tuple[SituationState, InferenceRequest]:
        signals = collect_signals(
            situation.utterance,
            situation.parsed,
            situation.relevant_memories,
        )
        score, reasons = score_complexity(signals)
        deep = score >= self.settings.complexity_threshold
        if _force_deep(signals):
            deep = True
            if "forced:documented_trigger" not in reasons:
                reasons.append("forced:documented_trigger")
        elif not deep and self._in_gray_zone(score):
            verdict = self._arbitrate(situation, signals, score)
            if verdict is not None:
                reasons.append(f"llm_arbitrated:{verdict}")
                deep = verdict == "deep"
        complexity = ComplexityAssessment(
            score=min(1.0, score),
            requires_deep_path=deep,
            reasons=reasons,
            signals=signals,
        )
        updated = situation.model_copy(update={"complexity": complexity})
        request = InferenceRequest(
            path="deep" if deep else "fast",
            score=complexity.score,
            reasons=reasons,
            situation_id=updated.situation_id,
            trigger_subagents=["social_interpreter"] if deep else [],
        )
        return updated, request

    def _in_gray_zone(self, score: float) -> bool:
        if self.llm is None or isinstance(self.llm, MockLLM):
            return False
        return self.settings.complexity_gray_lower <= score < self.settings.complexity_threshold

    def _arbitrate(
        self, situation: SituationState, signals: ComplexitySignals, score: float
    ) -> str | None:
        """LLM arbitration for gray-zone scores; may promote to deep, never demotes."""
        try:
            raw = self.llm.complete_json(
                system=_LLM_GATE_SYSTEM,
                user=str(
                    {
                        "utterance": situation.utterance,
                        "rule_score": round(score, 4),
                        "signals": signals.model_dump(),
                    }
                ),
            )
            path = str(raw.get("path", "")).strip().lower()
        except (ValueError, TypeError, AttributeError):
            return None
        if path in ("fast", "deep"):
            return path
        return None


def collect_signals(
    text: str,
    parsed: ParsedInteraction | None,
    memories: list[ScoredMemory],
) -> ComplexitySignals:
    stripped = text.strip()
    emotion = 0.0
    if parsed and parsed.emotion_cues:
        emotion = max(parsed.emotion_cues.values())
    return ComplexitySignals(
        time_scope=_match_time_scope(text),
        has_commitment_language=bool(_COMMITMENT_RE.search(text)),
        has_memory_probe=bool(_MEMORY_PROBE_RE.search(text)),
        has_value_conflict=bool(_VALUE_RE.search(text)),
        is_greeting=bool(_GREETING_RE.match(stripped)),
        is_question=bool(_QUESTION_RE.search(text)),
        text_length=len(stripped),
        emotion_strength=emotion,
        retrieval_commitment_conflict=any(
            "commitment_conflict" in item.why_retrieved for item in memories
        ),
    )


def score_complexity(signals: ComplexitySignals) -> tuple[float, list[str]]:
    score = 0.0
    reasons: list[str] = []
    if signals.is_greeting:
        reasons.append("greeting")
        if not (
            signals.has_commitment_language
            or signals.time_scope
            or signals.has_value_conflict
            or signals.has_memory_probe
        ):
            return 0.1, reasons

    if signals.time_scope == "yesterday":
        score += 0.20
        reasons.append("time:yesterday")
    elif signals.time_scope == "three_months":
        score += 0.30
        reasons.append("time:distant")
    elif signals.time_scope:
        score += 0.12
        reasons.append(f"time:{signals.time_scope}")

    if signals.has_memory_probe:
        score += 0.18
        reasons.append("memory_probe")
    if signals.has_commitment_language:
        score += 0.22
        reasons.append("commitment")
    if signals.has_value_conflict:
        score += 0.82
        reasons.append("value_conflict")
    if signals.retrieval_commitment_conflict:
        score += 0.16
        reasons.append("retrieval:commitment_conflict")
    if signals.emotion_strength:
        score += round(signals.emotion_strength * 0.22, 4)
        reasons.append("emotion_cues")
    if signals.is_question:
        score += 0.04
        reasons.append("question")
    if signals.text_length > 16:
        score += 0.04
        reasons.append("length")
    return min(1.0, score), reasons


def _force_deep(signals: ComplexitySignals) -> bool:
    if signals.has_value_conflict:
        return True
    if signals.time_scope in ("three_months", "last_month"):
        return True
    if signals.has_commitment_language and (signals.time_scope or signals.has_memory_probe):
        return True
    if signals.has_memory_probe and signals.time_scope:
        # "你还记得昨天…吗" — a dated memory check is the canonical deep-path case
        return True
    return False
