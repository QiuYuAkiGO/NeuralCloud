"""InferencePipeline: Event → Understanding → Situation → Judgment → Expression (02)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any, Protocol

from harness.compiler.compiler import ContextCompiler
from harness.config import Settings, get_settings
from harness.core.ids import new_id
from harness.core.inference import (
    MAX_RELEVANT_MEMORIES,
    CandidateResponse,
    FinalResponse,
    ModelContext,
    SituationState,
    StructuredInsight,
    TokenUsage,
    ValidationResult,
)
from harness.core.models import InteractionEvent
from harness.llm.character import CharacterModel
from harness.llm.client import LLMClient
from harness.memory.service import MemoryService
from harness.pipeline.continuation import (
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
from harness.validator.validator import ResponseValidator
from infra.trace.call_log import emit_pipeline_step, pipeline_emit_guard
from infra.trace.recorder import NullTracer, Tracer


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

    def respond(
        self,
        source: InteractionEvent | str,
        *,
        now: datetime | None = None,
        session_id: str | None = None,
        speaker: str | None = None,
        call_id: str | None = None,
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
            final = self._respond(source, now=now, ingest_kwargs=ingest_kwargs)
            call.set_result(final)
            return final

    def _respond(
        self,
        source: InteractionEvent | str,
        *,
        now: datetime | None,
        ingest_kwargs: dict[str, Any],
    ) -> FinalResponse:
        started = time.perf_counter()
        event = self._traced("ingest", lambda: self.gateway.ingest(source, **ingest_kwargs))
        moment = now or event.timestamp
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
            lambda: self.state_builder.build(event, parsed, now=moment),
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
        context = self._traced(
            "compile",
            lambda: self.compiler.compile(situation, insights=insights, session=recent),
            extra={"situation_id": situation.situation_id, "path": request.path},
        )

        response_id = new_id("response")
        segments, stopped_by, revisions, validation, usage = self._speak(
            context,
            situation=situation,
            insights=insights,
            path=request.path,
            session_id=event.session_id,
            response_id=response_id,
            now=moment,
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
        if self.settings.record_interaction:
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
    ) -> tuple[list[str], str, int, ValidationResult, TokenUsage]:
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
            segments = self._commit_segments(
                accepted,
                session_id=session_id,
                response_id=response_id,
                now=now,
            )
            return segments, "disabled", revisions, validation, usage

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
    ) -> list[str]:
        segments = existing if existing is not None else []
        cap = self.settings.max_reply_segments
        for body in bodies:
            if not body or len(segments) >= cap:
                break
            segments.append(body)
            self.session_store.append(
                session_id,
                "character",
                body,
                speaker=self._speaker_label("character"),
                event_id=f"{response_id}:s{len(segments)}",
                timestamp=now,
            )
            self._emit(
                "reply_segment",
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
        if not self.validator.should_run(path):
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
        memory_empty = not situation.relevant_memories
        while True:
            validation = self._traced(
                "validate",
                lambda: self.validator.check(
                    candidate,
                    persona=situation.snapshot.persona if situation.snapshot else None,
                    insights=insights,
                    memory_empty=memory_empty,
                ),
                extra={"revision": revisions, "path": path, "segment": segment_index},
            )
            if validation.verdict == "pass" or revisions >= self.settings.max_revisions:
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
        result = fn()
        payload: dict[str, Any] = {"latency_ms": round((time.perf_counter() - started) * 1000, 3)}
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
        with pipeline_emit_guard():
            self.tracer.record(kind, payload)
        emit_pipeline_step(self.memory.call_log, kind, result, payload)
        if self.observer is not None:
            self.observer.on_stage(kind, result, payload)


def build_pipeline(memory: MemoryService, **kwargs: Any) -> InferencePipeline:
    return InferencePipeline(memory, **kwargs)
