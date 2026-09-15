"""Six retrieval signals. Weights come from Settings, not call sites."""

from __future__ import annotations

import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from harness.config import Settings, get_settings
from harness.core.models import (
    EpisodicEvent,
    PersonaCore,
    RelationshipState,
    RetrievalPlan,
    RetrievalQuery,
    ScoreBreakdown,
    SemanticFact,
)
from harness.memory.store.vector_util import cosine

TZ = ZoneInfo("Asia/Shanghai")


def time_window(plan: RetrievalPlan, now: datetime) -> tuple[datetime | None, datetime | None]:
    now = _ensure_tz(now)
    scope = plan.time_scope
    if scope == "yesterday":
        local = now.astimezone(TZ)
        start_local = (local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = local.replace(hour=0, minute=0, second=0, microsecond=0)
        # include late-night "yesterday" utterances slightly into today for chat timezone fuzz
        return start_local, end_local + timedelta(hours=4)
    if scope == "day_before_yesterday":
        local = now.astimezone(TZ)
        start_local = (local - timedelta(days=2)).replace(hour=0, minute=0, second=0, microsecond=0)
        end_local = (local - timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        return start_local, end_local + timedelta(hours=4)
    if scope == "last_month":
        # wide window: user-stated dates are unreliable, ranking is recency's job
        return now - timedelta(days=62), now
    if scope == "last_week":
        return now - timedelta(days=7), now
    if scope == "three_months":
        return now - timedelta(days=90), now
    if scope == "recent":
        return now - timedelta(hours=6), now
    return None, None


def score_episodic(
    event: EpisodicEvent,
    *,
    query: RetrievalQuery,
    plan: RetrievalPlan,
    query_embedding: list[float] | None,
    settings: Settings | None = None,
) -> tuple[ScoreBreakdown, list[str]]:
    settings = settings or get_settings()
    weights = settings.retrieval_weights
    semantic = cosine(query_embedding, event.embedding)
    entity = _entity_score(event.participants, plan.entities or query.entities)
    recency = _recency_score(
        event.time,
        query.now,
        settings.recency_half_life_hours,
        plan.time_scope,
        tier=event.tier,
        long_floor=settings.long_term_recency_floor,
        mid_factor=settings.mid_term_half_life_factor,
    )
    importance = max(0.0, min(1.0, event.importance))
    relationship = _relationship_score(event, query.relationship)
    persona = _persona_score(event.summary + " " + event.objective_fact, query.persona)
    total = (
        weights["semantic"] * semantic
        + weights["entity"] * entity
        + weights["recency"] * recency
        + weights["importance"] * importance
        + weights["relationship"] * relationship
        + weights["persona"] * persona
    )
    why = _why_tags(
        event,
        plan=plan,
        semantic=semantic,
        entity=entity,
        recency=recency,
        query=query,
    )
    return (
        ScoreBreakdown(
            semantic=semantic,
            entity=entity,
            recency=recency,
            importance=importance,
            relationship=relationship,
            persona=persona,
            total=total,
        ),
        why,
    )


def score_semantic(
    fact: SemanticFact,
    *,
    query: RetrievalQuery,
    plan: RetrievalPlan,
    query_embedding: list[float] | None,
    settings: Settings | None = None,
) -> tuple[ScoreBreakdown, list[str]]:
    settings = settings or get_settings()
    weights = settings.retrieval_weights
    blob = f"{fact.subject} {fact.predicate} {fact.value}"
    semantic = cosine(query_embedding, fact.embedding)
    entity = _entity_score([fact.subject], plan.entities or query.entities)
    recency = 0.4
    importance = fact.confidence
    relationship = 0.5 if query.relationship and fact.subject == query.relationship.target else 0.2
    persona = _persona_score(blob, query.persona)
    total = (
        weights["semantic"] * semantic
        + weights["entity"] * entity
        + weights["recency"] * recency
        + weights["importance"] * importance
        + weights["relationship"] * relationship
        + weights["persona"] * persona
    )
    why = [f"entity:{fact.subject}"] if entity > 0 else []
    if semantic > 0.3:
        why.append("semantic")
    return (
        ScoreBreakdown(
            semantic=semantic,
            entity=entity,
            recency=recency,
            importance=importance,
            relationship=relationship,
            persona=persona,
            total=total,
        ),
        why,
    )


def _entity_score(participants: list[str], entities: list[str]) -> float:
    if not entities:
        return 0.0
    hay = {item.lower() for item in participants}
    hits = sum(1 for entity in entities if entity.lower() in hay)
    return hits / len(entities)


def _recency_score(
    event_time: datetime,
    now: datetime,
    half_life_hours: float,
    time_scope: str | None,
    *,
    tier: str = "short",
    long_floor: float = 0.4,
    mid_factor: float = 7.0,
) -> float:
    now = _ensure_tz(now)
    event_time = _ensure_tz(event_time)
    hours = max(0.0, (now - event_time).total_seconds() / 3600.0)
    half = half_life_hours * mid_factor if tier == "mid" else half_life_hours
    decay = math.exp(-math.log(2) * hours / max(half, 1e-6))
    if time_scope == "yesterday" and 8 <= hours <= 40:
        decay = max(decay, 0.85)
    decay = max(0.0, min(1.0, decay))
    if tier == "long":
        return max(decay, long_floor)
    return decay


def _relationship_score(event: EpisodicEvent, relationship: RelationshipState | None) -> float:
    if relationship is None:
        return 0.3
    if relationship.target not in event.participants and relationship.target != "user":
        return 0.2
    closeness = relationship.closeness
    tension = relationship.recent_tension
    return max(0.0, min(1.0, 0.5 * closeness + 0.5 * tension))


def _persona_score(text: str, persona: PersonaCore | None) -> float:
    if persona is None:
        return 0.2
    needles = persona.identity.core_traits + persona.identity.values
    if not needles:
        return 0.2
    hits = sum(1 for item in needles if item and item in text)
    return min(1.0, 0.2 + 0.2 * hits)


def _why_tags(
    event: EpisodicEvent,
    *,
    plan: RetrievalPlan,
    semantic: float,
    entity: float,
    recency: float,
    query: RetrievalQuery,
) -> list[str]:
    tags: list[str] = []
    for participant in event.participants:
        if participant in (plan.entities or query.entities) or participant == "user":
            tags.append(f"entity:{participant}")
    if plan.time_scope:
        window = time_window(plan, query.now)
        if window[0] is None or (window[0] <= _ensure_tz(event.time) <= (window[1] or query.now)):
            tags.append(f"time:{plan.time_scope}")
    if plan.event_type == "commitment_or_statement" and _is_commitment(event):
        tags.append("commitment_conflict")
    if semantic > 0.35:
        tags.append("semantic")
    if event.importance >= 0.6:
        tags.append("importance")
    if recency >= 0.5:
        tags.append("recency")
    tags.append(f"tier:{event.tier}")
    # unique preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for tag in tags:
        if tag not in seen:
            seen.add(tag)
            ordered.append(tag)
    return ordered


def _is_commitment(event: EpisodicEvent) -> bool:
    blob = f"{event.summary} {event.objective_fact} {event.event_type or ''}"
    return event.event_type == "commitment_or_statement" or bool(
        re.search(r"不管|承诺|答应|说好|不用管", blob)
    )


def _ensure_tz(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=TZ)
    return value
