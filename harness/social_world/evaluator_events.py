"""Fact-only export for offline evaluation; this module defines no classifications."""

from harness.research.grounded_agency.hashes import content_hash
from harness.social_world.contracts import EntityObservation, FrozenModel


class EvaluatorEventReference(FrozenModel):
    event_id: str
    event_hash: str
    actor_refs: tuple[str, ...]
    evidence_refs: tuple[str, ...]


def evaluator_event_reference(event: EntityObservation) -> EvaluatorEventReference:
    return EvaluatorEventReference(event_id=event.event_id, event_hash=content_hash(event),
                                   actor_refs=event.actor_refs, evidence_refs=event.evidence_refs)
