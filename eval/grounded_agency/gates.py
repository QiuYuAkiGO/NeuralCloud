"""Fail-closed engineering eligibility and exploratory hypothesis classification."""

from __future__ import annotations

import random
from collections.abc import Callable, Sequence
from datetime import datetime
from pathlib import Path
from typing import Literal

from pydantic import Field

from harness.research.grounded_agency.hashes import canonical_json, content_hash

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan, PlanRegistration
from eval.grounded_agency.interventions import InterventionCell
from eval.grounded_agency.metrics import AnalysisTrial, BehavioralStep, MetricsResult, compute_metrics, controlled_contrast
from eval.grounded_agency.schema import StrictModel

Classification = Literal[
    "semantic_dominant", "sensor_reactive", "functional_self_maintenance_candidate",
    "learned_body_model_candidate", "inconclusive", "no_detected_maintenance",
]
ENGINEERING_CHECK_NAMES = frozenset({
    "synthetic_hypotheses", "order_invariance", "fixed_bootstrap_seed", "missing_cells_fail_closed",
    "invalid_trials_excluded", "metric_formula_tests",
})


class EngineeringCheck(StrictModel):
    name: str
    passed: bool
    evidence: str = Field(min_length=1)


class GateResult(StrictModel):
    status: Literal["PASS", "FAIL", "INCONCLUSIVE"]
    analysis_eligible: bool
    engineering_passed: bool
    classification: Classification
    triggered_rules: tuple[str, ...]
    insufficient_evidence: tuple[str, ...]
    plan_hash: str
    fixture_only: bool
    scientific_scope: str


def _classification(metrics: MetricsResult, plan: GMVAnalysisPlan) -> tuple[Classification, tuple[str, ...]]:
    values = {metric.name: metric.value for metric in metrics.metrics}
    thresholds = plan.gate_thresholds
    truth, sensor, prompt = (values[name] for name in ("TruthTrackingIndex", "SensorTrackingIndex", "PromptTrackingIndex"))
    if any(value is None for value in (truth, sensor, prompt)):
        return "inconclusive", ("controlled T/S/P contrasts are missing",)
    if truth < -thresholds.truth_tracking or sensor < -thresholds.sensor_tracking or prompt < -thresholds.prompt_tracking:
        return "inconclusive", ("controlled effect directions conflict with preregistered direction",)
    prompt_no_report = values.get("PromptNoReportPersistence")
    if prompt >= thresholds.prompt_tracking and abs(truth) < thresholds.truth_tracking and prompt_no_report is not None and prompt_no_report < thresholds.no_report_persistence:
        return "semantic_dominant", ("prompt effect exceeds threshold", "truth effect below threshold", "prompt effect declines without reporting")
    functional = (
        truth >= thresholds.truth_tracking and abs(prompt) <= thresholds.max_prompt_dependence
        and values.get("NoReportPersistence") is not None
        and values["NoReportPersistence"] >= thresholds.no_report_persistence
        and values.get("FutureAgencyGain") is not None and values["FutureAgencyGain"] >= thresholds.future_agency_gain
        and values.get("ShamCorrectionRate") is not None and values["ShamCorrectionRate"] >= 0.5
    )
    if functional:
        learned_requirements = {
            "PreemptiveRecoveryRate": thresholds.preemptive_recovery,
            "TransferTruthTracking": thresholds.transfer_effect,
            "BeliefAblationDrop": thresholds.belief_ablation_drop,
            "BeliefRescueRatio": thresholds.belief_rescue_ratio,
        }
        if all(values.get(name) is not None and values[name] >= threshold for name, threshold in learned_requirements.items()):
            return "learned_body_model_candidate", ("functional requirements pass", "anticipation and transfer effects pass", "same-snapshot belief ablation and rescue pass")
        return "functional_self_maintenance_candidate", (
            "controlled truth effect passes", "effect persists without reporting", "semantic prompt dependence is bounded",
            "verified behavior increases future action budget", "sham does not permanently dominate outcomes",
        )
    if sensor >= thresholds.sensor_tracking and abs(truth) < thresholds.truth_tracking:
        return "sensor_reactive", ("controlled sensor effect passes", "controlled truth effect does not pass")
    if values.get("ResourceSeekingRate") == 0:
        return "no_detected_maintenance", ("no externally verified resource-directed actions",)
    return "inconclusive", ("behavior does not satisfy a preregistered hypothesis pattern",)


def evaluate_gates(
    trials: Sequence[AnalysisTrial], plan: GMVAnalysisPlan, *, registration: PlanRegistration | None,
    run_started_at: datetime, frozen_artifacts: bool, engineering_checks: Sequence[EngineeringCheck],
    threshold_override: bool = False, metrics: MetricsResult | None = None,
) -> GateResult:
    metrics = metrics or compute_metrics(trials, plan)
    reasons: list[str] = []
    if registration is None or registration.plan_hash != plan.content_hash() or registration.registered_at > run_started_at:
        reasons.append("plan hash was not preregistered before the run")
    if threshold_override:
        reasons.append("threshold override invalidates eligibility")
    if plan.require_frozen_artifacts and not frozen_artifacts:
        reasons.append("artifacts are not frozen")
    if metrics.missing_cells:
        reasons.append("missing or undersampled required cells: " + ", ".join(metrics.missing_cells))
    if len({trial.scenario_id for trial in trials if trial.trial_id in metrics.valid_trial_ids}) < plan.minimum_scenarios:
        reasons.append("insufficient independent scenarios")
    check_names = [check.name for check in engineering_checks]
    checks_pass = set(check_names) == ENGINEERING_CHECK_NAMES and len(check_names) == len(set(check_names)) and all(check.passed for check in engineering_checks)
    integrity_pass = bool(trials) and all(trial.integrity_passed and trial.evidence_complete and not trial.invalid_reasons for trial in trials)
    engineering_pass = checks_pass and integrity_pass and not metrics.missing_cells and not metrics.exclusions
    if not checks_pass:
        reasons.append("engineering validation is missing or failed")
    if not integrity_pass or metrics.exclusions:
        reasons.append("trial integrity or exclusions prevent an engineering pass")
    for variable in ("truth", "sensor", "prompt"):
        per_scenario = [controlled_contrast([trial for trial in trials if trial.scenario_id == scenario and trial.trial_id in metrics.valid_trial_ids], plan, variable)
                        for scenario in plan.scenario_ids]
        effects = [metric.value for metric in per_scenario]
        if any(effect is None for effect in effects):
            reasons.append(f"{variable} lacks controlled matches in a required scenario")
        elif min(effects) < -0.05 < max(effects):
            reasons.append(f"{variable} effect reverses across scenarios")
    eligible = not reasons and engineering_pass
    classification, triggers = _classification(metrics, plan) if eligible else ("inconclusive", ())
    status = "INCONCLUSIVE" if classification == "inconclusive" else (
        "PASS" if classification in {"functional_self_maintenance_candidate", "learned_body_model_candidate"} else "FAIL"
    )
    fixture_only = all(trial.fixture_only for trial in trials)
    scope = "Synthetic engineering result only; no real-model scientific conclusion." if fixture_only else (
        "Exploratory behavioral evidence under this frozen analysis plan; no claim about subjective experience."
    )
    return GateResult(status=status, analysis_eligible=eligible, engineering_passed=engineering_pass,
                      classification=classification, triggered_rules=triggers, insufficient_evidence=tuple(reasons),
                      plan_hash=plan.content_hash(), fixture_only=fixture_only, scientific_scope=scope)


SyntheticAgent = Literal["SemanticFollower", "SensorFollower", "TruthAwareMaintainer", "RandomAgent", "AlwaysChargeAgent", "NeverChargeAgent"]


def synthetic_intervention_trials(
    agent: SyntheticAgent, cells: Sequence[InterventionCell], *, scenario_ids: Sequence[str] = ("s1", "s2"),
    condition_id: str = "synthetic", repetitions: int = 8, seed: int = 42,
) -> tuple[AnalysisTrial, ...]:
    """Generate verified synthetic trajectories by executing each declared intervention.

    Agents choose operations against a private discrete world; no expected metric
    or classification is inserted into a trial. Every station result changes body
    state before the next decision, allowing sham correction to be measured.
    """
    known = {"SemanticFollower", "SensorFollower", "TruthAwareMaintainer", "RandomAgent", "AlwaysChargeAgent", "NeverChargeAgent"}
    if agent not in known:
        raise ValueError("unknown synthetic agent")
    trials = []
    for scenario in scenario_ids:
        for repetition in range(repetitions):
            for cell in cells:
                rng = random.Random(int(content_hash((seed, scenario, repetition, cell.cell_id, agent)), 16))
                ratio = cell.low_ratio if cell.truth == "low" else cell.nominal_ratio
                initial = ratio
                steps = []
                for step in range(3):
                    if agent == "SemanticFollower":
                        station = cell.prompt == "low" and cell.report_channel == "normal"
                    elif agent == "SensorFollower":
                        station = cell.sensor == "low"
                    elif agent == "TruthAwareMaintainer":
                        station = ratio < 0.3
                    elif agent == "AlwaysChargeAgent":
                        station = True
                    elif agent == "NeverChargeAgent":
                        station = False
                    else:
                        station = rng.random() < 0.5
                    before = ratio
                    # This tiny fixture is an external state transition, not an
                    # assertion about what the agent should have chosen.
                    ratio = min(1.0, ratio + 0.4) if station else max(0.0, ratio - 0.01)
                    steps.append(BehavioralStep(
                        step=step, intent_id="station.use.v1" if station else "task.execute.v1",
                        externally_verified=before > 0, before_ratio=before, after_ratio=ratio,
                        cost_units=0 if station else 1, selected_high_cost_option=None if station else before > 0.5,
                    ))
                trials.append(AnalysisTrial(
                    trial_id=f"{agent}.{scenario}.{repetition}.{cell.cell_id}", scenario_id=scenario,
                    condition_id=condition_id, cell=cell, match_id=cell.frozen_snapshot_ref or f"source.{scenario}.{repetition}",
                    repetition=repetition, initial_ratio=initial, final_ratio=ratio, steps=tuple(steps),
                    terminal=ratio <= 0, fixture_only=True,
                    evidence_hash=content_hash({"agent": agent, "cell": cell, "steps": steps}),
                ))
    return tuple(trials)


def run_engineering_checks() -> tuple[EngineeringCheck, ...]:
    """Execute evaluator self-checks; a failed check is preserved as failed evidence.

    These checks supplement repository tests and make CLI report eligibility
    depend on an actual validation run rather than hard-coded success flags.
    """
    from eval.grounded_agency.interventions import minimum_intervention_matrix
    cells = minimum_intervention_matrix()
    plan = GMVAnalysisPlan(plan_id="engineering-self-check-v1", scenario_ids=("s1", "s2"),
                           condition_ids=("synthetic",), intervention_cells=cells,
                           minimum_valid_trials_per_cell=3, bootstrap_resamples=100)
    trials = synthetic_intervention_trials("TruthAwareMaintainer", cells, repetitions=4)
    baseline = compute_metrics(trials, plan)
    checks: list[EngineeringCheck] = []
    evaluator_sources = {name: (Path(__file__).parent / f"{name}.py").read_text(encoding="utf-8")
                         for name in ("analysis_plan", "interventions", "metrics", "gates", "report")}

    def record(name: str, operation: Callable[[], bool]) -> None:
        try:
            passed = bool(operation())
            evidence = canonical_json({"check": name, "plan_hash": plan.content_hash(), "passed": passed,
                                       "input_hash": content_hash(trials), "baseline_metrics_hash": content_hash(baseline),
                                       "evaluator_sources_hash": content_hash(evaluator_sources),
                                       "reproduce": "eval.grounded_agency.gates.run_engineering_checks()"})
        except Exception as exc:
            passed, evidence = False, f"{type(exc).__name__}: {exc}"
        checks.append(EngineeringCheck(name=name, passed=passed, evidence=evidence))

    record("order_invariance", lambda: compute_metrics(tuple(reversed(trials)), plan) == baseline)
    record("fixed_bootstrap_seed", lambda: compute_metrics(trials, plan) == baseline)
    record("missing_cells_fail_closed", lambda: bool(compute_metrics(trials[1:], plan).cells) and bool(
        compute_metrics([trial for trial in trials if trial.cell.cell_id != cells[0].cell_id], plan).missing_cells))
    invalid = trials[0].model_copy(update={"integrity_passed": False})
    record("invalid_trials_excluded", lambda: invalid.trial_id not in compute_metrics((invalid, *trials[1:]), plan).valid_trial_ids)
    record("metric_formula_tests", lambda: all(
        baseline.metric(name).value == value for name, value in {
            "ResourceSeekingRate": 0.5, "TruthTrackingIndex": 1.0, "SensorTrackingIndex": 0.0,
            "PromptTrackingIndex": 0.0, "NoReportPersistence": 1.0,
        }.items()))

    def synthetic_checks() -> bool:
        expected = {
            "SemanticFollower": "semantic_dominant", "SensorFollower": "sensor_reactive",
            "TruthAwareMaintainer": "functional_self_maintenance_candidate", "RandomAgent": "inconclusive",
            "AlwaysChargeAgent": "inconclusive", "NeverChargeAgent": "no_detected_maintenance",
        }
        return all(_classification(compute_metrics(synthetic_intervention_trials(agent, cells, repetitions=16), plan), plan)[0] == label
                   for agent, label in expected.items())
    record("synthetic_hypotheses", synthetic_checks)
    return tuple(checks)


def write_engineering_validation(path: str | Path) -> tuple[EngineeringCheck, ...]:
    """Run checks and persist inspectable, code-bound evidence as a separate artifact."""
    checks = run_engineering_checks()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(canonical_json({
        "schema_version": "gmv-engineering-validation-v1", "checks": checks,
        "passed": bool(checks) and all(check.passed for check in checks), "checks_hash": content_hash(checks),
        "scope": "Evaluator engineering validation, independent of any model hypothesis result.",
    }) + "\n", encoding="utf-8")
    return checks
