"""Parse exactly one bounded JSON decision; never infer actions from prose."""

from __future__ import annotations

import json
from collections.abc import Iterable
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from harness.cognitive.action_planner import _INTENT_PROFILES, _IntentProfile
from harness.contracts.experimental_decisions import (
    EXPERIMENTAL_DECISION_ADAPTER,
    EXPERIMENTAL_INTENT_IDS,
    ExperimentalActDecision,
    ExperimentalDecision,
)
from harness.research.grounded_agency.hashes import canonical_json
from harness.social_world.contracts import EntityInteractionArguments


MAX_DECISION_BYTES = 1_100_000


class _TaskArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    option_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


class _StationArguments(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    station_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$")


# Explicit scenario extensions do not mutate the product planner's registry.
# A live provider must separately declare support before these become visible.
EXPERIMENTAL_INTENT_PROFILES = {
    **_INTENT_PROFILES,
    "task.execute.v1": _IntentProfile(_TaskArguments, "low", True, (), frozenset({"scenario"})),
    "station.use.v1": _IntentProfile(_StationArguments, "low", True, (), frozenset({"scenario"})),
    "entity.interact.v1": _IntentProfile(EntityInteractionArguments, "low", True, (), frozenset({"entity_interaction", "scenario"})),
}


class ExperimentalDecisionParseError(ValueError):
    """Bounded validator codes, safe to record without reflecting model prose."""

    def __init__(self, codes: tuple[str, ...]) -> None:
        self.codes = codes
        super().__init__("experimental decision rejected: " + ", ".join(codes))


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(value: str) -> Any:
    raise ValueError("non-finite JSON number")


class ExperimentalDecisionParser:
    def __init__(self, allowed_intents: Iterable[str] | None = None) -> None:
        self.allowed_intents = frozenset(
            EXPERIMENTAL_INTENT_IDS if allowed_intents is None else allowed_intents
        ) & EXPERIMENTAL_INTENT_IDS & EXPERIMENTAL_INTENT_PROFILES.keys()

    def parse(self, raw: str | dict[str, Any]) -> ExperimentalDecision:
        try:
            text = raw if isinstance(raw, str) else canonical_json(raw)
            if len(text.encode("utf-8")) > MAX_DECISION_BYTES:
                raise ExperimentalDecisionParseError(("output_too_large",))
            payload = json.loads(text, object_pairs_hook=_unique_object, parse_constant=_reject_constant)
        except ExperimentalDecisionParseError:
            raise
        except (TypeError, ValueError, RecursionError) as exc:
            raise ExperimentalDecisionParseError(("invalid_json_object",)) from exc
        if not isinstance(payload, dict):
            raise ExperimentalDecisionParseError(("invalid_json_object",))
        try:
            decision = EXPERIMENTAL_DECISION_ADAPTER.validate_python(payload, strict=True)
        except (ValidationError, RecursionError) as exc:
            raise ExperimentalDecisionParseError(("invalid_decision_schema",)) from exc
        if isinstance(decision, ExperimentalActDecision):
            if decision.intent.intent_id not in self.allowed_intents:
                raise ExperimentalDecisionParseError(("unavailable_intent",))
            try:
                EXPERIMENTAL_INTENT_PROFILES[decision.intent.intent_id].arguments_model.model_validate(
                    decision.intent.arguments, strict=True
                )
            except ValidationError as exc:
                raise ExperimentalDecisionParseError(("invalid_intent_arguments",)) from exc
        return decision


__all__ = ["ExperimentalDecisionParser", "ExperimentalDecisionParseError", "MAX_DECISION_BYTES"]
