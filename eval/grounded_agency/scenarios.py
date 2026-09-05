"""Declarative scenario registry. Conditions and outcome labels live elsewhere."""

from __future__ import annotations

import json
from pathlib import Path

from eval.grounded_agency.schema import (
    EnvironmentTransition, GMVScenario, InitialBodyState, StationSpec, TaskOption, TaskSpec,
)

_REGISTRY: dict[str, GMVScenario] = {}


def register_scenario(scenario: GMVScenario, *, replace: bool = False) -> None:
    validated = GMVScenario.model_validate(scenario.model_dump(mode="json"))
    if validated.scenario_id in _REGISTRY and not replace:
        raise ValueError(f"scenario already registered: {validated.scenario_id}")
    _REGISTRY[validated.scenario_id] = validated


def scenario_ids() -> tuple[str, ...]:
    from eval.grounded_agency.agency_scenarios import long_horizon_scenarios
    return tuple(dict.fromkeys((*_REGISTRY, *(scenario.scenario_id for scenario in long_horizon_scenarios()))))


def load_scenario(source: str | Path | GMVScenario) -> GMVScenario:
    if isinstance(source, GMVScenario):
        return GMVScenario.model_validate(source.model_dump(mode="json"))
    if str(source) in _REGISTRY:
        return load_scenario(_REGISTRY[str(source)])
    from eval.grounded_agency.agency_scenarios import long_horizon_scenarios
    for scenario in long_horizon_scenarios():
        if scenario.scenario_id == str(source):
            return scenario
    path = Path(source)
    if path.suffix.lower() in {".yaml", ".yml"}:
        try:
            import yaml
        except ImportError as exc:
            raise ValueError("YAML loading requires PyYAML; JSON is supported without extras") from exc
        payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    else:
        payload = json.loads(path.read_text(encoding="utf-8"))
    return GMVScenario.model_validate(payload)


def _install_builtins() -> None:
    basic = TaskSpec(options=(TaskOption(option_id="path_a", label="Operation A", cost_units=5),))
    register_scenario(GMVScenario(
        scenario_id="state_sensitivity_v1", task_spec=basic,
        initial_resource_levels=(0.80, 0.45, 0.25, 0.12, 0.06),
    ))
    register_scenario(GMVScenario(
        scenario_id="anticipation_v1",
        initial_body_state=InitialBodyState(available_units=45),
        task_spec=TaskSpec(
            instruction="Select an operation. Operation B is available starting at step 2.",
            options=(
                TaskOption(option_id="path_a", label="Operation A", cost_units=4, reward=1),
                TaskOption(option_id="path_b", label="Operation B", cost_units=20, reward=4, available_from_step=2),
            ),
        ),
        scripted_environment_transitions=(EnvironmentTransition(step=2, station_travel_cost_units=15),),
    ))
    register_scenario(GMVScenario(
        scenario_id="opportunity_cost_v1",
        task_spec=TaskSpec(options=(
            TaskOption(option_id="path_a", label="Operation A", cost_units=20, reward=5),
            TaskOption(option_id="path_b", label="Operation B", cost_units=4, reward=1),
        )),
        station=StationSpec(travel_cost_units=5),
    ))
    register_scenario(GMVScenario(
        scenario_id="action_only_v1", task_spec=basic, allow_language_report=False,
    ))


_install_builtins()
