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
# Best-effort fallbacks only — real name / unknown-noun detection is the gate
# model's job; these patterns exist for the no-LLM (MockLLM / failure) path.
_NAME_INTRO_RE = re.compile(r"(?:叫|名叫)(?![我你他她它吧吗呢啊哦啦])([一-龥]{2,3})(?=$|[\s，。！？!?,、的])")
_QUOTED_TITLE_RE = re.compile(r"《[^》]+》|「[^」]+」|『[^』]+』")
_DEFAULT_ENTITIES = {"user", "character"}

_LLM_GATE_SYSTEM = """You are the Complexity Gate for a character-AI inference pipeline.
You are one of two independent judges; the other is a regex scoring table whose
score and matched signals are attached as a weak prior. A deep verdict from
either judge triggers the deep path (Social Interpreter + full validation);
the fast path requires both judges to agree. If the table misreads the
utterance as simpler than it is, choose deep.

MANDATORY deep path:
- The utterance mentions a person by name (a known character or someone new).
- The utterance contains a noun the character may not know — an unfamiliar proper
  noun, term, title, or concept. If you cannot resolve it, choose deep.

Other deep path signals: emotionally loaded probes, references to shared past events
(user-stated dates are often imprecise — judge by meaning, not literal keywords),
commitment checks, value dilemmas, relationship tests, indirect accusation or hurt.
Fast path: greetings, small talk, simple factual questions, logistics.
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
        forced = _force_deep(signals)
        # Two judges, OR-combined: the regex side (score / forced triggers) and the
        # gate model vote independently. Either deep verdict triggers the Social
        # Interpreter; fast requires both sides to agree. A rule-side deep verdict
        # already settles the OR, so the model is only consulted on rule-side fast.
        rule_deep = forced or score >= self.settings.complexity_threshold
        deep = rule_deep
        if forced and "forced:documented_trigger" not in reasons:
            reasons.append("forced:documented_trigger")
        # Pure greetings carry no ambiguity for a second judge to resolve;
        # skipping keeps trivial small talk entirely off the LLM.
        if not rule_deep and self._llm_enabled and not _pure_greeting(signals):
            # The parser's LLM call doubles as this judge when it ran;
            # only messages the parser handled rule-only need a live call here.
            verdict = _parsed_gate_vote(situation)
            if verdict is None:
                verdict = self._decide(situation, signals, score)
            if verdict is not None:
                reasons.append(f"llm_decided:{verdict}")
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

    @property
    def _llm_enabled(self) -> bool:
        return self.llm is not None and not isinstance(self.llm, MockLLM)

    def _decide(
        self, situation: SituationState, signals: ComplexitySignals, score: float
    ) -> str | None:
        """Second judge, consulted only when the rule side votes fast; a deep verdict here always wins."""
        llm = self.llm
        if llm is None:
            return None
        try:
            raw = llm.complete_json(
                system=_LLM_GATE_SYSTEM,
                user=str(
                    {
                        "utterance": situation.utterance,
                        "rule_score": round(score, 4),
                        "signals": signals.model_dump(),
                    }
                ),
                max_tokens=self.settings.llm_json_max_tokens,
            )
            path = str(raw.get("path", "")).strip().lower()
        except (ValueError, TypeError, AttributeError):
            return None
        if path in ("fast", "deep"):
            return path
        return None


def _pure_greeting(signals: ComplexitySignals) -> bool:
    """Same condition as the early return in score_complexity: a greeting match
    with zero other signals leaves nothing for the second judge to catch."""
    return signals.is_greeting and not (
        signals.has_commitment_language
        or signals.time_scope
        or signals.has_value_conflict
        or signals.has_memory_probe
        or signals.has_person_name
        or signals.has_unknown_entity
    )


def _parsed_gate_vote(situation: SituationState) -> str | None:
    parsed = situation.parsed
    if parsed is not None and parsed.gate_vote in ("fast", "deep"):
        return parsed.gate_vote
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
    known_names = _memory_entity_names(memories)
    if parsed:
        known_names |= {e for e in parsed.entities if e not in _DEFAULT_ENTITIES}
    has_person_name = bool(_NAME_INTRO_RE.search(text)) or any(
        len(name) >= 2 and name in text for name in known_names
    )
    return ComplexitySignals(
        time_scope=_match_time_scope(text),
        has_commitment_language=bool(_COMMITMENT_RE.search(text)),
        has_memory_probe=bool(_MEMORY_PROBE_RE.search(text)),
        has_value_conflict=bool(_VALUE_RE.search(text)),
        has_person_name=has_person_name,
        has_unknown_entity=bool(_QUOTED_TITLE_RE.search(text)),
        is_greeting=bool(_GREETING_RE.match(stripped)),
        is_question=bool(_QUESTION_RE.search(text)),
        text_length=len(stripped),
        emotion_strength=emotion,
        retrieval_commitment_conflict=any(
            "commitment_conflict" in item.why_retrieved for item in memories
        ),
    )


def _memory_entity_names(memories: list[ScoredMemory]) -> set[str]:
    """Names seen in prior retrieval tags count as known people when they reappear."""
    names: set[str] = set()
    for item in memories:
        for tag in item.why_retrieved:
            if tag.startswith("entity:"):
                name = tag.split(":", 1)[1].strip()
                if name and name not in _DEFAULT_ENTITIES:
                    names.add(name)
    return names


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
            or signals.has_person_name
            or signals.has_unknown_entity
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
    if signals.has_person_name:
        score += 0.30
        reasons.append("person_name")
    if signals.has_unknown_entity:
        score += 0.30
        reasons.append("unknown_entity")
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
    if signals.has_person_name or signals.has_unknown_entity:
        # Mandatory routing: names and unfamiliar nouns always need the Social
        # Interpreter; the gate model applies the same rule as its primary check.
        return True
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
