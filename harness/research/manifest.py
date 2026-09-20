"""Frozen, hashable ExperimentManifest used as the study/runtime authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

from harness.embodiment.capabilities import CapabilityRequirement
from harness.regulation.config import RegulationConfig
from harness.research.grounded_agency.manifest import GroundedAgencyBinding, GroundedAgencyCondition

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


class BasicTerminalCondition(BaseModel):
    """Explicit permission for user-requested, verified desktop-terminal tasks."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    terminal_app_id: str = Field(default="org.xfce.Terminal", min_length=1, max_length=128)
    max_steps: int = Field(default=12, ge=1, le=30)
    task_timeout_seconds: int = Field(default=600, ge=30, le=1800)
    approval_timeout_seconds: int = Field(default=180, ge=1, le=600)
    permitted_intents: tuple[str, ...] = (
        "app.open.v1", "window.focus.v1", "ui.activate.v1", "ui.type_text.v1",
        "ui.key_chord.v1", "control.observe.v1", "control.wait.v1",
    )

    @model_validator(mode="after")
    def _validate_intents(self):
        allowed = {
            "app.open.v1", "window.focus.v1", "ui.activate.v1", "ui.type_text.v1",
            "ui.key_chord.v1", "control.observe.v1", "control.wait.v1",
        }
        if not self.permitted_intents or set(self.permitted_intents) - allowed:
            raise ValueError("basic terminal permits desktop interaction intents only")
        if len(set(self.permitted_intents)) != len(self.permitted_intents):
            raise ValueError("basic terminal intents must be unique")
        return self


class WebAccessCondition(BaseModel):
    """Explicit, frozen authorization for the optional network information body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    conversation_enabled: bool = False
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=lambda: [
        CapabilityRequirement(capability="web.search.v1", required=False),
        CapabilityRequirement(capability="web.read.v1", required=False),
    ])

    @model_validator(mode="after")
    def validate_web_capabilities(self):
        names = [item.capability for item in self.capability_requirements]
        if len(names) != len(set(names)) or set(names) - {"web.search.v1", "web.read.v1"}:
            raise ValueError("invalid_web_capability_requirements")
        return self


class ServerAccessCondition(BaseModel):
    """Explicit, frozen authorization for the optional remote Linux server body."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    capability_requirements: list[CapabilityRequirement] = Field(default_factory=lambda: [
        CapabilityRequirement(capability="server.observe.v1", required=False),
        CapabilityRequirement(capability="server.execute.v1", required=False),
    ])
    approval_timeout_seconds: int = Field(default=1800, ge=60, le=86400)
    sudo_requires_approval: bool = False

    @model_validator(mode="after")
    def validate_server_capabilities(self):
        names = [item.capability for item in self.capability_requirements]
        if len(names) != len(set(names)) or set(names) - {"server.observe.v1", "server.execute.v1"}:
            raise ValueError("invalid_server_capability_requirements")
        return self


class AutonomousCapabilitiesCondition(BaseModel):
    """Opt-in shared capability scheduling, independent of autonomous chat."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    enabled: bool = False
    owners: dict[Literal["board", "qq", "desktop", "voice", "web", "diary", "server"], Literal["legacy", "shared", "off"]] = Field(
        default_factory=lambda: {"board": "legacy", "qq": "legacy", "desktop": "off", "diary": "legacy"})
    allowed_capabilities: tuple[str, ...] = ()
    qq_send_enabled: bool = False
    quiet_seconds: int = Field(default=60, ge=30, le=3600)
    opportunity_seconds: int = Field(default=1800, ge=300, le=86400)
    deadline_seconds: int = Field(default=60, ge=5, le=60)

    @model_validator(mode="after")
    def unique_capabilities(self):
        if len(set(self.allowed_capabilities)) != len(self.allowed_capabilities):
            raise ValueError("autonomous capabilities must be unique")
        return self


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    web_access: WebAccessCondition = Field(default_factory=WebAccessCondition)
    server_access: ServerAccessCondition = Field(default_factory=ServerAccessCondition)

    experiment_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    control_mode: ControlMode = "study_runtime"
    participant_ref: str | None = None
    condition: SelectionCondition
    persona_version: str = Field(min_length=1)
    homeostatic_profile: str = Field(min_length=1)
    heartbeat_seconds: int = Field(default=60, ge=1, le=3600)
    autonomous_chat_enabled: bool = True
    command_timeout_seconds: float = Field(default=30.0, ge=1, le=300, allow_inf_nan=False)
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
    emotion_mode: Literal["off", "shadow", "on"] = "off"
    homeostatic_expression: HomeostaticExpressionCondition = Field(
        default_factory=HomeostaticExpressionCondition
    )
    embodiment: EmbodimentCondition = Field(default_factory=EmbodimentCondition)
    basic_terminal: BasicTerminalCondition = Field(default_factory=BasicTerminalCondition)
    autonomous_capabilities: AutonomousCapabilitiesCondition = Field(default_factory=AutonomousCapabilitiesCondition)
    goal_regulation: RegulationConfig = Field(default_factory=RegulationConfig)
    grounded_agency: GroundedAgencyCondition | None = None

    @model_validator(mode="after")
    def _validate_study_authority(self) -> ExperimentManifest:
        if self.goal_regulation.enabled:
            if self.grounded_agency is not None:
                raise ValueError("production goal regulation and grounded research require separate manifests")
            if not self.autonomous_capabilities.enabled:
                raise ValueError("goal regulation requires shared autonomous capabilities")
        if self.autonomous_capabilities.enabled and self.grounded_agency is not None:
            raise ValueError("shared autonomous capabilities and grounded research require separate manifests")
        if self.basic_terminal.enabled:
            if self.grounded_agency is not None:
                raise ValueError("basic terminal tasks and grounded-agency research must use separate manifests")
            required = {item.capability for item in self.embodiment.capability_requirements if item.required}
            if self.embodiment.mode == "off" or not {
                "action.execute.v1", "action.verify.v1", "observation.snapshot.v1", "evidence.reference.v1",
            }.issubset(required):
                raise ValueError("basic terminal requires embodied execution, verification, observation and evidence")
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
        if self.emotion_mode == "on" and self.prompt_version != "qwen-emotion-v1":
            raise ValueError("emotion_mode=on requires prompt_version=qwen-emotion-v1")
        if self.grounded_agency is not None:
            ga = self.grounded_agency
            if self.emotion_mode != "off":
                raise ValueError("grounded agency research requires emotion_mode=off")
            if ga.is_opaque and expression_mode != "off":
                raise ValueError("opaque interoception requires homeostatic_expression.mode=off")
            if ga.action_selection == "model_structured" and self.condition == "argmin_efe":
                raise ValueError("model_structured forbids condition=argmin_efe")
            if ga.action_selection == "engineered_argmin" and self.condition != "argmin_efe":
                raise ValueError("engineered_argmin requires condition=argmin_efe")
        if expression_mode != "off":
            _profile_id, separator, revision_text = self.homeostatic_profile.rpartition("@")
            try:
                profile_revision = int(revision_text) if separator else 0
            except ValueError:
                profile_revision = 0
            supported_social_profile = self.homeostatic_profile == "qq-social-drive-v1@1"
            if not _profile_id.strip() or (profile_revision < 3 and not supported_social_profile):
                raise ValueError(
                    "homeostatic expression requires a revision 3 profile or qq-social-drive-v1@1"
                )
            if self.schema_version != "homeostasis-state-v2":
                raise ValueError(
                    "homeostatic expression requires schema_version=homeostasis-state-v2"
                )
        if expression_mode in {"responses", "full"} and (
            self.prompt_version != ("qwen-emotion-v1" if self.emotion_mode == "on" else "homeostatic-interoception-v1")
        ):
            raise ValueError(
                "response injection requires prompt_version=homeostatic-interoception-v1"
            )
        return self

    @property
    def manifest_ref(self) -> str:
        return f"manifest://{self.experiment_id}/r{self.revision}"

    def canonical_json(self) -> str:
        # An absent opt-in extension must not invalidate existing journal hashes.
        payload = self.model_dump(mode="json")
        if self.emotion_mode == "off":
            payload.pop("emotion_mode")
        if self.web_access == WebAccessCondition():
            payload.pop("web_access")
        if self.server_access == ServerAccessCondition():
            payload.pop("server_access")
        if self.autonomous_chat_enabled:
            payload.pop("autonomous_chat_enabled")
        if self.command_timeout_seconds == 30.0:
            payload.pop("command_timeout_seconds")
        if not self.basic_terminal.enabled:
            payload.pop("basic_terminal")
        if self.autonomous_capabilities == AutonomousCapabilitiesCondition():
            payload.pop("autonomous_capabilities")
        if self.goal_regulation == RegulationConfig():
            payload.pop("goal_regulation")
        if self.grounded_agency is None:
            payload.pop("grounded_agency")
        else:
            for optional in ("runtime", "agency", "open_social", "body_belief_profile"):
                if getattr(self.grounded_agency, optional) is None:
                    payload["grounded_agency"].pop(optional)
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()

    def bind_condition(self) -> GroundedAgencyBinding:
        if self.grounded_agency is None:
            raise ValueError("grounded_agency must be configured before binding a condition")
        return GroundedAgencyBinding(
            experiment_id=self.experiment_id,
            condition_id=self.grounded_agency.condition_id,
            revision=self.revision,
            manifest_hash=self.content_hash(),
        )

    def with_revision(self, **updates: Any) -> ExperimentManifest:
        """Validate a replacement configuration under a strictly newer revision."""
        payload = {**self.model_dump(mode="json"), "revision": self.revision + 1, **updates}
        candidate = type(self).model_validate(payload)
        if candidate.experiment_id != self.experiment_id:
            raise ValueError("a manifest revision must retain experiment_id")
        if candidate.revision <= self.revision:
            raise ValueError("a manifest revision must increase revision")
        return candidate

    def model_copy(self, *, update: Mapping[str, Any] | None = None, deep: bool = False) -> ExperimentManifest:
        # Pydantic's default copy bypasses validators; preserve it only for legacy callers.
        if self.grounded_agency is None and (update or {}).get("grounded_agency") is None:
            if (self.basic_terminal.enabled or self.goal_regulation.enabled
                    or set(update or {}) & {"basic_terminal", "autonomous_capabilities", "goal_regulation", "server_access"}):
                return type(self).model_validate({**self.model_dump(mode="json"), **(update or {})})
            return super().model_copy(update=update, deep=deep)
        candidate = type(self).model_validate({**self.model_dump(mode="json"), **(update or {})})
        if candidate.content_hash() != self.content_hash():
            if candidate.experiment_id != self.experiment_id or candidate.revision <= self.revision:
                raise ValueError("grounded agency changes require a new manifest revision")
        return candidate


def load_experiment_manifest(path: Path) -> ExperimentManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ExperimentManifest.model_validate(payload)
