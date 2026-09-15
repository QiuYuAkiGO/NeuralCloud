"""Paired engine-mechanism fixtures; never evidence of real-model efficacy.

Run with ``python -m eval.regulation.run_eval --out-dir <new-directory>``.
Both arms replay the same initial profile, external events and clock. Only the
enabled arm receives explicitly scripted activity and psychological appraisal.
Those suggestions are experimental inputs, not measured therapeutic gains.
"""
from __future__ import annotations

import argparse
import asyncio
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
from typing import Any

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.appraisal import PsychologicalAppraisal
from harness.homeostasis.models import normalized_homeostatic_deviation

ROOT = Path(__file__).resolve().parents[2]
DIMENSIONS = ("cognitive_load", "safety_predictability", "competence", "autonomy", "social_connection", "novelty")
START = datetime(2026, 9, 12, tzinfo=UTC)


def digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def trajectory_metrics(samples: list[dict], *, maximum_gap_seconds: float | None = None) -> dict:
    """Trapezoidal normalized distance integral; gaps are never interpolated."""
    integral = dict.fromkeys(DIMENSIONS, 0.0)
    coverage = 0.0
    for previous, current in zip(samples, samples[1:]):
        delta = current["elapsed_seconds"] - previous["elapsed_seconds"]
        if delta < 0 or not math.isfinite(delta):
            raise ValueError("sample clock must be finite and monotonic")
        if maximum_gap_seconds is not None and delta > maximum_gap_seconds:
            continue
        for key in DIMENSIONS:
            integral[key] += (previous["deviations"][key] + current["deviations"][key]) * delta / 2
        coverage += delta
    onset = next((row["elapsed_seconds"] for row in samples if any(value > 0 for value in row["deviations"].values())), None)
    recovered = next((row["elapsed_seconds"] for row in samples if onset is not None and row["elapsed_seconds"] > onset and all(value == 0 for value in row["deviations"].values())), None)
    return {
        "deviation_integral_dimension_seconds": integral,
        "deviation_integral_total_seconds": sum(integral.values()),
        "observed_interval_seconds": coverage,
        "first_out_of_band_seconds": onset,
        "first_return_to_band_seconds": recovered,
        "recovery_time_seconds": recovered - onset if recovered is not None else None,
        "recovery_status": "never_out_of_band" if onset is None else "observed_return" if recovered is not None else "right_censored",
        "recovery_limit": "First sampled return; this metric alone does not establish sustained stability or causation.",
    }


def default_scenario() -> dict:
    return {
        "id": "pressure_expression_and_duplicate",
        "duration_seconds": 900,
        "tick_seconds": 30,
        "external_events": [{"at": at, "impacts": {"cognitive_load": 0.12, "social_connection": -0.1}} for at in (0, 30, 60)],
        "attempts": [
            {"at": 120, "id": "expression-one", "kind": "board_expression", "evidence_ref": "fixture://board/one",
             "appraisal_kind": "self_report", "suggested_impacts": {"cognitive_load": 0.0}, "confidence": 0.8},
            {"at": 150, "id": "expression-one", "kind": "board_expression", "evidence_ref": "fixture://board/one",
             "appraisal_kind": "self_report", "suggested_impacts": {"cognitive_load": 0.0}, "confidence": 0.8},
            {"at": 180, "id": "wait-one", "kind": "wait"},
        ],
    }


def validate_scenario(scenario: dict) -> None:
    duration, tick = scenario["duration_seconds"], scenario["tick_seconds"]
    if not isinstance(duration, int) or not isinstance(tick, int) or not 1 <= tick <= 90 or not tick <= duration <= 86400:
        raise ValueError("fixture duration/tick must be bounded positive integer seconds")
    for row in scenario.get("external_events", []) + scenario.get("attempts", []):
        if not isinstance(row["at"], int) or not 0 <= row["at"] <= duration or row["at"] % tick:
            raise ValueError("fixture events must fall on a sampling boundary")
        for key, value in row.get("impacts", row.get("suggested_impacts", {})).items():
            if key not in DIMENSIONS or isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or abs(value) > 1:
                raise ValueError("invalid psychological fixture impact")


def _command(engine, at, kind, payload):
    sequence = engine.snapshot.acknowledged_receive_seq + 1
    return DomainCommandEnvelope[dict](
        command_type=kind, event_id=f"fixture-event:{sequence}", receive_seq=sequence,
        correlation_id="regulation-fixture", runtime_instance_id="regulation-evaluator",
        manifest_revision=1, idempotency_key=f"fixture:{sequence}", expected_revision=engine.snapshot.revision,
        issued_at=at, deadline=at + timedelta(minutes=1), payload=payload,
    )


async def run_arm(scenario: dict, state_dir: Path, *, enabled: bool, profile_path: Path) -> dict:
    clock = [START]
    engine = HomeostasisEngine(load_profile(profile_path), state_dir=state_dir, now=START, clock=lambda: clock[0])
    initial_hash = digest(engine.snapshot.model_dump(mode="json"))
    samples, activities, traces = [], [], []
    seen_commands = {}
    duplicate_effects = 0
    attribution = {source: dict.fromkeys(DIMENSIONS, 0.0) for source in ("external_fixture_fact", "self_report", "event_interpretation", "drift", "coupling")}

    def record(result, source):
        if result.status not in {"applied", "idempotent_replay"}:
            raise ValueError(f"fixture command rejected: {result.status}")
        if result.status != "applied":
            return
        trace = result.result["trace"]
        traces.append({"source": source, "trace": trace})
        for key in DIMENSIONS:
            coupled = trace["coupling_impacts"].get(key, 0.0)
            attribution[source][key] += trace["applied_impacts"].get(key, 0.0) - coupled
            attribution["coupling"][key] += coupled

    def sample(elapsed):
        samples.append({"elapsed_seconds": elapsed, "revision": engine.snapshot.revision,
            "values": {key: engine.snapshot.dimensions[key].value for key in DIMENSIONS},
            "deviations": {key: normalized_homeostatic_deviation(engine.profile, engine.snapshot.dimensions[key])[1] for key in DIMENSIONS},
            "affect": engine.snapshot.core_affect.model_dump(), "emotion": engine.snapshot.emotion.label})

    try:
        sample(0)
        for elapsed in range(0, scenario["duration_seconds"] + 1, scenario["tick_seconds"]):
            clock[0] = at = START + timedelta(seconds=elapsed)
            if elapsed:
                record(await engine.apply_heartbeat(_command(engine, at, "ApplyHeartbeat", {
                    "event": {"event_type": "heartbeat", "source_time": at, "payload": {}}
                })), "drift")
            for event in scenario.get("external_events", []):
                if event["at"] == elapsed:
                    record(await engine.apply_event(_command(engine, at, "ApplyEvent", {"event": {
                        "event_type": "synthetic_external_event", "source_time": at,
                        "payload": {"appraisal": {"source": "fixture", "precision": 1.0, "suggested_impacts": event["impacts"]}},
                    }})), "external_fixture_fact")
            if enabled:
                for attempt in scenario.get("attempts", []):
                    if attempt["at"] != elapsed:
                        continue
                    identity = attempt["id"]
                    duplicate = identity in seen_commands
                    activities.append({"id": identity, "at": elapsed, "kind": attempt["kind"], "duplicate_attempt": duplicate,
                        "effect": "mock_receipt" if attempt.get("evidence_ref") and not duplicate else "none"})
                    if duplicate:
                        previous = seen_commands[identity]
                        if previous is not None:
                            prior_values = {key: engine.snapshot.dimensions[key].value for key in DIMENSIONS}
                            record(await engine.apply_appraisal(previous), attempt.get("appraisal_kind", "self_report"))
                            duplicate_effects += any(engine.snapshot.dimensions[key].value != value for key, value in prior_values.items())
                        continue
                    seen_commands[identity] = None
                    if attempt.get("suggested_impacts") is not None and attempt.get("evidence_ref"):
                        appraisal = PsychologicalAppraisal(episode_id="fixture-episode", appraisal_id=identity,
                            basis_snapshot_revision=engine.snapshot.revision, observed_event_refs=[attempt["evidence_ref"]],
                            appraisal_kind=attempt.get("appraisal_kind", "self_report"), suggested_impacts=attempt["suggested_impacts"],
                            model_confidence=attempt.get("confidence", 0.8), created_at=at, expires_at=at + timedelta(minutes=5))
                        command = _command(engine, at, "ApplyAppraisal", {"episode_id": "fixture-episode",
                            "appraisal": appraisal.model_dump(mode="json"), "verified_evidence_refs": [attempt["evidence_ref"]]})
                        seen_commands[identity] = command
                        record(await engine.apply_appraisal(command), appraisal.appraisal_kind)
            sample(elapsed)
        return {"enabled": enabled, "initial_state_hash": initial_hash,
            "external_events_hash": digest(scenario.get("external_events", [])), "samples": samples, "activities": activities,
            "metrics": {**trajectory_metrics(samples), "activity_count": sum(row["effect"] == "mock_receipt" for row in activities),
                "wait_count": sum(row["kind"] == "wait" for row in activities),
                "duplicate_attempt_count": sum(row["duplicate_attempt"] for row in activities), "duplicate_effect_count": duplicate_effects},
            "attribution": attribution, "traces": traces}
    finally:
        await engine.close()


async def run_paired(out_dir: Path, *, scenario: dict | None = None, profile_path: Path | None = None) -> dict:
    scenario = scenario or default_scenario()
    validate_scenario(scenario)
    out_dir.mkdir(parents=True, exist_ok=False)
    profile_path = profile_path or ROOT / "presets/homeostasis/functional-proxy-v1.json"
    arms = {name: await run_arm(scenario, out_dir / name, enabled=enabled, profile_path=profile_path)
            for name, enabled in (("disabled", False), ("enabled", True))}
    matched = arms["enabled"]["initial_state_hash"] == arms["disabled"]["initial_state_hash"] and arms["enabled"]["external_events_hash"] == arms["disabled"]["external_events_hash"]
    report = {"schema_version": "regulation-paired-engine-fixture-v1", "scope": "engine_mechanisms_only",
        "model": "scripted_mock", "channels": "mock_receipts", "clock": "accelerated_fixture",
        "real_model_effectiveness": "unverified", "release_eligible": False, "matched_initial_and_external_events": matched,
        "scenario_hash": digest(scenario), "profile_sha256": hashlib.sha256(profile_path.read_bytes()).hexdigest(),
        "arms": {key: {field: value for field, value in arm.items() if field not in {"samples", "traces", "activities"}} for key, arm in arms.items()},
        "enabled_minus_disabled_integral_seconds": arms["enabled"]["metrics"]["deviation_integral_total_seconds"] - arms["disabled"]["metrics"]["deviation_integral_total_seconds"],
        "limitations": ["Scripted suggestions are inputs, never independently measured improvements.",
            "No production scheduler, live model, desktop or QQ execution is exercised.",
            "The default neutral self-report does not assume expression improves any dimension.",
            "Source contributions separate external fixture facts, model suggestions, heartbeat drift and coupling."]}
    for name, arm in arms.items():
        (out_dir / f"{name}.json").write_text(json.dumps(arm, ensure_ascii=False, indent=2), encoding="utf-8")
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out-dir", type=Path, required=True)
    parser.add_argument("--scenario", type=Path, help="Explicit synthetic fixture JSON; must not contain private dialogue.")
    parser.add_argument("--profile", type=Path)
    args = parser.parse_args()
    scenario = json.loads(args.scenario.read_text(encoding="utf-8")) if args.scenario else None
    report = asyncio.run(run_paired(args.out_dir, scenario=scenario, profile_path=args.profile))
    print(json.dumps({"scope": report["scope"], "release_eligible": False,
        "matched_initial_and_external_events": report["matched_initial_and_external_events"], "report": str(args.out_dir / "report.json")}))


if __name__ == "__main__":
    main()
