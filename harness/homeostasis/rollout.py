"""Offline intake replay and a fail-closed 24-hour shadow promotion gate.

Run with ``python -m harness.homeostasis.rollout --help``. Promotion writes a
new profile, never replaces live state or attention policies.
"""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import UTC, datetime, timedelta
import hashlib
import json
import math
from pathlib import Path
import tempfile

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis.dynamics import DynamicsConfig
from harness.homeostasis.engine import HomeostasisEngine
from harness.homeostasis.models import HomeostaticSnapshot, initial_snapshot
from harness.homeostasis.profiles import load_profile


async def replay(timeline, profile):
    data = json.loads(Path(timeline).read_text(encoding="utf-8"))
    events = sorted(data["events"], key=lambda row: row["at"])
    moments = [(datetime.fromisoformat(row["at"]), row) for row in events]
    moments.extend((datetime.fromisoformat(at), None) for at in data.get("heartbeat_times", []))
    moments.sort(key=lambda item: item[0])
    clock = [datetime.fromisoformat(data["initial_at"]) if data.get("initial_at") else moments[0][0]]
    profile = profile.model_copy(update={"cognitive_dynamics": DynamicsConfig(mode="active")})
    with tempfile.TemporaryDirectory(prefix="nc-attention-replay-") as directory:
        if data.get("initial_values"):
            seed = initial_snapshot(profile, now=clock[0])
            seed = seed.model_copy(update={"dimensions": {key: factor.model_copy(update={
                "value": data["initial_values"][key], "precision": 0}) for key,factor in seed.dimensions.items()}})
            Path(directory, "snapshot.json").write_text(seed.model_dump_json(), encoding="utf-8")
        engine = HomeostasisEngine(profile, state_dir=directory, clock=lambda: clock[0])
        initial = {k: v.value for k,v in engine.snapshot.dimensions.items()}
        watchdogs, peak_load = [], initial["cognitive_load"]
        for index, (at, row) in enumerate(moments, 1):
            clock[0] = at
            payload = {} if row is None else {"qq_fact": {"fact_id": row["fact_id"],
                "group_id":row["group_id"], "kind":"verified_message", "occurred_at":at.isoformat(),
                "verification":"qq_protocol_verified", "evidence_refs":["timeline:"+row["fact_id"]]}}
            command = DomainCommandEnvelope(command_type="ApplyHeartbeat" if row is None else "ApplyEvent",
                event_id=f"replay-{index}", receive_seq=index, correlation_id="attention-replay",
                runtime_instance_id="offline-replay", manifest_revision=1, idempotency_key=f"replay-{index}",
                expected_revision=engine.snapshot.revision, issued_at=at, deadline=at+timedelta(seconds=60),
                payload={"event":{"event_type":"heartbeat" if row is None else "qq_fact",
                    "source_time":at.isoformat(), "payload":payload}})
            result = await (engine.apply_heartbeat(command) if row is None else engine.apply_event(command))
            if result.status != "applied":
                raise RuntimeError(result.error)
            watchdogs.extend(engine.snapshot.watchdog)
            peak_load = max(peak_load, engine.snapshot.dimensions["cognitive_load"].value)
        final = {k:v.value for k,v in engine.snapshot.dimensions.items()}
        report = {"schema_version":"cognitive-dynamics-replay-v1", "passed":not watchdogs,
            "timeline_sha256":hashlib.sha256(Path(timeline).read_bytes()).hexdigest(),
            "messages":len(events), "groups":dict(Counter(r["group_id"] for r in events)),
            "heartbeats":len(moments)-len(events), "watchdog_events":watchdogs, "peak_load":peak_load,
            "initial_values":initial, "final_values":final, "actual_processing_receipts":0,
            "initialization":"recorded_seven_values_unknown_latents" if data.get("initial_values") else "profile_default",
            "scope":"Raw intake regression; the historical archive has no processing receipts. No work or semantic appraisals are fabricated.",
            "finished_at":datetime.now(UTC).isoformat()}
        await engine.close()
        return report


def shadow_report(snapshot, *, now=None):
    now = now or datetime.now(UTC)
    snapshot = HomeostaticSnapshot.model_validate(snapshot)
    dynamics = snapshot.dynamics or {}
    observation = dynamics.get("shadow_observation", {})
    coverage = dynamics.get("assessment_coverage", {})
    last = observation.get("last_sample_at")
    age = (now-datetime.fromisoformat(last)).total_seconds() if last else None
    reasons = []
    if dynamics.get("mode") != "shadow":
        reasons.append("not_shadow_mode")
    duration = observation.get("continuous_seconds", 0)
    if not isinstance(duration, (int,float)) or not math.isfinite(duration) or duration < 86400:
        reasons.append("continuous_shadow_observation_less_than_24_hours")
    if age is None or not 0 <= age <= 30:
        reasons.append("shadow_sampler_not_current")
    if coverage.get("exposures", 0) < 1:
        reasons.append("no_actual_processing_receipts")
    if coverage.get("assessed", 0)+coverage.get("unassessed", 0) != coverage.get("exposures", 0):
        reasons.append("inconsistent_assessment_coverage")
    last_work = coverage.get("last_work_at")
    work_age = (now-datetime.fromisoformat(last_work)).total_seconds() if last_work else None
    if coverage.get("work_units", 0) <= 0 or work_age is None or not 0 <= work_age <= 86400:
        reasons.append("no_measured_work_within_shadow_window")
    candidates = dynamics.get("shadow_values") or {}
    required = {"cognitive_load", "novelty", "safety_predictability", "competence", "autonomy", "social_connection"}
    if set(candidates) != required or any(not isinstance(v, (int,float)) or not math.isfinite(v) or not 0 <= v <= 1
                                         for v in candidates.values()):
        reasons.append("invalid_shadow_dimensions")
    for name in ("fatigue", "boredom", "latent_confidence"):
        value = dynamics.get(name)
        if not isinstance(value, (int,float)) or not math.isfinite(value) or not 0 <= value <= 1:
            reasons.append("invalid_shadow_"+name)
    return {"schema_version":"cognitive-dynamics-shadow-gate-v1", "ready":not reasons,
        "reasons":reasons, "observation":observation, "assessment_coverage":coverage,
        "snapshot_revision":snapshot.revision, "profile_ref":f"{snapshot.profile_id}@{snapshot.profile_revision}",
        "checked_at":now.isoformat()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    playback = sub.add_parser("replay")
    playback.add_argument("--timeline", required=True)
    playback.add_argument("--profile", default="presets/homeostasis/qq-social-drive-v1.json")
    playback.add_argument("--output", required=True)
    stage = sub.add_parser("stage-shadow")
    stage.add_argument("--profile", required=True)
    stage.add_argument("--output", required=True)
    gate = sub.add_parser("check-shadow")
    gate.add_argument("--snapshot", required=True)
    gate.add_argument("--output", required=True)
    promote = sub.add_parser("promote")
    promote.add_argument("--snapshot", required=True)
    promote.add_argument("--profile", required=True)
    promote.add_argument("--replay-report", required=True)
    promote.add_argument("--direct-config", required=True, help="current Direct QQ configuration")
    promote.add_argument("--direct-output", required=True, help="new Direct QQ configuration with fair intake enabled")
    promote.add_argument("--output", required=True)
    args = parser.parse_args()
    if args.command == "replay":
        result = asyncio.run(replay(args.timeline, load_profile(args.profile)))
    elif args.command == "check-shadow":
        result = shadow_report(json.loads(Path(args.snapshot).read_text(encoding="utf-8")))
    else:
        profile = load_profile(args.profile)
        if args.command == "promote":
            result = shadow_report(json.loads(Path(args.snapshot).read_text(encoding="utf-8")))
            offline = json.loads(Path(args.replay_report).read_text(encoding="utf-8"))
            if (not result["ready"] or not offline.get("passed") or offline.get("messages") != 224
                    or offline.get("groups", {}).get("1060979317") != 207 or offline.get("watchdog_events") != []
                    or offline.get("schema_version") != "cognitive-dynamics-replay-v1"):
                raise SystemExit(json.dumps({"promotion":"blocked", "shadow":result, "offline":offline.get("passed")}))
            if result["profile_ref"] != profile.profile_ref or profile.cognitive_dynamics.mode != "shadow":
                raise SystemExit("snapshot/profile binding mismatch")
            from harness.qq.direct_config import DirectQQSettings
            direct = DirectQQSettings.read(Path(args.direct_config)).model_dump(mode="json")
            direct["attention_budget_enabled"] = True
            direct_output = Path(args.direct_output)
            if direct_output.exists() or direct_output.resolve() == Path(args.output).resolve():
                raise SystemExit("direct output must be a distinct new file")
        result = profile.model_dump(mode="json")
        result["revision"] += 1
        result["cognitive_dynamics"]["mode"] = "shadow" if args.command == "stage-shadow" else "active"
        if args.command == "stage-shadow":
            # Legacy scalar calculations remain the control, but their boundary
            # counter must not pause Runtime and stop representative traffic.
            # Numeric validity, persistence and service watchdogs still apply.
            result["watchdog_boundary_heartbeats"] = None
    output = Path(args.output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists() and args.command in {"stage-shadow", "promote"}:
        raise SystemExit("output already exists; choose a new revision file")
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    if args.command == "promote":
        direct_output.parent.mkdir(parents=True, exist_ok=True)
        direct_output.write_text(json.dumps(direct, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps({"output":str(output.resolve()), "ready":result.get("ready"), "passed":result.get("passed")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
