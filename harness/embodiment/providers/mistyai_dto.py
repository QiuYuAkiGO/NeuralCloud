"""Private validation models for MistyAI body-contract-v1 responses."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


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
    environment: NativeComponentHealth
    observation: NativeComponentHealth
    action: NativeComponentHealth
    evidence_store: NativeComponentHealth
    body_state: NativeComponentHealth | None = None
    active_action_id: str | None = None
    manual_takeover_active: bool = False
    checked_at: datetime


class NativeActionResult(NativeModel):
    schema_version: Literal["body-contract-v1"]
    success: bool
    summary: str
    evidence_refs: list[str]
    external_effect_ref: str | None = None
    safe_point: str | None = None
    continuation: dict[str, Any] | None = None
    unknown: bool = False
    retry_safe: bool = False


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
    created_at: datetime
    updated_at: datetime


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
    captured_at: datetime
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
    estimated_time_to_empty_seconds: int | None = Field(default=None, ge=0)
    terminal: bool = False


class NativeBodyState(NativeModel):
    schema_version: Literal["body-contract-v1"]
    body_instance_id: str
    revision: int = Field(ge=0)
    captured_at: datetime
    energy: NativeEnergyState
    evidence_refs: list[str] = Field(min_length=1)


class NativeErrorDetail(NativeModel):
    code: str
    message: str
    retryable: bool = False
    details: dict[str, Any] = Field(default_factory=dict)


class NativeError(NativeModel):
    schema_version: Literal["body-contract-v1"]
    error: NativeErrorDetail
