"""Debug-panel mutations for the memory subsystem. All writes are audited."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from harness.config import DEFAULT_RETRIEVAL_WEIGHTS, Settings
from harness.core.exceptions import MemoryNotFoundError
from harness.core.ids import new_id
from harness.core.models import EpisodicEvent, PsychState, default_event_participants
from harness.memory.service import MemoryService

DEBUG_ACTOR = "debug_panel"
DEFAULT_SESSION_ID = "sess_0881"
WEIGHT_KEYS = tuple(DEFAULT_RETRIEVAL_WEIGHTS)
WEIGHT_SUM_TOLERANCE = 0.01
_EXCLUDE_EMBEDDING = {"embedding"}
_SHORT_TRANSIENT = {"set_at", "expired"}


class RelationshipPatch(BaseModel):
    target: str = "user"
    closeness: float | None = Field(default=None, ge=0.0, le=1.0)
    trust: float | None = Field(default=None, ge=0.0, le=1.0)
    attachment: float | None = Field(default=None, ge=0.0, le=1.0)
    recent_tension: float | None = Field(default=None, ge=0.0, le=1.0)


class PsychPatch(BaseModel):
    character_id: str = "character"
    long: dict[str, Any] | None = None
    mid: dict[str, Any] | None = None
    short: dict[str, Any] | None = None


class PersonaPatch(BaseModel):
    name: str | None = None
    core_traits: list[str] | None = None
    values: list[str] | None = None
    speech_style: list[str] | None = None
    knowledge_boundary: list[str] | None = None


class EpisodicPatch(BaseModel):
    event_id: str | None = None
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    emotional_valence: float | None = Field(default=None, ge=-1.0, le=1.0)
    summary: str | None = None
    objective_fact: str | None = None
    insight: str | None = None
    insight_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    character_action: str | None = None
    outcome: str | None = None
    event_type: str | None = None
    speaker: str = "user"
    session_id: str | None = None
    regenerate_insight: bool = False
    curate: bool = False


class SemanticPatch(BaseModel):
    fact_id: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    value: str | None = None


class CandidatePatch(BaseModel):
    candidate_id: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    status: Literal["candidate", "consolidated", "dropped"] | None = None


class TuningPatch(BaseModel):
    retrieval_weights: dict[str, float] | None = None
    normalize: bool = False
    consolidation_threshold: float | None = Field(default=None, ge=0.0, le=1.0)
    initial_candidate_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    confidence_increment: float | None = Field(default=None, ge=0.0, le=1.0)
    retrieval_pool_size: int | None = Field(default=None, ge=1, le=500)
    retrieval_top_k: int | None = Field(default=None, ge=1, le=50)
    recency_half_life_hours: float | None = Field(default=None, gt=0.0, le=720.0)


def collect_debug_state(memory: MemoryService, settings: Settings | None = None) -> dict[str, Any]:
    settings = settings or memory.settings
    snap = memory.get_state()
    psych_raw = memory.stores.psych.get("character")
    return {
        "persona": _dump(snap.persona),
        "relationship": _dump(snap.relationship),
        "psych": _dump(psych_raw),
        "short_psych_expired": snap.short_psych_expired,
        "world": _dump(snap.world),
        "episodic": [_dump(item, exclude=_EXCLUDE_EMBEDDING) for item in _sorted_episodic(memory)],
        "semantic": [_dump(item, exclude=_EXCLUDE_EMBEDDING) for item in memory.stores.semantic.list_all()],
        "candidates": [_dump(item) for item in memory.stores.candidates.list_all()],
        "tuning": _tuning_payload(settings),
    }


def apply_relationship(memory: MemoryService, patch: RelationshipPatch) -> Any:
    current = memory.stores.relationship.get(patch.target)
    baseline_closeness = current.closeness if current is not None else 0.5
    baseline_trust = current.trust if current is not None else 0.5
    baseline_attachment = current.attachment if current is not None else 0.5
    baseline_tension = current.recent_tension if current is not None else 0.0
    updated = memory.apply_relationship_delta(
        patch.target,
        closeness=_delta(patch.closeness, baseline_closeness),
        trust=_delta(patch.trust, baseline_trust),
        attachment=_delta(patch.attachment, baseline_attachment),
        recent_tension=_delta(patch.recent_tension, baseline_tension),
    )
    _log_debug(
        memory,
        op="UPDATE",
        target_type="relationship",
        target_id=updated.relation_id,
        detail={"target": patch.target, "patch": patch.model_dump(exclude_none=True)},
    )
    return updated


def apply_psych(memory: MemoryService, patch: PsychPatch) -> PsychState:
    current = memory.stores.psych.get(patch.character_id)
    if current is None:
        current = PsychState(character_id=patch.character_id, updated_at=datetime.now(UTC))
    long = dict(patch.long) if patch.long is not None else dict(current.long)
    mid = dict(patch.mid) if patch.mid is not None else dict(current.mid)
    if patch.short is not None:
        short = {key: value for key, value in patch.short.items() if key not in _SHORT_TRANSIENT}
    else:
        short = dict(current.short)
    state = PsychState(
        character_id=patch.character_id,
        long=long,
        mid=mid,
        short=short,
        updated_at=current.updated_at,
    )
    updated = memory.upsert_psych(state)
    _log_debug(
        memory,
        op="UPDATE",
        target_type="psych",
        target_id=patch.character_id,
        detail={"patch": patch.model_dump(exclude_none=True)},
    )
    return updated


def apply_persona(memory: MemoryService, patch: PersonaPatch) -> Any:
    persona = memory.get_persona()
    if persona is None:
        raise MemoryNotFoundError("persona not found")
    force_update = getattr(memory.stores.persona, "force_update", None)
    if not callable(force_update):
        raise NotImplementedError("persona debug bypass is only available on the in-memory store")
    identity_updates: dict[str, Any] = {}
    if patch.name is not None:
        identity_updates["name"] = patch.name
    if patch.core_traits is not None:
        identity_updates["core_traits"] = patch.core_traits
    if patch.values is not None:
        identity_updates["values"] = patch.values
    if patch.speech_style is not None:
        identity_updates["speech_style"] = patch.speech_style
    if patch.knowledge_boundary is not None:
        identity_updates["knowledge_boundary"] = patch.knowledge_boundary
    identity = persona.identity.model_copy(update=identity_updates)
    updated = persona.model_copy(update={"identity": identity})
    force_update(updated)
    stored = memory.get_persona(persona.persona_id) or memory.get_persona()
    _log_debug(
        memory,
        op="UPDATE",
        target_type="persona_core",
        target_id=persona.persona_id,
        detail={"bypass": "persona_lock", "patch": patch.model_dump(exclude_none=True)},
    )
    return stored


def apply_episodic(memory: MemoryService, patch: EpisodicPatch) -> Any:
    if not patch.event_id:
        return _create_episodic(memory, patch)
    event = memory.stores.episodic.get(patch.event_id)
    if event is None:
        raise MemoryNotFoundError(f"episodic event not found: {patch.event_id}")
    updates: dict[str, Any] = {}
    if patch.importance is not None:
        updates["importance"] = patch.importance
    if patch.emotional_valence is not None:
        updates["emotional_valence"] = patch.emotional_valence
    if patch.summary is not None:
        updates["summary"] = patch.summary
    if patch.objective_fact is not None:
        updates["objective_fact"] = patch.objective_fact
    if patch.character_action is not None:
        updates["character_action"] = patch.character_action or None
    if patch.outcome is not None:
        updates["outcome"] = patch.outcome or None
    if patch.event_type is not None:
        updates["event_type"] = patch.event_type or None
    if patch.insight is not None and not patch.regenerate_insight:
        updates["persona_conditioned_insight"] = patch.insight or None
    if patch.insight_confidence is not None and not patch.regenerate_insight:
        updates["insight_confidence"] = patch.insight_confidence
    updated = event.model_copy(update=updates)
    text_changed = any(key in updates for key in ("summary", "objective_fact"))
    if text_changed:
        updated = updated.model_copy(
            update={"embedding": memory.embeddings.embed_one(f"{updated.summary} {updated.objective_fact}")}
        )
    if patch.regenerate_insight:
        updated = _apply_insight(memory, updated, raise_on_error=True)
    memory.stores.episodic.update(updated)
    _log_debug(
        memory,
        op="UPDATE",
        target_type="episodic",
        target_id=updated.event_id,
        detail={"patch": patch.model_dump(exclude_none=True)},
    )
    if patch.curate:
        memory.curate(updated)
        updated = memory.stores.episodic.get(updated.event_id) or updated
    return updated


def _create_episodic(memory: MemoryService, patch: EpisodicPatch) -> EpisodicEvent:
    from gui.presets import DEFAULT_SESSION_ID

    summary = (patch.summary or "").strip()
    if not summary:
        raise ValueError("summary is required to create an event")
    fact = (patch.objective_fact or summary).strip() or summary
    event_id = new_id("event")
    session_id = (patch.session_id or "").strip() or DEFAULT_SESSION_ID
    speaker = (patch.speaker or "user").strip() or "user"
    evidence = [f"debug:{session_id}:{event_id}"]
    event = EpisodicEvent(
        event_id=event_id,
        time=datetime.now(UTC),
        participants=default_event_participants(speaker),
        summary=summary,
        character_action=patch.character_action or None,
        outcome=patch.outcome or None,
        importance=patch.importance if patch.importance is not None else 0.5,
        emotional_valence=patch.emotional_valence if patch.emotional_valence is not None else 0.0,
        evidence=evidence,
        objective_fact=fact,
        persona_conditioned_insight=None if patch.regenerate_insight else (patch.insight or None),
        insight_confidence=None if patch.regenerate_insight else patch.insight_confidence,
        supporting_evidence=list(evidence),
        channel="chat",
        event_type=patch.event_type or None,
        session_id=session_id,
    )
    if patch.regenerate_insight:
        event = _apply_insight(memory, event, raise_on_error=True)
    created = memory.record_event(event, generate_insight=False, curate=patch.curate)
    _log_debug(
        memory,
        op="ADD",
        target_type="episodic",
        target_id=created.event_id,
        detail={"patch": patch.model_dump(exclude_none=True), "source": "debug_create"},
    )
    return created


def _apply_insight(memory: MemoryService, event: EpisodicEvent, *, raise_on_error: bool) -> EpisodicEvent:
    try:
        dual = memory.dual.enrich(event, memory.stores.persona.get(), raise_on_error=raise_on_error)
    except Exception as exc:
        raise ValueError(f"regenerate insight failed: {exc}") from exc
    return event.model_copy(
        update={
            "persona_conditioned_insight": dual.persona_conditioned_insight,
            "insight_confidence": dual.insight_confidence,
            "supporting_evidence": dual.supporting_evidence or list(event.evidence),
        }
    )


def apply_semantic(memory: MemoryService, patch: SemanticPatch) -> Any:
    fact = memory.stores.semantic.get(patch.fact_id)
    if fact is None:
        raise MemoryNotFoundError(f"semantic fact not found: {patch.fact_id}")
    updates: dict[str, Any] = {}
    if patch.confidence is not None:
        updates["confidence"] = patch.confidence
    if patch.value is not None:
        updates["value"] = patch.value
    updated = fact.model_copy(update=updates)
    if patch.value is not None:
        updated = updated.model_copy(
            update={"embedding": memory.embeddings.embed_one(f"{updated.subject} {updated.predicate} {updated.value}")}
        )
    memory.stores.semantic.update(updated)
    _log_debug(
        memory,
        op="UPDATE",
        target_type="semantic",
        target_id=updated.fact_id,
        detail={"patch": patch.model_dump(exclude_none=True)},
    )
    return updated


def apply_candidate(memory: MemoryService, patch: CandidatePatch) -> Any:
    candidate = memory.stores.candidates.get(patch.candidate_id)
    if candidate is None:
        raise MemoryNotFoundError(f"candidate not found: {patch.candidate_id}")
    updates: dict[str, Any] = {"updated_at": datetime.now(UTC)}
    if patch.confidence is not None:
        updates["confidence"] = patch.confidence
    if patch.status is not None:
        updates["status"] = patch.status
    updated = candidate.model_copy(update=updates)
    memory.stores.candidates.update(updated)
    _log_debug(
        memory,
        op="UPDATE",
        target_type="candidate",
        target_id=updated.candidate_id,
        detail={"patch": patch.model_dump(exclude_none=True)},
    )
    return updated


def apply_tuning(settings: Settings, patch: TuningPatch, *, memory: MemoryService | None = None) -> dict[str, Any]:
    if patch.retrieval_weights is not None:
        settings.retrieval_weights = _coerce_weights(patch.retrieval_weights, normalize=patch.normalize)
    if patch.consolidation_threshold is not None:
        settings.consolidation_threshold = patch.consolidation_threshold
    if patch.initial_candidate_confidence is not None:
        settings.initial_candidate_confidence = patch.initial_candidate_confidence
    if patch.confidence_increment is not None:
        settings.confidence_increment = patch.confidence_increment
    if patch.retrieval_pool_size is not None:
        settings.retrieval_pool_size = patch.retrieval_pool_size
    if patch.retrieval_top_k is not None:
        settings.retrieval_top_k = patch.retrieval_top_k
    if patch.recency_half_life_hours is not None:
        settings.recency_half_life_hours = patch.recency_half_life_hours
    payload = _tuning_payload(settings)
    if memory is not None:
        _log_debug(
            memory,
            op="UPDATE",
            target_type="settings",
            target_id="tuning",
            detail={"patch": patch.model_dump(exclude_none=True), "tuning": payload},
        )
    return payload


def _coerce_weights(raw: dict[str, float], *, normalize: bool) -> dict[str, float]:
    missing = [key for key in WEIGHT_KEYS if key not in raw]
    extra = [key for key in raw if key not in WEIGHT_KEYS]
    if missing:
        raise ValueError(f"missing retrieval weight keys: {missing}")
    if extra:
        raise ValueError(f"unknown retrieval weight keys: {extra}")
    values = {key: float(raw[key]) for key in WEIGHT_KEYS}
    if any(value < 0 for value in values.values()):
        raise ValueError("retrieval weights must be >= 0")
    total = sum(values.values())
    if normalize:
        if total <= 0:
            raise ValueError("weight sum must be positive to normalize")
        return {key: values[key] / total for key in WEIGHT_KEYS}
    if abs(total - 1.0) > WEIGHT_SUM_TOLERANCE:
        raise ValueError(f"retrieval weights must sum to 1.0 (±{WEIGHT_SUM_TOLERANCE}), got {total:.4f}")
    return values


def _tuning_payload(settings: Settings) -> dict[str, Any]:
    weights = {key: float(settings.retrieval_weights.get(key, DEFAULT_RETRIEVAL_WEIGHTS[key])) for key in WEIGHT_KEYS}
    return {
        "retrieval_weights": weights,
        "weight_sum": sum(weights.values()),
        "consolidation_threshold": settings.consolidation_threshold,
        "initial_candidate_confidence": settings.initial_candidate_confidence,
        "confidence_increment": settings.confidence_increment,
        "retrieval_pool_size": settings.retrieval_pool_size,
        "retrieval_top_k": settings.retrieval_top_k,
        "recency_half_life_hours": settings.recency_half_life_hours,
    }


def _sorted_episodic(memory: MemoryService) -> list[Any]:
    events = list(memory.stores.episodic.list_all())
    events.sort(key=lambda item: item.time, reverse=True)
    return events


def _dump(value: Any, *, exclude: set[str] | None = None) -> Any:
    if value is None:
        return None
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", exclude=exclude or set())
    return value


def _delta(new_value: float | None, current: float) -> float | None:
    if new_value is None:
        return None
    return new_value - current


def _log_debug(
    memory: MemoryService,
    *,
    op: str,
    target_type: str,
    target_id: str | None,
    detail: dict[str, Any],
) -> None:
    memory.stores.audit.log(
        op=op,
        target_type=target_type,
        target_id=target_id,
        actor=DEBUG_ACTOR,
        success=True,
        detail=detail,
    )
    memory.tracer.record(
        "debug_edit",
        {"op": op, "target_type": target_type, "target_id": target_id, **detail},
    )
