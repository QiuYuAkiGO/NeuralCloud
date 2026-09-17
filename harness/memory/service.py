"""MemoryService facade: record_event / query / get_state / curate."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from contextlib import contextmanager
from datetime import UTC, datetime
import time
from typing import Any, cast

from harness.config import Settings, get_settings
from harness.qq.telemetry import measure_stage
from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
from harness.core.models import (
    CharacterSnapshot,
    CuratorOp,
    EpisodicEvent,
    InteractionEvent,
    PersonaCore,
    PsychState,
    RelationshipState,
    RetrievalPlan,
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
from harness.memory.tier import (
    TierClassifier,
    register_hits,
    review_expiries,
    stamp_retention,
)
from harness.memory.writer.curator import CuratorBackend, default_curator
from harness.memory.writer.pipeline import WriterPipeline
from infra.trace.call_log import CallAwareTracer, CallLog, detached_call_log
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
        self.qq_memory_state: dict[str, Any] = {}
        self.daily_revision_state: dict[str, Any] = {}
        self.settings = settings or get_settings()
        self.llm = llm or MockLLM()
        self.embeddings = embeddings or MockEmbedding(dim=self.settings.embedding_dim)
        self.curator = curator or default_curator(self.llm)
        self.call_log = CallLog(self.settings)
        self.perception = perception or ChatPerceptionBackend()
        self._inner_tracer: Tracer
        self._tracer: Tracer
        self.tracer = tracer or NullTracer()
        self.dual = DualMemoryWriter(self.llm)
        self.pipeline = WriterPipeline(stores, self.embeddings, self.settings)
        self.planner = RetrievalPlanner(self.llm, llm_enabled=self.settings.planner_llm_enabled)
        self._retrieve_pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="retrieve")
        self._tier_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tier")
        self._tier_pending: list[Any] = []
        self._maintenance_accepting = True
        self.hybrid = HybridRetriever(stores, self.embeddings, self.settings)
        self.conflicts = ConflictRetriever(self.hybrid, self.planner, self.settings)
        self.tier_classifier = TierClassifier(self.llm, llm_enabled=self.settings.tier_llm_enabled)

    @property
    def tracer(self) -> Tracer:
        return self._tracer

    @tracer.setter
    def tracer(self, value: Tracer | None) -> None:
        inner: Tracer = value or NullTracer()
        if isinstance(inner, CallAwareTracer):
            inner = inner.inner
        self._inner_tracer = inner
        self._tracer = CallAwareTracer(inner, self.call_log)

    def bootstrap_persona(self, persona: PersonaCore) -> PersonaCore:
        with self.call_log.scope("bootstrap_persona", {"persona_id": persona.persona_id}) as call:
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
            call.set_result(persona)
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
        uniqueness: float | None = None,
        now: datetime | None = None,
    ) -> EpisodicEvent:
        incoming_id = incoming.event_id
        incoming_text = incoming.raw_text if isinstance(incoming, InteractionEvent) else incoming.summary
        incoming_session = incoming.session_id
        with self.call_log.scope(
            "record_event",
            {"event_id": incoming_id, "text": incoming_text, "session_id": incoming_session},
        ) as call:
            event = self._record_event(
                incoming,
                generate_insight=generate_insight,
                curate=curate,
                importance=importance,
                emotional_valence=emotional_valence,
                event_type=event_type,
                uniqueness=uniqueness,
                now=now,
            )
            call.set_result(event)
            return event

    def _record_event(
        self,
        incoming: InteractionEvent | EpisodicEvent,
        *,
        generate_insight: bool,
        curate: bool,
        importance: float,
        emotional_valence: float,
        event_type: str | None,
        uniqueness: float | None,
        now: datetime | None,
    ) -> EpisodicEvent:
        moment = now or _now()
        event = self._as_episodic(
            incoming,
            importance=importance,
            emotional_valence=emotional_valence,
            event_type=event_type,
            uniqueness=uniqueness,
            now=moment,
        )
        event = stamp_retention(event, now=moment, settings=self.settings)
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
                "tier": event.tier,
                "uniqueness": event.uniqueness,
                "expire_at": event.expire_at.isoformat() if event.expire_at else None,
            },
        )
        self._submit_tier(lambda when=moment: self._review_expiries(when))
        if curate:
            self.curate(event)
        return event

    def curate(self, event: EpisodicEvent) -> list[CuratorOp]:
        with self.call_log.scope("curate", {"event_id": event.event_id}) as call:
            persona = self.stores.persona.get()
            candidates = [candidate for candidate in self.stores.candidates.list_open()
                          if candidate.scope == event.scope]
            output = self.curator.propose(event, persona=persona, open_candidates=candidates)
            applied = self.pipeline.apply(output, event=event, strict=False)
            self.tracer.record(
                "curator_ops",
                {"event_id": event.event_id, "ops": [op.model_dump() for op in applied]},
            )
            call.set_result(applied)
            return applied

    def _plan_and_embed(self, query: RetrievalQuery) -> tuple[RetrievalPlan, list[float]]:
        # The plan and the query embedding are independent; run them concurrently
        # so a live planner LLM and a remote embedding API overlap instead of
        # adding their round-trips.
        def plan():
            with measure_stage("memory.plan"):
                return self.planner.plan(query)
        def embed():
            with measure_stage("memory.embedding"):
                return self.embeddings.embed_one(query.text)
        fut_plan = self._retrieve_pool.submit(copy_context().run, plan)
        fut_embed = self._retrieve_pool.submit(copy_context().run, embed)
        return fut_plan.result(), fut_embed.result()

    def query(self, query: RetrievalQuery) -> list[ScoredMemory]:
        with self.call_log.scope(
            "query",
            {"text": query.text, "session_id": query.session_id},
        ) as call:
            if query.persona is None:
                query = query.model_copy(update={"persona": self.stores.persona.get()})
            if query.relationship is None:
                query = query.model_copy(update={"relationship": self.stores.relationship.get("user")})
            plan, query_embedding = self._plan_and_embed(query)
            with measure_stage("memory.ranking"):
                results = self.hybrid.search(query, plan, query_embedding=query_embedding)
            self._submit_tier(lambda items=results, when=query.now: self._after_query(items, when))
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
            call.set_result(results)
            return results

    def query_with_conflicts(self, query: RetrievalQuery) -> tuple[list[ScoredMemory], list[ScoredMemory]]:
        with self.call_log.scope(
            "query_with_conflicts",
            {"text": query.text, "session_id": query.session_id},
        ) as call:
            query = query.model_copy(update={"include_conflicts": True})
            current_inference = query.current_inference
            if current_inference is None:
                raise ValueError("current_inference is required for conflict retrieval")
            if query.persona is None:
                query = query.model_copy(update={"persona": self.stores.persona.get()})
            if query.relationship is None:
                query = query.model_copy(update={"relationship": self.stores.relationship.get("user")})
            plan, query_embedding = self._plan_and_embed(query)
            with measure_stage("memory.ranking"):
                results = self.hybrid.search(query, plan, query_embedding=query_embedding)
            conflicts = self.conflicts.find(query, current_inference)
            self.tracer.record(
                "retrieval",
                {
                    "text": query.text,
                    "plan": plan.model_dump(),
                    "hits": [item.ref for item in results],
                    "conflicts": [item.ref for item in conflicts],
                },
            )
            call.set_result((results, conflicts))
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
        with self.call_log.scope("upsert_psych", {"character_id": state.character_id}) as call:
            moment = now or _now()
            short = dict(state.short)
            if short and "set_at" not in short:
                short["set_at"] = moment.isoformat()
            state = state.model_copy(update={"short": short, "updated_at": moment})
            self.stores.psych.upsert(state)
            self.tracer.record("psych_upsert", {"character_id": state.character_id})
            call.set_result(state)
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
        with self.call_log.scope(
            "apply_relationship_delta",
            {"target": target, "last_major_event": last_major_event},
        ) as call:
            updated = self._apply_relationship_delta(
                target,
                closeness=closeness,
                trust=trust,
                attachment=attachment,
                recent_tension=recent_tension,
                last_major_event=last_major_event,
                history_note=history_note,
                now=now,
            )
            call.set_result(updated)
            return updated

    def _apply_relationship_delta(
        self,
        target: str,
        *,
        closeness: float | None,
        trust: float | None,
        attachment: float | None,
        recent_tension: float | None,
        last_major_event: str | None,
        history_note: str | None,
        now: datetime | None,
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
        with self.call_log.scope(
            "update_world",
            {"channel": None if state is None else state.channel},
        ) as call:
            world = state or self.perception.observe(now=now)
            self.stores.world.insert(world)
            self.tracer.record("world_update", {"time": world.time.isoformat(), "channel": world.channel})
            call.set_result(world)
            return world

    def force_write_semantic(self, fact: SemanticFact) -> SemanticFact:
        """Explicit API that still enforces evidence. Used by negative tests."""
        with self.call_log.scope("force_write_semantic", {"fact_id": fact.fact_id}) as call:
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
            call.set_result(fact)
            return fact

    def wait_background(self, timeout: float = 5.0) -> None:
        """Block until queued tier classify / hit / sweep tasks finish. Tests use this."""
        pending = self._tier_pending
        self._tier_pending = []
        deadline = time.monotonic() + timeout
        for index, fut in enumerate(pending):
            try:
                fut.result(timeout=max(0.0, deadline - time.monotonic()))
            except TimeoutError:
                self._tier_pending.extend(pending[index:])
                raise

    def drain_maintenance(self, timeout: float = 5.0) -> bool:
        """Stop accepting tier work and drain already-owned jobs within a bound."""

        self._maintenance_accepting = False
        try:
            self.wait_background(timeout=timeout)
        except TimeoutError:
            return False
        return True

    def pause_maintenance(self, timeout: float = 5.0) -> bool:
        return self.drain_maintenance(timeout=timeout)

    def resume_maintenance(self) -> None:
        self._maintenance_accepting = True

    @contextmanager
    def suppress_maintenance(self):
        """Temporarily disable tier mutations during replayable prepare work."""

        previous = self._maintenance_accepting
        self._maintenance_accepting = False
        try:
            yield
        finally:
            self._maintenance_accepting = previous

    def close(self) -> None:
        try:
            self.wait_background()
        finally:
            self._tier_pool.shutdown(wait=True, cancel_futures=True)
            self._retrieve_pool.shutdown(wait=True, cancel_futures=True)
            closer = getattr(self.embeddings, "close", None)
            if callable(closer):
                try:
                    closer()
                except Exception:
                    pass

    def _submit_tier(self, fn) -> None:
        if not self._maintenance_accepting:
            return
        fut = self._tier_pool.submit(copy_context().run, self._tier_with_context, fn)
        self._tier_pending.append(fut)

    def _tier_with_context(self, fn) -> None:
        # Maintenance can outlive the initiating call log. Keep its causal
        # trace context, but give its eventual records an independent log.
        with detached_call_log():
            self._tier_safe(fn)

    def _tier_safe(self, fn) -> None:
        try:
            fn()
        except Exception as exc:
            self.tracer.record("tier_error", {"error": str(exc)})

    def _after_query(self, results: list[ScoredMemory], now: datetime) -> None:
        self._register_hits(results, now)
        self._review_expiries(now)

    def _review_expiries(self, now: datetime) -> None:
        reviewed = review_expiries(
            self.stores,
            self.settings,
            self.tier_classifier,
            self.stores.persona.get(),
            now=now,
        )
        if not reviewed:
            return
        payload = {"decisions": [{"ref": ref, "tier": tier} for ref, tier in reviewed]}
        with self.call_log.scope("tier_expiry_review", payload) as call:
            self.tracer.record("tier_expiry_review", payload)
            call.set_result(payload)

    def _register_hits(self, results: list[ScoredMemory], now: datetime) -> None:
        promotions = register_hits(self.stores, results, self.settings, now=now)
        if not promotions:
            return
        payload = {
            "promotions": [{"ref": ref, "from": src, "to": dst} for ref, src, dst in promotions]
        }
        with self.call_log.scope("tier_hits", payload) as call:
            self.tracer.record("tier_hits", payload)
            call.set_result(payload)

    def _as_episodic(
        self,
        incoming: InteractionEvent | EpisodicEvent,
        *,
        importance: float,
        emotional_valence: float,
        event_type: str | None,
        uniqueness: float | None,
        now: datetime,
    ) -> EpisodicEvent:
        if isinstance(incoming, EpisodicEvent):
            if uniqueness is None:
                return incoming
            return incoming.model_copy(update={"uniqueness": uniqueness})
        evidence = list(incoming.evidence)
        if not evidence:
            evidence = [f"chat:{incoming.session_id}:{incoming.event_id}"]
        passed = uniqueness if uniqueness is not None else incoming.uniqueness
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
            uniqueness=passed,
            tier="short",
            tier_updated_at=now,
        )


def build_inmemory_service(**kwargs: Any) -> MemoryService:
    # Protocol attributes are mutable and therefore invariant to mypy even
    # though every concrete in-memory store implements its corresponding port.
    return MemoryService(cast(MemoryStores, InMemoryStores()), **kwargs)
