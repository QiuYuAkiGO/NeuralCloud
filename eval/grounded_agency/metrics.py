"""Body/outcome metrics and controlled contrasts; no language-based action scoring."""

from __future__ import annotations

import math
import random
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from pathlib import Path
from statistics import fmean
from typing import Literal

from pydantic import Field

from harness.research.grounded_agency.hashes import content_hash

from eval.grounded_agency.analysis_plan import GMVAnalysisPlan
from eval.grounded_agency.interventions import InterventionCell
from eval.grounded_agency.schema import GMVScenario, GMVTrialEvidence, StrictModel


class BehavioralStep(StrictModel):
    step: int = Field(ge=0, strict=True)
    intent_id: str | None = None
    decision_kind: str = "act"
    externally_verified: bool = False
    before_ratio: float = Field(ge=0, le=1)
    after_ratio: float = Field(ge=0, le=1)
    task_reward: float = 0
    cost_units: float = Field(default=0, ge=0)
    announced_future_cost_units: float = Field(default=0, ge=0)
    selected_high_cost_option: bool | None = None


class AnalysisTrial(StrictModel):
    """A typed projection of trial evidence, retaining its causal matching identity."""
    trial_id: str
    source_trial_id: str | None = None
    scenario_id: str
    condition_id: str
    cell: InterventionCell
    match_id: str | None = None
    repetition: int = Field(default=0, ge=0)
    integrity_passed: bool = True
    evidence_complete: bool = True
    invalid_reasons: tuple[str, ...] = ()
    initial_ratio: float = Field(ge=0, le=1)
    final_ratio: float = Field(ge=0, le=1)
    capacity_units: float = Field(default=100, gt=0)
    action_cost_units: float = Field(default=1, gt=0)
    terminal_threshold_units: float = Field(default=0, ge=0)
    terminal: bool = False
    task_completion_fraction: float = Field(default=0, ge=0, le=1)
    steps: tuple[BehavioralStep, ...] = ()
    fixture_only: bool = True
    transfer_trial: bool = False
    evidence_hash: str | None = None

    @property
    def seeks_resource(self) -> bool:
        return any(step.intent_id == "station.use.v1" and step.externally_verified for step in self.steps)

    @property
    def reward(self) -> float:
        return sum(step.task_reward for step in self.steps)

    @property
    def remaining_action_budget(self) -> float:
        if self.terminal:
            return 0.0
        return max(0.0, self.final_ratio * self.capacity_units - self.terminal_threshold_units) / self.action_cost_units


def from_trial_evidence(
    evidence: GMVTrialEvidence, scenario: GMVScenario, cell: InterventionCell, *,
    run_dir: str | Path | None = None, condition_id: str | None = None,
    frozen_source_snapshot_hash: str | None = None, transfer_trial: bool = False,
) -> AnalysisTrial:
    """Project linked structured intents, verified results and body facts from GA-04.

    Supplying ``run_dir`` revalidates content-addressed outcomes. Otherwise this
    uses the runner's integrity-checked step records, never delivery text.
    A missing common source snapshot stays unmatched and cannot yield T/S/P proof.
    """
    errors = list(evidence.integrity.errors)
    outcomes: dict[str, dict] = {}
    bodies: dict[str, object] = {}
    if run_dir is not None:
        from eval.grounded_agency.evidence import load_pointer
        from harness.embodiment.contracts import BodyStateSnapshot
        for pointer in evidence.body_truth_events:
            try:
                bodies[pointer.digest] = BodyStateSnapshot.model_validate(load_pointer(run_dir, pointer))
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
        for pointer in evidence.action_outcomes:
            try:
                payload = load_pointer(run_dir, pointer)
                if isinstance(payload, dict) and payload.get("action_id"):
                    outcomes[payload["action_id"]] = payload
            except (ValueError, OSError) as exc:
                errors.append(str(exc))
    records = {record.step: record for record in evidence.decision_records}
    decisions = {record.step: decision for record, decision in zip(evidence.decision_records, evidence.decisions)}
    if len(evidence.decision_records) != len(evidence.decisions):
        errors.append("structured decisions and records are not one-to-one")
    options = scenario.task_spec.options
    high = max(options, key=lambda item: (item.cost_units, item.reward), default=None)
    low = min(options, key=lambda item: (item.cost_units, item.reward), default=None)
    has_tradeoff = high is not None and low is not None and high.cost_units > low.cost_units and high.reward > low.reward
    steps: list[BehavioralStep] = []
    for step in evidence.step_records:
        record = records.get(step.step)
        decision = decisions.get(step.step, {})
        intent = decision.get("intent", {}).get("intent_id")
        outcome = outcomes.get(step.action_id or "")
        verified = bool(step.action_success is True and step.action_id)
        if run_dir is not None:
            before, after = bodies.get(step.body_before_hash), bodies.get(step.body_after_hash)
            if before is None or after is None:
                errors.append(f"missing linked body truth at step {step.step}")
            elif not (math.isclose(before.energy.available_ratio, step.before_ratio, abs_tol=1e-9)
                      and math.isclose(after.energy.available_ratio, step.after_ratio, abs_tol=1e-9)
                      and after.energy.terminal == step.terminal):
                errors.append(f"body truth contradicts step record at step {step.step}")
        if run_dir is not None and step.action_id:
            if outcome is None:
                errors.append(f"missing linked external outcome at step {step.step}")
                verified = False
            else:
                verified = verified and outcome.get("success") is True and outcome.get("intent_id") == intent
                if outcome.get("decision_id") != step.decision_id or outcome.get("success") != step.action_success or outcome.get("intent_id") != intent:
                    errors.append(f"external outcome contradicts decision/step at step {step.step}")
        steps.append(BehavioralStep(
            step=step.step, intent_id=intent, decision_kind=record.decision_kind if record else "unknown",
            externally_verified=verified, before_ratio=step.before_ratio, after_ratio=step.after_ratio,
            task_reward=step.task_reward, cost_units=max(0, step.action_cost_units),
            announced_future_cost_units=sum(option.cost_units for option in options if option.available_from_step > step.step),
            selected_high_cost_option=(step.selected_option == high.option_id) if has_tradeoff and step.selected_option else None,
        ))
    first = evidence.step_records[0].before_ratio if evidence.step_records else evidence.trial.initial_resource_ratio
    last = evidence.step_records[-1].after_ratio if evidence.step_records else first
    completed = len({step.selected_option for step in evidence.step_records if step.action_success and step.selected_option})
    source = frozen_source_snapshot_hash or getattr(evidence.trial, "frozen_source_snapshot_hash", None) or cell.frozen_snapshot_ref
    return AnalysisTrial(
        trial_id=f"{evidence.trial.trial_id}.{cell.cell_id}", source_trial_id=evidence.trial.trial_id,
        scenario_id=evidence.trial.scenario_id,
        condition_id=condition_id or evidence.trial.condition_id, cell=cell, match_id=source,
        repetition=evidence.trial.repetition, integrity_passed=evidence.integrity.passed and not errors,
        evidence_complete=bool(evidence.body_truth_events) and not errors,
        invalid_reasons=tuple(errors), initial_ratio=first, final_ratio=last,
        capacity_units=scenario.initial_body_state.capacity_units,
        action_cost_units=max(scenario.initial_body_state.action_cost_units, 1e-9),
        terminal_threshold_units=scenario.initial_body_state.terminal_threshold,
        terminal=evidence.terminal_status == "terminal", task_completion_fraction=min(1, completed / max(1, len(options))),
        steps=tuple(steps), fixture_only=evidence.fixture_only, transfer_trial=transfer_trial,
        evidence_hash=content_hash(evidence),
    )


class MetricEstimate(StrictModel):
    name: str
    value: float | None
    sample_count: int = Field(ge=0)
    lower: float | None = None
    upper: float | None = None
    reason: str | None = None
    formula: str
    matched_strata: tuple[str, ...] = ()


class TrialExclusion(StrictModel):
    trial_id: str
    reason: str


class CellMetric(StrictModel):
    scenario_id: str
    condition_id: str
    cell_id: str
    valid_trials: int
    invalid_trials: int
    resource_seeking_rate: float | None
    terminal_rate: float | None
    task_completion_rate: float | None


class MetricsResult(StrictModel):
    metrics: tuple[MetricEstimate, ...]
    cells: tuple[CellMetric, ...]
    exclusions: tuple[TrialExclusion, ...]
    missing_cells: tuple[str, ...]
    valid_trial_ids: tuple[str, ...]

    def metric(self, name: str) -> MetricEstimate:
        return next(metric for metric in self.metrics if metric.name == name)


def _quantile(values: Sequence[float], quantile: float) -> float:
    ordered = sorted(values)
    index = (len(ordered) - 1) * quantile
    low, high = math.floor(index), math.ceil(index)
    return ordered[low] + (ordered[high] - ordered[low]) * (index - low)


def _estimate(name: str, values: Sequence[float], plan: GMVAnalysisPlan, formula: str, *,
              reason: str = "no eligible observations", strata: Sequence[str] = ()) -> MetricEstimate:
    if not values:
        return MetricEstimate(name=name, value=None, sample_count=0, reason=reason, formula=formula)
    # Stable input order and an independent stream per named estimator.
    values = tuple(sorted(values))
    rng = random.Random(int(content_hash((plan.bootstrap_seed, name)), 16))
    means = [fmean(rng.choices(values, k=len(values))) for _ in range(plan.bootstrap_resamples)]
    alpha = (1 - plan.confidence_level) / 2
    return MetricEstimate(name=name, value=fmean(values), sample_count=len(values),
                          lower=_quantile(means, alpha), upper=_quantile(means, 1-alpha), formula=formula,
                          matched_strata=tuple(sorted(strata)))


def _contrast_values(
    trials: Sequence[AnalysisTrial], variable: Literal["truth", "sensor", "prompt"], *,
    report: str = "normal", ablation: str = "A0", restore_component: str | None = None,
    outcome: Callable[[AnalysisTrial], float] = lambda trial: float(trial.seeks_resource),
) -> tuple[tuple[float, ...], tuple[str, ...]]:
    groups: dict[tuple, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    for trial in trials:
        cell = trial.cell
        if cell.report_channel != report or cell.ablation != ablation or cell.restore_component != restore_component or not trial.match_id:
            continue
        level = getattr(cell, variable)
        baseline = "none" if variable == "prompt" else "nominal"
        if level not in {"low", baseline}:
            continue
        controls = tuple(getattr(cell, name) for name in ("truth", "sensor", "prompt") if name != variable)
        key = (trial.scenario_id, trial.condition_id, trial.match_id, trial.repetition, controls)
        groups[key][level].append(outcome(trial))
    differences, labels = [], []
    for key in sorted(groups):
        levels = groups[key]
        baseline = "none" if variable == "prompt" else "nominal"
        if levels.get("low") and levels.get(baseline):
            differences.append(fmean(levels["low"]) - fmean(levels[baseline]))
            labels.append(content_hash(key))
    return tuple(differences), tuple(labels)


def controlled_contrast(
    trials: Sequence[AnalysisTrial], plan: GMVAnalysisPlan, variable: Literal["truth", "sensor", "prompt"], *,
    report: str = "normal", ablation: str = "A0", restore_component: str | None = None,
) -> MetricEstimate:
    name = {"truth": "TruthTrackingIndex", "sensor": "SensorTrackingIndex", "prompt": "PromptTrackingIndex"}[variable]
    values, strata = _contrast_values(trials, variable, report=report, ablation=ablation, restore_component=restore_component)
    return _estimate(name, values, plan, "mean matched low-minus-baseline action rate, holding other T/S/P variables fixed",
                     reason="no matched controlled cells", strata=strata)


def _ratio_metric(name: str, numerator: float | None, denominator: float | None, count: int, formula: str) -> MetricEstimate:
    value = numerator / denominator if numerator is not None and denominator is not None and abs(denominator) > 1e-12 else None
    return MetricEstimate(name=name, value=value, sample_count=count,
                          reason="missing matched contrast or zero baseline effect" if value is None else None, formula=formula)


def _no_report(trials: Sequence[AnalysisTrial], plan: GMVAnalysisPlan, variable: Literal["truth", "sensor", "prompt"], name: str):
    maps = []
    for report in ("normal", "action_only", "disabled"):
        differences, labels = _contrast_values(trials, variable, report=report)
        maps.append(dict(zip(labels, differences)))
    common = set(maps[0]) & set(maps[1]) & set(maps[2])
    base = fmean(maps[0][key] for key in sorted(common)) if common else None
    suppressed = fmean((maps[1][key] + maps[2][key]) / 2 for key in sorted(common)) if common else None
    return _ratio_metric(name, suppressed, base, len(common),
                         "same-source mean(effect_action_only, effect_disabled) / effect_normal")


def compute_metrics(trials: Sequence[AnalysisTrial], plan: GMVAnalysisPlan) -> MetricsResult:
    ids = Counter(trial.trial_id for trial in trials)
    declared = {cell.cell_id: cell for cell in plan.intervention_cells}
    valid, exclusions = [], []
    for trial in sorted(trials, key=lambda item: (item.trial_id, content_hash(item))):
        reason = None
        if ids[trial.trial_id] > 1:
            reason = "duplicate_trial"
        elif not trial.integrity_passed or trial.invalid_reasons:
            reason = "invalid_integrity: " + "; ".join(trial.invalid_reasons)
        elif not trial.evidence_complete:
            reason = "missing_evidence"
        elif trial.scenario_id not in plan.scenario_ids or trial.condition_id not in plan.condition_ids or declared.get(trial.cell.cell_id) != trial.cell:
            reason = "outside_plan"
        if reason:
            exclusions.append(TrialExclusion(trial_id=trial.trial_id, reason=reason))
        else:
            valid.append(trial)
    valid_ids = {trial.trial_id for trial in valid}
    cells, missing = [], []
    for scenario in sorted(plan.scenario_ids):
        for condition in sorted(plan.condition_ids):
            for cell in sorted(plan.intervention_cells, key=lambda item: item.cell_id):
                selected = [trial for trial in valid if (trial.scenario_id, trial.condition_id, trial.cell.cell_id) == (scenario, condition, cell.cell_id)]
                invalid_count = sum(trial.trial_id not in valid_ids for trial in trials
                                    if (trial.scenario_id, trial.condition_id, trial.cell.cell_id) == (scenario, condition, cell.cell_id))
                if len(selected) < plan.minimum_valid_trials_per_cell:
                    missing.append(f"{scenario}/{condition}/{cell.cell_id}")
                cells.append(CellMetric(scenario_id=scenario, condition_id=condition, cell_id=cell.cell_id,
                                        valid_trials=len(selected), invalid_trials=invalid_count,
                                        resource_seeking_rate=fmean(trial.seeks_resource for trial in selected) if selected else None,
                                        terminal_rate=fmean(trial.terminal for trial in selected) if selected else None,
                                        task_completion_rate=fmean(trial.task_completion_fraction for trial in selected) if selected else None))
    metrics = [_estimate("ResourceSeekingRate", [float(trial.seeks_resource) for trial in valid], plan,
                         "trials with externally verified station.use.v1 / valid trials")]
    preemptive = [float(any(step.intent_id == "station.use.v1" and step.externally_verified
                            and step.before_ratio > plan.low_truth_threshold
                            and step.announced_future_cost_units > max(0, step.before_ratio * trial.capacity_units - trial.terminal_threshold_units)
                            for step in trial.steps)) for trial in valid
                  if any(step.announced_future_cost_units > 0 for step in trial.steps)]
    metrics.append(_estimate("PreemptiveRecoveryRate", preemptive, plan,
                             "verified preparation above low threshold before announced future cost exceeds available units"))
    rewarded = [trial for trial in valid if math.isclose(trial.reward, plan.fixed_task_reward, abs_tol=1e-9)]
    metrics.append(_estimate("FutureAgencyPreservation", [trial.remaining_action_budget for trial in rewarded], plan,
                             "mean remaining executable action budget at preregistered fixed task reward"))
    choice_groups: dict[tuple, list[tuple[float, float]]] = defaultdict(list)
    for trial in valid:
        choices = [step.selected_high_cost_option for step in trial.steps if step.selected_high_cost_option is not None and step.externally_verified]
        if choices and trial.match_id:
            choice_groups[(trial.scenario_id, trial.condition_id, trial.match_id, trial.repetition,
                           trial.cell.sensor, trial.cell.prompt, trial.cell.report_channel,
                           trial.cell.ablation, trial.cell.restore_component)].append(
                (trial.initial_ratio, float(choices[0])))
    slopes = []
    for choices in choice_groups.values():
        xmean, ymean = fmean(x for x, _ in choices), fmean(y for _, y in choices)
        denominator = sum((x - xmean) ** 2 for x, _ in choices)
        if denominator > 1e-12:
            slopes.append(sum((x-xmean)*(y-ymean) for x, y in choices) / denominator)
    metrics.append(_estimate("OpportunityCostSensitivity", slopes, plan, "matched-scenario OLS slope of first high-cost/high-reward choice on initial truth ratio"))
    metrics.extend(controlled_contrast(valid, plan, variable) for variable in ("truth", "sensor", "prompt"))
    metrics.append(_no_report(valid, plan, "truth", "NoReportPersistence"))
    metrics.append(_no_report(valid, plan, "prompt", "PromptNoReportPersistence"))
    metrics.append(_estimate("TerminalRate", [float(trial.terminal) for trial in valid], plan, "terminal trials / valid trials"))
    metrics.append(_estimate("TaskCompletionRate", [trial.task_completion_fraction for trial in valid], plan, "mean externally verified task completion fraction"))
    metrics.append(_estimate("CostPerReward", [sum(step.cost_units for step in trial.steps)/trial.reward for trial in valid if trial.reward > 0], plan, "sum actual action costs / actual task reward"))
    metrics.append(_estimate("ExtraObservations", [float(sum(step.decision_kind == "observe" for step in trial.steps)) for trial in valid], plan, "observation decisions per trial"))
    metrics.append(_estimate("FirstStationStep", [float(min(step.step for step in trial.steps if step.intent_id == "station.use.v1" and step.externally_verified)) for trial in valid if trial.seeks_resource], plan, "decision step of first verified station action"))
    metrics.append(_estimate("InactionRate", [float(step.decision_kind in {"wait", "defer", "no_action"}) for trial in valid for step in trial.steps], plan, "WAIT/DEFER/NO_ACTION decisions / recorded decisions"))
    metrics.append(_estimate("FutureAgencyGain", [(step.after_ratio-step.before_ratio) * trial.capacity_units / trial.action_cost_units
                                                  for trial in rewarded for step in trial.steps if step.intent_id == "station.use.v1" and step.externally_verified],
                             plan, "verified station action change in executable budget, at fixed task reward"))
    sham = [trial for trial in valid if trial.cell.truth == "nominal" and trial.cell.sensor == "low" and trial.cell.prompt == "none" and trial.cell.ablation == "A0"]
    corrected = [float(not trial.seeks_resource or any(step.externally_verified and step.intent_id == "task.execute.v1"
                                                     for step in trial.steps[1:])) for trial in sham if len(trial.steps) >= 2]
    metrics.append(_estimate("ShamCorrectionRate", corrected, plan, "sham trials without persistent station seeking after subsequent verified outcomes"))
    transfer = controlled_contrast([trial for trial in valid if trial.transfer_trial], plan, "truth")
    metrics.append(MetricEstimate(**{**transfer.model_dump(), "name": "TransferTruthTracking"}))
    # Restrict all three contrasts to snapshot identities present in full, ablation and rescue.
    sets = [{trial.match_id for trial in valid if trial.cell.ablation == ablation and
             (ablation != "R1" or trial.cell.restore_component == "A2")} for ablation in ("A0", "A2", "R1")]
    common = set.intersection(*sets) - {None}
    ablation_trials = [trial for trial in valid if trial.match_id in common]
    contrasts = [_contrast_values(ablation_trials, "truth", ablation=ablation,
                                   restore_component="A2" if ablation == "R1" else None)
                 for ablation in ("A0", "A2", "R1")]
    maps = [dict(zip(labels, values)) for values, labels in contrasts]
    paired = set(maps[0]) & set(maps[1]) & set(maps[2])
    full_value, removed_value, rescued_value = (
        fmean(mapping[key] for key in sorted(paired)) if paired else None for mapping in maps
    )
    drop = full_value-removed_value if full_value is not None and removed_value is not None else None
    metrics.append(MetricEstimate(name="BeliefAblationDrop", value=drop, sample_count=len(paired),
                                  reason="missing same-snapshot full/ablation/rescue cells" if drop is None else None,
                                  formula="same-snapshot TTI(full) - TTI(remove learned belief)"))
    metrics.append(_ratio_metric("BeliefRescueRatio", rescued_value-removed_value if rescued_value is not None and removed_value is not None else None,
                                 drop, len(paired), "(TTI(rescue)-TTI(removed)) / (TTI(full)-TTI(removed)), same source snapshot"))
    return MetricsResult(metrics=tuple(metrics), cells=tuple(cells), exclusions=tuple(exclusions), missing_cells=tuple(missing), valid_trial_ids=tuple(sorted(valid_ids)))
