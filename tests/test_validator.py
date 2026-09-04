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
