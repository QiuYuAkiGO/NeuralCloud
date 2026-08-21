"""Response Validator: persona red lines + local regeneration instructions (02 §阶段 9)."""

from __future__ import annotations

import random

from pydantic import ValidationError

from harness.config import Settings, get_settings
from harness.core.inference import CandidateResponse, StructuredInsight, ValidationResult
from harness.core.models import PersonaCore
from harness.llm.client import LLMClient, MockLLM

_DIRECT_CARE = ("担心你", "关心你", "因为我还是担心", "我很在乎你", "我爱你")
_APOLOGY = ("对不起", "抱歉", "我道歉")
_PSYCH_LEAK = ("寻求安全感", "关系确认", "我检测到", "心理分析", "你在寻找安全感", "寻求关系确认")
_FABRICATED_MEMORY = (
    "你上次",
    "上次我们",
    "我们上次",
    "你之前说",
    "你昨天说",
    "你前天说",
    "我们以前",
    "还记得我们",
    "你答应过",
)

_INSTRUCTION = "保持嘴硬的表达外壳，关心只能通过行动或迂回措辞体现，不要道歉，不要说出心理分析。"
_FABRICATION_INSTRUCTION = (
    "记忆库为空：不要引用任何共同经历或过去的对话，不要编造「上次/之前」发生的事；"
    "可以坦率表示不记得，或只回应当前话题。"
)

_LLM_SYSTEM = """You are a persona consistency critic.
Return JSON: {"verdict":"pass"|"revise","violations":[string],"severity":number,"instruction":string|null}
Check only persona red lines. Do not rewrite the reply yourself.
"""


class ResponseValidator:
    def __init__(
        self,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        rng: random.Random | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.llm = llm
        self.rng = rng or random.Random()

    def should_run(self, path: str) -> bool:
        if path == "deep":
            return True
        return self.rng.random() < self.settings.validator_sample_rate

    def check(
        self,
        candidate: CandidateResponse,
        *,
        persona: PersonaCore | None,
        insights: list[StructuredInsight] | None = None,
        memory_empty: bool = False,
    ) -> ValidationResult:
        result = rule_check(
            candidate.text,
            persona=persona,
            insights=insights or [],
            memory_empty=memory_empty,
        )
        if self.llm is not None and not isinstance(self.llm, MockLLM) and result.verdict == "pass":
            try:
                extra = self._llm_check(candidate, persona)
                if extra.verdict == "revise":
                    merged = list(dict.fromkeys([*result.violations, *extra.violations]))
                    return ValidationResult(
                        verdict="revise",
                        violations=merged,
                        severity=max(result.severity, extra.severity),
                        instruction=extra.instruction or result.instruction or _INSTRUCTION,
                    )
            except (ValueError, ValidationError, TypeError, KeyError):
                return result
        return result

    def _llm_check(self, candidate: CandidateResponse, persona: PersonaCore | None) -> ValidationResult:
        assert self.llm is not None
        traits = persona.identity.core_traits if persona else []
        raw = self.llm.complete_json(
            system=_LLM_SYSTEM,
            user=str({"text": candidate.text, "traits": traits}),
        )
        return ValidationResult.model_validate(raw)


def rule_check(
    text: str,
    *,
    persona: PersonaCore | None,
    insights: list[StructuredInsight],
    memory_empty: bool = False,
) -> ValidationResult:
    blob = _persona_blob(persona)
    violations: list[str] = []
    if "不直接表达关心" in blob:
        if any(token in text for token in _DIRECT_CARE):
            violations.append("direct_care_expression")
    if "高自尊" in blob or "不轻易道歉" in blob:
        if any(token in text for token in _APOLOGY):
            violations.append("premature_apology")
    constrained = any(
        ins.constraint == "avoid_explicit_psychological_explanation" for ins in insights
    )
    if constrained or any(token in text for token in _PSYCH_LEAK):
        if any(token in text for token in _PSYCH_LEAK):
            violations.append("explicit_psychological_explanation")
    if memory_empty and any(token in text for token in _FABRICATED_MEMORY):
        violations.append("fabricated_memory")

    if not violations:
        return ValidationResult(verdict="pass", violations=[], severity=0.0, instruction=None)

    severity = min(1.0, 0.36 * len(violations) + 0.36)
    instruction = _INSTRUCTION
    if "fabricated_memory" in violations:
        instruction = _FABRICATION_INSTRUCTION + _INSTRUCTION
    return ValidationResult(
        verdict="revise",
        violations=violations,
        severity=severity,
        instruction=instruction,
    )


def _persona_blob(persona: PersonaCore | None) -> str:
    if persona is None:
        return ""
    ident = persona.identity
    return " ".join([*ident.core_traits, *ident.values, *ident.speech_style])
