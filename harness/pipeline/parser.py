"""Context Parser: structure the utterance, never adjudicate social meaning (02 §阶段 2)."""

from __future__ import annotations

import re

from pydantic import ValidationError

from harness.core.inference import Hypothesis, ParsedInteraction, Reference
from harness.core.models import InteractionEvent
from harness.llm.client import LLMClient, MockLLM
from harness.memory.retrieval.planner import _match_time_scope

_SYSTEM = """Parse a character-AI user utterance. Do not declare facts about the speaker's feelings.
Return JSON:
{"speech_act": "question|statement|greeting|request",
 "references": [{"type": "past_event", "time_hint": string|null, "about": string|null}],
 "possible_social_intent": {"about": "user_intent", "value": string, "confidence": number}|null,
 "emotion_cues": {"hurt": number, "testing": number},
 "entities": ["user", "character"]}
Output hypothesis + confidence only. Never say the user "is" doing something.
"""

_QUESTION_RE = re.compile(r"[?？]|吗|呢|是不是|不是说")
_COMMITMENT_RE = re.compile(r"答应|承诺|不管|说好|不会再|说了")
_MEMORY_PROBE_RE = re.compile(r"还记得|记得")
_VALUE_RE = re.compile(r"如果.{0,24}意味着|放弃.{0,12}目标|救我")
_GREETING_RE = re.compile(
    r"^(早上好|晚上好|早安|晚安|你好|嗨|哈喽|在吗|你在干嘛|在干嘛|一起吃饭吗)[。？?！!\s]*$"
)
_HURT_MARKERS = ("不管我", "不会再管", "不要我", "不理我", "不管了")


class ContextParser:
    def __init__(self, llm: LLMClient | None = None) -> None:
        self.llm = llm or MockLLM()

    def parse(self, event: InteractionEvent) -> ParsedInteraction:
        if isinstance(self.llm, MockLLM):
            return rule_parse(event)
        try:
            raw = self.llm.complete_json(
                system=_SYSTEM,
                user=str({"text": event.raw_text, "speaker": event.speaker}),
            )
            parsed = _coerce_parsed(raw)
            if not parsed.entities:
                parsed.entities = ["user", "character"]
            return parsed
        except (ValueError, ValidationError, TypeError, KeyError):
            return rule_parse(event)


def rule_parse(event: InteractionEvent) -> ParsedInteraction:
    text = event.raw_text.strip()
    is_greeting = bool(_GREETING_RE.match(text))
    is_question = bool(_QUESTION_RE.search(text))
    if is_greeting:
        speech_act = "greeting"
    elif is_question:
        speech_act = "question"
    else:
        speech_act = "statement"

    references: list[Reference] = []
    time_hint = _match_time_scope(text)
    if time_hint:
        about = "character_statement" if _COMMITMENT_RE.search(text) else "past_event"
        references.append(Reference(type="past_event", time_hint=time_hint, about=about))

    intent: Hypothesis | None = None
    if _VALUE_RE.search(text):
        intent = Hypothesis(about="user_intent", value="values_dilemma", confidence=0.62)
    elif _COMMITMENT_RE.search(text) and is_question:
        intent = Hypothesis(about="user_intent", value="seeking_reassurance", confidence=0.58)
    elif _MEMORY_PROBE_RE.search(text):
        intent = Hypothesis(about="user_intent", value="memory_check", confidence=0.6)

    emotion_cues: dict[str, float] = {}
    if any(marker in text for marker in _HURT_MARKERS) or ("不管" in text and is_question):
        emotion_cues = {"hurt": 0.5, "testing": 0.3}

    return ParsedInteraction(
        speech_act=speech_act,
        references=references,
        possible_social_intent=intent,
        emotion_cues=emotion_cues,
        entities=["user", "character"],
    )


def _coerce_parsed(raw: dict) -> ParsedInteraction:
    payload = raw.get("parsed", raw)
    intent = payload.get("possible_social_intent")
    if isinstance(intent, dict) and "about" not in intent:
        intent = {**intent, "about": "user_intent"}
        payload = {**payload, "possible_social_intent": intent}
    return ParsedInteraction.model_validate(payload)
