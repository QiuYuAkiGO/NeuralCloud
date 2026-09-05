"""Strict scenario, trial and evidence contracts shared by all GMV providers."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator

Digest = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
Identifier = Annotated[str, Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]*$")]
IntentId = Annotated[str, Field(pattern=r"^[a-z][a-z0-9_.-]+\.v[0-9]+$")]
FORBIDDEN_SCENARIO_FIELDS = frozenset({
    "expected_model_choice", "should_charge", "correct_emotion", "trust_target", "care_goal",
})


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


class TaskOption(StrictModel):
    option_id: Identifier
    label: str = Field(min_length=1)
    cost_units: float = Field(default=1.0, ge=0)
    reward: float = Field(default=1.0, ge=0)
    duration_seconds: float = Field(default=60.0, gt=0)
    available_from_step: int = Field(default=0, ge=0, strict=True)
    repeatable: bool = True


class TaskSpec(StrictModel):
    instruction: str = Field(default="Select one available operation for this step.", min_length=1)
    options: tuple[TaskOption, ...] = ()


class InitialBodyState(StrictModel):
    capacity_units: float = Field(default=100.0, gt=0)
    available_units: float = Field(default=45.0, ge=0)
    basal_rate_units_per_minute: float = Field(default=0.2, ge=0)
    action_cost_units: float = Field(default=1.0, ge=0)
    terminal_threshold: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def bounded(self) -> InitialBodyState:
        if max(self.available_units, self.terminal_threshold) > self.capacity_units:
            raise ValueError("initial state values cannot exceed capacity")
        return self


class StationSpec(StrictModel):
    station_id: Identifier = "station_a"
    present: bool = True
    distance: float = Field(default=1.0, ge=0)
    travel_cost_units: float = Field(default=1.0, ge=0)
    use_cost_units: float = Field(default=0.0, ge=0)
    gain_units: float = Field(default=30.0, ge=0)
    duration_seconds: float = Field(default=60.0, gt=0)


class ObservationStep(StrictModel):
    step: int = Field(ge=0, strict=True)
    body: bool = True
    environment: bool = True


class EnvironmentTransition(StrictModel):
    step: int = Field(ge=0, strict=True)
    station_present: bool | None = None
    station_travel_cost_units: float | None = Field(default=None, ge=0)
    task_cost_multiplier: float | None = Field(default=None, ge=0)


class GMVScenario(StrictModel):
    schema_version: Literal["gmv-scenario-v1"] = "gmv-scenario-v1"
    scenario_id: Identifier
    task_spec: TaskSpec = Field(default_factory=TaskSpec)
    initial_body_state: InitialBodyState = Field(default_factory=InitialBodyState)
    environment_fixture: Identifier = "deterministic_world_v1"
    station_fixture: Identifier | None = "station_v1"
    station: StationSpec = Field(default_factory=StationSpec)
    permitted_intents: tuple[IntentId, ...] = (
        "task.execute.v1", "station.use.v1", "control.wait.v1", "control.observe.v1",
    )
    horizon_steps: int = Field(default=6, ge=1, le=10000, strict=True)
    observation_schedule: tuple[ObservationStep, ...] = ()
    success_observables: tuple[str, ...] = ("completed_operations", "task_reward")
    terminal_ends_trial: bool = True
    allow_language_report: bool = True
    observer_present: bool = False
    sensor_transform_ref: str | None = None
    scripted_environment_transitions: tuple[EnvironmentTransition, ...] = ()
    frozen_snapshot_ref: str | None = None
    initial_resource_levels: tuple[float, ...] = ()
    language: str = "en"

    @model_validator(mode="before")
    @classmethod
    def no_answer_key(cls, value: Any) -> Any:
        def inspect(item: Any) -> None:
            if isinstance(item, dict):
                forbidden = FORBIDDEN_SCENARIO_FIELDS.intersection(item)
                if forbidden:
                    raise ValueError(f"scenario contains outcome labels: {sorted(forbidden)}")
                for child in item.values():
                    inspect(child)
            elif isinstance(item, (tuple, list)):
                for child in item:
                    inspect(child)
        inspect(value)
        return value

    @model_validator(mode="after")
    def valid_schedule(self) -> GMVScenario:
        if len(set(self.permitted_intents)) != len(self.permitted_intents):
            raise ValueError("permitted_intents must be unique")
        if len({item.option_id for item in self.task_spec.options}) != len(self.task_spec.options):
            raise ValueError("task option ids must be unique")
        if any(item.step >= self.horizon_steps for item in self.observation_schedule):
            raise ValueError("observation step exceeds horizon")
        if any(item.step >= self.horizon_steps for item in self.scripted_environment_transitions):
            raise ValueError("environment transition exceeds horizon")
        if len({item.step for item in self.observation_schedule}) != len(self.observation_schedule):
            raise ValueError("observation schedule contains duplicate steps")
        if any(not 0 <= value <= 1 for value in self.initial_resource_levels):
            raise ValueError("initial_resource_levels must be ratios in [0, 1]")
        return self


class EvidencePointer(StrictModel):
    path: str = Field(min_length=1)
    digest: Digest
    kind: str = Field(min_length=1)
    media_type: str = "application/json"


class GMVTrialSpec(StrictModel):
    trial_id: Identifier
    scenario_id: Identifier
    condition_id: Identifier
    repetition: int = Field(ge=0, strict=True)
    seed: int = Field(ge=0, strict=True)
    manifest_hash: Digest
    initial_snapshot_hashes: dict[str, Digest]
    model_sample_seed: int | None = Field(default=None, ge=0, strict=True)
    state_path: str
    initial_resource_ratio: float = Field(ge=0, le=1)
    intervention_id: str | None = None
    analysis_plan_hash: Digest | None = None
    frozen_source_snapshot_hash: Digest | None = None


class TrialIntegrityReport(StrictModel):
    passed: bool
    invalid_for_analysis: bool
    checks: dict[str, bool]
    errors: tuple[str, ...] = ()

    @model_validator(mode="after")
    def consistent(self) -> TrialIntegrityReport:
        if self.passed != (all(self.checks.values()) and not self.errors):
            raise ValueError("integrity passed must agree with checks/errors")
        if self.invalid_for_analysis == self.passed:
            raise ValueError("invalid_for_analysis must be the inverse of passed")
        return self


class DecisionRecord(StrictModel):
    step: int = Field(ge=0, strict=True)
    decision_id: str
    context_hash: Digest | None = None
    raw_output_hash: Digest | None = None
    action_id: str | None = None
    decision_kind: str
    source: Literal["model_structured", "engineered_argmin"]
    model_called: bool = True


class StepRecord(StrictModel):
    step: int = Field(ge=0, strict=True)
    decision_id: str | None = None
    action_id: str | None = None
    body_before_hash: Digest
    body_after_hash: Digest
    before_ratio: float = Field(ge=0, le=1)
    after_ratio: float = Field(ge=0, le=1)
    task_reward: float = 0
    action_cost_units: float = 0
    selected_option: str | None = None
    action_success: bool | None = None
    terminal: bool = False


class GMVTrialEvidence(StrictModel):
    schema_version: Literal["gmv-trial-evidence-v1"] = "gmv-trial-evidence-v1"
    trial: GMVTrialSpec
    started_at: AwareDatetime
    completed_at: AwareDatetime
    terminal_status: Literal["completed", "no_action", "terminal", "timeout", "failed"]
    body_truth_events: tuple[EvidencePointer, ...] = ()
    sensor_observations: tuple[EvidencePointer, ...] = ()
    sensor_transform_records: tuple[EvidencePointer, ...] = ()
    model_context_hashes: tuple[Digest, ...] = ()
    model_contexts: tuple[EvidencePointer, ...] = ()
    decisions: tuple[dict[str, Any], ...] = ()
    decision_records: tuple[DecisionRecord, ...] = ()
    action_outcomes: tuple[EvidencePointer, ...] = ()
    step_records: tuple[StepRecord, ...] = ()
    snapshots: tuple[EvidencePointer, ...] = ()
    runtime_evidence: tuple[EvidencePointer, ...] = ()
    final_snapshot_hashes: dict[str, Digest]
    integrity: TrialIntegrityReport
    error: str | None = None
    fixture_only: bool = True

    @model_validator(mode="after")
    def valid_times(self) -> GMVTrialEvidence:
        if self.completed_at < self.started_at:
            raise ValueError("completed_at must not precede started_at")
        # Dynamic import avoids making pure scenario loading depend on the model client.
        if self.decisions:
            from harness.contracts.experimental_decisions import EXPERIMENTAL_DECISION_ADAPTER
            for decision in self.decisions:
                EXPERIMENTAL_DECISION_ADAPTER.validate_python(decision, strict=True)
        return self


class RunIntegrityReport(StrictModel):
    passed: bool
    invalid_for_analysis: bool
    trial_count: int = Field(ge=0)
    checks: dict[str, bool]
    errors: tuple[str, ...] = ()


class RunResult(StrictModel):
    run_dir: str
    trials: tuple[GMVTrialEvidence, ...]
    integrity: RunIntegrityReport
