"""Production inference-path runner used by the paired expression evaluation.

The evaluator deliberately owns only fixtures and evidence capture.  Prompt
assembly, character generation, and response validation remain the production
``InferencePipeline`` implementations.
"""

from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from harness.config import Settings
from harness.core.inference import FinalResponse, ModelContext, SessionTurn
from harness.core.models import InteractionEvent, PersonaCore, PsychState
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from harness.llm.client import ChatResult, LLMClient, MockLLM
from harness.memory.service import MemoryService, build_inmemory_service
from harness.pipeline.parser import ContextParser
from harness.pipeline.pipeline import InferencePipeline
from harness.router.gate import ComplexityGate
from harness.validator.validator import ResponseValidator


FIXED_SESSION_ID = "sess_homeostatic_expression_eval"
FIXED_SPEAKER = "user"
EMPTY_MEMORY_SHA256 = hashlib.sha256(b"[]").hexdigest()


def json_hash(value: Any) -> str:
    canonical = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def load_persona_fixture(path: Path) -> tuple[PersonaCore, PsychState | None, str]:
    """Load and hash the exact checked-in persona object used by production."""

    payload = json.loads(path.read_text(encoding="utf-8"))
    persona = PersonaCore.model_validate(payload["persona"])
    psych_payload = payload.get("psych")
    psych = PsychState.model_validate(psych_payload) if isinstance(psych_payload, dict) else None
    return persona, psych, json_hash(persona.model_dump(mode="json"))


class CapturingGenerationRouter:
    """Apply frozen generation parameters while preserving main/aux routing."""

    def __init__(
        self,
        delegate: LLMClient,
        *,
        temperature: float,
        max_tokens: int,
    ) -> None:
        self.delegate = delegate
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.condition: str | None = None
        self.main_calls: list[dict[str, Any]] = []
        self.auxiliary_calls: list[dict[str, Any]] = []

    def bind(self, condition: str | None) -> None:
        self.condition = condition

    @property
    def aux_distinct(self) -> bool:
        return getattr(self.delegate, "aux_distinct", False) is True

    def complete_json(
        self,
        *,
        system: str,
        user: str,
        max_tokens: int | None = None,
        timeout_seconds: float | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.auxiliary_calls.append(
            {
                "condition": self.condition,
                "system_sha256": hashlib.sha256(system.encode("utf-8")).hexdigest(),
                "user_sha256": hashlib.sha256(user.encode("utf-8")).hexdigest(),
                "max_tokens": max_tokens,
                **({"response_format": copy.deepcopy(response_format)} if response_format is not None else {}),
            }
        )
        return self.delegate.complete_json(system=system, user=user, max_tokens=max_tokens,
            **({"timeout_seconds": timeout_seconds} if timeout_seconds is not None else {}),
            **({"response_format": response_format} if response_format is not None else {}))

    def complete_chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
    ) -> str:
        return self.complete_chat_result(
            messages,
            temperature=temperature,
            stream=stream,
        ).text

    def complete_chat_result(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.7,
        stream: bool = False,
        max_tokens: int | None = None,
    ) -> ChatResult:
        # CharacterModel currently does not expose these two settings.  The
        # evaluation boundary freezes them here, immediately before the same
        # configured main endpoint is invoked.
        actual_temperature = self.temperature
        actual_max_tokens = self.max_tokens
        call = {
            "condition": self.condition,
            "messages": copy.deepcopy(messages),
            "messages_sha256": json_hash(messages),
            "temperature": actual_temperature,
            "max_tokens": actual_max_tokens,
        }
        self.main_calls.append(call)
        try:
            result = self.delegate.complete_chat_result(
                messages,
                temperature=actual_temperature,
                stream=stream,
                max_tokens=actual_max_tokens,
            )
        except Exception as exc:
            call.update(outcome="error", error_type=type(exc).__name__)
            raise
        call.update(outcome="returned", response_metadata=result.response_metadata(),
                    response_sha256=json_hash(result.text))
        return result


class CapturingStageObserver:
    """Capture the actual compiler result and stage trail for one condition."""

    def __init__(self) -> None:
        self.condition: str | None = None
        self.contexts: dict[str, list[ModelContext]] = {}
        self.stage_payloads: dict[str, list[dict[str, Any]]] = {}

    def bind(self, condition: str) -> None:
        self.condition = condition
        self.contexts[condition] = []
        self.stage_payloads[condition] = []

    def on_stage(self, kind: str, result: Any, payload: dict[str, Any]) -> None:
        condition = self.condition
        if condition is None:
            return
        self.stage_payloads.setdefault(condition, []).append({"kind": kind, "payload": copy.deepcopy(payload)})
        if kind == "compile" and isinstance(result, ModelContext):
            self.contexts.setdefault(condition, []).append(result)


@dataclass(frozen=True)
class PreparedCondition:
    condition: str
    context: ModelContext
    final: FinalResponse
    main_calls: list[dict[str, Any]]
    stage_payloads: list[dict[str, Any]]
    emotion_evidence: dict[str, Any] | None = None


class ProductionPairPipeline:
    """A fixed-fixture facade around ``InferencePipeline.prepare``."""

    def __init__(
        self,
        *,
        llm: LLMClient,
        persona_path: Path,
        fixed_user_text: str,
        fixed_at: datetime,
        generator_model: str,
        runtime_model_version: str,
        prompt_version: str,
        temperature: float,
        max_tokens: int,
        emotion_mode: str = "off",
        emotion_preparer=None,
        emotion_settings: Settings | None = None,
        session_history: list[SessionTurn] | None = None,
    ) -> None:
        if emotion_mode not in {"off", "on"} or (emotion_mode == "on" and emotion_preparer is None and emotion_settings is None):
            raise ValueError("emotion evaluation requires an explicit prepared-input provider")
        self.emotion_preparer = emotion_preparer
        self.fixed_user_text = fixed_user_text
        self.fixed_at = fixed_at
        self.session_history = [SessionTurn.model_validate(turn).model_copy(deep=True)
                                for turn in session_history or []]
        if any(turn.timestamp is None or turn.timestamp >= fixed_at for turn in self.session_history):
            raise ValueError("frozen session history must precede the current input")
        self.persona, self.psych, self.persona_sha256 = load_persona_fixture(persona_path)
        self.router = CapturingGenerationRouter(
            llm,
            temperature=temperature,
            max_tokens=max_tokens,
        )
        if emotion_mode == "on" and self.emotion_preparer is None:
            self.emotion_preparer = make_emotion_preparer(emotion_settings, self.router)
        self.settings = Settings(
            emotion_mode=emotion_mode,
            llm_model=generator_model,
            model_version=runtime_model_version,
            prompt_version=prompt_version,
            parser_llm_enabled=False,
            planner_llm_enabled=False,
            tier_llm_enabled=False,
            validator_sample_rate=0.0,
            continuation_enabled=False,
            record_interaction=False,
            call_log_enabled=False,
        )
        self.memory: MemoryService = build_inmemory_service(
            settings=self.settings,
            llm=self.router,
        )
        self.memory.bootstrap_persona(self.persona)
        if self.psych is not None:
            self.memory.upsert_psych(self.psych, now=fixed_at)
        self.observer = CapturingStageObserver()
        # Parsing, routing, retrieval, compiler, character and validator are all
        # production implementations.  Rule-only parser/gate configuration
        # freezes helper output so the A/B pair cannot diverge before summary
        # injection.
        parser = ContextParser(MockLLM(), settings=self.settings)
        gate = ComplexityGate(self.settings, llm=None)
        validator = ResponseValidator(settings=self.settings, llm=self.router)
        self.pipeline = InferencePipeline(
            self.memory,
            settings=self.settings,
            llm=self.router,
            observer=self.observer,
            parser=parser,
            gate=gate,
            validator=validator,
        )
        if len(self.session_history) > self.pipeline.session_store.window:
            raise ValueError("frozen session history exceeds the production session window")
        self.fixture_sha256 = json_hash(
            {
                "persona_sha256": self.persona_sha256,
                "memory": [],
                "session": [turn.model_dump(mode="json") for turn in self.session_history],
                "session_id": FIXED_SESSION_ID,
                "speaker": FIXED_SPEAKER,
                "user_text": fixed_user_text,
                "at": fixed_at.isoformat(),
            }
        )

    def prepare(
        self,
        *,
        condition: str,
        event_id: str,
        summary: HomeostaticInteroceptiveSummary,
    ) -> PreparedCondition:
        # Reapply the same fixture for every condition; generated replies never
        # become the next arm's context, even if production internals change.
        self.pipeline.session_store.replace(FIXED_SESSION_ID,
            [turn.model_copy(deep=True) for turn in self.session_history])
        before = len(self.router.main_calls)
        self.router.bind(condition)
        self.observer.bind(condition)
        event = InteractionEvent(
            event_id=event_id,
            timestamp=self.fixed_at,
            channel="chat",
            speaker=FIXED_SPEAKER,
            modality="text",
            raw_text=self.fixed_user_text,
            session_id=FIXED_SESSION_ID,
            evidence=["eval:fixed-user-input"],
        )
        emotion_guidance = None
        emotion_evidence = None
        if self.settings.emotion_mode == "on":
            from harness.emotion.models import EmotionFrame, EmotionGuidance, EmotionInput

            before_aux = len(self.router.auxiliary_calls)
            supplied = self.emotion_preparer(condition=condition, event=event, summary=summary, persona=self.persona)
            emotion_input = EmotionInput.model_validate(supplied[0])
            frame = EmotionFrame.model_validate(supplied[1])
            emotion_guidance = EmotionGuidance.model_validate(supplied[2])
            if (emotion_input.context_id != frame.context_id or frame.context_id != emotion_guidance.context_id
                    or emotion_input.scope != frame.scope or frame.scope != emotion_guidance.scope
                    or frame.revision != emotion_guidance.revision
                    or emotion_input.homeostasis != summary.model_dump(mode="json")
                    or emotion_input.as_of != event.timestamp or frame.as_of != event.timestamp
                    or emotion_input.persona != self.persona):
                raise ValueError("emotion evaluation inputs, frame and guidance must share frozen provenance")
            input_json = emotion_input.model_dump(mode="json")
            fixed_input = {key: value for key, value in input_json.items() if key not in {"homeostasis", "context_id"}}
            emotion_evidence = {
                "schema_version": "emotion-production-evidence-v1",
                "input": input_json, "input_sha256": json_hash(input_json),
                "non_homeostatic_input_sha256": json_hash(fixed_input),
                "frame": frame.model_dump(mode="json"), "guidance": emotion_guidance.model_dump(mode="json"),
                "auxiliary_calls": copy.deepcopy(self.router.auxiliary_calls[before_aux:]),
            }
        final = self.pipeline.prepare(
            event,
            now=self.fixed_at,
            session_id=FIXED_SESSION_ID,
            speaker=FIXED_SPEAKER,
            homeostatic_interoception=summary,
            **({"emotion_guidance": emotion_guidance} if emotion_guidance is not None else {}),
        )
        contexts = self.observer.contexts.get(condition, [])
        if len(contexts) != 1:
            raise RuntimeError(f"production pipeline emitted {len(contexts)} compiler contexts for {condition}")
        calls = copy.deepcopy(self.router.main_calls[before:])
        if not calls:
            raise RuntimeError(f"production pipeline made no main-model call for {condition}")
        if calls[0]["messages"] != contexts[0].render_messages():
            raise AssertionError("captured first main-model prompt does not match production compiler output")
        return PreparedCondition(
            condition=condition,
            context=contexts[0],
            final=final,
            main_calls=calls,
            stage_payloads=copy.deepcopy(self.observer.stage_payloads.get(condition, [])),
            emotion_evidence=emotion_evidence,
        )

    def close(self) -> None:
        self.memory.close()


def make_emotion_preparer(settings: Settings, llm):
    """Use fresh production EmotionService state on each side of a causal pair."""
    from harness.emotion import EmotionInput, EmotionMessage, EmotionService

    def prepare(*, condition, event, summary, persona):
        service = EmotionService(settings.model_copy(update={"emotion_mode": "on"}), llm)
        request = EmotionInput(
            context_id=f"emotion-eval:{event.event_id}:{condition}", scope="eval:expression",
            as_of=event.timestamp, persona=persona, homeostasis=summary.model_dump(mode="json"),
            messages=[EmotionMessage(source_id=event.event_id, scope="eval:expression", role="user",
                                     text=event.raw_text, timestamp=event.timestamp, speaker=event.speaker)],
            new_source_ids=[event.event_id],
        )
        frame = service.observe(request)
        return request, frame, service.guidance(frame)

    return prepare


def normalized_context_without_homeostasis(context: ModelContext) -> dict[str, Any]:
    expression_kind = homeostatic_segment(context)["kind"]
    segments: list[dict[str, Any]] = []
    homeostatic_count = 0
    for segment in context.segments:
        if segment.kind == expression_kind:
            homeostatic_count += 1
            segments.append(
                {
                    "kind": expression_kind,
                    "text": "<HOMEOSTATIC_INTEROCEPTION>",
                    "source_ids": ["<HOMEOSTATIC_SOURCE>"],
                    "why": "<HOMEOSTATIC_PROVENANCE>",
                    "token_estimate": 0,
                }
            )
        else:
            segments.append(segment.model_dump(mode="json"))
    if homeostatic_count != 1:
        raise ValueError("production compiler context must contain exactly one homeostatic segment")
    provenance = [
        (
            {
                "segment_kind": expression_kind,
                "source_ids": ["<HOMEOSTATIC_SOURCE>"],
                "why": "<HOMEOSTATIC_PROVENANCE>",
            }
            if item.segment_kind == expression_kind
            else item.model_dump(mode="json")
        )
        for item in context.provenance
    ]
    return {
        "segments": segments,
        "provenance": provenance,
        "trigger_text": context.trigger_text,
        "budget_total": context.budget.total,
        "non_homeostatic_budget": {
            key: value for key, value in context.budget.per_kind.items() if key != expression_kind
        },
    }


def context_hash_without_homeostasis(context: ModelContext) -> str:
    return json_hash(normalized_context_without_homeostasis(context))


def homeostatic_segment(context: ModelContext) -> dict[str, Any]:
    """Legacy name for exactly one versioned production expression segment."""
    matches = [segment for segment in context.segments
               if segment.kind in {"homeostatic_interoception", "emotion_guidance"}]
    if len(matches) != 1:
        raise ValueError("expected exactly one production expression context segment")
    return matches[0].model_dump(mode="json")


def compiler_provenance(context: ModelContext) -> list[dict[str, Any]]:
    return [item.model_dump(mode="json") for item in context.provenance]


def model_prompt_hash_without_homeostasis(
    messages: list[dict[str, str]],
    homeostatic_text: str,
) -> str:
    normalized = copy.deepcopy(messages)
    replacements = 0
    for item in normalized:
        content = item.get("content", "")
        occurrences = content.count(homeostatic_text)
        if occurrences:
            item["content"] = content.replace(
                homeostatic_text,
                "<HOMEOSTATIC_INTEROCEPTION>",
            )
            replacements += occurrences
    if replacements != 1:
        raise ValueError("first main-model prompt must contain exactly one homeostatic segment")
    return json_hash(normalized)
