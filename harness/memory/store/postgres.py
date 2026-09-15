"""PostgreSQL + pgvector repositories."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast

from sqlalchemy import and_, create_engine, or_, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from harness.config import Settings, get_settings
from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
from harness.core.models import (
    CandidateMemory,
    EpisodicEvent,
    MemoryTier,
    MemoryScope,
    PersonaCore,
    PersonaIdentity,
    PsychState,
    RelationshipState,
    SemanticFact,
    WorldState,
)
from harness.memory.store.schema import (
    PERSONA_LOCK_FUNCTION_SQL,
    Base,
    EpisodicEventRow,
    EvidenceLinkRow,
    MemoryCandidateRow,
    MemoryOpsLogRow,
    PersonaCoreRow,
    PsychStateRow,
    RelationshipStateRow,
    SemanticFactRow,
    WorldStateRow,
)
from harness.memory.store.vector_util import cosine, event_in_filters
from harness.memory.store.scope import candidate_matches


def _visibility_filter(column, audience: str):
    scoped = and_(column["owner_id"].astext == "misty", column["visibility"].contains([audience]))
    if audience != "local:main":
        private_boundary = (column["conversation_id"].astext == audience
                            if audience.startswith("qq:private:") else False)
        scoped = and_(scoped, or_(column["channel"].astext != "qq_private", private_boundary))
    return or_(column.is_(None), scoped) if audience == "local:main" else scoped


def _provenance(row) -> dict:
    return {"scope": row.scope, "source_refs": list(row.source_refs or []),
            "processing_version": row.processing_version}


def _stored_provenance(memory) -> dict:
    return {"scope": memory.scope.model_dump(mode="json") if memory.scope is not None else None,
            "source_refs": list(memory.source_refs), "processing_version": memory.processing_version}


def _update_provenance(row, memory) -> None:
    for name, value in _stored_provenance(memory).items():
        setattr(row, name, value)


def make_engine(url: str | None = None, settings: Settings | None = None) -> Engine:
    settings = settings or get_settings()
    return create_engine(url or settings.database_url)


def make_session_factory(engine: Engine) -> sessionmaker[Session]:
    return sessionmaker(engine, expire_on_commit=False)


def _persona_from_row(row: PersonaCoreRow) -> PersonaCore:
    return PersonaCore(
        persona_id=row.persona_id,
        version=row.version,
        identity=PersonaIdentity.model_validate(row.identity),
        created_at=row.created_at,
    )


def _episodic_from_row(row: EpisodicEventRow) -> EpisodicEvent:
    embedding = list(row.embedding) if row.embedding is not None else None
    return EpisodicEvent(
        event_id=row.event_id,
        time=row.time,
        participants=list(row.participants),
        summary=row.summary,
        character_action=row.character_action,
        outcome=row.outcome,
        importance=row.importance,
        emotional_valence=row.emotional_valence,
        evidence=list(row.evidence),
        objective_fact=row.objective_fact,
        persona_conditioned_insight=row.persona_conditioned_insight,
        insight_confidence=row.insight_confidence,
        supporting_evidence=list(row.supporting_evidence or []),
        channel=row.channel,
        event_type=row.event_type,
        session_id=row.session_id,
        uniqueness=row.uniqueness,
        tier=cast(MemoryTier, row.tier),
        expire_at=row.expire_at,
        hit_count=row.hit_count,
        last_hit_at=row.last_hit_at,
        tier_updated_at=row.tier_updated_at,
        tier_reasons=list(row.tier_reasons or []),
        embedding=embedding,
        **_provenance(row),
        strength=row.strength,
        strength_updated_at=row.strength_updated_at,
        last_decay_review_at=row.last_decay_review_at,
        decay_demoted=row.decay_demoted,
    )


def _semantic_from_row(row: SemanticFactRow) -> SemanticFact:
    embedding = list(row.embedding) if row.embedding is not None else None
    return SemanticFact(
        fact_id=row.fact_id,
        subject=row.subject,
        predicate=row.predicate,
        value=row.value,
        confidence=row.confidence,
        supporting_events=list(row.supporting_events),
        formed_at=row.formed_at,
        embedding=embedding,
        **_provenance(row),
    )


def _candidate_from_row(row: MemoryCandidateRow) -> CandidateMemory:
    return CandidateMemory(
        candidate_id=row.candidate_id,
        claim=row.claim,
        source=row.source,
        confidence=row.confidence,
        status=row.status,  # type: ignore[arg-type]
        evidence=list(row.evidence),
        subject=row.subject,
        predicate=row.predicate,
        value=row.value,
        created_at=row.created_at,
        updated_at=row.updated_at,
        consolidated_fact_id=row.consolidated_fact_id,
        **_provenance(row),
    )


class PostgresPersonaStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, persona: PersonaCore) -> None:
        row = PersonaCoreRow(
            persona_id=persona.persona_id,
            version=persona.version,
            identity=persona.identity.model_dump(),
            created_at=persona.created_at,
            locked=True,
        )
        self.session.add(row)
        self.session.flush()

    def get(self, persona_id: str | None = None) -> PersonaCore | None:
        stmt = select(PersonaCoreRow)
        if persona_id:
            stmt = stmt.where(PersonaCoreRow.persona_id == persona_id)
        else:
            stmt = stmt.order_by(PersonaCoreRow.created_at.desc())
        row = self.session.execute(stmt.limit(1)).scalar_one_or_none()
        return _persona_from_row(row) if row else None

    def update(self, persona: PersonaCore) -> None:
        raise PersonaLockedError("Persona Core is locked and cannot be modified")

    def delete(self, persona_id: str) -> None:
        raise PersonaLockedError("Persona Core is locked and cannot be modified")

    def force_update(self, persona: PersonaCore) -> None:
        raise NotImplementedError(
            f"persona debug bypass is not available on PostgreSQL (lock trigger); {persona.persona_id}"
        )


class PostgresEpisodicStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, event: EpisodicEvent) -> None:
        if not event.evidence:
            raise EvidenceRequiredError("episodic write requires evidence")
        self.session.add(
            EpisodicEventRow(
                event_id=event.event_id,
                time=event.time,
                participants=event.participants,
                summary=event.summary,
                character_action=event.character_action,
                outcome=event.outcome,
                importance=event.importance,
                emotional_valence=event.emotional_valence,
                evidence=event.evidence,
                objective_fact=event.objective_fact,
                persona_conditioned_insight=event.persona_conditioned_insight,
                insight_confidence=event.insight_confidence,
                supporting_evidence=event.supporting_evidence,
                channel=event.channel,
                event_type=event.event_type,
                session_id=event.session_id,
                uniqueness=event.uniqueness,
                tier=event.tier,
                expire_at=event.expire_at,
                hit_count=event.hit_count,
                last_hit_at=event.last_hit_at,
                tier_updated_at=event.tier_updated_at,
                tier_reasons=event.tier_reasons,
                embedding=event.embedding,
                **_stored_provenance(event),
                strength=event.strength,
                strength_updated_at=event.strength_updated_at,
                last_decay_review_at=event.last_decay_review_at,
                decay_demoted=event.decay_demoted,
            )
        )
        self.session.flush()

    def get(self, event_id: str) -> EpisodicEvent | None:
        row = self.session.get(EpisodicEventRow, event_id)
        return _episodic_from_row(row) if row else None

    def update(self, event: EpisodicEvent) -> None:
        if not event.evidence:
            raise EvidenceRequiredError("episodic write requires evidence")
        row = self.session.get(EpisodicEventRow, event.event_id)
        if row is None:
            raise KeyError(event.event_id)
        row.time = event.time
        row.participants = event.participants
        row.summary = event.summary
        row.character_action = event.character_action
        row.outcome = event.outcome
        row.importance = event.importance
        row.emotional_valence = event.emotional_valence
        row.evidence = event.evidence
        row.objective_fact = event.objective_fact
        row.persona_conditioned_insight = event.persona_conditioned_insight
        row.insight_confidence = event.insight_confidence
        row.supporting_evidence = event.supporting_evidence
        row.channel = event.channel
        row.event_type = event.event_type
        row.session_id = event.session_id
        row.uniqueness = event.uniqueness
        row.tier = event.tier
        row.expire_at = event.expire_at
        row.hit_count = event.hit_count
        row.last_hit_at = event.last_hit_at
        row.tier_updated_at = event.tier_updated_at
        row.tier_reasons = event.tier_reasons
        row.embedding = event.embedding
        _update_provenance(row, event)
        row.strength = event.strength
        row.strength_updated_at = event.strength_updated_at
        row.last_decay_review_at = event.last_decay_review_at
        row.decay_demoted = event.decay_demoted
        self.session.flush()

    def list_all(self) -> list[EpisodicEvent]:
        rows = self.session.execute(select(EpisodicEventRow)).scalars().all()
        return [_episodic_from_row(row) for row in rows]

    def count(self) -> int:
        return int(self.session.execute(text("SELECT COUNT(*) FROM episodic_events")).scalar_one())

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
        stmt = select(EpisodicEventRow).where(
            _visibility_filter(EpisodicEventRow.scope, trusted_audience),
            EpisodicEventRow.tier != "forgotten",
        )
        if since is not None:
            stmt = stmt.where(EpisodicEventRow.time >= since)
        if until is not None:
            stmt = stmt.where(EpisodicEventRow.time <= until)
        if channels:
            stmt = stmt.where(EpisodicEventRow.channel.in_(channels))
        fetch_limit = max(limit * 4, limit)
        if query_embedding:
            stmt = stmt.order_by(EpisodicEventRow.embedding.cosine_distance(query_embedding))
        else:
            stmt = stmt.order_by(EpisodicEventRow.time.desc())
        rows = self.session.execute(stmt.limit(fetch_limit)).scalars().all()
        events = [_episodic_from_row(row) for row in rows]
        events = [
            event
            for event in events
            if event.tier != "forgotten"
            and event_in_filters(event, since=None, until=None, channels=None, entities=entities)
        ]
        if query_embedding:
            events.sort(key=lambda event: cosine(query_embedding, event.embedding or []), reverse=True)
        return events[:limit]


class PostgresSemanticStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, fact: SemanticFact) -> None:
        if not fact.supporting_events:
            raise EvidenceRequiredError("semantic write requires supporting_events")
        self.session.add(
            SemanticFactRow(
                fact_id=fact.fact_id,
                subject=fact.subject,
                predicate=fact.predicate,
                value=fact.value,
                confidence=fact.confidence,
                supporting_events=fact.supporting_events,
                formed_at=fact.formed_at,
                embedding=fact.embedding,
                **_stored_provenance(fact),
            )
        )
        self.session.flush()

    def get(self, fact_id: str) -> SemanticFact | None:
        row = self.session.get(SemanticFactRow, fact_id)
        return _semantic_from_row(row) if row else None

    def update(self, fact: SemanticFact) -> None:
        if not fact.supporting_events:
            raise EvidenceRequiredError("semantic write requires supporting_events")
        row = self.session.get(SemanticFactRow, fact.fact_id)
        if row is None:
            raise KeyError(fact.fact_id)
        row.subject = fact.subject
        row.predicate = fact.predicate
        row.value = fact.value
        row.confidence = fact.confidence
        row.supporting_events = fact.supporting_events
        row.formed_at = fact.formed_at
        row.embedding = fact.embedding
        _update_provenance(row, fact)
        self.session.flush()

    def delete(self, fact_id: str) -> None:
        row = self.session.get(SemanticFactRow, fact_id)
        if row is not None:
            self.session.delete(row)
            self.session.flush()

    def list_all(self) -> list[SemanticFact]:
        rows = self.session.execute(select(SemanticFactRow)).scalars().all()
        return [_semantic_from_row(row) for row in rows]

    def find(self, subject: str, predicate: str, value: str, *, scope: MemoryScope | None = None) -> SemanticFact | None:
        stmt = select(SemanticFactRow).where(
            SemanticFactRow.subject == subject,
            SemanticFactRow.predicate == predicate,
            SemanticFactRow.value == value,
            SemanticFactRow.scope == scope.model_dump(mode="json") if scope is not None else SemanticFactRow.scope.is_(None),
        )
        row = self.session.execute(stmt).scalar_one_or_none()
        return _semantic_from_row(row) if row else None

    def search_pool(
        self,
        *,
        query_embedding: list[float] | None,
        subject: str | None,
        limit: int,
        trusted_audience: str = "local:main",
    ) -> list[SemanticFact]:
        stmt = select(SemanticFactRow).where(_visibility_filter(SemanticFactRow.scope, trusted_audience))
        if subject:
            stmt = stmt.where(SemanticFactRow.subject == subject)
        if query_embedding:
            stmt = stmt.order_by(SemanticFactRow.embedding.cosine_distance(query_embedding))
        else:
            stmt = stmt.order_by(SemanticFactRow.confidence.desc())
        rows = self.session.execute(stmt.limit(limit)).scalars().all()
        return [_semantic_from_row(row) for row in rows]


class PostgresRelationshipStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, state: RelationshipState) -> None:
        row = self.session.execute(
            select(RelationshipStateRow).where(RelationshipStateRow.target == state.target)
        ).scalar_one_or_none()
        if row is None:
            self.session.add(
                RelationshipStateRow(
                    relation_id=state.relation_id,
                    target=state.target,
                    closeness=state.closeness,
                    trust=state.trust,
                    attachment=state.attachment,
                    recent_tension=state.recent_tension,
                    shared_history_summary=state.shared_history_summary,
                    last_major_event=state.last_major_event,
                    updated_at=state.updated_at,
                )
            )
        else:
            row.closeness = state.closeness
            row.trust = state.trust
            row.attachment = state.attachment
            row.recent_tension = state.recent_tension
            row.shared_history_summary = state.shared_history_summary
            row.last_major_event = state.last_major_event
            row.updated_at = state.updated_at
        self.session.flush()

    def get(self, target: str) -> RelationshipState | None:
        row = self.session.execute(
            select(RelationshipStateRow).where(RelationshipStateRow.target == target)
        ).scalar_one_or_none()
        if row is None:
            return None
        return RelationshipState(
            relation_id=row.relation_id,
            target=row.target,
            closeness=row.closeness,
            trust=row.trust,
            attachment=row.attachment,
            recent_tension=row.recent_tension,
            shared_history_summary=row.shared_history_summary,
            last_major_event=row.last_major_event,
            updated_at=row.updated_at,
        )

    def list_all(self) -> list[RelationshipState]:
        rows = self.session.execute(select(RelationshipStateRow)).scalars().all()
        return [
            RelationshipState(
                relation_id=row.relation_id,
                target=row.target,
                closeness=row.closeness,
                trust=row.trust,
                attachment=row.attachment,
                recent_tension=row.recent_tension,
                shared_history_summary=row.shared_history_summary,
                last_major_event=row.last_major_event,
                updated_at=row.updated_at,
            )
            for row in rows
        ]


class PostgresPsychStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(self, state: PsychState) -> None:
        row = self.session.get(PsychStateRow, state.character_id)
        if row is None:
            self.session.add(
                PsychStateRow(
                    character_id=state.character_id,
                    long_state=state.long,
                    mid_state=state.mid,
                    short_state=state.short,
                    updated_at=state.updated_at,
                )
            )
        else:
            row.long_state = state.long
            row.mid_state = state.mid
            row.short_state = state.short
            row.updated_at = state.updated_at
        self.session.flush()

    def get(self, character_id: str = "character") -> PsychState | None:
        row = self.session.get(PsychStateRow, character_id)
        if row is None:
            return None
        return PsychState(
            character_id=row.character_id,
            long=dict(row.long_state),
            mid=dict(row.mid_state),
            short=dict(row.short_state),
            updated_at=row.updated_at,
        )


class PostgresWorldStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, state: WorldState) -> None:
        self.session.add(
            WorldStateRow(captured_at=state.time, payload=state.model_dump(mode="json"))
        )
        self.session.flush()

    def latest(self) -> WorldState | None:
        row = self.session.execute(
            select(WorldStateRow).order_by(WorldStateRow.captured_at.desc()).limit(1)
        ).scalar_one_or_none()
        if row is None:
            return None
        return WorldState.model_validate(row.payload)


class PostgresCandidateStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def insert(self, candidate: CandidateMemory) -> None:
        if not candidate.evidence:
            raise EvidenceRequiredError("candidate requires evidence")
        self.session.add(
            MemoryCandidateRow(
                candidate_id=candidate.candidate_id,
                claim=candidate.claim,
                source=candidate.source,
                confidence=candidate.confidence,
                status=candidate.status,
                evidence=candidate.evidence,
                subject=candidate.subject,
                predicate=candidate.predicate,
                value=candidate.value,
                created_at=candidate.created_at,
                updated_at=candidate.updated_at,
                consolidated_fact_id=candidate.consolidated_fact_id,
                **_stored_provenance(candidate),
            )
        )
        for event_id in candidate.evidence:
            self.session.add(
                EvidenceLinkRow(
                    candidate_id=candidate.candidate_id,
                    event_id=event_id,
                    added_at=candidate.updated_at,
                )
            )
        self.session.flush()

    def get(self, candidate_id: str) -> CandidateMemory | None:
        row = self.session.get(MemoryCandidateRow, candidate_id)
        return _candidate_from_row(row) if row else None

    def update(self, candidate: CandidateMemory) -> None:
        row = self.session.get(MemoryCandidateRow, candidate.candidate_id)
        if row is None:
            raise KeyError(candidate.candidate_id)
        row.claim = candidate.claim
        row.source = candidate.source
        row.confidence = candidate.confidence
        row.status = candidate.status
        row.evidence = candidate.evidence
        row.subject = candidate.subject
        row.predicate = candidate.predicate
        row.value = candidate.value
        row.updated_at = candidate.updated_at
        row.consolidated_fact_id = candidate.consolidated_fact_id
        _update_provenance(row, candidate)
        self.session.flush()

    def list_open(self) -> list[CandidateMemory]:
        rows = (
            self.session.execute(
                select(MemoryCandidateRow).where(MemoryCandidateRow.status == "candidate")
            )
            .scalars()
            .all()
        )
        return [_candidate_from_row(row) for row in rows]

    def list_all(self) -> list[CandidateMemory]:
        rows = self.session.execute(select(MemoryCandidateRow)).scalars().all()
        return [_candidate_from_row(row) for row in rows]

    def find_similar(
        self,
        *,
        subject: str | None,
        predicate: str | None,
        value: str | None,
        claim: str,
        scope: MemoryScope | None = None,
    ) -> CandidateMemory | None:
        for row in self.list_open():
            if candidate_matches(row, subject=subject, predicate=predicate, value=value, claim=claim, scope=scope):
                return row
        return None


class PostgresAuditStore:
    def __init__(self, session: Session) -> None:
        self.session = session

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
        self.session.add(
            MemoryOpsLogRow(
                op=op,
                target_type=target_type,
                target_id=target_id,
                actor=actor,
                success=success,
                detail=detail,
            )
        )
        self.session.flush()

    def list_for(self, target_type: str, target_id: str | None = None) -> list[dict[str, Any]]:
        stmt = select(MemoryOpsLogRow).where(MemoryOpsLogRow.target_type == target_type)
        if target_id is not None:
            stmt = stmt.where(MemoryOpsLogRow.target_id == target_id)
        rows = self.session.execute(stmt).scalars().all()
        return [
            {
                "op": row.op,
                "target_type": row.target_type,
                "target_id": row.target_id,
                "actor": row.actor,
                "success": row.success,
                "detail": row.detail or {},
            }
            for row in rows
        ]

    def unauthorized_persona_attempts(self) -> int:
        stmt = select(MemoryOpsLogRow).where(
            MemoryOpsLogRow.target_type == "persona_core",
            MemoryOpsLogRow.success.is_(False),
        )
        return len(self.session.execute(stmt).scalars().all())


@dataclass
class PostgresStores:
    session: Session
    persona: PostgresPersonaStore
    episodic: PostgresEpisodicStore
    semantic: PostgresSemanticStore
    relationship: PostgresRelationshipStore
    psych: PostgresPsychStore
    world: PostgresWorldStore
    candidates: PostgresCandidateStore
    audit: PostgresAuditStore

    @classmethod
    def from_session(cls, session: Session) -> PostgresStores:
        return cls(
            session=session,
            persona=PostgresPersonaStore(session),
            episodic=PostgresEpisodicStore(session),
            semantic=PostgresSemanticStore(session),
            relationship=PostgresRelationshipStore(session),
            psych=PostgresPsychStore(session),
            world=PostgresWorldStore(session),
            candidates=PostgresCandidateStore(session),
            audit=PostgresAuditStore(session),
        )


def create_all(engine: Engine) -> None:
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
    Base.metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(text(PERSONA_LOCK_FUNCTION_SQL))
        conn.execute(text("DROP TRIGGER IF EXISTS persona_core_no_update ON persona_core"))
        conn.execute(
            text(
                "CREATE TRIGGER persona_core_no_update "
                "BEFORE UPDATE OR DELETE ON persona_core "
                "FOR EACH ROW EXECUTE FUNCTION prevent_persona_mutation()"
            )
        )
