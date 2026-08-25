"""Retrieval plan from query text. LLM with rule fallback (01 §4.1)."""

from __future__ import annotations

import re

from pydantic import ValidationError

from harness.core.models import RetrievalPlan, RetrievalQuery
from harness.llm.client import LLMClient, MockLLM

_SYSTEM = """Build a memory retrieval plan.
Return JSON: {"time_scope": string|null, "entities": [string], "event_type": string|null,
"emotional_relevance": string|null, "channels": [string]}
time_scope one of: yesterday, day_before_yesterday, last_week, last_month, three_months, recent, null
Map user-stated dates to the scope that actually covers them (上个月 → last_month, not last_week).
When unsure of the true date, prefer a wider scope over a narrower one.
event_type examples: commitment_or_statement, preference, conflict, daily
channels is a hard filter: only fill it when the user explicitly names a channel
(e.g. "在聊天记录里"); otherwise return [] so memories from every channel stay reachable.
"""


class RetrievalPlanner:
    def __init__(self, llm: LLMClient, *, llm_enabled: bool = True) -> None:
        self.llm = llm
        self.llm_enabled = llm_enabled

    def plan(self, query: RetrievalQuery) -> RetrievalPlan:
        if isinstance(self.llm, MockLLM) or not self.llm_enabled:
            return rule_plan(query)
        try:
            raw = self.llm.complete_json(
                system=_SYSTEM,
                user=str({"text": query.text, "entities": query.entities}),
            )
            plan = RetrievalPlan.model_validate(raw)
            if not plan.entities:
                plan.entities = query.entities or ["user", "character"]
            return plan
        except (ValueError, ValidationError):
            return rule_plan(query)


def rule_plan(query: RetrievalQuery) -> RetrievalPlan:
    text = query.text
    time_scope = _match_time_scope(text)
    event_type = None
    emotional = None
    if re.search(r"答应|承诺|不管|说好|不会再", text):
        event_type = "commitment_or_statement"
        emotional = "high"
    elif re.search(r"喜欢|讨厌|偏好", text):
        event_type = "preference"
    elif re.search(r"雨|那场", text):
        event_type = "shared_history"
        emotional = "high"
    entities = list(query.entities) if query.entities else ["user", "character"]
    if "我" in text and "user" not in entities:
        entities.append("user")
    return RetrievalPlan(
        time_scope=time_scope,
        entities=entities,
        event_type=event_type,
        emotional_relevance=emotional,
        # Rule parsing cannot infer a channel from text; leave unfiltered.
        channels=[],
    )


def _match_time_scope(text: str) -> str | None:
    if re.search(r"昨天|昨日|yesterday", text, re.I):
        return "yesterday"
    if re.search(r"前天", text):
        return "day_before_yesterday"
    if re.search(r"上个月|上月|一个月前|last month", text, re.I):
        return "last_month"
    if re.search(r"上周|上星期|last week", text, re.I):
        return "last_week"
    if re.search(r"三个月前|三月前", text):
        return "three_months"
    if re.search(r"刚才|刚刚|刚刚才", text):
        return "recent"
    return None
