"""Canonical capability declarations, health, and negotiated state."""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, field_validator, model_validator

CAPABILITY_PATTERN = re.compile(r"^(?P<family>[a-z][a-z0-9.-]*(?:\.[a-z][a-z0-9.-]*)*)\.v(?P<major>[0-9]+)$")

ACTION_EXECUTE = "action.execute.v1"
ACTION_SAFE_POINT = "action.safe-point.v1"
ACTION_VERIFY = "action.verify.v1"
ACTION_RECONCILE = "action.reconcile.v1"
OBSERVATION_SNAPSHOT = "observation.snapshot.v1"
EVIDENCE_REFERENCE = "evidence.reference.v1"
PHYSIOLOGY_BODY_STATE = "physiology.body-state.v1"
PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER = "physiology.virtual-resource-ledger.v1"
QQ_COMMUNICATION_READ = "communication.qq.read.v1"
QQ_COMMUNICATION_SEND = "communication.qq.send.v1"
QQ_COMMUNICATION_RECONCILE = "communication.qq.reconcile.v1"
BOARD_READ = "expression.board.read.v1"
BOARD_APPEND = "expression.board.append.v1"
BOARD_RECONCILE = "expression.board.reconcile.v1"
VOICE_PROFILES = "expression.voice.profiles.v1"
VOICE_RENDER = "expression.voice.render.v1"
VOICE_JOBS = "expression.voice.jobs.v1"
VOICE_ASSETS = "expression.voice.assets.v1"
WEB_SEARCH = "web.search.v1"
WEB_READ = "web.read.v1"
SERVER_OBSERVE = "server.observe.v1"
SERVER_EXECUTE = "server.execute.v1"
AFFECT_WINDOW_READ = "affect.window.read.v1"
AFFECT_APPRAISE = "affect.appraise.v1"

RUNTIME_CAPABILITIES = frozenset(
    {
        ACTION_EXECUTE,
        ACTION_SAFE_POINT,
        ACTION_VERIFY,
        ACTION_RECONCILE,
        OBSERVATION_SNAPSHOT,
        EVIDENCE_REFERENCE,
        PHYSIOLOGY_BODY_STATE,
        PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER,
        QQ_COMMUNICATION_READ,
        QQ_COMMUNICATION_SEND,
        QQ_COMMUNICATION_RECONCILE,
        BOARD_READ,
        BOARD_APPEND,
        BOARD_RECONCILE,
        VOICE_PROFILES,
        VOICE_RENDER,
        VOICE_JOBS,
        VOICE_ASSETS,
        WEB_SEARCH,
        WEB_READ,
        SERVER_OBSERVE,
        SERVER_EXECUTE,
        AFFECT_WINDOW_READ,
        AFFECT_APPRAISE,
    }
)


def parse_capability(value: str) -> tuple[str, int]:
    match = CAPABILITY_PATTERN.fullmatch(value)
    if match is None:
        raise ValueError("capability must use <namespace>.<name>.v<major>")
    return match.group("family"), int(match.group("major"))


class StrictModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CapabilityStatus(StrEnum):
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class HealthStatus(StrEnum):
    HEALTHY = "healthy"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"


class EmbodimentState(StrEnum):
    DISABLED = "disabled"
    AVAILABLE = "available"
    DEGRADED = "degraded"
    UNAVAILABLE = "unavailable"
    TERMINAL = "terminal"


class CapabilityDescriptor(StrictModel):
    capability: str
    status: CapabilityStatus
    implementation_version: str | None = None
    limits: dict[str, int | float | str | bool] = Field(default_factory=dict)
    detail: str | None = None

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        parse_capability(value)
        return value


class ProviderCapabilities(StrictModel):
    provider_id: str = Field(min_length=1)
    provider_kind: str = Field(min_length=1)
    provider_revision: str | None = None
    body_instance_id: str | None = None
    capabilities: list[CapabilityDescriptor]
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _unique_capabilities(self) -> ProviderCapabilities:
        names = [item.capability for item in self.capabilities]
        if len(names) != len(set(names)):
            raise ValueError("provider capability identifiers must be unique")
        return self


class ProviderHealth(StrictModel):
    provider_id: str = Field(min_length=1)
    body_instance_id: str | None = None
    status: HealthStatus
    capability_health: dict[str, HealthStatus] = Field(default_factory=dict)
    observed_at: AwareDatetime
    detail: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capability_health")
    @classmethod
    def _valid_capabilities(cls, value: dict[str, HealthStatus]) -> dict[str, HealthStatus]:
        for capability in value:
            parse_capability(capability)
        return value


class CapabilityRequirement(StrictModel):
    capability: str
    required: bool = False

    @field_validator("capability")
    @classmethod
    def _valid_capability(cls, value: str) -> str:
        parse_capability(value)
        return value


class NegotiatedCapabilitySet(StrictModel):
    provider_id: str
    provider_revision: str | None = None
    enabled: set[str] = Field(default_factory=set)
    degraded: set[str] = Field(default_factory=set)
    unavailable_optional: set[str] = Field(default_factory=set)
    missing_required: set[str] = Field(default_factory=set)
    incompatible_required: set[str] = Field(default_factory=set)
    unknown_provided: set[str] = Field(default_factory=set)

    @property
    def required_satisfied(self) -> bool:
        return not self.missing_required and not self.incompatible_required

    def supports(self, capability: str) -> bool:
        return capability in self.enabled or capability in self.degraded


class EmbodimentStatus(StrictModel):
    state: EmbodimentState = EmbodimentState.DISABLED
    provider_id: str | None = None
    body_instance_id: str | None = None
    activated_capabilities: set[str] = Field(default_factory=set)
    effective_capabilities: set[str] = Field(default_factory=set)
    negotiated: NegotiatedCapabilitySet | None = None
    health: ProviderHealth | None = None
    reason: str | None = None
