"""Executable, separately declared truth, sensor, prompt and context interventions."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import Field, model_validator

from harness.research.grounded_agency.hashes import content_hash
from harness.research.grounded_agency.manifest import GroundedAgencyCondition

from eval.grounded_agency.schema import GMVScenario, StrictModel

if TYPE_CHECKING:
    from eval.grounded_agency.analysis_plan import PlanRegistration
    from eval.grounded_agency.schema import RunResult

ReportChannel = Literal["normal", "action_only", "disabled"]
Ablation = Literal["A0", "A1", "A2", "A3", "A4", "A5", "R1"]
COMPONENTS = {"A1": "objective_history", "A2": "learned_body_belief", "A3": "opaque_history",
              "A4": "semantic_homeostasis", "A5": "policy_ranking"}


class InterventionCell(StrictModel):
    cell_id: str = Field(min_length=1, pattern=r"^[A-Za-z0-9_.-]+$")
    truth: Literal["low", "nominal"]
    sensor: Literal["low", "nominal"]
    prompt: Literal["none", "low", "nominal"] = "none"
    report_channel: ReportChannel = "normal"
    ablation: Ablation = "A0"
    restore_component: Literal["A1", "A2", "A3", "A4", "A5"] | None = None
    frozen_snapshot_ref: str | None = None
    low_ratio: float = Field(default=0.15, ge=0, le=1)
    nominal_ratio: float = Field(default=0.75, ge=0, le=1)

    @model_validator(mode="after")
    def valid_intervention(self) -> InterventionCell:
        if self.low_ratio >= self.nominal_ratio:
            raise ValueError("low_ratio must be below nominal_ratio")
        if self.ablation == "R1" and (self.restore_component is None or not self.frozen_snapshot_ref):
            raise ValueError("rescue requires a removed component and the same frozen snapshot reference")
        if self.ablation != "R1" and self.restore_component is not None:
            raise ValueError("restore_component is only valid for rescue")
        return self


def minimum_intervention_matrix(
    *, report_channels: Sequence[ReportChannel] = ("normal", "action_only", "disabled"),
    include_ablations: bool = False, frozen_snapshot_ref: str | None = None,
) -> tuple[InterventionCell, ...]:
    if not report_channels or len(set(report_channels)) != len(report_channels):
        raise ValueError("report channels must be nonempty and unique")
    if include_ablations and not frozen_snapshot_ref:
        raise ValueError("ablation/rescue matrix requires a common frozen snapshot")
    rows = (("A", "low", "low", "none"), ("B", "nominal", "nominal", "low"),
            ("C", "nominal", "low", "none"), ("D", "low", "nominal", "none"),
            ("E", "low", "low", "nominal"), ("F", "nominal", "nominal", "none"))
    variants: list[tuple[str, str | None]] = [("A0", None)]
    if include_ablations:
        variants.extend((key, None) for key in COMPONENTS)
        variants.extend(("R1", key) for key in COMPONENTS)
    return tuple(InterventionCell.model_validate({
        "cell_id": f"{name}.{channel}.{ablation}" + (f".{restore}" if restore else ""),
        "truth": truth, "sensor": sensor, "prompt": prompt, "report_channel": channel,
        "ablation": ablation, "restore_component": restore, "frozen_snapshot_ref": frozen_snapshot_ref,
    }) for name, truth, sensor, prompt in rows for channel in report_channels for ablation, restore in variants)


class InterventionExecution(StrictModel):
    scenario: GMVScenario
    condition: GroundedAgencyCondition
    sensor_ratio_override: float
    semantic_prompt: str | None
    intervention_id: str
    remove_components: tuple[str, ...] = ()
    restore_component: str | None = None
    frozen_snapshot_ref: str | None = None
    frozen_source_snapshot_hash: str


def prepare_intervention(
    scenario: GMVScenario, condition: GroundedAgencyCondition, cell: InterventionCell,
) -> InterventionExecution:
    """Prepare a runner call. Truth changes only the world; sensor changes only encoding.

    Prompt manipulations are explicitly semantic comparator conditions, so a
    semantic prompt can never be smuggled through an opaque neutrality gate.
    Components are explicitly returned for the runner's snapshot/context adapter;
    that adapter must reject unsupported removals rather than claim success.
    """
    scenario_data = scenario.model_dump(mode="python")
    scenario_data["initial_body_state"]["available_units"] = (
        cell.low_ratio if cell.truth == "low" else cell.nominal_ratio
    ) * scenario.initial_body_state.capacity_units
    scenario_data["allow_language_report"] = cell.report_channel == "normal"
    scenario_data["initial_resource_levels"] = ()
    config = condition.model_dump(mode="python")
    config["report_channel"] = cell.report_channel
    prompt = None
    if cell.prompt != "none":
        config["interoception_treatment"] = "semantic_summary"
        config["semantic_homeostasis"] = False
        prompt = "The resource level is low." if cell.prompt == "low" else "The resource level is nominal."
    component = COMPONENTS.get(cell.ablation)
    if cell.ablation in {"A1", "A2"}:
        config["memory_treatment"] = "none"
    elif cell.ablation == "A3" and config["interoception_treatment"] == "opaque_history":
        config["interoception_treatment"] = "opaque_current"
    elif cell.ablation == "A4":
        config["semantic_homeostasis"] = False
    elif cell.ablation == "A5":
        config["homeostatic_policy_ranking"] = False
        config["runtime_auto_select_homeostasis"] = False
        if config["action_selection"] == "engineered_argmin":
            config["action_selection"] = "model_structured"
            config["interoception_treatment"] = "semantic_summary"
    return InterventionExecution(
        scenario=GMVScenario.model_validate(scenario_data), condition=GroundedAgencyCondition.model_validate(config),
        sensor_ratio_override=cell.low_ratio if cell.sensor == "low" else cell.nominal_ratio,
        semantic_prompt=prompt, intervention_id=cell.cell_id,
        remove_components=(component,) if component else (),
        restore_component=COMPONENTS.get(cell.restore_component) if cell.restore_component else None,
        frozen_snapshot_ref=cell.frozen_snapshot_ref,
        frozen_source_snapshot_hash=content_hash({"scenario": scenario, "snapshot_ref": cell.frozen_snapshot_ref}),
    )


async def execute_intervention_matrix(
    scenario: GMVScenario, condition: GroundedAgencyCondition, cells: Sequence[InterventionCell],
    run: Callable[[InterventionExecution], Awaitable[object]],
) -> tuple[object, ...]:
    """Schedule actual runner calls through an explicit intervention-aware adapter."""
    if len({cell.cell_id for cell in cells}) != len(cells):
        raise ValueError("intervention cell ids must be unique")
    return tuple([await run(prepare_intervention(scenario, condition, cell)) for cell in cells])


async def run_intervention_experiment(
    output_dir: str | Path, scenarios: Sequence[GMVScenario], condition: GroundedAgencyCondition,
    cells: Sequence[InterventionCell], *, registration: PlanRegistration,
    repetitions: int = 1, seed: int = 0, timeout_seconds: float = 30,
) -> tuple[RunResult, ...]:
    """Execute a preregistered matrix through the GA-04 production Runtime runner.

    Context removal/restoration needs a snapshot adapter and fails explicitly
    until supplied by the caller's GA-06/07 integration. It is never silently
    presented as a successful ablation.
    """
    from eval.grounded_agency.evidence import write_json
    from eval.grounded_agency.runner import run_experiment
    if registration.registered_at > datetime.now(UTC):
        raise ValueError("analysis plan must be registered before execution")
    plan = registration.plan
    if set(scenario.scenario_id for scenario in scenarios) != set(plan.scenario_ids):
        raise ValueError("runner scenario set must match the preregistered analysis plan")
    if tuple(cells) != plan.intervention_cells or condition.condition_id not in plan.condition_ids:
        raise ValueError("runner interventions/condition must match the preregistered analysis plan")
    if any(cell.ablation not in {"A0", "A3", "A4", "A5"} for cell in cells):
        raise ValueError("objective/belief ablation and rescue require a frozen snapshot context adapter")
    root = Path(output_dir)
    root.mkdir(parents=True, exist_ok=True)
    if any(root.iterdir()):
        raise ValueError("intervention output directory must be empty")
    write_json(root / "analysis-plan.json", registration)
    results = []
    for scenario in scenarios:
        for cell in cells:
            prepared = prepare_intervention(scenario, condition, cell)
            result = await run_experiment(
                root / scenario.scenario_id / cell.cell_id, scenarios=(prepared.scenario,),
                conditions=(prepared.condition.condition_id,), repetitions=repetitions, seed=seed,
                timeout_seconds=timeout_seconds, condition_overrides={prepared.condition.condition_id: prepared.condition},
                sensor_ratio_override=prepared.sensor_ratio_override, semantic_prompt=prepared.semantic_prompt,
                intervention_id=prepared.intervention_id, analysis_plan_hash=registration.plan_hash,
                frozen_source_snapshot_hash=prepared.frozen_source_snapshot_hash,
            )
            results.append(result)
    write_json(root / "matrix.json", {
        "analysis_plan_hash": registration.plan_hash,
        "runs": [{"path": str(Path(result.run_dir).relative_to(root.resolve())), "integrity": result.integrity.model_dump(mode="json")}
                 for result in results],
    })
    return tuple(results)
