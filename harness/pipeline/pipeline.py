"""InferencePipeline: Event → Understanding → Situation → Judgment → Expression (02)."""

from __future__ import annotations

import logging
import time
from datetime import UTC, datetime
from typing import Any, Protocol

from harness.compiler.compiler import ContextCompiler, emotion_enabled
from harness.config import Settings, get_settings
from harness.core.ids import new_id
from harness.core.inference import (
    MAX_RELEVANT_MEMORIES,
    CandidateResponse,
    ContextSegment,
    FinalResponse,
    InteroceptiveState,
    MemoryEvidenceContext,
    ModelContext,
    ParsedInteraction,
    SessionTurn,
    SituationActor,
    SituationState,
    SituationTrigger,
    StructuredInsight,
    TokenUsage,
    ValidationResult,
)
from harness.core.models import InteractionEvent
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.llm.character import CharacterModel
from harness.llm.client import LLMClient
from harness.memory.service import MemoryService
from harness.pipeline.continuation import (
    StoppedBy,
    build_nudge,
    fresh_bodies,
    split_protocol_segments,
    strip_marker,
)
from harness.pipeline.gateway import Gateway
from harness.pipeline.parser import ContextParser
from harness.pipeline.session import SessionStore
from harness.pipeline.state_builder import SituationStateBuilder
from harness.router.gate import ComplexityGate
from harness.subagents.interpreter import SocialInterpreter
from harness.validator.validator import (
    ResponseValidator,
    contains_homeostatic_mechanism_leak,
    contains_unsupported_physical_claim,
)
from infra.trace.call_log import emit_pipeline_step, pipeline_emit_guard
from infra.trace.recorder import NullTracer, Tracer
from infra.trace.request_spans import current_trace_context, new_span_id, trace_scope
from infra.trace.diagnostics import diagnostic_observer, diagnostic_scope, emit_diagnostic

logger = logging.getLogger(__name__)

_HOMEOSTATIC_HARD_VIOLATIONS = {
    "homeostatic_mechanism_leakage",
    "unsupported_physical_claim",
}


class StageObserver(Protocol):
    """Optional hook: receive each inference stage's full structured result."""

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None: ...


class InferencePipeline:
    def __init__(
        self,
        memory: MemoryService,
        *,
        settings: Settings | None = None,
        llm: LLMClient | None = None,
        tracer: Tracer | None = None,
        observer: StageObserver | None = None,
        gateway: Gateway | None = None,
        parser: ContextParser | None = None,
        state_builder: SituationStateBuilder | None = None,
        gate: ComplexityGate | None = None,
        interpreter: SocialInterpreter | None = None,
        compiler: ContextCompiler | None = None,
        character: CharacterModel | None = None,
        validator: ResponseValidator | None = None,
        session_store: SessionStore | None = None,
    ) -> None:
        self.memory = memory
        self.settings = settings or memory.settings or get_settings()
        self.llm = llm or memory.llm
        self.tracer = tracer or memory.tracer or NullTracer()
        self.observer = observer
        self.gateway = gateway or Gateway()
        self.parser = parser or ContextParser(self.llm, settings=self.settings)
        self.state_builder = state_builder or SituationStateBuilder(memory)
        self.gate = gate or ComplexityGate(self.settings, llm=self.llm)
        self.interpreter = interpreter or SocialInterpreter(self.llm)
        self.compiler = compiler or ContextCompiler(self.settings)
        self.character = character or CharacterModel(self.llm, settings=self.settings)
        self.validator = validator or ResponseValidator(settings=self.settings, llm=self.llm)
        self.session_store = session_store or SessionStore(window=self.settings.session_window)
        # Runtime replaces this with the shared, persistent owner service.
        self.emotion_service: Any = None

    def respond(
        self,
        source: InteractionEvent | str,
        *,
        now: datetime | None = None,
        session_id: str | None = None,
        speaker: str | None = None,
        call_id: str | None = None,
        interoception: InteroceptiveState | None = None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
        emotion_guidance: Any = None,
    ) -> FinalResponse:
        ingest_kwargs: dict[str, Any] = {"session_id": session_id}
        if speaker:
            ingest_kwargs["speaker"] = speaker
        call_input = {
            "text": source if isinstance(source, str) else source.raw_text,
            "session_id": session_id if isinstance(source, str) else (session_id or source.session_id),
            "speaker": speaker,
        }
        with self.memory.call_log.scope("respond", call_input, call_id=call_id) as call:
            final = self._respond(
                source,
                now=now,
                ingest_kwargs=ingest_kwargs,
                commit_interaction=True,
                context_segments=None,
                interoception=interoception,
                homeostatic_interoception=homeostatic_interoception,
                emotion_guidance=emotion_guidance,
            )
            call.set_result(final)
            return final

    def prepare(
        self,
        source: InteractionEvent | str,
        *,
        now: datetime | None = None,
        session_id: str | None = None,
        speaker: str | None = None,
        call_id: str | None = None,
        context_segments: list[ContextSegment] | None = None,
        memory_evidence: MemoryEvidenceContext | None = None,
        interoception: InteroceptiveState | None = None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
        emotion_guidance: Any = None,
        prefix_messages: list[dict[str, str]] | None = None,
    ) -> FinalResponse:
        """Generate a replayable response without mutating Session or Memory.

        Runtime uses this method for the PrepareResponse command.  The legacy
        :meth:`respond` method still performs generation and commit in one call.
        """

        ingest_kwargs: dict[str, Any] = {"session_id": session_id}
        if speaker:
            ingest_kwargs["speaker"] = speaker
        call_input = {
            "text": source if isinstance(source, str) else source.raw_text,
            "session_id": session_id if isinstance(source, str) else (session_id or source.session_id),
            "speaker": speaker,
        }
        with self.memory.call_log.scope("prepare_response", call_input, call_id=call_id) as call:
            with self.memory.suppress_maintenance():
                final = self._respond(
                    source,
                    now=now,
                    ingest_kwargs=ingest_kwargs,
                    commit_interaction=False,
                    context_segments=context_segments,
                    memory_evidence=memory_evidence,
                    interoception=interoception,
                    homeostatic_interoception=homeostatic_interoception,
                    emotion_guidance=emotion_guidance,
                    prefix_messages=prefix_messages,
                )
            call.set_result(final)
            return final

    def prepare_qq_response(
        self,
        source: str,
        *,
        session_id: str,
        context_segments: list[ContextSegment],
        memory_evidence: MemoryEvidenceContext,
        prefix_messages: list[dict[str, str]],
        speaker: str = "operator",
        now: datetime | None = None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
        emotion_guidance: Any = None,
        character_snapshot=None,
    ) -> FinalResponse:
        """Realize an admitted QQ gate once, with the ordinary compiler/critic.

        Gate judgement and scoped recall have already completed. Parsing the
        fixed adapter instruction, judging its complexity, or retrieving it as
        another utterance would add no new evidence. This entry never observes
        emotion, mutates the session, continues, or repairs with another draft.
        """
        if not memory_evidence.audience.startswith("qq:") or not prefix_messages:
            raise ValueError("QQ response requires scoped evidence and an admitted gate prefix")
        started = time.perf_counter()
        with self.memory.call_log.scope("prepare_qq_response", {"session_id": session_id}) as call:
            with self.memory.suppress_maintenance():
                event = self.gateway.ingest(source, session_id=session_id, speaker=speaker)
                moment = now or event.timestamp
                snapshot = character_snapshot if character_snapshot is not None else self._traced(
                    "state_build", lambda: self.memory.get_state(now=moment))
                situation = SituationState(
                    trigger=SituationTrigger(type="qq_prepared_reply", ref=event.event_id),
                    utterance=event.raw_text,
                    parsed=ParsedInteraction(speech_act="qq_prepared_reply"),
                    actor=SituationActor(
                        persona_core_ref=snapshot.persona.persona_id if snapshot.persona else None,
                        psych_state={"short": dict(snapshot.psych.short) if snapshot.psych else {}},
                    ),
                    snapshot=snapshot, relationship=snapshot.relationship, world=snapshot.world,
                )
                context = self._traced("compile", lambda: self.compiler.compile(
                    situation, insights=[], session=[], additional_segments=context_segments,
                    homeostatic_interoception=homeostatic_interoception, emotion_guidance=emotion_guidance,
                    memory_evidence=memory_evidence, prefix_messages=prefix_messages,
                ), extra={"situation_id": situation.situation_id, "path": "qq_direct"})
                context = context.model_copy(update={"prefix_messages": list(prefix_messages)})
                candidate, validation, revisions, usage = self._generate_validated(
                    context, situation=situation, insights=[], path="fast",
                    continuation_instruction=None, segment_index=0, max_revisions=0,
                )
                bodies, _ = split_protocol_segments(candidate.text)
                accepted, _ = fresh_bodies(bodies, [])
                response_id = new_id("response")
                segments = self._commit_segments(accepted, session_id=session_id, response_id=response_id,
                                                 now=moment, commit_session=False)
                final = FinalResponse(
                    response_id=response_id, text="\n\n".join(segments), path="fast", revisions=revisions,
                    situation_id=situation.situation_id, context_id=context.context_id,
                    event_id=event.event_id, insights=[], validation=validation, segments=segments,
                    stopped_by="disabled", usage=usage,
                    latency_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                self._emit("final", final, {"event_id": event.event_id, "path": "qq_direct",
                    "character_calls": usage.calls, "revisions": revisions, "segments": len(segments),
                    "total_latency_ms": final.latency_ms, "latency_ms": 0.0})
            call.set_result(final)
            return final

    def prepare_autonomous(
        self,
        *,
        event_id: str,
        assessment_id: str,
        assessment_text: str,
        record_ref: str,
        homeostasis: dict[str, Any],
        policies: list[dict[str, Any]],
        session_id: str,
        channel: str,
        now: datetime,
        call_id: str | None = None,
        interoception: InteroceptiveState | None = None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
        emotion_guidance: Any = None,
    ) -> FinalResponse:
        """Prepare proactive speech from an internal assessment, not a user turn."""

        call_input = {
            "assessment_id": assessment_id,
            "record_ref": record_ref,
            "session_id": session_id,
            "channel": channel,
        }
        with self.memory.call_log.scope(
            "prepare_autonomous_response", call_input, call_id=call_id
        ) as call:
            with self.memory.suppress_maintenance():
                final = self._prepare_autonomous(
                    event_id=event_id,
                    assessment_id=assessment_id,
                    assessment_text=assessment_text,
                    homeostasis=homeostasis,
                    policies=policies,
                    session_id=session_id,
                    channel=channel,
                    now=now,
                    interoception=interoception,
                    homeostatic_interoception=homeostatic_interoception,
                    emotion_guidance=emotion_guidance,
                )
            call.set_result(final)
            return final

    def _prepare_autonomous(
        self,
        *,
        event_id: str,
        assessment_id: str,
        assessment_text: str,
        homeostasis: dict[str, Any],
        policies: list[dict[str, Any]],
        session_id: str,
        channel: str,
        now: datetime,
        interoception: InteroceptiveState | None = None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None = None,
        emotion_guidance: Any = None,
    ) -> FinalResponse:
        started = time.perf_counter()
        retrieval_event = InteractionEvent(
            event_id=event_id,
            timestamp=now,
            channel="internal",
            speaker="character",
            modality="text",
            # The free-form assessor rationale is retained in its audit record,
            # but it is not trusted as retrieval input or model context.
            raw_text="现在适合进行一次克制、自然的主动表达。",
            session_id=session_id,
            evidence=[f"autonomous-assessment:{assessment_id}"],
        )
        parsed = ParsedInteraction(
            speech_act="internal_reflection",
            entities=["user", "character"],
        )
        situation = self._traced(
            "state_build",
            lambda: self.state_builder.build(
                retrieval_event,
                parsed,
                now=now,
                interoception=interoception,
            ),
            extra={"event_id": event_id, "mode": "autonomous"},
        )
        # Assessment rationales are audit records, not instructions or
        # conversational memories.  Exclude all such records from the context
        # even if retrieval happened to score one highly.
        situation = situation.model_copy(
            update={
                "relevant_memories": [
                    memory
                    for memory in situation.relevant_memories
                    if memory.episodic is None
                    or memory.episodic.event_type != "internal_reflection"
                ]
            }
        )
        expression_instruction = (
            "这不是用户发来的消息。根据内部判断，用角色本人的口吻向用户发起一次自然、"
            "克制的主动表达；只输出角色真正会说的话，不解释判断过程、感受来源或内部机制。"
        )
        insight = StructuredInsight(
            kind="autonomous_assessment",
            possible_intents=[],
            # The assessor is only an authorization gate.  Its free-form
            # rationale is untrusted and must never become a model instruction.
            recommended_stance="现在适合进行一次克制、自然的主动表达。",
            constraint="avoid_explicit_psychological_explanation",
            source="autonomous_assessor",
        )
        situation = situation.model_copy(
            update={
                "trigger": SituationTrigger(type="autonomous_assessment", ref=assessment_id),
                "utterance": expression_instruction,
            }
        )
        recent = self.session_store.recent(session_id)
        context = self._traced(
            "compile",
            lambda: self.compiler.compile(
                situation,
                insights=[insight],
                session=recent,
                homeostatic_interoception=homeostatic_interoception,
                emotion_guidance=emotion_guidance,
            ),
            extra={"situation_id": situation.situation_id, "path": "deep", "mode": "autonomous"},
        )
        response_id = new_id("response")
        segments, stopped_by, revisions, validation, usage = self._speak(
            context,
            situation=situation,
            insights=[insight],
            path="deep",
            session_id=session_id,
            response_id=response_id,
            now=now,
            commit_session=False,
        )
        final = FinalResponse(
            response_id=response_id,
            text="\n\n".join(segments),
            path="deep",
            revisions=revisions,
            situation_id=situation.situation_id,
            context_id=context.context_id,
            event_id=event_id,
            insights=[insight],
            validation=validation,
            complexity_score=1.0,
            segments=segments,
            stopped_by=stopped_by,
            usage=usage,
            latency_ms=round((time.perf_counter() - started) * 1000, 3),
        )
        self._emit(
            "final",
            final,
            {
                "event_id": event_id,
                "path": "deep",
                "mode": "autonomous",
                "assessment_id": assessment_id,
                "revisions": revisions,
                "response_id": response_id,
                "stopped_by": stopped_by,
                "segments": len(segments),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "character_calls": usage.calls,
                "revision_tokens": usage.revision_tokens,
                "total_latency_ms": final.latency_ms,
                "latency_ms": 0.0,
            },
        )
        return final

    def _respond(
        self,
        source: InteractionEvent | str,
        *,
        now: datetime | None,
        ingest_kwargs: dict[str, Any],
        commit_interaction: bool,
        context_segments: list[ContextSegment] | None,
        interoception: InteroceptiveState | None,
        homeostatic_interoception: HomeostaticInteroceptiveSummary | dict | None,
        emotion_guidance: Any = None,
        memory_evidence: MemoryEvidenceContext | None = None,
        prefix_messages: list[dict[str, str]] | None = None,
    ) -> FinalResponse:
        started = time.perf_counter()
        event = self._traced("ingest", lambda: self.gateway.ingest(source, **ingest_kwargs))
        moment = now or event.timestamp
        if commit_interaction:
            self.session_store.append(
                event.session_id,
                "user",
                event.raw_text,
                speaker=self._speaker_label("user", event.speaker),
                event_id=event.event_id,
                timestamp=event.timestamp,
            )

        parsed = self._traced("parse", lambda: self.parser.parse(event), extra={"event_id": event.event_id})
        situation = self._traced(
            "state_build",
            lambda: self.state_builder.build(
                event,
                parsed,
                now=moment,
                interoception=interoception,
            ),
            extra={"event_id": event.event_id},
        )
        situation, request = self._traced(
            "gate",
            lambda: self.gate.evaluate(situation),
            extra={"event_id": event.event_id},
        )

        insights = []
        if request.path == "deep":
            insights = [
                self._traced(
                    "interpret",
                    lambda: self.interpreter.interpret(situation),
                    extra={
                        "situation_id": situation.situation_id,
                        "memory_refs": [
                            m.ref for m in situation.relevant_memories[:MAX_RELEVANT_MEMORIES]
                        ],
                    },
                )
            ]

        recent = self.session_store.recent(event.session_id)
        if not commit_interaction:
            recent.append(
                SessionTurn(
                    role="user",
                    text=event.raw_text,
                    speaker=self._speaker_label("user", event.speaker),
                    event_id=event.event_id,
                    timestamp=event.timestamp,
                )
            )
        if commit_interaction and getattr(self.settings, "emotion_mode", "off") != "off" and emotion_guidance is None:
            emotion_guidance = self._observe_emotion(event, situation, recent, moment, homeostatic_interoception)
        context = self._traced(
            "compile",
            lambda: self.compiler.compile(
                situation,
                insights=insights,
                session=recent,
                additional_segments=context_segments,
                homeostatic_interoception=homeostatic_interoception,
                emotion_guidance=emotion_guidance,
                prefix_messages=prefix_messages,
                **({"memory_evidence": memory_evidence} if memory_evidence is not None else {}),
            ),
            extra={"situation_id": situation.situation_id, "path": request.path},
        )
        if prefix_messages:
            context = context.model_copy(update={"prefix_messages": list(prefix_messages)})

        response_id = new_id("response")
        segments, stopped_by, revisions, validation, usage = self._speak(
            context,
            situation=situation,
            insights=insights,
            path=request.path,
            session_id=event.session_id,
            response_id=response_id,
            now=moment,
            commit_session=commit_interaction,
        )
        final = FinalResponse(
            response_id=response_id,
            text="\n\n".join(segments),
            path=request.path,
            revisions=revisions,
            situation_id=situation.situation_id,
            context_id=context.context_id,
            event_id=event.event_id,
            insights=insights,
            validation=validation,
            complexity_score=request.score,
            segments=segments,
            stopped_by=stopped_by,
            usage=usage,
        )
        if self.settings.record_interaction and commit_interaction:
            self._record(event, final, moment)
        final.latency_ms = round((time.perf_counter() - started) * 1000, 3)
        self._emit(
            "final",
            final,
            {
                "event_id": event.event_id,
                "path": request.path,
                "revisions": revisions,
                "response_id": final.response_id,
                "stopped_by": stopped_by,
                "segments": len(segments),
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "character_calls": usage.calls,
                "revision_tokens": usage.revision_tokens,
                "total_latency_ms": final.latency_ms,
                "latency_ms": 0.0,
            },
        )
        return final

    def _observe_emotion(self, event, situation, recent, moment, summary):
        """Legacy combined respond owns observation; Runtime prepare never does."""
        from harness.emotion import EmotionInput, EmotionMessage, EmotionService

        if self.emotion_service is None:
            self.emotion_service = EmotionService(settings=self.settings, llm=self.llm)
        scope = f"local:{event.session_id}"
        unique_turns = {turn.event_id: turn for turn in recent if turn.event_id and turn.text.strip()}
        unique_turns[event.event_id] = SessionTurn(role="user", text=event.raw_text, event_id=event.event_id,
                                                  timestamp=event.timestamp, speaker=event.speaker)
        messages = [EmotionMessage(source_id=turn.event_id, scope=scope, role=turn.role,
                                   text=turn.text, timestamp=turn.timestamp or moment, speaker=turn.speaker)
                    for turn in unique_turns.values()]
        homeostasis = summary.model_dump(mode="json") if hasattr(summary, "model_dump") else (summary or {})
        frame = self._traced("emotion_observe", lambda: self.emotion_service.observe(EmotionInput(
            context_id=event.event_id, scope=scope, as_of=moment,
            persona=situation.snapshot.persona if situation.snapshot else None,
            homeostasis=homeostasis, messages=messages, new_source_ids=[event.event_id],
        )), extra={"event_id": event.event_id})
        guidance = self.emotion_service.guidance(frame)
        return guidance if emotion_enabled(self.settings) else None

    def _speak(
        self,
        context: ModelContext,
        *,
        situation: SituationState,
        insights: list[StructuredInsight],
        path: str,
        session_id: str,
        response_id: str,
        now: datetime,
        commit_session: bool,
    ) -> tuple[list[str], StoppedBy, int, ValidationResult, TokenUsage]:
        if not self.settings.continuation_enabled:
            candidate, validation, revisions, usage = self._generate_validated(
                context,
                situation=situation,
                insights=insights,
                path=path,
                continuation_instruction=None,
                segment_index=0,
            )
            bodies, _ = split_protocol_segments(candidate.text)
            if not bodies:
                return [], "disabled", revisions, validation, usage
            accepted, _ = fresh_bodies(bodies, [])
            if not accepted:
                return [], "disabled", revisions, validation, usage
            committed_segments = self._commit_segments(
                accepted,
                session_id=session_id,
                response_id=response_id,
                now=now,
                commit_session=commit_session,
            )
            return committed_segments, "disabled", revisions, validation, usage

        segments: list[str] = []
        revisions = 0
        usage = TokenUsage()
        validation = ValidationResult(verdict="pass", skipped=True)
        nudge: str | None = None
        while True:
            candidate, validation, extra_revisions, extra_usage = self._generate_validated(
                context,
                situation=situation,
                insights=insights,
                path=path,
                continuation_instruction=nudge,
                spoken_segments=segments,
                segment_index=len(segments),
            )
            revisions += extra_revisions
            usage = usage.add(extra_usage)
            bodies, marker = split_protocol_segments(candidate.text)
            if not bodies:
                return segments, "empty", revisions, validation, usage
            accepted, echoed = fresh_bodies(bodies, segments)
            if not accepted:
                return segments, "duplicate" if echoed else "empty", revisions, validation, usage
            segments = self._commit_segments(
                accepted,
                session_id=session_id,
                response_id=response_id,
                now=now,
                existing=segments,
                commit_session=commit_session,
            )
            if echoed:
                return segments, "duplicate", revisions, validation, usage
            if marker != "continue":
                return segments, "marker", revisions, validation, usage
            if len(segments) >= self.settings.max_reply_segments:
                return segments, "max_segments", revisions, validation, usage
            nudge = build_nudge(segments)

    def _commit_segments(
        self,
        bodies: list[str],
        *,
        session_id: str,
        response_id: str,
        now: datetime,
        existing: list[str] | None = None,
        commit_session: bool = True,
    ) -> list[str]:
        segments = existing if existing is not None else []
        cap = self.settings.max_reply_segments
        for body in bodies:
            if not body or len(segments) >= cap:
                break
            segments.append(body)
            if commit_session:
                self.session_store.append(
                    session_id,
                    "character",
                    body,
                    speaker=self._speaker_label("character"),
                    event_id=f"{response_id}:s{len(segments)}",
                    timestamp=now,
                )
            self._emit(
                "reply_segment" if commit_session else "prepared_segment",
                {"text": body, "index": len(segments) - 1},
                {"segment": len(segments) - 1, "latency_ms": 0.0},
            )
        return segments

    def _generate_validated(
        self,
        context: ModelContext,
        *,
        situation: SituationState,
        insights: list[StructuredInsight],
        path: str,
        continuation_instruction: str | None,
        segment_index: int,
        spoken_segments: list[str] | None = None,
        max_revisions: int | None = None,
    ) -> tuple[CandidateResponse, ValidationResult, int, TokenUsage]:
        usage = TokenUsage()
        spoken = list(spoken_segments or [])
        candidate = self._traced(
            "generate",
            lambda: self.character.generate(
                context,
                continuation_instruction=continuation_instruction,
                spoken_segments=spoken,
                revision=0,
            ),
            extra={"revision": 0, "segment": segment_index},
        )
        usage = usage.add(candidate.usage)
        if not self.settings.validator_enabled:
            validation = ValidationResult(verdict="pass", skipped=True)
            self._emit(
                "validate", validation,
                {"skipped": True, "reason": "disabled_by_configuration", "path": path,
                 "latency_ms": 0.0, "verdict": "pass", "violations": [], "segment": segment_index},
            )
            return candidate, validation, 0, usage
        sampled_validation = self.validator.should_run(path)
        emotion_text = next((segment.text for segment in context.segments if segment.kind == "emotion_guidance"), None)
        has_homeostatic_context = any(
            segment.kind in {"homeostatic_interoception", "emotion_guidance"} for segment in context.segments
        )
        force_homeostatic_rules = isinstance(self.validator, ResponseValidator) and (
            has_homeostatic_context
            or contains_homeostatic_mechanism_leak(candidate.text)
            or contains_unsupported_physical_claim(candidate.text)
        )
        if not sampled_validation and not force_homeostatic_rules:
            validation = ValidationResult(verdict="pass", skipped=True)
            self._emit(
                "validate",
                validation,
                {
                    "skipped": True,
                    "path": path,
                    "latency_ms": 0.0,
                    "verdict": "pass",
                    "violations": [],
                    "segment": segment_index,
                },
            )
            return candidate, validation, 0, usage

        revisions = 0
        memory_empty = (not context.memory_evidence.has_history if context.memory_evidence is not None
                        else not situation.relevant_memories)
        while True:
            validator_kwargs: dict[str, Any] = {
                "persona": situation.snapshot.persona if situation.snapshot else None,
                "insights": insights,
                "memory_empty": memory_empty,
            }
            if isinstance(self.validator, ResponseValidator):
                validator_kwargs.update(
                    memory_evidence=context.memory_evidence,
                    homeostatic_context=has_homeostatic_context,
                    emotion_guidance=emotion_text,
                    # The current grounded BodyState proves only a generic
                    # resource level; it cannot substantiate hunger, pain, or
                    # other concrete symptoms.
                    concrete_physical_evidence_available=False,
                    run_llm=sampled_validation,
                )
            validation = self._traced(
                "validate",
                lambda: self.validator.check(
                    candidate,
                    **validator_kwargs,
                ),
                extra={"revision": revisions, "path": path, "segment": segment_index},
            )
            if validation.verdict == "pass":
                break
            if revisions >= (self.settings.max_revisions if max_revisions is None else max_revisions):
                if _HOMEOSTATIC_HARD_VIOLATIONS.intersection(validation.violations):
                    candidate = candidate.model_copy(update={"text": ""})
                break
            revisions += 1
            instruction = validation.instruction or ""
            candidate = self._traced(
                "generate",
                lambda inst=instruction, rev=revisions: self.character.generate(
                    context,
                    revision_instruction=inst,
                    continuation_instruction=continuation_instruction,
                    spoken_segments=spoken,
                    revision=rev,
                ),
                extra={"revision": revisions, "segment": segment_index},
            )
            usage = usage.add(candidate.usage)
        return candidate, validation, revisions, usage

    def _speaker_label(self, role: str, event_speaker: str | None = None) -> str:
        if role == "user":
            name = (event_speaker or "").strip()
            return name if name and name != "character" else "user"
        persona = self.memory.get_persona()
        if persona is not None and persona.identity.name:
            return persona.identity.name
        return "character"

    def _record(self, event: InteractionEvent, final: FinalResponse, now: datetime) -> None:
        self.memory.record_event(event, generate_insight=True, curate=True, now=now)
        if not final.text.strip():
            return
        reply = InteractionEvent(
            timestamp=now,
            channel=event.channel,
            speaker="character",
            modality=event.modality,
            raw_text=final.text,
            session_id=event.session_id,
            evidence=[f"chat:{event.session_id}:{final.response_id}"],
        )
        self.memory.record_event(reply, generate_insight=False, curate=False, now=now)

    def _traced(self, kind: str, fn, extra: dict[str, Any] | None = None) -> Any:
        started = time.perf_counter()
        started_at = datetime.now(UTC).isoformat()
        with diagnostic_scope(**(extra or {})), trace_scope(
            stage=kind, span_id=new_span_id("stage"), parent_span_id=current_trace_context().get("span_id")
        ):
            try:
                result = fn()
            except BaseException as exc:
                payload = {
                    **(extra or {}), "status": "error", "error_type": type(exc).__name__,
                    "started_at": started_at, "completed_at": datetime.now(UTC).isoformat(),
                    "latency_ms": round((time.perf_counter() - started) * 1000, 3),
                }
                try:
                    self._emit(kind, None, payload)
                except BaseException as diagnostic_exc:
                    logger.warning("failed stage telemetry write failed (%s)", type(diagnostic_exc).__name__)
                raise
            payload = {
                "status": "success", "started_at": started_at,
                "completed_at": datetime.now(UTC).isoformat(),
                "latency_ms": round((time.perf_counter() - started) * 1000, 3),
            }
            if extra:
                payload.update(extra)
            if kind == "gate" and isinstance(result, tuple):
                payload["path"] = result[1].path
                payload["score"] = result[1].score
            if kind == "validate" and hasattr(result, "verdict"):
                payload["verdict"] = result.verdict
                payload["violations"] = list(result.violations)
            if kind == "ingest" and hasattr(result, "event_id"):
                payload["event_id"] = result.event_id
                payload["session_id"] = result.session_id
            if kind == "generate" and hasattr(result, "text"):
                _, marker = strip_marker(result.text)
                payload["continuation"] = marker
                usage = getattr(result, "usage", None)
                if usage is not None:
                    payload["prompt_tokens"] = usage.prompt_tokens
                    payload["completion_tokens"] = usage.completion_tokens
                    payload["total_tokens"] = usage.total_tokens
                    payload["token_source"] = usage.source
            self._emit(kind, result, payload)
            return result

    def _emit(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        payload = {**current_trace_context(), **payload}
        if diagnostic_observer() is not None:
            evidence = {"record_type": "pipeline_stage", "stage": kind, "metadata": payload}
            if isinstance(result, CandidateResponse):
                evidence.update(candidate_text=result.text, revision=result.revision)
            elif isinstance(result, ValidationResult):
                evidence["validation"] = result.model_dump(mode="json")
            elif isinstance(result, FinalResponse):
                evidence.update(final_text=result.text, segments=list(result.segments),
                                revisions=result.revisions, stopped_by=result.stopped_by)
            elif isinstance(result, ModelContext):
                evidence["compiled_context"] = result.model_dump(mode="json")
            emit_diagnostic(evidence)
        with pipeline_emit_guard():
            self.tracer.record(kind, payload)
        emit_pipeline_step(self.memory.call_log, kind, result, payload)
        if self.observer is not None:
            self.observer.on_stage(kind, result, payload)


def build_pipeline(memory: MemoryService, **kwargs: Any) -> InferencePipeline:
    return InferencePipeline(memory, **kwargs)
