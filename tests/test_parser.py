from __future__ import annotations

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from harness.config import Settings
from harness.core.models import InteractionEvent
from harness.pipeline.parser import ContextParser, rule_parse

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _event(text: str) -> InteractionEvent:
    return InteractionEvent(
        timestamp=NOW,
        speaker="user",
        raw_text=text,
        session_id="sess_0881",
    )


def test_running_example_is_hypothesis_not_verdict() -> None:
    parsed = rule_parse(_event("你昨天不是说不会再管我了吗？"))
    assert parsed.speech_act == "question"
    assert parsed.references
    assert parsed.references[0].time_hint == "yesterday"
    assert parsed.references[0].about == "character_statement"
    assert parsed.possible_social_intent is not None
    assert parsed.possible_social_intent.value == "seeking_reassurance"
    assert parsed.possible_social_intent.confidence == 0.58
    assert parsed.emotion_cues["hurt"] == 0.5
    assert parsed.emotion_cues["testing"] == 0.3


def test_greeting_is_phatic() -> None:
    parsed = rule_parse(_event("早上好。"))
    assert parsed.speech_act == "greeting"
    assert parsed.possible_social_intent is None


def test_value_dilemma_stays_a_hypothesis() -> None:
    parsed = rule_parse(_event("如果救我意味着放弃你一直追求的目标呢？"))
    assert parsed.speech_act == "question"
    assert parsed.possible_social_intent is not None
    assert parsed.possible_social_intent.value == "values_dilemma"


class _StubParserLLM:
    """Not a MockLLM, so ContextParser takes the live path."""

    def __init__(self, payload: dict[str, Any] | None = None, *, raises: bool = False) -> None:
        self.payload = payload if payload is not None else {}
        self.raises = raises
        self.calls = 0
        self.last_max_tokens: int | None = None

    def complete_json(self, *, system: str, user: str, max_tokens: int | None = None) -> dict:
        self.calls += 1
        self.last_max_tokens = max_tokens
        if self.raises:
            raise ValueError("boom")
        return dict(self.payload)


def test_trivial_greeting_skips_llm() -> None:
    llm = _StubParserLLM()
    parser = ContextParser(llm, settings=Settings())
    parsed = parser.parse(_event("早上好。"))
    assert parsed.speech_act == "greeting"
    assert llm.calls == 0


def test_short_signal_free_statement_skips_llm() -> None:
    llm = _StubParserLLM()
    parser = ContextParser(llm, settings=Settings())
    parsed = parser.parse(_event("嗯。"))
    assert parsed.speech_act == "statement"
    assert llm.calls == 0


def test_third_party_name_forces_llm_despite_short_text() -> None:
    llm = _StubParserLLM(
        {
            "speech_act": "question",
            "references": [],
            "possible_social_intent": None,
            "emotion_cues": {},
            "entities": ["user", "character", "鸽子少女"],
        }
    )
    parser = ContextParser(llm, settings=Settings())
    parsed = parser.parse(_event("之前和鸽子少女吵架了吗？"))
    assert llm.calls == 1
    assert "鸽子少女" in parsed.entities


def test_pronoun_after_he_still_skips_llm() -> None:
    llm = _StubParserLLM()
    parser = ContextParser(llm, settings=Settings())
    parsed = parser.parse(_event("和我一起吃饭吗"))
    assert parsed.speech_act == "question"
    assert llm.calls == 0


def test_signaled_utterance_calls_llm_and_reads_gate_vote() -> None:
    llm = _StubParserLLM(
        {
            "speech_act": "question",
            "references": [{"type": "past_event", "time_hint": "yesterday", "about": "character_statement"}],
            "possible_social_intent": {"value": "seeking_reassurance", "confidence": 0.7},
            "emotion_cues": {"hurt": 0.6, "testing": 0.4},
            "entities": ["user", "character"],
            "gate": {"path": "deep", "reason": "commitment probe"},
        }
    )
    parser = ContextParser(llm, settings=Settings(llm_json_max_tokens=320))
    parsed = parser.parse(_event("你昨天不是说不会再管我了吗？"))
    assert llm.calls == 1
    assert llm.last_max_tokens == 320
    assert parsed.gate_vote == "deep"
    assert parsed.possible_social_intent is not None
    assert parsed.possible_social_intent.about == "user_intent"


def test_llm_failure_falls_back_to_rule_baseline() -> None:
    llm = _StubParserLLM(raises=True)
    parser = ContextParser(llm, settings=Settings())
    parsed = parser.parse(_event("你昨天不是说不会再管我了吗？"))
    assert llm.calls == 1
    assert parsed.speech_act == "question"
    assert parsed.possible_social_intent is not None
    assert parsed.possible_social_intent.value == "seeking_reassurance"
    assert parsed.gate_vote is None


def test_parser_llm_disabled_runs_rule_only() -> None:
    llm = _StubParserLLM()
    parser = ContextParser(llm, settings=Settings(parser_llm_enabled=False))
    parsed = parser.parse(_event("你昨天不是说不会再管我了吗？"))
    assert llm.calls == 0
    assert parsed.speech_act == "question"
