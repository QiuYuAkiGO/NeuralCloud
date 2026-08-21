"""MemoryService facade: record_event / query / get_state / curate."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from harness.config import Settings, get_settings
from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
from harness.core.models import (
    CharacterSnapshot,
    CuratorOp,
    EpisodicEvent,
    InteractionEvent,
    PersonaCore,
    PsychState,
    RelationshipState,
    RetrievalQuery,
    ScoredMemory,
    SemanticFact,
    WorldState,
)
from harness.llm.client import LLMClient, MockLLM
from harness.llm.embeddings import EmbeddingClient, MockEmbedding
from harness.memory.dual import DualMemoryWriter
from harness.memory.perception import ChatPerceptionBackend, PerceptionBackend
from harness.memory.retrieval.conflict import ConflictRetriever
from harness.memory.retrieval.hybrid import HybridRetriever
from harness.memory.retrieval.planner import RetrievalPlanner
from harness.memory.store.inmemory import InMemoryStores
from harness.memory.store.protocols import MemoryStores
from harness.memory.writer.curator import CuratorBackend, default_curator
from harness.memory.writer.pipeline import WriterPipeline
from infra.trace.recorder import NullTracer, Tracer


def _now() -> datetime:
    return datetime.now(UTC)


def apply_psych_ttl(state: PsychState, *, now: datetime) -> tuple[PsychState, bool]:
    short = dict(state.short)
    ttl = short.get("ttl_seconds")
    set_at_raw = short.get("set_at")
    if not ttl or not set_at_raw:
        return state, False
    set_at = datetime.fromisoformat(str(set_at_raw))
    if set_at.tzinfo is None:
        set_at = set_at.replace(tzinfo=UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    expired = (now - set_at).total_seconds() > float(ttl)
    if not expired:
        return state, False
    cleared = {key: value for key, value in short.items() if key in {"ttl_seconds", "set_at"}}
    cleared["expired"] = True
    return state.model_copy(update={"short": cleared}), True


class MemoryService:
    def __init__(
        self,
        stores: MemoryStores,
        *,
        llm: LLMClient | None = None,
        embeddings: EmbeddingClient | None = None,
        curator: CuratorBackend | None = None,
        tracer: Tracer | None = None,
        perception: PerceptionBackend | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.stores = stores
        self.settings = settings or get_settings()
        self.llm = llm or MockLLM()
        self.embeddings = embeddings or MockEmbedding(dim=self.settings.embedding_dim)
        self.curator = curator or default_curator(self.llm)
        self.tracer = tracer or NullTracer()
        self.perception = perception or ChatPerceptionBackend()
        self.dual = DualMemoryWriter(self.llm)
        self.pipeline = WriterPipeline(stores, self.embeddings, self.settings)
        self.planner = RetrievalPlanner(self.llm)
        self.hybrid = HybridRetriever(stores, self.embeddings, self.settings)
        self.conflicts = ConflictRetriever(self.hybrid, self.planner, self.settings)

    def bootstrap_persona(self, persona: PersonaCore) -> PersonaCore:
        locked = persona.identity.model_copy(update={"locked": True})
        persona = persona.model_copy(update={"identity": locked})
        self.stores.persona.insert(persona)
        self.stores.audit.log(
            op="ADD",
            target_type="persona_core",
            target_id=persona.persona_id,
            actor="bootstrap",
            success=True,
            detail={"version": persona.version},
        )
        self.tracer.record("persona_bootstrap", {"persona_id": persona.persona_id})
        return persona

    def update_persona(self, persona: PersonaCore) -> None:
        self.stores.audit.log(
            op="UPDATE",
            target_type="persona_core",
            target_id=persona.persona_id,
            actor="unauthorized_attempt",
            success=False,
            detail={"reason": "persona_core_locked"},
        )
        self.tracer.record("persona_lock_violation", {"persona_id": persona.persona_id, "op": "UPDATE"})
        raise PersonaLockedError("Persona Core is locked and cannot be modified")

    def delete_persona(self, persona_id: str) -> None:
        self.stores.audit.log(
            op="DELETE",
            target_type="persona_core",
            target_id=persona_id,
            actor="unauthorized_attempt",
            success=False,
            detail={"reason": "persona_core_locked"},
        )
        raise PersonaLockedError("Persona Core is locked and cannot be modified")

    def get_persona(self, persona_id: str | None = None) -> PersonaCore | None:
        return self.stores.persona.get(persona_id)

    def record_event(
        self,
        incoming: InteractionEvent | EpisodicEvent,
        *,
        generate_insight: bool = True,
        curate: bool = True,
        importance: float = 0.5,
        emotional_valence: float = 0.0,
        event_type: str | None = None,
        now: datetime | None = None,
    ) -> EpisodicEvent:
        moment = now or _now()
        event = self._as_episodic(
            incoming,
            importance=importance,
            emotional_valence=emotional_valence,
            event_type=event_type,
            now=moment,
        )
        if not event.evidence:
            raise EvidenceRequiredError("record_event requires evidence")
        persona = self.stores.persona.get()
        if generate_insight:
            dual = self.dual.enrich(event, persona)
            event = event.model_copy(
                update={
                    "persona_conditioned_insight": dual.persona_conditioned_insight,
                    "insight_confidence": dual.insight_confidence,
                    "supporting_evidence": dual.supporting_evidence,
                }
            )
        text = f"{event.summary} {event.objective_fact}"
        event = event.model_copy(update={"embedding": self.embeddings.embed_one(text)})
        self.stores.episodic.insert(event)
        self.stores.audit.log(
            op="ADD",
            target_type="episodic",
            target_id=event.event_id,
            actor="memory_service",
            success=True,
            detail={"evidence": event.evidence},
        )
        self.tracer.record(
            "episodic_write",
            {
                "event_id": event.event_id,
                "evidence": event.evidence,
                "has_insight": bool(event.persona_conditioned_insight),
            },
        )
        if curate:
            self.curate(event)
        return event

    def curate(self, event: EpisodicEvent) -> list[CuratorOp]:
        persona = self.stores.persona.get()
        output = self.curator.propose(event, persona=persona, open_candidates=self.stores.candidates.list_open())
        applied = self.pipeline.apply(output, event=event, strict=False)
        self.tracer.record(
            "curator_ops",
            {"event_id": event.event_id, "ops": [op.model_dump() for op in applied]},
        )
        return applied

    def query(self, query: RetrievalQuery) -> list[ScoredMemory]:
        if query.persona is None:
            query = query.model_copy(update={"persona": self.stores.persona.get()})
        if query.relationship is None:
            query = query.model_copy(update={"relationship": self.stores.relationship.get("user")})
        plan = self.planner.plan(query)
        results = self.hybrid.search(query, plan)
        payload: dict[str, Any] = {
            "text": query.text,
            "plan": plan.model_dump(),
            "hits": [
                {
                    "ref": item.ref,
                    "type": item.memory_type,
                    "relevance": item.relevance,
                    "why_retrieved": item.why_retrieved,
                    "scores": item.score_breakdown.model_dump(),
                }
                for item in results
            ],
        }
        self.tracer.record("retrieval", payload)
        return results

    def query_with_conflicts(self, query: RetrievalQuery) -> tuple[list[ScoredMemory], list[ScoredMemory]]:
        query = query.model_copy(update={"include_conflicts": True})
        if query.current_inference is None:
            raise ValueError("current_inference is required for conflict retrieval")
        if query.persona is None:
            query = query.model_copy(update={"persona": self.stores.persona.get()})
        if query.relationship is None:
            query = query.model_copy(update={"relationship": self.stores.relationship.get("user")})
        plan = self.planner.plan(query)
        results = self.hybrid.search(query, plan)
        conflicts = self.conflicts.find(query, query.current_inference)
        self.tracer.record(
            "retrieval",
            {
                "text": query.text,
                "plan": plan.model_dump(),
                "hits": [item.ref for item in results],
                "conflicts": [item.ref for item in conflicts],
            },
        )
        return results, conflicts

    def get_state(
        self,
        *,
        relation_target: str = "user",
        now: datetime | None = None,
        character_id: str = "character",
    ) -> CharacterSnapshot:
        moment = now or _now()
        psych = self.stores.psych.get(character_id)
        expired = False
        if psych is not None:
            psych, expired = apply_psych_ttl(psych, now=moment)
        return CharacterSnapshot(
            persona=self.stores.persona.get(),
            relationship=self.stores.relationship.get(relation_target),
            psych=psych,
            world=self.stores.world.latest(),
            short_psych_expired=expired,
        )

    def upsert_psych(self, state: PsychState, *, now: datetime | None = None) -> PsychState:
        moment = now or _now()
        short = dict(state.short)
        if short and "set_at" not in short:
            short["set_at"] = moment.isoformat()
        state = state.model_copy(update={"short": short, "updated_at": moment})
        self.stores.psych.upsert(state)
        self.tracer.record("psych_upsert", {"character_id": state.character_id})
        return state

    def apply_relationship_delta(
        self,
        target: str,
        *,
        closeness: float | None = None,
        trust: float | None = None,
        attachment: float | None = None,
        recent_tension: float | None = None,
        last_major_event: str | None = None,
        history_note: str | None = None,
        now: datetime | None = None,
    ) -> RelationshipState:
        moment = now or _now()
        current = self.stores.relationship.get(target)
        if current is None:
            current = RelationshipState(target=target, updated_at=moment)
        updated = current.apply_delta(
            closeness=closeness,
            trust=trust,
            attachment=attachment,
            recent_tension=recent_tension,
            last_major_event=last_major_event,
            updated_at=moment,
            history_note=history_note,
        )
        self.stores.relationship.upsert(updated)
        self.stores.audit.log(
            op="UPDATE",
            target_type="relationship",
            target_id=updated.relation_id,
            actor="memory_service",
            success=True,
            detail={"target": target, "last_major_event": last_major_event},
        )
        self.tracer.record("relationship_delta", {"target": target, "last_major_event": last_major_event})
        return updated

    def update_world(self, state: WorldState | None = None, *, now: datetime | None = None) -> WorldState:
        world = state or self.perception.observe(now=now)
        self.stores.world.insert(world)
        self.tracer.record("world_update", {"time": world.time.isoformat(), "channel": world.channel})
        return world

    def force_write_semantic(self, fact: SemanticFact) -> SemanticFact:
        """Explicit API that still enforces evidence. Used by negative tests."""
        if not fact.supporting_events:
            self.stores.audit.log(
                op="ADD",
                target_type="semantic",
                target_id=fact.fact_id,
                actor="unauthorized_attempt",
                success=False,
                detail={"reason": "missing_evidence"},
            )
            raise EvidenceRequiredError("semantic facts require supporting_events")
        if fact.embedding is None:
            fact = fact.model_copy(
                update={"embedding": self.embeddings.embed_one(f"{fact.subject} {fact.predicate} {fact.value}")}
            )
        self.stores.semantic.insert(fact)
        return fact

    def _as_episodic(
        self,
        incoming: InteractionEvent | EpisodicEvent,
        *,
        importance: float,
        emotional_valence: float,
        event_type: str | None,
        now: datetime,
    ) -> EpisodicEvent:
        if isinstance(incoming, EpisodicEvent):
            return incoming
        evidence = list(incoming.evidence)
        if not evidence:
            evidence = [f"chat:{incoming.session_id}:{incoming.event_id}"]
        return EpisodicEvent(
            event_id=incoming.event_id,
            time=incoming.timestamp,
            participants=incoming.participants,
            summary=incoming.raw_text,
            character_action=None,
            outcome=None,
            importance=importance,
            emotional_valence=emotional_valence,
            evidence=evidence,
            objective_fact=incoming.raw_text,
            channel=incoming.channel,
            event_type=event_type,
            session_id=incoming.session_id,
        )


def build_inmemory_service(**kwargs: Any) -> MemoryService:
    return MemoryService(InMemoryStores(), **kwargs)
