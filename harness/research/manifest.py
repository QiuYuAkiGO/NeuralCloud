"""Frozen, hashable ExperimentManifest used as the study/runtime authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.embodiment.capabilities import CapabilityRequirement

ControlMode = Literal["development_debug", "study_runtime"]
SelectionCondition = Literal["argmin_efe", "llm_top3", "llm_open"]


class HomeostaticExpressionCondition(BaseModel):
    """Rollout controls for semantic homeostatic expression context."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["off", "shadow", "responses", "full"] = "off"
    marked_cooldown_seconds: int = Field(default=1800, ge=60, le=86400)


class EmbodimentCondition(BaseModel):
    """Frozen embodiment capabilities plus legacy MistyAI body settings."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    mode: Literal["off", "observe", "grounded", "active"] = "off"
    required_for_runtime: bool = False
    body_provider: Literal["none", "mistyai"] = "none"
    body_instance_id: str | None = None
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=list)
    body_state_interval_seconds: float = Field(default=5.0, gt=0.0, le=3600.0)
    body_state_max_age_seconds: float = Field(default=10.0, gt=0.0, le=3600.0)
    replan_hysteresis: float = Field(default=0.05, ge=0.0, le=0.5)
    minimum_replan_interval_seconds: float = Field(default=300.0, ge=0.0, le=86400.0)
    anthropomorphic_labels: bool = False
    blinded_names: bool = False
    enable_drive_preemption: bool = False
    poll_interval_seconds: float = Field(default=5.0, gt=0, le=3600)
    max_state_age_seconds: float = Field(default=10.0, gt=0, le=3600)
    future_clock_tolerance_seconds: float = Field(default=2.0, ge=0, le=60)
    blinded_labels: bool = False
    body_memory: bool = False
    salience_bias: bool = False
    drive_preemption: bool = False

    @model_validator(mode="after")
    def _validate_requirements(self) -> EmbodimentCondition:
        names = [item.capability for item in self.capability_requirements]
        if len(names) != len(set(names)):
            raise ValueError("embodiment capability requirements must be unique")
        if self.mode == "off" and self.required_for_runtime:
            raise ValueError("off embodiment cannot be required for runtime")
        reserved = {
            "anthropomorphic_labels": self.anthropomorphic_labels,
            "body_memory": self.body_memory,
            "salience_bias": self.salience_bias,
            "drive_preemption": self.drive_preemption,
        }
        enabled = [name for name, value in reserved.items() if value]
        if enabled:
            raise ValueError(f"first-release embodiment fields must be false: {', '.join(enabled)}")
        if self.mode == "active" and self.body_provider != "none":
            raise ValueError("embodiment mode 'active' is not implemented")
        if self.mode == "off":
            if self.body_provider != "none" or self.body_instance_id is not None:
                raise ValueError("off mode requires body_provider=none and no body_instance_id")
        elif self.body_provider == "mistyai" and not self.body_instance_id:
            raise ValueError("observe/grounded mode requires MistyAI and body_instance_id")
        return self


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    control_mode: ControlMode = "study_runtime"
    participant_ref: str | None = None
    condition: SelectionCondition
    persona_version: str = Field(min_length=1)
    homeostatic_profile: str = Field(min_length=1)
    heartbeat_seconds: int = Field(default=60, ge=1, le=3600)
    policy_horizon: int = Field(default=3, ge=1, le=3)
    random_seed: int = Field(ge=0)
    initial_snapshot_ref: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    a_version: str = Field(min_length=1)
    b_version: str = Field(min_length=1)
    c_version: str = Field(min_length=1)
    precision_version: str = Field(min_length=1)
    coupling_version: str = Field(min_length=1)
    priority_policy: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    authorized_data_scope_ref: str | None = None
    high_impact_gate: str | None = None
    retention: str = Field(min_length=1)
    watchdog_action: str = Field(min_length=1)
    timezone: str = Field(default="Asia/Shanghai", min_length=1)
    homeostatic_expression: HomeostaticExpressionCondition = Field(
        default_factory=HomeostaticExpressionCondition
    )
    embodiment: EmbodimentCondition = Field(default_factory=EmbodimentCondition)

    @model_validator(mode="after")
    def _validate_study_authority(self) -> ExperimentManifest:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown manifest timezone: {self.timezone}") from exc
        if self.control_mode == "study_runtime":
            required = {
                "participant_ref": self.participant_ref,
                "authorized_data_scope_ref": self.authorized_data_scope_ref,
                "high_impact_gate": self.high_impact_gate,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"study_runtime manifest missing: {', '.join(missing)}")
        expression_mode = self.homeostatic_expression.mode
        if expression_mode != "off":
            _profile_id, separator, revision_text = self.homeostatic_profile.rpartition("@")
            try:
                profile_revision = int(revision_text) if separator else 0
            except ValueError:
                profile_revision = 0
            if not _profile_id.strip() or profile_revision < 3:
                raise ValueError(
                    "homeostatic expression requires a profile reference at revision 3 or later"
                )
            if self.schema_version != "homeostasis-state-v2":
                raise ValueError(
                    "homeostatic expression requires schema_version=homeostasis-state-v2"
                )
        if expression_mode in {"responses", "full"} and (
            self.prompt_version != "homeostatic-interoception-v1"
        ):
            raise ValueError(
                "response injection requires prompt_version=homeostatic-interoception-v1"
            )
        return self

    @property
    def manifest_ref(self) -> str:
        return f"manifest://{self.experiment_id}/r{self.revision}"

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def load_experiment_manifest(path: Path) -> ExperimentManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ExperimentManifest.model_validate(payload)
