"""Provider-neutral embodiment contracts.

These models describe semantics consumed by NeuralCloud.  Provider transport
models (HTTP payloads, ROS messages, and similar types) must be translated at
an adapter boundary before reaching this module.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator


class StrictContract(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class EvidenceRef(StrictContract):
    provider_id: str = Field(min_length=1, max_length=256)
    ref: str = Field(min_length=1)
    digest: str | None = None
    media_type: str | None = None
    created_at: AwareDatetime | None = None

    @model_validator(mode="before")
    @classmethod
    def _upgrade_legacy_ref(cls, value: Any) -> Any:
        if isinstance(value, str):
            return {"provider_id": "legacy", "ref": value}
        return value


class ActionIntent(StrictContract):
    intent_id: str = Field(
        min_length=1,
        max_length=128,
        pattern=r"^[a-z][a-z0-9_.-]+\.v[0-9]+$",
    )
    arguments: dict[str, Any] = Field(default_factory=dict)


class WindowCriterion(StrictContract):
    kind: Literal["window"] = "window"
    condition: Literal["present", "absent", "present_and_focused", "title_equals"]
    app_id: str | None = None
    window_id: str | None = None
    title: str | None = None

    @model_validator(mode="after")
    def _validate_title(self) -> WindowCriterion:
        if self.condition == "title_equals" and self.title is None:
            raise ValueError("title_equals requires title")
        return self


class FileCriterion(StrictContract):
    kind: Literal["file"] = "file"
    condition: Literal["exists", "absent", "sha256_equals", "size_equals"]
    root_id: str
    relative_path: str
    sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    size: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _validate_condition(self) -> FileCriterion:
        if self.condition == "sha256_equals" and self.sha256 is None:
            raise ValueError("sha256_equals requires sha256")
        if self.condition == "size_equals" and self.size is None:
            raise ValueError("size_equals requires size")
        return self


class ProcessCriterion(StrictContract):
    kind: Literal["process"] = "process"
    condition: Literal["running", "not_running"]
    app_id: str | None = None
    pid: int | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def _validate_selector(self) -> ProcessCriterion:
        if self.app_id is None and self.pid is None:
            raise ValueError("process criterion requires app_id or pid")
        return self


class ScreenChangeCriterion(StrictContract):
    kind: Literal["screen_change"] = "screen_change"
    condition: Literal["changed", "unchanged"]
    baseline_evidence_ref: str | None = None


class A11yNodeCriterion(StrictContract):
    kind: Literal["a11y_node"] = "a11y_node"
    condition: Literal["present", "absent"]
    app_id: str | None = None
    role: str | None = None
    name: str | None = None


class PointerCriterion(StrictContract):
    kind: Literal["pointer"] = "pointer"
    condition: Literal["over_element"] = "over_element"
    app_id: str | None = None
    role: str | None = None
    name: str | None = None

    @model_validator(mode="after")
    def _validate_selector(self) -> PointerCriterion:
        if self.app_id is None and self.role is None and self.name is None:
            raise ValueError("pointer criterion requires an element selector")
        return self


class IntentionalNoopCriterion(StrictContract):
    kind: Literal["intentional_noop"] = "intentional_noop"
    condition: Literal["waited", "observed", "deferred"]


class ScenarioCriterion(StrictContract):
    """Target-bound outcome supported by a declared experimental scenario provider."""

    kind: Literal["scenario"] = "scenario"
    condition: Literal["task_completed", "station_used"]
    target_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_.-]+$", strict=True)


ActionCriterion = Annotated[
    WindowCriterion
    | FileCriterion
    | ProcessCriterion
    | ScreenChangeCriterion
    | A11yNodeCriterion
    | PointerCriterion
    | IntentionalNoopCriterion
    | ScenarioCriterion,
    Field(discriminator="kind"),
]


class EmbodiedActionSpec(StrictContract):
    intent: ActionIntent
    success_criteria: list[ActionCriterion] = Field(min_length=1)


class PerceptionSnapshot(StrictContract):
    schema_version: Literal["observation.v1"] = "observation.v1"
    provider_id: str
    body_instance_id: str | None = None
    revision: int | None = Field(default=None, ge=0)
    captured_at: AwareDatetime
    summary: str | None = None
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    freshness_seconds: float = Field(default=0.0, ge=0.0)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    extensions: dict[str, Any] = Field(default_factory=dict)


class EnergyState(StrictContract):
    capacity_units: float = Field(gt=0)
    available_units: float = Field(ge=0)
    available_ratio: float = Field(ge=0, le=1)
    delta_units_per_minute: float
    charging: bool = False
    estimated_time_to_empty_seconds: int | None = Field(default=None, ge=0)
    terminal: bool = False

    @model_validator(mode="after")
    def _validate_ratio(self) -> EnergyState:
        if self.available_units > self.capacity_units:
            raise ValueError("available_units cannot exceed capacity_units")
        expected = self.available_units / self.capacity_units
        if not math.isclose(self.available_ratio, expected, abs_tol=1e-6):
            raise ValueError("available_ratio must match available_units / capacity_units")
        return self


class BodyStateSnapshot(StrictContract):
    schema_version: Literal["physiology.body-state.v1"] = "physiology.body-state.v1"
    provider_id: str
    body_instance_id: str
    revision: int = Field(ge=0)
    captured_at: AwareDatetime
    energy: EnergyState
    evidence_refs: list[EvidenceRef] = Field(min_length=1)


class InteroceptiveState(StrictContract):
    source_revision: int = Field(ge=0)
    resource_level: float = Field(ge=0.0, le=1.0)
    resource_trend: float
    estimated_time_to_limit_seconds: int | None = Field(default=None, ge=0)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)


class HomeostaticObservation(StrictContract):
    source: str = "system_sensor"
    precision: float = Field(default=0.95, ge=0.0, le=1.0)
    observed_values: dict[str, float]
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    observed_at: AwareDatetime

    @model_validator(mode="after")
    def _finite_values(self) -> HomeostaticObservation:
        if any(not math.isfinite(value) for value in self.observed_values.values()):
            raise ValueError("observed values must be finite")
        return self


def evidence_ref_strings(refs: list[EvidenceRef]) -> list[str]:
    return [item.ref for item in refs]


def captured_age_seconds(captured_at: datetime, now: datetime) -> float:
    return (now - captured_at).total_seconds()
