"""Runtime-owned offers and model-owned choices for one shared capability call.

An offer is a snapshot, never execution authority. Runtime rechecks its binding
and deadline before dispatch. Model output can reference an offer, not replace it.
"""

from __future__ import annotations

from datetime import datetime
import json
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, BeforeValidator, ConfigDict, Field, model_validator

from harness.embodiment.voice_contracts import Seed, VoiceScript


BOARD_READ = "expression.board.read.v1"
BOARD_EXPRESS = "expression.board.append.v1"
QQ_READ = "communication.qq.read.v1"
QQ_EXPRESS = "communication.qq.send.v1"
VOICE_PROFILES = "expression.voice.profiles.v1"
VOICE_RENDER = "expression.voice.render.v1"
WEB_SEARCH = "web.search.v1"
WEB_READ = "web.read.v1"
SERVER_OBSERVE = "server.observe.v1"
SERVER_EXECUTE = "server.execute.v1"
CHANNEL_CAPABILITIES = {
    "board": {BOARD_READ: "observe", BOARD_EXPRESS: "act"},
    "qq": {QQ_READ: "observe", QQ_EXPRESS: "act"},
    "voice": {VOICE_PROFILES: "observe", VOICE_RENDER: "act"},
    "web": {WEB_SEARCH: "observe", WEB_READ: "observe"},
    "server": {SERVER_OBSERVE: "observe", SERVER_EXECUTE: "act"},
}


def _iso_datetime(value: Any) -> Any:
    # Explicit ISO conversion preserves JSON transport without enabling numeric
    # timestamp, string-bool, or other strict-model coercions.
    if isinstance(value, str):
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    return value


Moment = Annotated[AwareDatetime, BeforeValidator(_iso_datetime)]
Identifier = Annotated[str, Field(min_length=1, max_length=256, pattern=r"\S")]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, revalidate_instances="always")


class VoiceCapabilityArguments(_Strict):
    """Model-controlled performance only; Runtime owns profile and dispatch IDs."""

    script: VoiceScript
    seed: Seed = 0


class CapabilityOffer(_Strict):
    offer_id: Identifier
    channel: Literal["board", "qq", "desktop", "voice", "web", "server"]
    capability_id: Identifier
    provider_id: Identifier
    body_instance_id: Identifier
    scope: dict[str, Any] = Field(default_factory=dict, repr=False)
    activation_revision: Identifier
    input_schema: dict[str, Any] = Field(default_factory=dict, repr=False)
    observation_refs: list[Identifier] = Field(default_factory=list, max_length=64)
    supported: bool
    available: bool
    ready: bool
    valid_until: Moment
    observation: dict[str, Any] | None = Field(default=None, repr=False)

    @model_validator(mode="after")
    def _state(self) -> CapabilityOffer:
        if self.available and not self.supported or self.ready and not self.available:
            raise ValueError("capability_offer_inconsistent_state")
        if len(set(self.observation_refs)) != len(self.observation_refs):
            raise ValueError("capability_offer_duplicate_observation")
        _bounded_json(self.model_dump(mode="json"), 128_000, "capability_offer_too_large")
        return self


class CapabilitySelectionRequest(_Strict):
    opportunity_id: Identifier
    generation: int = Field(ge=0)
    deadline_at: Moment
    homeostatic_summary: dict[str, Any] = Field(default_factory=dict, repr=False)
    offers: list[CapabilityOffer] = Field(default_factory=list, max_length=128)

    @model_validator(mode="after")
    def _catalog(self) -> CapabilitySelectionRequest:
        if len({offer.offer_id for offer in self.offers}) != len(self.offers):
            raise ValueError("capability_duplicate_offer")
        _bounded_json(self.homeostatic_summary, 16_000, "capability_summary_too_large")
        _bounded_json(self.model_dump(mode="json"), 512_000, "capability_catalog_too_large")
        return self


class CapabilityChoice(_Strict):
    kind: Literal["act", "observe", "wait", "defer", "no_action"]
    offer_id: Identifier | None = None
    arguments: dict[str, Any] = Field(default_factory=dict, repr=False)
    success_criteria: list[dict[str, Any]] = Field(default_factory=list, max_length=16, repr=False)
    reason: str | None = Field(default=None, max_length=256, repr=False)
    production_goal_id: Identifier | None = None

    @model_validator(mode="after")
    def _choice(self) -> CapabilityChoice:
        if self.kind in {"act", "observe"}:
            if self.offer_id is None:
                raise ValueError("capability_choice_requires_offer")
        elif self.offer_id is not None or self.arguments or self.success_criteria:
            raise ValueError("capability_nonaction_cannot_invoke")
        _bounded_json(self.model_dump(mode="json"), 64_000, "capability_choice_too_large")
        return self


class PreparedCapabilityChoice(_Strict):
    decision_id: Identifier
    opportunity_id: Identifier
    choice: CapabilityChoice
    prepared_at: Moment
    deadline_at: Moment
    catalog_hash: str = Field(pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _binding(self) -> PreparedCapabilityChoice:
        if self.prepared_at >= self.deadline_at:
            raise ValueError("capability_prepared_after_deadline")
        if self.choice.reason is not None:
            raise ValueError("capability_prepared_reason_not_durable")
        return self


def _bounded_json(value: Any, maximum: int, code: str) -> None:
    try:
        encoded = json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError("capability_non_json_value") from exc
    if len(encoded) > maximum:
        raise ValueError(code)


__all__ = [
    "BOARD_READ", "BOARD_EXPRESS", "QQ_READ", "QQ_EXPRESS", "VOICE_PROFILES", "VOICE_RENDER", "CHANNEL_CAPABILITIES",
    "CapabilityOffer", "CapabilitySelectionRequest", "CapabilityChoice", "PreparedCapabilityChoice",
    "VoiceCapabilityArguments",
    "WEB_SEARCH", "WEB_READ", "SERVER_OBSERVE", "SERVER_EXECUTE",
]
