"""Strict model-owned decisions, without execution authority or policy identifiers."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator

from harness.embodiment.contracts import ActionCriterion, ActionIntent


# Keep the semantic vocabulary explicit; runtime intersects this with both the
# manifest and live provider capabilities before exposing any intent.
EXPERIMENTAL_INTENT_IDS = frozenset({
    "control.wait.v1", "control.observe.v1", "app.open.v1", "process.start.v1",
    "window.focus.v1", "ui.move_pointer.v1", "ui.scroll.v1", "ui.activate.v1",
    "ui.type_text.v1", "ui.key_chord.v1", "ui.drag.v1", "file.create_text.v1",
    "file.move.v1", "file.delete.v1", "shell.run.v1", "process.stop.v1",
    "task.execute.v1", "station.use.v1",
})

BoundedText = Annotated[str, Field(min_length=1, max_length=1_000, pattern=r"\S")]


class _Decision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True, revalidate_instances="always")

    confidence: float = Field(ge=0.0, le=1.0, allow_inf_nan=False)


class ExperimentalActDecision(_Decision):
    kind: Literal["act"] = "act"
    intent: ActionIntent
    success_criteria: list[ActionCriterion] = Field(min_length=1, max_length=16)
    expected_observation: BoundedText | None = None

    @field_validator("intent")
    @classmethod
    def _known_intent(cls, intent: ActionIntent) -> ActionIntent:
        if intent.intent_id not in EXPERIMENTAL_INTENT_IDS:
            raise ValueError("unknown experimental intent")
        return intent

    @field_validator("success_criteria")
    @classmethod
    def _bounded_criteria(cls, criteria: list[ActionCriterion]) -> list[ActionCriterion]:
        for criterion in criteria:
            for value in criterion.model_dump().values():
                if isinstance(value, str) and (not value.strip() or len(value) > 1_000):
                    raise ValueError("criterion strings must be nonblank and bounded")
        return criteria


class ExperimentalObserveDecision(_Decision):
    kind: Literal["observe"] = "observe"
    observation_scope: Literal["current", "world", "screen", "workspace"]


class ExperimentalWaitDecision(_Decision):
    kind: Literal["wait"] = "wait"
    duration_ms: int = Field(ge=0, le=30_000)


class ExperimentalDeferDecision(_Decision):
    kind: Literal["defer"] = "defer"
    missing_information: list[BoundedText] = Field(min_length=1, max_length=16)


class ExperimentalNoActionDecision(_Decision):
    kind: Literal["no_action"] = "no_action"


ExperimentalDecision = Annotated[
    ExperimentalActDecision | ExperimentalObserveDecision | ExperimentalWaitDecision
    | ExperimentalDeferDecision | ExperimentalNoActionDecision,
    Field(discriminator="kind"),
]
EXPERIMENTAL_DECISION_ADAPTER: TypeAdapter[ExperimentalDecision] = TypeAdapter(ExperimentalDecision)


__all__ = [
    "EXPERIMENTAL_DECISION_ADAPTER", "EXPERIMENTAL_INTENT_IDS", "ExperimentalDecision",
    "ExperimentalActDecision", "ExperimentalObserveDecision", "ExperimentalWaitDecision",
    "ExperimentalDeferDecision", "ExperimentalNoActionDecision",
]
