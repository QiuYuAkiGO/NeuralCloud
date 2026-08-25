"""Store protocols shared by in-memory and PostgreSQL backends."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from harness.core.models import (
    CandidateMemory,
    EpisodicEvent,
    PersonaCore,
    PsychState,
    RelationshipState,
    SemanticFact,
    WorldState,
)


class PersonaStore(Protocol):
    def insert(self, persona: PersonaCore) -> None: ...

    def get(self, persona_id: str | None = None) -> PersonaCore | None: ...

    def update(self, persona: PersonaCore) -> None: ...

    def delete(self, persona_id: str) -> None: ...


class EpisodicStore(Protocol):
    """Episodic event store. `search_pool` must exclude `tier == "forgotten"`."""
    def insert(self, event: EpisodicEvent) -> None: ...

    def get(self, event_id: str) -> EpisodicEvent | None: ...

    def update(self, event: EpisodicEvent) -> None: ...

    def list_all(self) -> list[EpisodicEvent]: ...

    def search_pool(
        self,
        *,
        query_embedding: list[float] | None,
        since: datetime | None,
        until: datetime | None,
        channels: list[str] | None,
        entities: list[str] | None,
        limit: int,
    ) -> list[EpisodicEvent]: ...

    def count(self) -> int: ...


class SemanticStore(Protocol):
    def insert(self, fact: SemanticFact) -> None: ...

    def get(self, fact_id: str) -> SemanticFact | None: ...

    def update(self, fact: SemanticFact) -> None: ...

    def delete(self, fact_id: str) -> None: ...

    def list_all(self) -> list[SemanticFact]: ...

    def find(self, subject: str, predicate: str, value: str) -> SemanticFact | None: ...

    def search_pool(
        self,
        *,
        query_embedding: list[float] | None,
        subject: str | None,
        limit: int,
    ) -> list[SemanticFact]: ...


class RelationshipStore(Protocol):
    def upsert(self, state: RelationshipState) -> None: ...

    def get(self, target: str) -> RelationshipState | None: ...

    def list_all(self) -> list[RelationshipState]: ...


class PsychStore(Protocol):
    def upsert(self, state: PsychState) -> None: ...

    def get(self, character_id: str = "character") -> PsychState | None: ...


class WorldStore(Protocol):
    def insert(self, state: WorldState) -> None: ...

    def latest(self) -> WorldState | None: ...


class CandidateStore(Protocol):
    def insert(self, candidate: CandidateMemory) -> None: ...

    def get(self, candidate_id: str) -> CandidateMemory | None: ...

    def update(self, candidate: CandidateMemory) -> None: ...

    def list_open(self) -> list[CandidateMemory]: ...

    def list_all(self) -> list[CandidateMemory]: ...

    def find_similar(self, *, subject: str | None, predicate: str | None, value: str | None, claim: str) -> CandidateMemory | None: ...


class AuditStore(Protocol):
    def log(
        self,
        *,
        op: str,
        target_type: str,
        target_id: str | None,
        actor: str,
        success: bool,
        detail: dict[str, Any] | None = None,
    ) -> None: ...

    def list_for(self, target_type: str, target_id: str | None = None) -> list[dict[str, Any]]: ...

    def unauthorized_persona_attempts(self) -> int: ...


class MemoryStores(Protocol):
    persona: PersonaStore
    episodic: EpisodicStore
    semantic: SemanticStore
    relationship: RelationshipStore
    psych: PsychStore
    world: WorldStore
    candidates: CandidateStore
    audit: AuditStore
