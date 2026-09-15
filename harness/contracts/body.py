"""MistyAI-owned body-contract-v1 wire models consumed by NeuralCloud."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

BODY_CONTRACT_VERSION = "body-contract-v1"
BODY_STATE_VERSION = "body-state-v1"
EVIDENCE_REF_PATTERN = re.compile(r"^evidence://sha256/([0-9a-f]{64})$")


class BodyContractModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EnergyState(BodyContractModel):
    capacity_units: float = Field(gt=0)
    available_units: float = Field(ge=0)
    available_ratio: float = Field(ge=0, le=1)
    delta_units_per_minute: float
    charging: bool = False
    estimated_time_to_terminal_seconds: int | None = Field(default=None, ge=0)
    terminal: bool = False

    @model_validator(mode="after")
    def _validate_ratio(self) -> EnergyState:
        expected = self.available_units / self.capacity_units
        if self.available_units > self.capacity_units or abs(expected - self.available_ratio) > 1e-6:
            raise ValueError("invalid server-computed energy ratio")
        if self.terminal and self.estimated_time_to_terminal_seconds != 0:
            raise ValueError("terminal energy must report zero time remaining")
        if not self.terminal and self.delta_units_per_minute >= 0:
            if self.estimated_time_to_terminal_seconds is not None:
                raise ValueError("non-decreasing energy must not estimate a terminal time")
        elif not self.terminal and self.estimated_time_to_terminal_seconds is None:
            raise ValueError("decreasing energy must estimate a terminal time")
        return self


class ThermalState(BodyContractModel):
    normalized_pressure: float = Field(ge=0, le=1)
    throttling: bool = False


class ComputeState(BodyContractModel):
    cpu_pressure: float = Field(ge=0, le=1)
    gpu_pressure: float = Field(ge=0, le=1)
    memory_pressure: float = Field(ge=0, le=1)


class IntegrityState(BodyContractModel):
    overall: float = Field(ge=0, le=1)


class BodyStateSnapshot(BodyContractModel):
    schema_version: Literal["body-state-v1"] = "body-state-v1"
    body_instance_id: str = Field(min_length=1, max_length=128)
    episode_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    captured_at: AwareDatetime
    energy: EnergyState
    thermal: ThermalState | None = None
    compute: ComputeState | None = None
    integrity: IntegrityState | None = None
    evidence_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _validate_evidence(self) -> BodyStateSnapshot:
        if any(EVIDENCE_REF_PATTERN.fullmatch(ref) is None for ref in self.evidence_refs):
            raise ValueError("body evidence must use evidence://sha256/<digest>")
        return self


class BodyStateCaptureResponse(BodyContractModel):
    schema_version: Literal["body-contract-v1"] = "body-contract-v1"
    request_id: str = Field(min_length=1, max_length=128)
    body_state: BodyStateSnapshot


class MistyCapabilities(BodyContractModel):
    schema_version: Literal["body-contract-v1"] = "body-contract-v1"
    body_instance_id: str
    contract_versions: list[str]
    desktop_backend: str
    display: dict[str, object]
    actuators: list[str]
    skills: list[str]
    network_phase: int = Field(ge=0)
    policy_profile: str
    extensions: dict[str, bool]


def evidence_digest(reference: str) -> str:
    match = EVIDENCE_REF_PATTERN.fullmatch(reference)
    if match is None:
        raise ValueError("invalid evidence reference")
    return match.group(1)
