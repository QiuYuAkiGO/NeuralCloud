from __future__ import annotations

from harness.core.inference import CandidateResponse, StructuredInsight
from harness.llm.client import REVISED_IN_CHARACTER_REPLY, VIOLATING_CARE_REPLY
from harness.validator.validator import rule_check
from gui.presets import load_builtin
from tests.factories import vaelith


def test_direct_care_and_apology_are_caught() -> None:
    result = rule_check(
        VIOLATING_CARE_REPLY,
        persona=vaelith(),
        insights=[],
    )
    assert result.verdict == "revise"
    assert "direct_care_expression" in result.violations
    assert "premature_apology" in result.violations
    assert result.instruction is not None
    assert "嘴硬" in result.instruction


def test_in_character_revision_passes() -> None:
    result = rule_check(
        REVISED_IN_CHARACTER_REPLY,
        persona=vaelith(),
        insights=[StructuredInsight(constraint="avoid_explicit_psychological_explanation")],
    )
    assert result.verdict == "pass"
    assert result.violations == []


def test_psych_leak_is_caught() -> None:
    result = rule_check(
        "我检测到你正在寻求关系确认。",
        persona=vaelith(),
        insights=[],
    )
    assert result.verdict == "revise"
    assert "explicit_psychological_explanation" in result.violations


def test_misty_revision_does_not_inherit_vaelith_style() -> None:
    result = rule_check(
        "我检测到你正在寻求关系确认。",
        persona=load_builtin("misty").persona,
        insights=[],
    )
    assert result.verdict == "revise"
    assert result.instruction is not None
    assert "心理分析" in result.instruction
    assert "嘴硬" not in result.instruction
    assert "不要道歉" not in result.instruction


def test_candidate_wrapper_roundtrip() -> None:
    candidate = CandidateResponse(text=VIOLATING_CARE_REPLY, revision=0)
    result = rule_check(candidate.text, persona=vaelith(), insights=[])
    assert result.severity > 0.5


def test_fabricated_memory_caught_when_store_empty() -> None:
    result = rule_check(
        "你上次明明说过你不在乎，现在装什么？",
        persona=vaelith(),
        insights=[],
        memory_empty=True,
    )
    assert result.verdict == "revise"
    assert "fabricated_memory" in result.violations
    assert result.instruction is not None
    assert "不要引用任何共同经历" in result.instruction


def test_past_reference_allowed_when_memories_exist() -> None:
    result = rule_check(
        "你上次明明说过你不在乎，现在装什么？",
        persona=vaelith(),
        insights=[],
        memory_empty=False,
    )
    assert "fabricated_memory" not in result.violations


def test_homeostatic_mechanism_and_metrics_are_not_spoken() -> None:
    for text in (
        "我的内稳态系统显示 cognitive_load 偏离度=0.75。",
        "我的 autonomy 维度偏低，当前值是 0.2。",
        "My competence dimension is 0.2.",
        "My competence is 0.2.",
        "My autonomy is at 20%.",
        "我的胜任感是0.2。",
        "我当前的自主性为20%。",
        "我的新奇性指标偏高。",
        "我的认知负荷很高。",
        "我的安全可预测性偏低。",
        "我觉得认知负荷很高。",
        "能量储备偏低，所以我想休息。",
        "认知负荷过高。",
        "安全可预测性不足。",
        "社会连接偏低。",
        "My cognitive load is high.",
        "My energy reserve feels low.",
        "competence=0.2; autonomy: 0.3.",
        "主动推断中的 A 矩阵决定了观测映射。",
    ):
        result = rule_check(
            text,
            persona=None,
            insights=[],
            homeostatic_context=True,
        )
        assert result.verdict == "revise"
        assert "homeostatic_mechanism_leakage" in result.violations
        assert result.instruction is not None
        assert "内部系统" in result.instruction


def test_homeostatic_mechanism_is_rejected_even_without_summary_context() -> None:
    result = rule_check(
        "我的内稳态系统显示 cognitive_load=0.75。",
        persona=None,
        insights=[],
        homeostatic_context=False,
    )
    assert result.verdict == "revise"
    assert "homeostatic_mechanism_leakage" in result.violations


def test_proxy_cannot_invent_physical_symptoms_from_homeostatic_context() -> None:
    result = rule_check(
        "我现在胃很疼，也有点饿了。",
        persona=None,
        insights=[],
        homeostatic_context=True,
    )
    assert result.verdict == "revise"
    assert "unsupported_physical_claim" in result.violations


def test_proxy_cannot_invent_physical_symptoms_without_summary_context() -> None:
    result = rule_check(
        "我饿了。",
        persona=None,
        insights=[],
        homeostatic_context=False,
        concrete_physical_evidence_available=False,
    )
    assert result.verdict == "revise"
    assert "unsupported_physical_claim" in result.violations


def test_concrete_physical_claim_is_allowed_with_matching_sensor_evidence() -> None:
    result = rule_check(
        "我饿了。",
        persona=None,
        insights=[],
        homeostatic_context=False,
        concrete_physical_evidence_available=True,
    )
    assert result.verdict == "pass"


def test_proxy_cannot_invent_elliptical_or_english_physical_symptoms() -> None:
    for text in (
        "饿了，先吃点东西。",
        "我感到头痛。",
        "我的肚子很饿。",
        "肚子很饿，先停一下。",
        "I'm hungry, so I need a break.",
        "I am famished.",
        "I have a headache.",
        "I have a migraine.",
        "I feel a sharp pain in my stomach.",
        "My stomach is growling.",
        "There is a sharp pain in my chest.",
    ):
        result = rule_check(
            text,
            persona=None,
            insights=[],
            homeostatic_context=True,
        )
        assert result.verdict == "revise"
        assert "unsupported_physical_claim" in result.violations


def test_proxy_cannot_invent_breathing_circulation_or_injury_claims() -> None:
    for text in (
        "I cannot breathe.",
        "I'm short of breath.",
        "I have trouble breathing.",
        "My heart is racing.",
        "My hands are shaking.",
        "I am bleeding.",
        "I feel cold.",
        "我喘不过气。",
        "我的心跳很快。",
        "我的双手在发抖。",
        "我在流血。",
        "我觉得冷。",
        "我觉得热。",
        "我觉得渴了。",
        "我发烧两天了。",
        "我恶心想吐。",
    ):
        result = rule_check(
            text,
            persona=None,
            insights=[],
            concrete_physical_evidence_available=False,
        )
        assert result.verdict == "revise", text
        assert "unsupported_physical_claim" in result.violations


def test_physical_check_does_not_treat_a_user_question_as_the_proxy_body() -> None:
    result = rule_check(
        "你的头疼缓解了吗？",
        persona=None,
        insights=[],
        homeostatic_context=True,
    )
    assert result.verdict == "pass"


def test_short_physical_words_do_not_match_longer_nonphysical_words() -> None:
    for text in (
        "我现在很冷静。",
        "我对这件事依然很热情。",
        "这里今天很热闹。",
        "我渴望把这件事做好。",
        "饥饿营销是常见策略。",
        "眩晕特效可以关闭。",
        "头晕是这个游戏的状态效果。",
        "发热量需要测量。",
        "流血效果做得很假。",
    ):
        result = rule_check(
            text,
            persona=None,
            insights=[],
            concrete_physical_evidence_available=False,
        )
        assert result.verdict == "pass", text


def test_elliptical_physical_claim_requires_symptom_syntax() -> None:
    for text in (
        "饿了，先吃点东西。",
        "头晕，先让我休息一下。",
        "发烧两天了。",
        "恶心想吐，先缓一缓。",
        "流血不止，需要处理。",
    ):
        result = rule_check(
            text,
            persona=None,
            insights=[],
            concrete_physical_evidence_available=False,
        )
        assert result.verdict == "revise", text
        assert "unsupported_physical_claim" in result.violations


def test_natural_autonomy_word_is_not_an_internal_mechanism_leak() -> None:
    for text in (
        "I need a little more autonomy in how we approach this.",
        "我可以解释 homeostasis 在生物学里是什么意思。",
        "自主性水平很重要，但怎么做应该由你决定。",
        "自主性很重要，但不应把它量化为单一分数。",
        "Your competence score improved after practice.",
        "A matrix is rectangular, and a B matrix can represent the transform.",
    ):
        result = rule_check(
            text,
            persona=None,
            insights=[],
            homeostatic_context=True,
        )
        assert result.verdict == "pass"


def test_generic_low_resource_expression_remains_allowed() -> None:
    result = rule_check(
        "我现在精力有点低，想把事情说简单一点。",
        persona=None,
        insights=[],
        homeostatic_context=True,
    )
    assert result.verdict == "pass"


def test_abstract_emotional_pain_is_not_misclassified_as_body_evidence() -> None:
    result = rule_check(
        "这让我有点痛苦，但我想先安静一下。",
        persona=None,
        insights=[],
        homeostatic_context=True,
    )
    assert result.verdict == "pass"


def test_marked_context_does_not_force_explicit_feeling_statement() -> None:
    result = rule_check(
        "嗯，我先安静一会儿。",
        persona=None,
        insights=[],
        homeostatic_context=True,
    )
    assert result.verdict == "pass"
