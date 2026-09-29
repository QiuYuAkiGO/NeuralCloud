"""Runtime Supervisor domain models.

The models in this module are deliberately transport neutral.  They are used by
the in-process adapters, the JSONL journal and the optional HTTP control plane.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import IntEnum, StrEnum
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

from harness.contracts.envelope import VersionVector
from harness.core.ids import new_id
from harness.embodiment.capabilities import EmbodimentStatus, CapabilityDescriptor
from harness.embodiment.contracts import EmbodiedActionSpec, EvidenceRef

RUNTIME_SCHEMA_VERSION: Literal["runtime-v1"] = "runtime-v1"
RUNTIME_EVENT_SCHEMA_VERSION: Literal["runtime-event-v1"] = "runtime-event-v1"
RUNTIME_JOURNAL_SCHEMA_VERSION: Literal["runtime-journal-v1"] = "runtime-journal-v1"
RUNTIME_CHECKPOINT_SCHEMA_VERSION: Literal["runtime-checkpoint-v1"] = "runtime-checkpoint-v1"


def utc_now() -> datetime:
    return datetime.now(UTC)


class RuntimeLifecycle(StrEnum):
    BOOTING = "BOOTING"
    RECOVERING = "RECOVERING"
    RUNNING = "RUNNING"
    PAUSING = "PAUSING"
    PAUSED = "PAUSED"
    STOPPING = "STOPPING"
    STOPPED = "STOPPED"
    FAULTED = "FAULTED"


class AgencyState(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    TERMINAL = "TERMINAL"


class RuntimeEventPriority(IntEnum):
    """Lower numeric values are dispatched first."""

    CONTROL = 0
    ACTION_RESULT = 10
    USER = 20
    IMPORTANT_ENVIRONMENT = 30
    PERCEPTION = 40
    HEARTBEAT = 50
    IDLE = 60
    TELEMETRY = 70


class RuntimeEventType(StrEnum):
    EMERGENCY_STOP = "emergency_stop"
    PAUSE = "pause"
    RESUME = "resume"
    STOP = "stop"
    RECOVER = "recover"
    PERMISSION_REVOKED = "permission_revoked"
    WATCHDOG = "watchdog"
    APPROVAL_RESULT = "approval_result"
    ACTION_RESULT = "action_result"
    USER_MESSAGE = "user_message"
    QQ_FACT = "qq_fact"
    QQ_TURN = "qq_turn"
    BOARD_REFLECTION = "board_reflection"
    DIARY_REFLECTION = "diary_reflection"
    CAPABILITY_OPPORTUNITY = "capability_opportunity"
    TERMINAL_CONTINUATION = "terminal_continuation"
    ENVIRONMENT_CHANGE = "environment_change"
    PERCEPTION = "perception"
    BODY_STATE = "body_state"
    GOAL_RECONSIDERATION = "goal_reconsideration"
    AGENCY_TERMINAL = "agency_terminal"
    RESUME_OBSERVATION = "resume_observation"
    HEARTBEAT = "heartbeat"
    MAINTENANCE = "maintenance"
    IDLE = "idle"
    RECONCILIATION = "reconciliation"
    TIME_DISCONTINUITY = "time_discontinuity"
    DIAGNOSTIC = "diagnostic"


CONTROL_EVENT_TYPES = frozenset(
    {
        RuntimeEventType.EMERGENCY_STOP,
        RuntimeEventType.PAUSE,
        RuntimeEventType.RESUME,
        RuntimeEventType.STOP,
        RuntimeEventType.RECOVER,
        RuntimeEventType.PERMISSION_REVOKED,
        RuntimeEventType.WATCHDOG,
        RuntimeEventType.DIAGNOSTIC,
        RuntimeEventType.AGENCY_TERMINAL,
    }
)

PAUSED_RECORD_ONLY_TYPES = frozenset(
    {
        RuntimeEventType.PERCEPTION,
        RuntimeEventType.ENVIRONMENT_CHANGE,
        RuntimeEventType.BODY_STATE,
    }
)


class RuntimeEvent(BaseModel):
    """Canonical event after Runtime ingress assigned its receive sequence."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["runtime-event-v1"] = RUNTIME_EVENT_SCHEMA_VERSION
    event_id: str = Field(default_factory=lambda: new_id("event"), min_length=1, max_length=128)
    receive_seq: int = Field(ge=1)
    correlation_id: str = Field(min_length=1, max_length=128)
    event_type: RuntimeEventType
    priority: RuntimeEventPriority
    source: str = Field(default="runtime", min_length=1, max_length=128)
    source_time: AwareDatetime
    received_at: AwareDatetime
    idempotency_key: str = Field(min_length=1, max_length=256)
    manifest_revision: int = Field(ge=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    first_receive_seq: int | None = Field(default=None, ge=1)
    last_receive_seq: int | None = Field(default=None, ge=1)
    coalesced_count: int = Field(default=1, ge=1)

    @model_validator(mode="after")
    def _validate_sequence_range(self) -> RuntimeEvent:
        first = self.first_receive_seq or self.receive_seq
        last = self.last_receive_seq or self.receive_seq
        if first > self.receive_seq or last < self.receive_seq or first > last:
            raise ValueError("invalid coalesced receive sequence range")
        return self

    @property
    def sequence_range(self) -> tuple[int, int]:
        return self.first_receive_seq or self.receive_seq, self.last_receive_seq or self.receive_seq

    @property
    def is_control(self) -> bool:
        return self.event_type in CONTROL_EVENT_TYPES

    @property
    def is_protected(self) -> bool:
        return self.priority <= RuntimeEventPriority.USER

    def coalesce_with(self, newer: RuntimeEvent) -> RuntimeEvent:
        """Keep the latest payload while retaining the full receive-sequence range."""

        if self.event_type != newer.event_type:
            raise ValueError("only events of the same type can be coalesced")
        first = min(self.sequence_range[0], newer.sequence_range[0])
        last = max(self.sequence_range[1], newer.sequence_range[1])
        return newer.model_copy(
            update={
                "first_receive_seq": first,
                "last_receive_seq": last,
                "coalesced_count": self.coalesced_count + newer.coalesced_count,
            }
        )


class LifecycleTransition(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    previous: RuntimeLifecycle
    current: RuntimeLifecycle
    reason: str = Field(min_length=1)
    at: AwareDatetime = Field(default_factory=utc_now)


class ResumeObservation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    latest_world: dict[str, Any] = Field(default_factory=dict)
    significant_changes: list[dict[str, Any]] = Field(default_factory=list)
    first_receive_seq: int = Field(ge=1)
    last_receive_seq: int = Field(ge=1)
    observation_count: int = Field(ge=1)
    paused_at: AwareDatetime
    resumed_at: AwareDatetime
    duration_seconds: float = Field(ge=0)
    time_discontinuity: bool = True

    @model_validator(mode="after")
    def _validate_range(self) -> ResumeObservation:
        if self.last_receive_seq < self.first_receive_seq:
            raise ValueError("last_receive_seq must not precede first_receive_seq")
        if self.resumed_at < self.paused_at:
            raise ValueError("resumed_at must not precede paused_at")
        return self


class ActionExecutionState(StrEnum):
    PROPOSED = "PROPOSED"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    READY = "READY"
    RUNNING = "RUNNING"
    VERIFYING = "VERIFYING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


TERMINAL_ACTION_STATES = frozenset(
    {
        ActionExecutionState.SUCCEEDED,
        ActionExecutionState.FAILED,
        ActionExecutionState.CANCELLED,
        ActionExecutionState.UNKNOWN,
    }
)


class ActionExecution(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str = Field(default_factory=lambda: new_id("action"), min_length=1)
    goal_id: str = Field(min_length=1)
    goal: str = Field(min_length=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    state: ActionExecutionState = ActionExecutionState.PROPOSED
    risk_class: str = Field(default="low", min_length=1)
    reversible: bool
    verification_method: str = Field(min_length=1)
    required_approvals: list[str] = Field(default_factory=list)
    approvals: list[str] = Field(default_factory=list)
    current_step: int = Field(default=0, ge=0)
    safe_point: str | None = None
    continuation: dict[str, Any] | None = None
    version_vector: VersionVector = Field(default_factory=VersionVector)
    deadline: AwareDatetime
    created_at: AwareDatetime = Field(default_factory=utc_now)
    updated_at: AwareDatetime = Field(default_factory=utc_now)
    outcome: dict[str, Any] | None = None
    error: str | None = None
    embodied_action: EmbodiedActionSpec | None = None

    @property
    def high_impact(self) -> bool:
        return self.risk_class in {"high", "critical", "high_impact"}

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_ACTION_STATES


class ActionBackendResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    summary: str = Field(min_length=1)
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    external_effect_ref: str | None = None
    safe_point: str | None = None
    continuation: dict[str, Any] | None = None
    unknown: bool = False
    retry_safe: bool = False

    @model_validator(mode="after")
    def _validate_evidence(self) -> ActionBackendResult:
        if self.success and self.unknown:
            raise ValueError("successful action result cannot be unknown")
        return self


class DeliveryResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    success: bool
    delivered_segments: list[str] = Field(default_factory=list)
    external_ref: str | None = None
    error: str | None = None

    @model_validator(mode="after")
    def _validate_result(self) -> DeliveryResult:
        if self.success and not self.delivered_segments:
            raise ValueError("successful delivery requires delivered_segments")
        if not self.success and not self.error:
            raise ValueError("failed delivery requires error")
        return self


class WatchdogAction(StrEnum):
    PAUSE = "pause"
    DEGRADE = "degrade"
    FAULT = "fault"


class WatchdogDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    decision_id: str = Field(default_factory=lambda: new_id("watchdog"))
    action: WatchdogAction
    reason: str = Field(min_length=1)
    evidence: dict[str, Any] = Field(default_factory=dict)
    observed_at: AwareDatetime = Field(default_factory=utc_now)


class QueueStatistics(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    size: int = Field(ge=0)
    capacity: int = Field(ge=1)
    high_watermark: int = Field(ge=0)
    by_priority: dict[str, int] = Field(default_factory=dict)
    coalesced: int = Field(default=0, ge=0)
    dropped: int = Field(default=0, ge=0)
    rejected: int = Field(default=0, ge=0)


class RuntimeHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    lifecycle: RuntimeLifecycle
    journal_writable: bool = True
    cognitive_healthy: bool = True
    homeostasis_healthy: bool = True
    llm_healthy: bool = True
    body_healthy: bool = True
    degraded_reasons: list[str] = Field(default_factory=list)
    last_heartbeat_at: AwareDatetime | None = None
    last_journal_append_at: AwareDatetime | None = None
    embodiment: EmbodimentStatus | None = None
    agency_state: AgencyState = AgencyState.AVAILABLE
    body_episode_id: str | None = None
    body_revision: int | None = Field(default=None, ge=1)
    body_state_age_seconds: float | None = Field(default=None, ge=0)


class RuntimeStatus(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["runtime-v1"] = RUNTIME_SCHEMA_VERSION
    character_id: str
    runtime_instance_id: str
    manifest_revision: int
    lifecycle: RuntimeLifecycle
    lifecycle_since: AwareDatetime
    receive_seq: int = Field(ge=0)
    process_seq: int = Field(ge=0)
    version_vector: VersionVector
    queue: QueueStatistics
    health: RuntimeHealth
    current_action: ActionExecution | None = None
    pending_actions: int = Field(default=0, ge=0)
    pending_events: int = Field(default=0, ge=0)
    embodiment: EmbodimentStatus | None = None
    agency_state: AgencyState = AgencyState.AVAILABLE
    affect: list[CapabilityDescriptor] = Field(default_factory=list)
    recovery: dict[str, Any] | None = None
    production: dict[str, Any] | None = None


class EventDispatchResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str
    receive_seq: int = Field(ge=1)
    process_seq: int = Field(ge=1)
    status: Literal["completed", "deferred", "failed", "ignored"]
    decision_kind: str | None = None
    detail: dict[str, Any] = Field(default_factory=dict)
    completed_at: AwareDatetime = Field(default_factory=utc_now)
