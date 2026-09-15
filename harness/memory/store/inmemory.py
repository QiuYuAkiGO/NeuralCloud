"""In-memory stores used by unit tests and default development service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
from harness.core.models import (
    CandidateMemory,
    EpisodicEvent,
    MemoryScope,
    PersonaCore,
    PsychState,
    RelationshipState,
    SemanticFact,
    WorldState,
)
from harness.memory.store.vector_util import cosine, event_in_filters
from harness.memory.store.scope import candidate_matches, visible_to


class InMemoryPersonaStore:
    def __init__(self) -> None:
        self._rows: dict[str, PersonaCore] = {}

    def insert(self, persona: PersonaCore) -> None:
        if persona.persona_id in self._rows:
            raise PersonaLockedError("persona already exists; core identity cannot be overwritten")
        self._rows[persona.persona_id] = persona

    def get(self, persona_id: str | None = None) -> PersonaCore | None:
        if persona_id:
            return self._rows.get(persona_id)
        if not self._rows:
            return None
        return max(self._rows.values(), key=lambda row: row.created_at)

    def update(self, persona: PersonaCore) -> None:
        raise PersonaLockedError("Persona Core is locked and cannot be modified")

    def delete(self, persona_id: str) -> None:
        raise PersonaLockedError("Persona Core is locked and cannot be modified")

    def force_update(self, persona: PersonaCore) -> None:
        """Debug-only bypass. Normal write paths must keep using update()."""
        if persona.persona_id not in self._rows:
            raise KeyError(persona.persona_id)
        locked = persona.identity.model_copy(update={"locked": True})
        self._rows[persona.persona_id] = persona.model_copy(update={"identity": locked})


class InMemoryEpisodicStore:
    def __init__(self) -> None:
        self._rows: dict[str, EpisodicEvent] = {}

    def insert(self, event: EpisodicEvent) -> None:
        if not event.evidence:
            raise EvidenceRequiredError("episodic write requires evidence")
        self._rows[event.event_id] = event

    def get(self, event_id: str) -> EpisodicEvent | None:
        return self._rows.get(event_id)

    def update(self, event: EpisodicEvent) -> None:
        if event.event_id not in self._rows:
            raise KeyError(event.event_id)
        if not event.evidence:
            raise EvidenceRequiredError("episodic write requires evidence")
        self._rows[event.event_id] = event

    def list_all(self) -> list[EpisodicEvent]:
        return list(self._rows.values())

    def count(self) -> int:
        return len(self._rows)

    def delete(self, event_id: str) -> None:
        self._rows.pop(event_id, None)

    def search_pool(
        self,
        *,
        query_embedding: list[float] | None,
        since: datetime | None,
        until: datetime | None,
        channels: list[str] | None,
        entities: list[str] | None,
        limit: int,
        trusted_audience: str = "local:main",
    ) -> list[EpisodicEvent]:
        pool = [
            event
            for event in self._rows.values()
            if event.tier != "forgotten"
            and visible_to(event.scope, trusted_audience)
            and event_in_filters(event, since=since, until=until, channels=channels, entities=entities)
        ]
        if query_embedding:
            pool.sort(
                key=lambda event: cosine(query_embedding, event.embedding or []),
                reverse=True,
            )
        else:
            pool.sort(key=lambda event: event.time, reverse=True)
        return pool[:limit]


class InMemorySemanticStore:
    def __init__(self) -> None:
        self._rows: dict[str, SemanticFact] = {}

    def insert(self, fact: SemanticFact) -> None:
        if not fact.supporting_events:
            raise EvidenceRequiredError("semantic write requires supporting_events")
        self._rows[fact.fact_id] = fact

    def get(self, fact_id: str) -> SemanticFact | None:
        return self._rows.get(fact_id)

    def update(self, fact: SemanticFact) -> None:
        if fact.fact_id not in self._rows:
            raise KeyError(fact.fact_id)
        if not fact.supporting_events:
            raise EvidenceRequiredError("semantic write requires supporting_events")
        self._rows[fact.fact_id] = fact

    def delete(self, fact_id: str) -> None:
        self._rows.pop(fact_id, None)

    def list_all(self) -> list[SemanticFact]:
        return list(self._rows.values())

    def find(self, subject: str, predicate: str, value: str, *, scope: MemoryScope | None = None) -> SemanticFact | None:
        for fact in self._rows.values():
            if fact.scope == scope and fact.subject == subject and fact.predicate == predicate and fact.value == value:
                return fact
        return None

    def search_pool(
        self,
        *,
        query_embedding: list[float] | None,
        subject: str | None,
        limit: int,
        trusted_audience: str = "local:main",
    ) -> list[SemanticFact]:
        pool = [
            fact
            for fact in self._rows.values()
            if fact.revision_metadata.get("status") not in {"superseded", "disputed"}
            and visible_to(fact.scope, trusted_audience) and (subject is None or fact.subject == subject)
        ]
        if query_embedding:
            pool.sort(key=lambda fact: cosine(query_embedding, fact.embedding or []), reverse=True)
        else:
            pool.sort(key=lambda fact: fact.confidence, reverse=True)
        return pool[:limit]


class InMemoryRelationshipStore:
    def __init__(self) -> None:
        self._rows: dict[str, RelationshipState] = {}

    def upsert(self, state: RelationshipState) -> None:
        self._rows[state.target] = state

    def get(self, target: str) -> RelationshipState | None:
        return self._rows.get(target)

    def list_all(self) -> list[RelationshipState]:
        return list(self._rows.values())

    def delete(self, target: str) -> None:
        self._rows.pop(target, None)


class InMemoryPsychStore:
    def __init__(self) -> None:
        self._rows: dict[str, PsychState] = {}

    def upsert(self, state: PsychState) -> None:
        self._rows[state.character_id] = state

    def get(self, character_id: str = "character") -> PsychState | None:
        return self._rows.get(character_id)


class InMemoryWorldStore:
    def __init__(self) -> None:
        self._rows: list[WorldState] = []

    def insert(self, state: WorldState) -> None:
        self._rows.append(state)

    def latest(self) -> WorldState | None:
        return self._rows[-1] if self._rows else None


class InMemoryCandidateStore:
    def __init__(self) -> None:
        self._rows: dict[str, CandidateMemory] = {}

    def insert(self, candidate: CandidateMemory) -> None:
        if not candidate.evidence:
            raise EvidenceRequiredError("candidate requires evidence")
        self._rows[candidate.candidate_id] = candidate

    def get(self, candidate_id: str) -> CandidateMemory | None:
        return self._rows.get(candidate_id)

    def update(self, candidate: CandidateMemory) -> None:
        self._rows[candidate.candidate_id] = candidate

    def list_open(self) -> list[CandidateMemory]:
        return [row for row in self._rows.values() if row.status == "candidate"]

    def list_all(self) -> list[CandidateMemory]:
        return list(self._rows.values())

    def delete(self, candidate_id: str) -> None:
        self._rows.pop(candidate_id, None)

    def find_similar(
        self,
        *,
        subject: str | None,
        predicate: str | None,
        value: str | None,
        claim: str,
        scope: MemoryScope | None = None,
    ) -> CandidateMemory | None:
        open_rows = self.list_open()
        for row in open_rows:
            if candidate_matches(row, subject=subject, predicate=predicate, value=value, claim=claim, scope=scope):
                return row
        return None


class InMemoryAuditStore:
    def __init__(self) -> None:
        self._rows: list[dict[str, Any]] = []

    def log(
        self,
        *,
        op: str,
        target_type: str,
        target_id: str | None,
        actor: str,
        success: bool,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self._rows.append(
            {
                "op": op,
                "target_type": target_type,
                "target_id": target_id,
                "actor": actor,
                "success": success,
                "detail": detail or {},
            }
        )

    def list_for(self, target_type: str, target_id: str | None = None) -> list[dict[str, Any]]:
        return [
            row
            for row in self._rows
            if row["target_type"] == target_type and (target_id is None or row["target_id"] == target_id)
        ]

    def unauthorized_persona_attempts(self) -> int:
        return sum(
            1
            for row in self._rows
            if row["target_type"] == "persona_core" and not row["success"]
        )


@dataclass
class InMemoryStores:
    persona: InMemoryPersonaStore = field(default_factory=InMemoryPersonaStore)
    episodic: InMemoryEpisodicStore = field(default_factory=InMemoryEpisodicStore)
    semantic: InMemorySemanticStore = field(default_factory=InMemorySemanticStore)
    relationship: InMemoryRelationshipStore = field(default_factory=InMemoryRelationshipStore)
    psych: InMemoryPsychStore = field(default_factory=InMemoryPsychStore)
    world: InMemoryWorldStore = field(default_factory=InMemoryWorldStore)
    candidates: InMemoryCandidateStore = field(default_factory=InMemoryCandidateStore)
    audit: InMemoryAuditStore = field(default_factory=InMemoryAuditStore)
