"""One main-model JSON call, bounded schema repair, and exact input evidence."""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from harness.cognitive.decision_parser import (
    EXPERIMENTAL_INTENT_PROFILES, ExperimentalDecisionParseError, ExperimentalDecisionParser,
)
from harness.contracts.experimental_decisions import (
    EXPERIMENTAL_DECISION_ADAPTER, EXPERIMENTAL_INTENT_IDS, ExperimentalDecision,
    ExperimentalNoActionDecision,
)
from harness.core.inference import ModelContext, TokenUsage
from harness.llm.client import LLMClient, estimate_chat_usage
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.neutrality import (
    NEUTRALITY_RULES_DIGEST, NEUTRALITY_RULES_VERSION, NeutralityReport, NeutralityScanner,
)


GROUNDED_AGENCY_DECISION_MODE = "grounded_agency_decision"


def _visible_schema(value: Any) -> Any:
    # oneOf + const fields fully express the union. Pydantic's redundant
    # discriminator mapping includes a `file` key that the pinned scanner
    # correctly treats as an unsupported content container. Omit that metadata.
    if isinstance(value, dict):
        return {key: _visible_schema(child) for key, child in value.items() if key != "discriminator"}
    if isinstance(value, list):
        return [_visible_schema(child) for child in value]
    return value


class DecisionAttempt(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    revision: int = Field(ge=0)
    messages: list[dict[str, str]]
    input_context_hash: str
    raw_output_hash: str | None = None
    validation_errors: tuple[str, ...] = ()
    neutrality_report: NeutralityReport


class GroundedAgencyModelResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision: ExperimentalDecision
    success: bool
    attempts: tuple[DecisionAttempt, ...]
    input_context_hash: str
    available_intents_hash: str
    raw_output_hash: str | None = None
    usage: TokenUsage = Field(default_factory=TokenUsage)

    @property
    def messages(self) -> list[dict[str, str]]:
        return self.attempts[-1].messages


class GroundedAgencyDecisionModel:
    def __init__(self, llm: LLMClient, *, max_repairs: int = 1) -> None:
        if type(max_repairs) is not int or not 0 <= max_repairs <= 3:
            raise ValueError("max_repairs must be an integer between 0 and 3")
        self.llm = llm
        self.max_repairs = max_repairs

    def decide(
        self,
        context: ModelContext | list[dict[str, str]],
        *,
        manifest_intents: Iterable[str],
        provider_intents: Iterable[str],
        opaque: bool = True,
        rules_version: str = NEUTRALITY_RULES_VERSION,
        rules_digest: str = NEUTRALITY_RULES_DIGEST,
    ) -> GroundedAgencyModelResult:
        if rules_version != NEUTRALITY_RULES_VERSION or rules_digest != NEUTRALITY_RULES_DIGEST:
            raise ValueError("neutrality rules drift: the pinned rules must match the installed scanner")
        allowed = tuple(sorted(
            set(manifest_intents) & set(provider_intents) & EXPERIMENTAL_INTENT_IDS & EXPERIMENTAL_INTENT_PROFILES.keys()
        ))
        original = context.render_messages() if isinstance(context, ModelContext) else context
        # Deep-copy exact input so model clients and callers cannot mutate evidence.
        import json

        messages: list[dict[str, str]] = json.loads(canonical_json(original))
        directive: dict[str, Any] = {
            "mode": GROUNDED_AGENCY_DECISION_MODE,
            "instruction": (
                "Return exactly one JSON object matching decision_schema. No Markdown, reply text, or reasoning. "
                "Select one kind: act, observe, wait, defer, no_action. Use only available_intents. "
                "observe and wait require their corresponding control intent in available_intents. "
                "expected_observation is an optional prediction."
            ),
            "decision_schema": _visible_schema(EXPERIMENTAL_DECISION_ADAPTER.json_schema()),
            "available_intents": [
                {
                    "intent_id": intent_id,
                    "arguments_schema": EXPERIMENTAL_INTENT_PROFILES[intent_id].arguments_model.model_json_schema(),
                    "criterion_kinds": sorted(EXPERIMENTAL_INTENT_PROFILES[intent_id].criterion_kinds),
                }
                for intent_id in allowed
            ],
        }
        messages.append({"role": "system", "content": canonical_json(directive)})
        input_hash = content_hash(messages)
        attempts: list[DecisionAttempt] = []
        usage = TokenUsage()
        decision: ExperimentalDecision = ExperimentalNoActionDecision(confidence=0.0)
        success = False
        raw_hash: str | None = None
        for revision in range(self.max_repairs + 1):
            # Scan AFTER all schema and revision additions, on every actual call.
            report = NeutralityScanner().assert_neutral(messages, opaque=opaque)
            errors: tuple[str, ...] = ()
            raw_hash = None
            try:
                result_fn = getattr(self.llm, "complete_chat_result", None)
                call_messages = json.loads(canonical_json(messages))
                if callable(result_fn):
                    result = result_fn(call_messages, stream=False)
                    raw = result.text
                    call_usage = result.usage
                    if not call_usage.total_tokens and not call_usage.prompt_tokens:
                        call_usage = estimate_chat_usage(messages, raw)
                else:
                    raw = self.llm.complete_chat(call_messages, stream=False)
                    call_usage = estimate_chat_usage(messages, raw)
                usage = usage.add(call_usage.for_call(revision=revision))
                raw_hash = content_hash(raw)
                decision = ExperimentalDecisionParser(allowed).parse(raw)
                success = True
            except ExperimentalDecisionParseError as exc:
                errors = exc.codes
            except Exception:
                # Transport/provider failure has no semantic fallback or revision.
                errors = ("model_call_failed",)
            attempts.append(DecisionAttempt(
                revision=revision, messages=json.loads(canonical_json(messages)), input_context_hash=report.message_hash,
                raw_output_hash=raw_hash, validation_errors=errors, neutrality_report=report,
            ))
            if success or errors == ("model_call_failed",):
                break
            if revision < self.max_repairs:
                messages.append({
                    "role": "system",
                    "content": (
                        "The previous output failed strict JSON decision validation. "
                        "Return one object matching the supplied schema and available intent arguments."
                    ),
                })
        return GroundedAgencyModelResult(
            decision=decision, success=success, attempts=tuple(attempts), input_context_hash=input_hash,
            available_intents_hash=content_hash(allowed), raw_output_hash=raw_hash, usage=usage,
        )


__all__ = ["DecisionAttempt", "GroundedAgencyDecisionModel", "GroundedAgencyModelResult"]
