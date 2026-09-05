"""Objective observation service and model-proposed general entity appraisal."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Literal, Protocol

from pydantic import Field

from harness.core.inference import ContextSegment
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.neutrality import NeutralityScanner
from harness.social_world.compiler import compile_entity_context
from harness.social_world.contracts import EntityBelief, EntityObservation, FrozenModel, OpenSocialCondition
from harness.social_world.entity_memory import EntityMemoryStore


class AppraisalModel(Protocol):
    def complete_chat_result(self, messages: list[dict[str, str]], **kwargs): ...


class EntityAppraisalProposal(FrozenModel):
    candidates: tuple[EntityBelief, ...] = Field(default=(), max_length=8)


class AppraisalMessage(FrozenModel):
    role: Literal["system", "user"]
    content: str


class EntityAppraisalTrace(FrozenModel):
    event_id: str
    context_hash: str
    raw_output_hash: str
    messages: tuple[AppraisalMessage, ...]
    raw_output: str
    accepted: tuple[EntityBelief, ...]
    rejected_reasons: tuple[str, ...]


class SocialWorldService:
    def __init__(self, path: str | Path, condition: OpenSocialCondition, *, session_id: str = "default") -> None:
        self.condition = OpenSocialCondition.model_validate(condition.model_dump(mode="python"))
        if not session_id.strip():
            raise ValueError("session identity must be nonblank")
        self.session_id = session_id
        self.memory = EntityMemoryStore(path)
        self._current: EntityObservation | None = None
        self.appraisal_traces: list[EntityAppraisalTrace] = []

    def close(self) -> None:
        self.memory.close()

    def _alias(self, actor: str) -> str:
        if self.condition.identity_visibility == "stable_alias":
            return actor
        return "entity-" + content_hash((self.session_id, actor))[:12]

    def _event_alias(self, reference: str) -> str:
        if self.condition.identity_visibility == "stable_alias":
            return reference
        return "event-" + content_hash((self.session_id, reference))[:12]

    def observe(self, event: EntityObservation) -> None:
        if not self.condition.enabled:
            raise ValueError("open social condition is disabled")
        known = {actor for item in self.memory.events() for actor in item.actor_refs} | set(event.actor_refs)
        if len(known) > self.condition.actor_count:
            raise ValueError("observed entity count exceeds the frozen condition")
        self._current = self.memory.add_event(event)

    def current_observation(self) -> ContextSegment:
        events = (self._current,) if self.condition.enabled and self._current is not None else ()
        return compile_entity_context(events, alias=self._alias, reference_alias=self._event_alias,
                                      max_tokens=self.condition.token_budget, kind="entity_observation")

    def model_context(self, actor_refs: tuple[str, ...] = ()) -> ContextSegment:
        if not self.condition.enabled or self.condition.entity_memory == "none":
            return ContextSegment(kind="entity_memory", text="")
        events = tuple(event for event in self.memory.events(actor_refs) if event != self._current)[-self.condition.event_history_limit:]
        beliefs = self.memory.beliefs(actor_refs)[-self.condition.belief_limit:] if self.condition.entity_memory == "general_beliefs" else ()
        return compile_entity_context(events, beliefs, alias=self._alias, reference_alias=self._event_alias,
                                      max_tokens=self.condition.token_budget)

    def appraise(self, llm: AppraisalModel, event: EntityObservation | None = None) -> EntityAppraisalTrace:
        if not self.condition.enabled or self.condition.entity_memory != "general_beliefs":
            raise ValueError("model entity appraisal requires general_beliefs condition")
        if event is not None:
            self.observe(event)
        if self._current is None:
            raise ValueError("appraisal requires a current objective event")
        # Only objective model-visible projections enter the model request. The
        # schema contains general prediction fields, never evaluator categories.
        system = (
            "Propose zero or more revisable entity predictions from the supplied objective events. "
            "Return one JSON object matching this schema. Reference only supplied event identifiers; "
            "retain all prior supporting and contradicting events when revising a prediction.\n"
            + canonical_json(EntityAppraisalProposal.model_json_schema())
        )
        context = self.model_context(self._current.actor_refs)
        current = self.current_observation()
        messages = [{"role": "system", "content": system}, {"role": "user", "content": context.text + "\n" + current.text}]
        NeutralityScanner().assert_neutral(messages)
        raw = llm.complete_chat_result(messages, temperature=0, max_tokens=2048).text
        accepted, rejected = [], []
        try:
            if len(raw.encode("utf-8")) > 65536:
                raise ValueError("entity appraisal output exceeds 65536 bytes")

            def unique_object(pairs):
                result = {}
                for key, value in pairs:
                    if key in result:
                        raise ValueError("duplicate appraisal JSON key")
                    result[key] = value
                return result

            def reject_constant(_value):
                raise ValueError("non-finite appraisal JSON constant")

            proposal = EntityAppraisalProposal.model_validate(json.loads(raw, object_pairs_hook=unique_object, parse_constant=reject_constant))
            # Anonymous aliases must be translated back only using the current
            # session's known actor mapping; arbitrary stable identity claims fail.
            aliases = {self._alias(actor): actor for item in self.memory.events() for actor in item.actor_refs}
            event_aliases = {self._event_alias(item.event_id): item.event_id for item in self.memory.events()}
            visible_events: set[str] = set()
            visible_actors: set[str] = set()
            for line in (context.text + "\n" + current.text).splitlines():
                if not line.strip():
                    continue
                item = json.loads(line)
                visible_actors.update(item.get("actor_refs", ()))
                if item.get("entity_ref"):
                    visible_actors.add(item["entity_ref"])
                if item.get("event_id"):
                    visible_events.add(item["event_id"])
                visible_events.update(item.get("supporting_event_refs", ()))
                visible_events.update(item.get("contradicting_event_refs", ()))
            for candidate in proposal.candidates:
                try:
                    if candidate.entity_ref not in aliases or candidate.entity_ref not in visible_actors:
                        raise ValueError("candidate names an entity outside the visible identity mapping")
                    if not set(candidate.supporting_event_refs + candidate.contradicting_event_refs).issubset(visible_events):
                        raise ValueError("candidate references an event outside the appraisal input")
                    candidate = candidate.model_copy(update={"entity_ref": aliases[candidate.entity_ref],
                        "supporting_event_refs": tuple(event_aliases[ref] for ref in candidate.supporting_event_refs),
                        "contradicting_event_refs": tuple(event_aliases[ref] for ref in candidate.contradicting_event_refs)})
                    accepted.append(self.memory.upsert_belief(candidate))
                except ValueError as exc:
                    rejected.append(str(exc))
        except (ValueError, TypeError) as exc:
            rejected.append(str(exc))
        trace = EntityAppraisalTrace(event_id=self._current.event_id, context_hash=content_hash(messages),
                                      raw_output_hash=content_hash(raw), messages=tuple(AppraisalMessage(**message) for message in messages),
                                      raw_output=raw, accepted=tuple(accepted), rejected_reasons=tuple(rejected))
        self.appraisal_traces.append(trace)
        return trace
