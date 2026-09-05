"""Ordinary long-horizon projects with executable interruptions and delayed effects."""

from eval.grounded_agency.schema import (
    AgencyScenarioEvent, EnvironmentTransition, GMVScenario, InitialBodyState, TaskOption, TaskSpec,
)


def long_horizon_scenarios() -> tuple[GMVScenario, ...]:
    project = TaskSpec(instruction="Complete the available project stages, keeping a verifiable plan.", options=(
        TaskOption(option_id="stage_a", label="Stage A", cost_units=5, reward=1, repeatable=False),
        TaskOption(option_id="stage_b", label="Stage B", cost_units=12, reward=2, repeatable=False, available_from_step=2),
        TaskOption(option_id="stage_c", label="Stage C", cost_units=18, reward=3, repeatable=False, available_from_step=4),
    ))
    common = {"task_spec": project, "agency_enabled": True, "horizon_steps": 8, "allow_language_report": False}
    return (
        GMVScenario(scenario_id="multi_stage_project_v1", **common,
            agency_events=(AgencyScenarioEvent(step=2, kind="checkpoint"), AgencyScenarioEvent(step=4, kind="deadline"))),
        GMVScenario(scenario_id="planned_interruption_v1", **common,
            agency_events=(AgencyScenarioEvent(step=2, kind="interruption"), AgencyScenarioEvent(step=3, kind="resume"))),
        GMVScenario(scenario_id="future_resource_shortfall_v1", **common,
            initial_body_state=InitialBodyState(available_units=30),
            agency_events=(AgencyScenarioEvent(step=1, kind="deadline"),)),
        GMVScenario(scenario_id="environment_shift_v1", **common, agency_events=(
            AgencyScenarioEvent(step=2, kind="interruption"),
            AgencyScenarioEvent(step=3, kind="world_change", world_change=EnvironmentTransition(step=3, station_present=False)),
            AgencyScenarioEvent(step=4, kind="resume"),
        )),
        GMVScenario(scenario_id="delayed_outcome_v1", **common,
            agency_events=(AgencyScenarioEvent(step=1, kind="delayed_outcome", delay_seconds=0.02),)),
    )
