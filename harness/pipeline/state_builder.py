"""SituationState Builder: parallel state + retrieval → canonical situation (02 §阶段 3–5)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from datetime import datetime
from typing import Any

from harness.core.inference import (
    MAX_RELEVANT_MEMORIES,
    Hypothesis,
    InteroceptiveState,
    ParsedInteraction,
    SituationActor,
    SituationState,
    SituationTrigger,
)
from harness.core.models import CharacterSnapshot, InteractionEvent, RetrievalQuery
from harness.memory.service import MemoryService


class SituationStateBuilder:
    def __init__(self, memory: MemoryService, *, state_cache: dict[str, Any] | None = None) -> None:
        self.memory = memory
        # Reserved for Fast Path cache-first reads; V1 always hits live stores.
        self.state_cache = state_cache if state_cache is not None else {}
        # Long-lived pool: creating a pool per request costs ~1ms, which dominates
        # once both branches are local sub-millisecond work.
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="state-build")

    def build(
        self,
        event: InteractionEvent,
        parsed: ParsedInteraction,
        *,
        now: datetime | None = None,
        interoception: InteroceptiveState | None = None,
    ) -> SituationState:
        moment = now or event.timestamp
        query = RetrievalQuery(
            text=event.raw_text,
            now=moment,
            session_id=event.session_id,
            entities=list(parsed.entities) or ["user", "character"],
        )
        fut_state = self._pool.submit(copy_context().run, self._get_state, moment)
        fut_mem = self._pool.submit(copy_context().run, self.memory.query, query)
        snapshot = fut_state.result()
        # Autonomous-assessment rationales are durable audit artifacts, not
        # conversational evidence. Older state directories may contain them,
        # so exclude them at the shared retrieval boundary for every path.
        memories = [
            memory
            for memory in fut_mem.result()
            if memory.episodic is None
            or memory.episodic.event_type != "internal_reflection"
        ]

        persona_ref = snapshot.persona.persona_id if snapshot.persona else None
        psych_short = dict(snapshot.psych.short) if snapshot.psych else {}
        hypotheses: list[Hypothesis] = []
        if parsed.possible_social_intent is not None:
            hypotheses.append(parsed.possible_social_intent)

        return SituationState(
            trigger=SituationTrigger(type="user_message", ref=event.event_id),
            utterance=event.raw_text,
            parsed=parsed,
            actor=SituationActor(persona_core_ref=persona_ref, psych_state={"short": psych_short}),
            relationship=snapshot.relationship,
            world=snapshot.world,
            relevant_memories=memories[:MAX_RELEVANT_MEMORIES],
            hypotheses=hypotheses,
            snapshot=snapshot,
            interoception=interoception,
        )

    def _get_state(self, now: datetime) -> CharacterSnapshot:
        _ = self.state_cache  # reserved slot; live read
        return self.memory.get_state(now=now)
