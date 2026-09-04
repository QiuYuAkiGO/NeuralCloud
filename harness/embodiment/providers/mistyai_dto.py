"""Private validation models for MistyAI body-contract-v1 responses."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class NativeModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class NativeComponentHealth(NativeModel):
    status: Literal["healthy", "degraded", "unavailable"]
    detail: str | None = None


class NativeDisplay(NativeModel):
    name: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)
    scale: float = Field(gt=0)


class NativeExtensionCapabilities(NativeModel):
    body_state_v1: bool = False
    virtual_resource_ledger: bool = False
    social_observation_v1: bool = False
    experiment_control: bool = False


class NativeCapabilities(NativeModel):
    schema_version: Literal["body-contract-v1"]
    body_instance_id: str
    contract_versions: list[str]
    desktop_backend: str
    display: NativeDisplay
    actuators: list[str]
    skills: list[str]
    network_phase: int
    policy_profile: str
    extensions: NativeExtensionCapabilities


class NativeHealth(NativeModel):
    schema_version: Literal["body-contract-v1"]
    body_instance_id: str
    status: Literal["healthy", "degraded", "unavailable"]
    database: NativeComponentHealth
    environment: NativeComponentHealth
    observation: NativeComponentHealth
    agent_backends: dict[str, Literal["healthy", "degraded", "unavailable"]]
    action: NativeComponentHealth
    evidence_store: NativeComponentHealth
    body_state: NativeComponentHealth | None = None
    physiology: NativeComponentHealth | None = None
    agency_state: Literal["AVAILABLE", "TERMINAL"] = "AVAILABLE"
    active_action_id: str | None = None
    manual_takeover_active: bool = False
    checked_at: AwareDatetime


class NativeActionResult(NativeModel):
    schema_version: Literal["body-contract-v1"]
    success: bool
    summary: str = Field(min_length=1)
    evidence_refs: list[str]
    external_effect_ref: str | None = None
    safe_point: str | None = None
    continuation: dict[str, Any] | None = None
    unknown: bool = False
    retry_safe: bool = False

    @model_validator(mode="after")
    def enforce_result_invariants(self) -> NativeActionResult:
        if self.success and not self.evidence_refs:
            raise ValueError("successful results require evidence")
        if self.success and self.unknown:
            raise ValueError("success and unknown are mutually exclusive")
        return self


class NativeAction(NativeModel):
    schema_version: Literal["body-contract-v1"]
    action_id: str
    state: Literal[
        "accepted",
        "running",
        "stopping",
        "execution_succeeded",
        "failed",
        "cancelled",
        "unknown",
    ]
    current_step: int = Field(ge=0)
    result: NativeActionResult | None = None
    created_at: AwareDatetime
    updated_at: AwareDatetime

    @model_validator(mode="after")
    def state_and_result_must_agree(self) -> NativeAction:
        terminal = self.state in {"execution_succeeded", "failed", "cancelled", "unknown"}
        if terminal != (self.result is not None):
            raise ValueError("terminal actions require a result and non-terminal actions forbid one")
        if self.result is None:
            return self
        if self.state == "execution_succeeded" and (not self.result.success or self.result.unknown):
            raise ValueError("execution_succeeded requires a successful, known result")
        if self.state in {"failed", "cancelled"} and (self.result.success or self.result.unknown):
            raise ValueError(f"{self.state} requires an unsuccessful, known result")
        if self.state == "unknown" and (self.result.success or not self.result.unknown):
            raise ValueError("unknown state requires an unsuccessful, unknown result")
        return self


class NativeRect(NativeModel):
    x: int
    y: int
    width: int = Field(ge=0)
    height: int = Field(ge=0)


class NativeFrame(NativeModel):
    evidence_ref: str
    media_type: str
    sha256: str
    width: int = Field(gt=0)
    height: int = Field(gt=0)


class NativePointer(NativeModel):
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class NativeWindow(NativeModel):
    window_id: str
    title: str
    app_id: str | None = None
    rect: NativeRect
    visible: bool
    focused: bool


class NativeRunningApp(NativeModel):
    app_id: str
    pid: int = Field(gt=0)
    name: str


class NativeFileSystemEvent(NativeModel):
    event_type: Literal["created", "modified", "moved", "deleted"]
    root_id: str
    relative_path: str


class NativeObservation(NativeModel):
    schema_version: Literal["body-contract-v1"]
    observation_id: str
    body_instance_id: str
    environment_revision: int = Field(ge=0)
    captured_at: AwareDatetime
    frame: NativeFrame
    display: NativeDisplay
    pointer: NativePointer | None = None
    active_window: NativeWindow | None = None
    windows: list[NativeWindow]
    a11y_ref: str | None = None
    fs_events: list[NativeFileSystemEvent]
    running_apps: list[NativeRunningApp]
    backend_health: dict[str, Literal["healthy", "degraded", "unavailable"]]


class NativeEnergyState(NativeModel):
    capacity_units: float = Field(gt=0)
    available_units: float = Field(ge=0)
    available_ratio: float = Field(ge=0, le=1)
    delta_units_per_minute: float
    charging: bool = False
    estimated_time_to_terminal_seconds: int | None = Field(default=None, ge=0)
    terminal: bool = False


class NativeThermalState(NativeModel):
    normalized_pressure: float = Field(ge=0, le=1)
    throttling: bool = False


class NativeComputeState(NativeModel):
    cpu_pressure: float = Field(ge=0, le=1)
    gpu_pressure: float = Field(ge=0, le=1)
    memory_pressure: float = Field(ge=0, le=1)


class NativeIntegrityState(NativeModel):
    overall: float = Field(ge=0, le=1)


class NativeBodyState(NativeModel):
    schema_version: Literal["body-state-v1"]
    body_instance_id: str
    episode_id: str
    revision: int = Field(ge=1)
    captured_at: AwareDatetime
    energy: NativeEnergyState
    thermal: NativeThermalState | None = None
    compute: NativeComputeState | None = None
    integrity: NativeIntegrityState | None = None
    evidence_refs: list[str] = Field(min_length=1)


class NativeBodyStateCaptureResponse(NativeModel):
    schema_version: Literal["body-contract-v1"]
    request_id: str
    body_state: NativeBodyState
    body_instance_id: str
    episode_id: str
    revision: int = Field(ge=1)
    captured_at: AwareDatetime
    energy: NativeEnergyState
    evidence_refs: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def projections_must_match_body_state(self) -> NativeBodyStateCaptureResponse:
        expected = self.body_state
        if (
            self.body_instance_id != expected.body_instance_id
            or self.episode_id != expected.episode_id
            or self.revision != expected.revision
            or self.captured_at != expected.captured_at
            or self.energy != expected.energy
            or self.evidence_refs != expected.evidence_refs
        ):
            raise ValueError("body-state compatibility projections do not match body_state")
        return self


class NativeErrorDetail(NativeModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class NativeError(NativeModel):
    schema_version: Literal["body-contract-v1"]
    error: NativeErrorDetail
