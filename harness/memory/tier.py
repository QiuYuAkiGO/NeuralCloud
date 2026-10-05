"""Episodic lifecycle: uniqueness TTL then small-model promote/forget."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel, Field, ValidationError, field_validator

from harness.config import Settings
from harness.core.models import EpisodicEvent, MemoryTier, PersonaCore, ScoredMemory
from harness.llm.client import LLMClient, MockLLM
from harness.llm.structured import helper_options
from harness.memory.store.protocols import MemoryStores

TZ = ZoneInfo("Asia/Shanghai")

_TIER_RANK: dict[MemoryTier, int] = {
    "forgotten": -1,
    "short": 0,
    "mid": 1,
    "long": 2,
}

_COMMITMENT_RE = re.compile(r"答应|承诺|不管|说好|不会再|不用管")
_EMOTION_RE = re.compile(r"生气|失望|哭|恨|永远|再也不|背叛|死")
_MEMORY_PROBE_RE = re.compile(r"还记得|记得")
_GREETING_RE = re.compile(
    r"^(早上好|晚上好|早安|晚安|你好|嗨|哈喽|在吗|你在干嘛|在干嘛|一起吃饭吗|嗯|哦|好)[。？?！!\s]*$"
)

_SYSTEM = """Judge how memorable this event is for the character.
Return JSON only:
{"impression": number 0-1, "tier": "short"|"mid"|"long"|"forgotten",
 "importance": number 0-1, "emotional_valence": number -1-1, "reason": string}
tier:
- short: ordinary chat, logistics, greetings (only valid before uniqueness TTL)
- mid: emotionally loaded, commitments, named people, preferences worth keeping
- long: identity-defining, betrayal, core relationship turning points
- forgotten: noise with no lasting value (typos, phatic fillers)
If this is an expiry review, the uniqueness window has elapsed: choose mid, long,
or forgotten. Do not return short.
Never invent facts. If unsure on expiry, forget; if unsure before expiry, keep short.
"""


class TierVerdict(BaseModel):
    impression: float = Field(default=0.0, ge=0.0, le=1.0)
    tier: MemoryTier = "short"
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    emotional_valence: float | None = Field(default=None, ge=-1.0, le=1.0)
    reason: str = ""

    @field_validator("tier", mode="before")
    @classmethod
    def _coerce_tier(cls, value: Any) -> Any:
        if isinstance(value, str) and value.strip().lower() in {"forget", "forgotten"}:
            return "forgotten"
        return value


class TierClassifier:
    def __init__(self, llm: LLMClient, *, llm_enabled: bool = True) -> None:
        self.llm = llm
        self.llm_enabled = llm_enabled

    def classify(
        self,
        event: EpisodicEvent,
        persona: PersonaCore | None,
        *,
        on_expiry: bool = False,
    ) -> TierVerdict:
        if isinstance(self.llm, MockLLM) or not self.llm_enabled:
            return rule_verdict(event, on_expiry=on_expiry)
        user = {
            "persona": persona.identity.model_dump() if persona else None,
            "objective_fact": event.objective_fact,
            "summary": event.summary,
            "importance": event.importance,
            "emotional_valence": event.emotional_valence,
            "event_type": event.event_type,
            "uniqueness": event.uniqueness,
            "on_expiry": on_expiry,
        }
        try:
            raw = self.llm.complete_json(system=_SYSTEM, user=str(user),
                **helper_options(self.llm, TierVerdict, "memory_tier"))
            verdict = TierVerdict.model_validate(raw)
            if on_expiry and verdict.tier == "short":
                return verdict.model_copy(
                    update={"tier": "forgotten", "reason": (verdict.reason or "expiry") + ":no_promotion"}
                )
            return verdict
        except (ValueError, ValidationError, TypeError, httpx.HTTPError):
            return rule_verdict(event, on_expiry=on_expiry)


def compute_uniqueness(event: EpisodicEvent) -> float:
    """Cheap 0–1 score. Low → 5–30 min TTL; high → longer retention."""
    text = f"{event.summary} {event.objective_fact}".strip()
    stripped = event.summary.strip()
    if _GREETING_RE.match(stripped) or len(stripped) <= 2:
        return 0.05
    score = 0.12
    score += 0.35 * max(0.0, min(1.0, event.importance))
    score += 0.2 * min(1.0, abs(event.emotional_valence))
    blob = f"{text} {event.event_type or ''}"
    if _COMMITMENT_RE.search(blob) or event.event_type == "commitment_or_statement":
        score += 0.3
    if _EMOTION_RE.search(blob):
        score += 0.18
    if _MEMORY_PROBE_RE.search(blob) or event.event_type == "shared_history":
        score += 0.15
    extra = {p for p in event.participants if p not in {"user", "character"}}
    if extra:
        score += 0.1
    if len(stripped) > 24:
        score += 0.05
    return max(0.0, min(1.0, score))


def ttl_for_uniqueness(uniqueness: float, settings: Settings) -> timedelta:
    """Piecewise: 0→min, low_cutoff→low minutes, 1→max minutes."""
    u = max(0.0, min(1.0, uniqueness))
    low = max(1e-6, settings.uniqueness_low_cutoff)
    min_m = settings.uniqueness_ttl_min_minutes
    low_m = settings.uniqueness_ttl_low_minutes
    max_m = settings.uniqueness_ttl_max_minutes
    if u <= low:
        minutes = min_m + (low_m - min_m) * (u / low)
    else:
        minutes = low_m + (max_m - low_m) * ((u - low) / (1.0 - low))
    return timedelta(minutes=max(min_m, minutes))


def stamp_retention(event: EpisodicEvent, *, now: datetime, settings: Settings) -> EpisodicEvent:
    """Fill uniqueness and expire_at. Does not call the LLM."""
    uniqueness = event.uniqueness if event.uniqueness is not None else compute_uniqueness(event)
    expire_at = event.expire_at or (_ensure_tz(now) + ttl_for_uniqueness(uniqueness, settings))
    updates: dict[str, Any] = {"uniqueness": uniqueness, "expire_at": expire_at}
    if event.tier_updated_at is None:
        updates["tier_updated_at"] = now
    return event.model_copy(update=updates)


def rule_verdict(event: EpisodicEvent, *, on_expiry: bool = False) -> TierVerdict:
    """Deterministic fallback. On expiry, default short becomes forgotten."""
    blob = f"{event.summary} {event.objective_fact} {event.event_type or ''}"
    commitment = bool(_COMMITMENT_RE.search(blob))
    emotional = bool(_EMOTION_RE.search(blob)) or abs(event.emotional_valence) >= 0.5
    if event.importance >= 0.9 and (commitment or emotional):
        return TierVerdict(impression=0.9, tier="long", reason="rule:extreme_importance")
    if commitment or event.importance >= 0.75 or emotional:
        return TierVerdict(impression=0.7, tier="mid", reason="rule:salient")
    if on_expiry:
        return TierVerdict(impression=0.1, tier="forgotten", reason="rule:expiry_default")
    return TierVerdict(impression=0.2, tier="short", reason="rule:default")


def merge_tier(current: MemoryTier, proposed: MemoryTier) -> MemoryTier:
    """Hits and LLM may promote; forgotten only applies to short. No demotion."""
    if proposed == "forgotten":
        return "forgotten" if current == "short" else current
    if _TIER_RANK[proposed] > _TIER_RANK[current]:
        return proposed
    return current


def apply_verdict(event: EpisodicEvent, verdict: TierVerdict, *, now: datetime) -> EpisodicEvent:
    new_tier = merge_tier(event.tier, verdict.tier)
    if _is_qq_event(event) and event.decay_demoted and _TIER_RANK[new_tier] > _TIER_RANK[event.tier]:
        new_tier = event.tier
    updates: dict[str, Any] = {}
    if new_tier != event.tier:
        reason = verdict.reason or f"classifier:{new_tier}"
        updates["tier"] = new_tier
        updates["tier_updated_at"] = now
        updates["tier_reasons"] = [*event.tier_reasons, reason]
        if new_tier in {"mid", "long", "forgotten"}:
            updates["expire_at"] = None
    if verdict.importance is not None:
        updates["importance"] = verdict.importance
    if verdict.emotional_valence is not None:
        updates["emotional_valence"] = verdict.emotional_valence
    if not updates:
        return event
    return event.model_copy(update=updates)


def apply_local_event_hit(event: EpisodicEvent, settings: Settings, *, now: datetime) -> EpisodicEvent:
    """Pure local hit update, also usable inside a durable owner transaction.

    QQ events have their own receipt-owned reinforcement policy. The caller is
    responsible for selecting an actually used memory and deduplicating a hit.
    """
    if event.tier == "forgotten" or _is_qq_event(event):
        return event
    moment = _ensure_tz(now)
    hit_count = event.hit_count + 1
    new_tier: MemoryTier = event.tier
    reasons = list(event.tier_reasons)
    if event.tier == "short" and hit_count >= settings.tier_promotion_hits_mid:
        new_tier = "mid"
        reasons.append("hits:short_to_mid")
    elif event.tier == "mid" and hit_count >= settings.tier_promotion_hits_long:
        new_tier = "long"
        reasons.append("hits:mid_to_long")
    updates: dict[str, Any] = {"hit_count": hit_count, "last_hit_at": moment}
    if new_tier != event.tier:
        updates.update({"tier": new_tier, "tier_updated_at": moment,
                        "tier_reasons": reasons, "expire_at": None})
    elif event.tier == "short":
        uniqueness = event.uniqueness if event.uniqueness is not None else 0.0
        updates["expire_at"] = moment + ttl_for_uniqueness(uniqueness, settings)
    return event.model_copy(update=updates)


def register_hits(
    stores: MemoryStores,
    results: list[ScoredMemory],
    settings: Settings,
    *,
    now: datetime,
) -> list[tuple[str, MemoryTier, MemoryTier]]:
    """Increment hit_count on strong episodic hits and promote short→mid→long."""
    promotions: list[tuple[str, MemoryTier, MemoryTier]] = []
    min_rel = settings.tier_hit_min_relevance
    moment = _ensure_tz(now)
    for item in results:
        if item.memory_type != "episodic" or item.relevance < min_rel:
            continue
        event = stores.episodic.get(item.ref)
        if event is None:
            continue
        updated = apply_local_event_hit(event, settings, now=moment)
        if updated is event:
            # QQ receipts register only memories actually adopted by a turn.
            # Background retrieval and counterevidence must not strengthen them.
            continue
        stores.episodic.update(updated)
        if updated.tier != event.tier:
            promotions.append((event.event_id, event.tier, updated.tier))
            stores.audit.log(
                op="TIER",
                target_type="episodic",
                target_id=event.event_id,
                actor="hit_promotion",
                success=True,
                detail={"from": event.tier, "to": updated.tier, "hit_count": updated.hit_count},
            )
    return promotions


def due_for_review(event: EpisodicEvent, *, now: datetime, settings: Settings) -> bool:
    if event.tier != "short":
        return False
    moment = _ensure_tz(now)
    if event.expire_at is not None:
        return moment >= _ensure_tz(event.expire_at)
    uniqueness = event.uniqueness if event.uniqueness is not None else 0.0
    anchor = event.last_hit_at or event.tier_updated_at or event.time
    return moment >= _ensure_tz(anchor) + ttl_for_uniqueness(uniqueness, settings)


def review_expiries(
    stores: MemoryStores,
    settings: Settings,
    classifier: TierClassifier,
    persona: PersonaCore | None,
    *,
    now: datetime,
) -> list[tuple[str, MemoryTier]]:
    """At uniqueness TTL, call the small model to promote or forget."""
    moment = _ensure_tz(now)
    reviewed: list[tuple[str, MemoryTier]] = []
    for event in stores.episodic.list_all():
        if _is_qq_event(event):
            # The QQ worker owns its state transaction and processing receipts.
            continue
        if not due_for_review(event, now=moment, settings=settings):
            continue
        verdict = classifier.classify(event, persona, on_expiry=True)
        if verdict.tier == "short":
            verdict = verdict.model_copy(
                update={"tier": "forgotten", "reason": (verdict.reason or "expiry") + ":no_promotion"}
            )
        updated = apply_verdict(event, verdict, now=moment)
        if updated is event:
            updated = event.model_copy(
                update={
                    "tier": "forgotten",
                    "tier_updated_at": moment,
                    "expire_at": None,
                    "tier_reasons": [*event.tier_reasons, "expiry:no_change"],
                }
            )
        stores.episodic.update(updated)
        stores.audit.log(
            op="TIER",
            target_type="episodic",
            target_id=event.event_id,
            actor="expiry_review",
            success=True,
            detail={
                "from": event.tier,
                "to": updated.tier,
                "uniqueness": event.uniqueness,
                "reason": verdict.reason,
            },
        )
        reviewed.append((event.event_id, updated.tier))
    return reviewed


def _is_qq_event(event: EpisodicEvent) -> bool:
    scope = event.scope
    return scope is not None and (scope.channel == "qq_group" or scope.qq_private_audience is not None)


def qq_event_strength(event: EpisodicEvent, settings: Settings, *, now: datetime) -> float:
    """Compute continuous strength without a write or a model request."""
    if not _is_qq_event(event) or event.tier not in {"mid", "long"}:
        return event.strength
    anchor = event.strength_updated_at or event.tier_updated_at or event.time
    days = max(0.0, (_ensure_tz(now) - _ensure_tz(anchor)).total_seconds() / 86400.0)
    half_life = settings.qq_memory_decay_long_days if event.tier == "long" else settings.qq_memory_decay_mid_days
    return max(0.0, event.strength * 2.0 ** (-days / half_life))


def maintain_qq_events(
    events: list[EpisodicEvent],
    settings: Settings,
    *,
    now: datetime,
) -> list[EpisodicEvent]:
    """Pure QQ maintenance: short TTL, daily mid/long decay and one-step demotion.

    Demotion records a new strength anchor at 0.50 and does not immediately
    re-promote an old salient event. A new externally observed episode may clear
    ``decay_demoted`` explicitly; retrieval alone cannot do so.
    """
    moment = _ensure_tz(now)
    values: list[EpisodicEvent] = []
    for event in events:
        if not _is_qq_event(event) or event.tier == "forgotten":
            values.append(event)
            continue
        if event.tier == "short":
            if not due_for_review(event, now=moment, settings=settings):
                values.append(event)
                continue
            verdict = (
                TierVerdict(tier="forgotten", reason="qq_decay:short_expired")
                if event.decay_demoted else rule_verdict(event, on_expiry=True)
            )
            updated = apply_verdict(event, verdict, now=moment)
            values.append(updated.model_copy(update={
                "strength_updated_at": moment, "last_decay_review_at": moment,
            }))
            continue
        anchor = event.last_decay_review_at or event.strength_updated_at or event.tier_updated_at or event.time
        if moment - _ensure_tz(anchor) < timedelta(hours=settings.qq_memory_review_hours):
            values.append(event)
            continue
        strength = qq_event_strength(event, settings, now=moment)
        changes: dict[str, Any] = {
            "strength": strength, "strength_updated_at": moment, "last_decay_review_at": moment,
        }
        if strength < settings.qq_memory_decay_threshold:
            next_tier = "mid" if event.tier == "long" else "short"
            changes.update({
                "tier": next_tier, "tier_updated_at": moment, "strength": 0.50,
                "decay_demoted": True,
                "tier_reasons": [*event.tier_reasons, f"qq_decay:{event.tier}_to_{next_tier}"],
                "expire_at": moment + ttl_for_uniqueness(event.uniqueness or 0.0, settings)
                if next_tier == "short" else None,
            })
        values.append(event.model_copy(update=changes))
    return values


def reinforce_qq_event(
    event: EpisodicEvent,
    *,
    now: datetime,
    amount: float = 0.15,
    settings: Settings | None = None,
    adopted: bool = True,
) -> EpisodicEvent:
    """Strengthen a verified adoption or contrast; caller owns receipt deduping.

    Strength has headroom above its initial 1.0 so a new contrast episode can be
    more memorable. This never changes belief confidence or silently revives an
    archived event, and recall alone does not cancel a prior decay demotion.
    """
    if not 0.0 <= amount <= 1.0:
        raise ValueError("QQ reinforcement amount must be between zero and one")
    if not _is_qq_event(event) or event.tier == "forgotten" or amount == 0.0:
        return event
    settings = settings or Settings()
    moment = _ensure_tz(now)
    anchor = event.strength_updated_at or event.tier_updated_at or event.time
    if moment < _ensure_tz(anchor):
        raise ValueError("reinforcement cannot move the strength clock backwards")
    changes: dict[str, Any] = {
        "strength": min(2.0, qq_event_strength(event, settings, now=moment) + amount),
        "strength_updated_at": moment,
    }
    if adopted:
        changes.update({"last_hit_at": moment, "hit_count": event.hit_count + 1})
    if event.tier == "short":
        changes["expire_at"] = moment + ttl_for_uniqueness(event.uniqueness or 0.0, settings)
    if adopted and not event.decay_demoted:
        if event.tier == "short" and changes["hit_count"] >= settings.tier_promotion_hits_mid:
            changes.update({"tier": "mid", "expire_at": None, "tier_updated_at": moment,
                            "tier_reasons": [*event.tier_reasons, "qq_adoption:short_to_mid"]})
        elif event.tier == "mid" and changes["hit_count"] >= settings.tier_promotion_hits_long:
            changes.update({"tier": "long", "expire_at": None, "tier_updated_at": moment,
                            "tier_reasons": [*event.tier_reasons, "qq_adoption:mid_to_long"]})
    return event.model_copy(update=changes)


def _ensure_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value
