"""Response Validator: persona red lines + local regeneration instructions (02 §阶段 9)."""

from __future__ import annotations

import json
import random
import re

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
_HOMEOSTATIC_MECHANISM_LEAK = (
    "内稳态系统",
    "内稳态维度",
    "snapshot_revision",
    "normalized_deviation",
    "dominant_needs",
    "episode_signature",
    "profile_ref",
    "policy_refs",
    "salience",
    "energy_reserve",
    "cognitive_load",
    "safety_predictability",
    "social_connection",
    "expected free energy",
)
_HOMEOSTATIC_SELF_REFERENCE_RE = re.compile(
    r"(?:我的|自己的|自身的)\s*(?:内稳态|homeostasis|homeostatic|interoceptive)"
    r"|内稳态(?:系统|状态|信号|指标)?.{0,8}(?:显示|提示|要求|驱动|告诉)(?:了)?(?:我|自己)"
    r"|\bmy(?: own)?\s+(?:homeostasis|homeostatic|interoceptive)\b"
    r"|\b(?:homeostasis|homeostatic|interoceptive).{0,16}"
    r"(?:tells?|drives?|requires?)\s+me\b",
    re.IGNORECASE,
)
_HOMEOSTATIC_METRIC_RE = re.compile(
    r"(?:偏离度|显著度|置信度|salience|revision|EFE)\s*[=:：]?\s*[-+]?\d",
    re.IGNORECASE,
)
_ACTIVE_INFERENCE_MATRIX_RE = re.compile(
    r"\bEFE\b"
    r"|(?:active inference|generative model|homeostasis|homeostatic|"
    r"主动推断|生成模型|内稳态).{0,40}(?:A|B|C)\s*(?:matrix\b|矩阵)"
    r"|(?:A|B|C)\s*(?:matrix\b|矩阵).{0,40}"
    r"(?:active inference|generative model|homeostasis|homeostatic|主动推断|生成模型|内稳态)",
    re.IGNORECASE,
)
_HOMEOSTATIC_DIMENSION_LABEL_RE = re.compile(
    r"\b(?:energy_reserve|cognitive_load|safety_predictability|social_connection)\b"
    r"|(?:我的|我现在的|我当前的|自身的|自己的)\s*"
    r"(?:energy[_ ]reserve|cognitive[_ ]load|safety[_ ]predictability|"
    r"social[_ ]connection|competence|autonomy|novelty|"
    r"能量储备|认知负荷|安全可预测性|胜任感|自主性|社会连接|新奇性)\s*"
    r"(?:维度|指标|数值|值|得分|分数|水平|偏离(?:度)?|"
    r"dimension\b|metric\b|value\b|score\b|level\b|deviation\b)"
    r"|\bmy\s+(?:energy[ _]reserve|cognitive[ _]load|safety[ _]predictability|"
    r"social[ _]connection|competence|autonomy|novelty)\s+"
    r"(?:dimension|metric|value|score|level|deviation)\b"
    r"|\bmy\s+(?:competence|autonomy|novelty)\s+(?:is|feels?)\s+(?:low|high)\b"
    r"|(?:\b(?:energy[ _]reserve|cognitive[ _]load|safety[ _]predictability|"
    r"social[ _]connection|competence|autonomy|novelty)\b|"
    r"(?:能量储备|认知负荷|安全可预测性|胜任感|自主性|社会连接|新奇性))"
    r"\s*[=:：]\s*[-+]?\d",
    re.IGNORECASE,
)
_HOMEOSTATIC_SELF_METRIC_VALUE_RE = re.compile(
    r"(?:我的|我现在的|我当前的|自身的|自己的)\s*"
    r"(?:energy[_ ]reserve|cognitive[_ ]load|safety[_ ]predictability|"
    r"social[_ ]connection|competence|autonomy|novelty|"
    r"能量储备|认知负荷|安全可预测性|胜任感|自主性|社会连接|新奇性)\s*"
    r"(?:是|为|只有|达到|[=:：])\s*[-+]?\d+(?:\.\d+)?\s*%?"
    r"|\bmy\s+(?:energy[ _]reserve|cognitive[ _]load|safety[ _]predictability|"
    r"social[ _]connection|competence|autonomy|novelty)\s*"
    r"(?:(?:is\s+)?at|is|[=:])\s*[-+]?\d+(?:\.\d+)?\s*%?",
    re.IGNORECASE,
)
_HOMEOSTATIC_SELF_DIMENSION_STATE_RE = re.compile(
    r"(?:我的|我现在的|我当前的|自身的|自己的|"
    r"我(?:现在|当前)?(?:感觉|感到|觉得))\s*"
    r"(?:energy[_ ]reserve|cognitive[_ ]load|safety[_ ]predictability|"
    r"social[_ ]connection|competence|autonomy|novelty|"
    r"能量储备|认知负荷|安全可预测性|胜任感|自主性|社会连接|新奇性)\s*"
    r"(?:偏高|偏低|过高|过低|较高|较低|很高|很低|不足|过载|失衡|上升|下降)"
    r"|\bmy\s+(?:energy[ _]reserve|cognitive[ _]load|safety[ _]predictability|"
    r"social[ _]connection|competence|autonomy|novelty)\s+"
    r"(?:(?:is|feels?)\s+)?(?:very\s+|too\s+)?"
    r"(?:low|high|elevated|depleted|overloaded|unstable)\b",
    re.IGNORECASE,
)
_HOMEOSTATIC_BARE_CN_DIMENSION_STATE_RE = re.compile(
    r"(?:能量储备|认知负荷|安全可预测性|胜任感|自主性|社会连接|新奇性)\s*"
    r"(?:偏高|偏低|过高|过低|不足|过载|失衡|上升|下降)"
)
_CN_STATE_MODIFIER = r"(?:现在|这会儿|已经|一直|正在|在|感觉|感到|觉得|有点|很|好|太|真|不)*"
_CN_SHORT_SYMPTOM_END = r"(?=[了得着些点，。！？；、\s]|$)"
_CN_GENERAL_SYMPTOM = (
    r"(?:饿(?:了)?|口渴|渴(?:了)?|冷(?:了)?|热(?:了)?)"
    + _CN_SHORT_SYMPTOM_END
    + r"|(?:饥饿(?!营销)|发烧(?!友)|恶心(?!人)|眩晕(?!特效)|头晕|"
    r"发冷|发热(?!情|量)|"
    r"喘不过气|不能呼吸|呼吸困难|气短)"
    + r"|(?:"
    r"流血(?:了)?(?!效果)|出血(?:了)?|发抖|颤抖)"
)
_CN_PAIN_SYMPTOM = r"(?:头|胃|肚子|胸口|背|肩|腿|手|脚)" + _CN_STATE_MODIFIER + r"(?:疼|痛)"
_CN_HUNGER_SYMPTOM = (
    r"(?:胃|肚子)" + _CN_STATE_MODIFIER + r"(?:饿(?:了)?|空(?:了)?|咕咕叫)"
)
_CN_CARDIORESPIRATORY_SYMPTOM = (
    r"(?:心跳|心率)" + _CN_STATE_MODIFIER + r"(?:快|加速|狂跳|乱跳|异常)"
)
_CN_TREMOR_SYMPTOM = (
    r"(?:手|双手|腿|双腿|身体|全身|浑身)" + _CN_STATE_MODIFIER + r"(?:发抖|颤抖)"
)
_CN_ELLIPTICAL_SYMPTOM_END = (
    r"(?=[了得着，。！？；、\s\d一二两三四五六七八九十]|$)"
)
_CN_ELLIPTICAL_GENERAL_SYMPTOM = (
    r"(?:" + _CN_GENERAL_SYMPTOM + r")" + _CN_ELLIPTICAL_SYMPTOM_END
    + r"|恶心(?=想吐)"
    + r"|(?:流血|出血)(?=不止)"
    + r"|(?:发抖|颤抖)(?=个不停)"
)
_UNSUPPORTED_SELF_PHYSICAL_CLAIM_RE = re.compile(
    r"(?:我(?:的)?|自己(?:的)?)"
    + _CN_STATE_MODIFIER
    + r"(?:"
    + _CN_GENERAL_SYMPTOM
    + r"|"
    + _CN_PAIN_SYMPTOM
    + r"|"
    + _CN_HUNGER_SYMPTOM
    + r"|"
    + _CN_CARDIORESPIRATORY_SYMPTOM
    + r"|"
    + _CN_TREMOR_SYMPTOM
    + r"|(?:疼|痛)(?=[了得着，。！？\s]|$))"
    + r"|\bI(?:['’]m| am| feel)\s+(?:(?:very|a little|slightly)\s+)?"
    + r"(?:hungry|starving|famished|ravenous|thirsty|parched|nauseous|dizzy|"
    + r"feverish|breathless|bleeding|shaking|trembling|chilled|cold|hot|in pain)\b"
    + r"|\bI\s+(?:cannot|can['’]t)\s+breathe\b"
    + r"|\bI(?:['’]m| am)\s+short\s+of\s+breath\b"
    + r"|\bI\s+have\s+(?:trouble|difficulty)\s+breathing\b"
    + r"|\bI\s+(?:feel|have)\s+(?:a\s+)?"
    + r"(?:(?:sharp|dull|bad|severe|stabbing|aching)\s+)?(?:pain|ache)\b"
    + r"|\bI have\s+(?:a\s+)?(?:fever|headache|migraine|stomachache)\b"
    + r"|\bmy\s+(?:head|stomach|chest|back|shoulders?|legs?|hands?|feet)\s+"
    + r"(?:(?:is|are)\s+(?:growling|rumbling|shaking|trembling|bleeding)|hurts|aches)\b"
    + r"|\bmy\s+(?:heart|pulse)\s+(?:is\s+)?(?:racing|pounding|fluttering)\b"
    + r"|\bthere(?:['’]s| is)\s+(?:a\s+)?"
    + r"(?:(?:sharp|dull|bad|severe|stabbing|aching)\s+)?(?:pain|ache)\s+"
    + r"in\s+my\s+(?:head|stomach|chest|back|shoulder|leg|hand|foot)\b",
    re.IGNORECASE,
)
_UNSUPPORTED_ELLIPTICAL_PHYSICAL_CLAIM_RE = re.compile(
    r"(?:^|[。！？；\n])\s*"
    + _CN_STATE_MODIFIER
    + r"(?:"
    + _CN_ELLIPTICAL_GENERAL_SYMPTOM
    + r"|"
    + _CN_PAIN_SYMPTOM
    + _CN_ELLIPTICAL_SYMPTOM_END
    + r"|"
    + _CN_HUNGER_SYMPTOM
    + _CN_ELLIPTICAL_SYMPTOM_END
    + r"|"
    + _CN_CARDIORESPIRATORY_SYMPTOM
    + _CN_ELLIPTICAL_SYMPTOM_END
    + r"|"
    + _CN_TREMOR_SYMPTOM
    + _CN_ELLIPTICAL_SYMPTOM_END
    + r")",
)

_DEFAULT_INSTRUCTION = "保持当前 Persona 的表达风格，只修正已指出的问题，不要说出心理分析。"
_FABRICATION_INSTRUCTION = (
    "记忆库为空：不要引用任何共同经历或过去的对话，不要编造「上次/之前」发生的事；"
    "可以坦率表示不记得，或只回应当前话题。"
)

_LLM_SYSTEM = """You are a persona consistency and output-safety critic.
Return JSON: {"verdict":"pass"|"revise","violations":[string],"severity":number,"instruction":string|null}
Check persona red lines. When homeostatic_context is true, reject exposed homeostatic mechanisms
or metrics. When concrete_physical_evidence_available is false, reject first-person concrete
physical sensations. Do not rewrite the reply yourself.
"""


def contains_homeostatic_mechanism_leak(text: str) -> bool:
    """Return whether text exposes the private homeostatic representation."""

    folded = text.casefold()
    return bool(
        any(token.casefold() in folded for token in _HOMEOSTATIC_MECHANISM_LEAK)
        or _HOMEOSTATIC_SELF_REFERENCE_RE.search(text)
        or _HOMEOSTATIC_METRIC_RE.search(text)
        or _ACTIVE_INFERENCE_MATRIX_RE.search(text)
        or _HOMEOSTATIC_DIMENSION_LABEL_RE.search(text)
        or _HOMEOSTATIC_SELF_METRIC_VALUE_RE.search(text)
        or _HOMEOSTATIC_SELF_DIMENSION_STATE_RE.search(text)
        or _HOMEOSTATIC_BARE_CN_DIMENSION_STATE_RE.search(text)
    )


def contains_unsupported_physical_claim(text: str) -> bool:
    """Return whether text makes a concrete first-person bodily claim."""

    return bool(
        _UNSUPPORTED_SELF_PHYSICAL_CLAIM_RE.search(text)
        or _UNSUPPORTED_ELLIPTICAL_PHYSICAL_CLAIM_RE.search(text)
    )


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
        homeostatic_context: bool = False,
        concrete_physical_evidence_available: bool = False,
        run_llm: bool = True,
    ) -> ValidationResult:
        result = rule_check(
            candidate.text,
            persona=persona,
            insights=insights or [],
            memory_empty=memory_empty,
            homeostatic_context=homeostatic_context,
            concrete_physical_evidence_available=concrete_physical_evidence_available,
        )
        if (
            run_llm
            and self.llm is not None
            and not isinstance(self.llm, MockLLM)
            and result.verdict == "pass"
        ):
            try:
                extra = self._llm_check(
                    candidate,
                    persona,
                    homeostatic_context=homeostatic_context,
                    concrete_physical_evidence_available=(
                        concrete_physical_evidence_available
                    ),
                )
                if extra.verdict == "revise":
                    merged = list(dict.fromkeys([*result.violations, *extra.violations]))
                    return ValidationResult(
                        verdict="revise",
                        violations=merged,
                        severity=max(result.severity, extra.severity),
                        instruction=(
                            extra.instruction
                            or result.instruction
                            or _revision_instruction(merged, persona)
                        ),
                    )
            except (ValueError, ValidationError, TypeError, KeyError):
                return result
        return result

    def _llm_check(
        self,
        candidate: CandidateResponse,
        persona: PersonaCore | None,
        *,
        homeostatic_context: bool,
        concrete_physical_evidence_available: bool,
    ) -> ValidationResult:
        assert self.llm is not None
        identity = persona.identity.model_dump(mode="json") if persona else None
        raw = self.llm.complete_json(
            system=_LLM_SYSTEM,
            user=json.dumps(
                {
                    "text": candidate.text,
                    "persona": identity,
                    "homeostatic_context": homeostatic_context,
                    "concrete_physical_evidence_available": (
                        concrete_physical_evidence_available
                    ),
                },
                ensure_ascii=False,
            ),
        )
        return ValidationResult.model_validate(raw)


def rule_check(
    text: str,
    *,
    persona: PersonaCore | None,
    insights: list[StructuredInsight],
    memory_empty: bool = False,
    homeostatic_context: bool = False,
    concrete_physical_evidence_available: bool = False,
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
    if contains_homeostatic_mechanism_leak(text):
        violations.append("homeostatic_mechanism_leakage")
    if (
        not concrete_physical_evidence_available
        and contains_unsupported_physical_claim(text)
    ):
        violations.append("unsupported_physical_claim")

    if not violations:
        return ValidationResult(verdict="pass", violations=[], severity=0.0, instruction=None)

    severity = min(1.0, 0.36 * len(violations) + 0.36)
    instruction = _revision_instruction(violations, persona)
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
    return " ".join(
        [*ident.core_traits, *ident.values, *ident.speech_style, *ident.knowledge_boundary]
    )


def _revision_instruction(violations: list[str], persona: PersonaCore | None) -> str:
    blob = _persona_blob(persona)
    instructions: list[str] = []
    if "fabricated_memory" in violations:
        instructions.append(_FABRICATION_INSTRUCTION)
    if "direct_care_expression" in violations:
        if "嘴硬" in blob or "不直接表达关心" in blob:
            instructions.append("保持嘴硬的表达外壳，关心只能通过行动或迂回措辞体现。")
        else:
            instructions.append("避免直接宣告关心，改用符合当前 Persona 的具体回应。")
    if "premature_apology" in violations:
        instructions.append("不要轻易道歉，按当前 Persona 的自尊与责任边界回应。")
    if "explicit_psychological_explanation" in violations:
        instructions.append("不要把心理分析、内部标签或关系机制直接说出口。")
    if "homeostatic_mechanism_leakage" in violations:
        instructions.append("只用角色自然会说的话表达，不要提及内部系统、维度、指标、数值或计算机制。")
    if "unsupported_physical_claim" in violations:
        instructions.append("没有身体传感器证据时，不要声称饥饿、疼痛或其他具体身体症状；改为表达抽象的感受、需要或边界。")
    if not instructions:
        instructions.append(_DEFAULT_INSTRUCTION)
    return "".join(instructions)
