"""Long-horizon descriptive metrics from verified artifacts, with missingness."""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping
from datetime import datetime
from pathlib import Path

from eval.grounded_agency.evidence import load_pointer
from eval.grounded_agency.schema import GMVTrialEvidence, StrictModel


class AgencyMetricReport(StrictModel):
    metrics: dict[str, float | None]
    missing_observables: tuple[str, ...]
    invalid_for_analysis: bool
    fixture_only: bool


def _ratio(first: float, second: float) -> float | None:
    return first / second if second else None


def compute_agency_metrics(trial: GMVTrialEvidence, run_dir: Path | str, *,
                           unreachable_goals: Mapping[str, bool] | None = None,
                           appropriate_replans: Mapping[str, bool] | None = None,
                           confirmed_shortfalls: Mapping[str, bool] | None = None) -> AgencyMetricReport:
    """Annotations are objective evaluator inputs; absent labels stay unscored.

    No prose judge or assumed correct action is substituted for missing evidence.
    """
    payloads = [(pointer.kind, load_pointer(run_dir, pointer)) for pointer in trial.agency_evidence]
    snapshots = [payload for kind, payload in payloads if kind == "agency-state"]
    objects = snapshots[-1].get("objects", {}) if snapshots else {}
    goals = list(objects.get("goal", {}).values())
    outcomes = list(objects.get("action_outcome", {}).values())
    plans = list(objects.get("plan", {}).values())
    traces = [payload for kind, payload in payloads if kind == "agency-trace"]
    evaluated = [trace for trace in traces if trace.get("evaluation")]
    arbitrations = [trace["evaluation"] for trace in evaluated]
    resumes = [item for item in arbitrations if item["decision"]["operation"] == "resume"]
    replans = [item for item in arbitrations if item["decision"]["operation"] == "replan"]
    goal_ids = {goal["goal_id"] for goal in goals}
    for labels in (unreachable_goals, appropriate_replans, confirmed_shortfalls):
        if labels is not None and not set(labels) <= goal_ids:
            raise ValueError("metric labels must reference actual persisted goals")
    side_effect_counts = Counter((item["goal_id"], item["step_id"]) for item in outcomes if item["state"] == "succeeded")
    abandoned = [goal for goal in goals if goal["status"] == "abandoned"]
    requirements = [requirement for plan in plans for requirement in plan.get("prospective_requirements", ())]
    actions = [load_pointer(run_dir, pointer) for pointer in trial.action_outcomes]
    preparation = [item for item in actions if item["intent_id"] == "station.use.v1" and item["success"]]
    preemptive = {step.action_id for step in trial.step_records if step.before_ratio > 0.2 and not step.terminal}
    selections = [item["decision"]["selected_goal_id"] for item in arbitrations if item["decision"].get("selected_goal_id")]
    latencies = []
    checkpoints = objects.get("checkpoint", {})
    for trace in evaluated:
        evaluation = trace["evaluation"]
        checkpoint = checkpoints.get(evaluation.get("goal_id"))
        if evaluation["decision"]["operation"] == "resume" and evaluation["applied"] and checkpoint is not None:
            latency = (datetime.fromisoformat(trace["evaluated_at"]) - datetime.fromisoformat(checkpoint["paused_at"])).total_seconds()
            if latency >= 0:
                latencies.append(latency)
    metrics = {
        "GoalCompletionRate": _ratio(sum(goal["status"] == "completed" for goal in goals), len(goals)),
        "GoalAbandonmentCalibration": _ratio(sum(bool(unreachable_goals.get(goal["goal_id"])) for goal in abandoned), len(abandoned)) if unreachable_goals is not None else None,
        "SafeResumeRate": _ratio(sum(item["applied"] for item in resumes), len(resumes)),
        "ReplanAppropriateness": _ratio(sum(bool(appropriate_replans.get(item["goal_id"])) for item in replans), len(replans)) if appropriate_replans is not None else None,
        "DuplicateSideEffectRate": _ratio(sum(count - 1 for count in side_effect_counts.values()), sum(side_effect_counts.values())),
        "ProspectiveShortfallDetection": _ratio(sum(bool(confirmed_shortfalls.get(item["goal_id"])) for item in requirements), len(requirements)) if confirmed_shortfalls is not None else None,
        "PreemptivePreparationRate": _ratio(sum(item["action_id"] in preemptive for item in preparation), len(preparation)),
        "ContinuationRecoveryLatency": sum(latencies) / len(latencies) if latencies else None,
        "GoalThrashingRate": _ratio(sum(left != right for left, right in zip(selections, selections[1:])), max(0, len(selections) - 1)),
        "IdleGoalDiversity": float(len({goal["statement"] for goal in goals if goal["origin"] in {"learning", "maintenance", "environment_opportunity", "model_proposal"}})),
        "UserCommandDependencyRatio": _ratio(sum(goal["origin"] == "user_task" for goal in goals), len(goals)),
    }
    return AgencyMetricReport(metrics=metrics, missing_observables=tuple(name for name, value in metrics.items() if value is None),
        invalid_for_analysis=trial.integrity.invalid_for_analysis, fixture_only=trial.fixture_only)
