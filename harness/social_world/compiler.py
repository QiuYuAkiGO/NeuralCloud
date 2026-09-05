"""Bounded context projections without entity scores or offline behavioral labels."""

from __future__ import annotations

from collections.abc import Callable, Sequence

from harness.core.inference import ContextSegment
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.social_world.contracts import EntityBelief, EntityObservation
from harness.social_world.entity_memory import belief_key


def _project_event(event: EntityObservation, alias: Callable[[str], str], reference_alias: Callable[[str], str]) -> dict[str, object]:
    facts = event.facts.model_dump(mode="json")
    if facts.get("recipient_ref"):
        facts["recipient_ref"] = alias(str(facts["recipient_ref"]))
    return {"event_id": reference_alias(event.event_id), "actor_refs": [alias(actor) for actor in event.actor_refs],
            "event_type": event.event_type, "facts": facts, "occurred_at": event.occurred_at.isoformat()}


def compile_entity_context(
    events: Sequence[EntityObservation], beliefs: Sequence[EntityBelief] = (), *,
    alias: Callable[[str], str] = lambda value: value,
    reference_alias: Callable[[str], str] = lambda value: value, max_tokens: int = 1024, kind: str = "entity_memory",
) -> ContextSegment:
    entries: list[tuple[str, str]] = []
    for event in events:
        entries.append((canonical_json(_project_event(event, alias, reference_alias)), f"event:{event.event_id}:{content_hash(event)}"))
    for belief in beliefs:
        # The free-text proposition is trace-only. Model context uses an exact,
        # revisable objective predicate, not an interpreted personality label.
        payload = {"entity_ref": alias(belief.entity_ref), "prediction": belief.prediction.model_dump(mode="json"),
                   "confidence": belief.confidence, "stability": belief.stability, "revision": belief.revision,
                   "supporting_event_refs": tuple(reference_alias(ref) for ref in belief.supporting_event_refs),
                   "contradicting_event_refs": tuple(reference_alias(ref) for ref in belief.contradicting_event_refs)}
        entries.append((canonical_json(payload), f"entity-prediction:{belief_key(belief)}:r{belief.revision}"))
    while entries and len("\n".join(text for text, _ in entries).encode("utf-8")) > max_tokens:
        entries.pop(0)
    if kind == "entity_observation" and events and not entries:
        raise ValueError("current objective event exceeds the context budget")
    text = "\n".join(text for text, _ in entries)
    if text:
        NeutralityScanner().assert_neutral([{"role": "system", "content": text}])
    return ContextSegment(kind=kind, text=text, source_ids=[reference for _, reference in entries],
                          token_estimate=len(text.encode("utf-8")))
