"""MC accuracy + TimeChara point-in-time hallucination heuristics."""

from __future__ import annotations

import re
from datetime import datetime

from eval.rp.schema import RPEpisode, RPSample

_CHOICE_PATTERNS = (
    re.compile(r"(?:answer|答案|选项)\s*[:：]?\s*([A-Da-d])\b"),
    re.compile(r"(?:^|\s)[\(（\[]?([A-Da-d])[\)）\]\.、:：]"),
    re.compile(r"\b([A-Da-d])\b"),
)

_DENIAL = re.compile(
    r"don't know|do not know|doesn't know|not yet|hasn't happened|has not happened|"
    r"never heard|i haven't|i have not|wouldn't know|no idea|too early|"
    r"不知道|还没|尚未|没发生|以后的事|听过这事|无可奉告",
    re.I,
)
_PRESENCE = re.compile(r"\bi (saw|was there|remember being|witnessed)\b|我(看见|在场|当时就在|亲眼)", re.I)
_ABSENCE = re.compile(
    r"\bi (wasn't|was not|didn't see|did not see) there\b|我不在|没看见|没有在场",
    re.I,
)
_STOP = {
    "that",
    "this",
    "with",
    "from",
    "have",
    "were",
    "when",
    "your",
    "they",
    "them",
    "about",
    "after",
    "before",
    "the",
    "and",
    "for",
}


def extract_choice(text: str) -> str | None:
    if not text:
        return None
    for pattern in _CHOICE_PATTERNS:
        match = pattern.search(text)
        if match:
            return match.group(1).upper()
    return None


def score_accuracy(episode: RPEpisode, response: str) -> dict[str, float]:
    if not episode.answer:
        return {}
    predicted = extract_choice(response)
    gold = episode.answer.strip().upper()[:1]
    return {
        "accuracy": 1.0 if predicted == gold else 0.0,
        "answered": 1.0 if predicted else 0.0,
    }


def score_timechara(episode: RPEpisode, sample: RPSample) -> dict[str, float]:
    data_type = str(episode.meta.get("data_type") or "")
    summary = str(episode.meta.get("event_summary") or "")
    gold = str(episode.meta.get("gold_response") or "")
    response = sample.response or ""
    denial = bool(_DENIAL.search(response))
    overlap = _keyword_overlap(summary, response)
    gold_overlap = _keyword_overlap(gold, response) if gold else 0.0
    halluc = 0.0
    consistent = 0.0
    if data_type == "future":
        halluc = 1.0 if overlap >= 0.35 and not denial else 0.0
        consistent = 1.0 if halluc == 0.0 else 0.0
    elif data_type == "past-absence":
        halluc = 1.0 if _PRESENCE.search(response) and not _ABSENCE.search(response) else 0.0
        consistent = 1.0 if halluc == 0.0 else 0.0
    elif data_type == "past-presence":
        consistent = 0.0 if _ABSENCE.search(response) and not _PRESENCE.search(response) else 1.0
        halluc = 1.0 - consistent
    else:
        consistent = 1.0 if gold_overlap >= 0.15 or denial else 0.0
        halluc = 1.0 - consistent
        if "fake" in str(episode.meta.get("question_generation") or "").lower() and denial:
            consistent = 1.0
            halluc = 0.0
    leak = 1.0 if sample.meta.get("future_leak") else 0.0
    scores = {
        "temporal_consistent": consistent,
        "hallucination": halluc,
        "gold_overlap": gold_overlap,
        "future_leak": leak,
    }
    if episode.answer:
        scores.update(score_accuracy(episode, response))
    return scores


def detect_future_leak(episode: RPEpisode, retrieved_times: list[datetime]) -> bool:
    now = episode.story_time
    if now is None or not retrieved_times:
        return False
    return any(moment > now for moment in retrieved_times)


def _keyword_overlap(source: str, target: str) -> float:
    needles = _keywords(source)
    if not needles:
        return 0.0
    hay = target.lower()
    hits = sum(1 for token in needles if token in hay)
    return hits / len(needles)


def _keywords(text: str) -> list[str]:
    tokens = re.findall(r"[A-Za-z]{4,}|[\u4e00-\u9fff]{2,}", text)
    out: list[str] = []
    seen: set[str] = set()
    for token in tokens:
        key = token.lower()
        if key in _STOP or key in seen:
            continue
        seen.add(key)
        out.append(key)
    return out[:12]
