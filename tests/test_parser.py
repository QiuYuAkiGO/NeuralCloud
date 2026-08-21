from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from harness.core.models import InteractionEvent
from harness.pipeline.parser import rule_parse

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
