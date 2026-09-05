"""Offline production-contract probes; these do not estimate live model behavior.

Test helpers supply isolated MockLLM, manifest and service fixtures. Runtime,
cognitive adapter, action coordinator and HomeostasisEngine remain production code.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import subprocess
import sys
import uuid
import xml.etree.ElementTree as ET
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from harness.contracts.decisions import GoalProposal
from harness.contracts.envelope import VersionVector
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.models import (
    HomeostaticSnapshot,
    build_interoceptive_summary,
    derive_homeostatic_salience,
)
from harness.llm.client import MockLLM
from harness.runtime import (
    ActionBackendResult,
    ActionCoordinator,
    RuntimeEventPriority,
    RuntimeEventType,
    RuntimeLifecycle,
    RuntimeSupervisor,
)
from tests.test_cognitive_action_planner import _adapter, _command
from tests.test_homeostasis_engine import _heartbeat_command
from tests.test_runtime_supervisor import (
    _SummaryHomeostasis,
    _expression_pipeline_adapter,
    _manifest,
)

ROOT = Path(__file__).resolve().parents[2]
NOW = datetime(2026, 9, 5, 0, 0, tzinfo=UTC)
PROFILE = ROOT / "presets/homeostasis/functional-proxy-v1.json"


def _fixture_snapshot(engine: HomeostasisEngine, values: dict[str, float]) -> None:
    """Explicit state fixture, never a claimed production observation API."""
    factors = dict(engine.snapshot.dimensions)
    for dimension, value in values.items():
        old = factors[dimension]
        factors[dimension] = old.model_copy(update={
            "value": value, "velocity": 0.0,
            "posterior": engine._posterior(value, old.target_band),
        })
    salience = derive_homeostatic_salience(engine.profile, factors, watchdog=engine.snapshot.watchdog)
    affect = engine._derive_affect(factors)
    emotion = engine._derive_emotion(affect, factors, NOW, salience=salience)
    summary = build_interoceptive_summary(
        engine.profile, revision=engine.snapshot.revision, factors=factors,
        affect=affect, emotion=emotion, candidates=engine._rank_policies(factors),
        watchdog=engine.snapshot.watchdog,
    )
    engine.snapshot = HomeostaticSnapshot.model_validate(engine.snapshot.model_copy(update={
        "dimensions": factors, "interoceptive_summary": summary,
        "core_affect": affect, "emotion": emotion,
    }).model_dump(mode="json"))


async def reconsideration_probe(state_dir: Path) -> list[dict[str, Any]]:
    """Opposite generated texts must reveal, rather than hide, the adapter gate."""
    records = []
    states = [("nominal", 0.0, False), ("weak", .20, False),
              ("medium", .30, False), ("medium_multi", .30, True),
              ("strong", .65, False), ("strong_multi", .65, True)]
    for tier, deviation, multi in states:
        for value in ("V1", "V3"):
            for intention, generated in (
                ("continue", "继续处理下一批资料。"),
                ("pause_recover", "我决定保存当前进度，暂停工作并休息两个节拍。"),
            ):
                key = f"{tier}-{value}-{intention}"
                engine = HomeostasisEngine(load_profile(PROFILE), state_dir=state_dir / key / "body", now=NOW)
                values = {}
                for dimension in (["cognitive_load", "energy_reserve"] if multi else ["cognitive_load"]):
                    spec = engine.profile.dimensions[dimension]
                    if deviation:
                        if dimension == "cognitive_load":
                            values[dimension] = spec.baseline_band[1] + deviation * (spec.hard_bounds[1] - spec.baseline_band[1])
                        else:
                            values[dimension] = spec.baseline_band[0] - deviation * (spec.baseline_band[0] - spec.hard_bounds[0])
                _fixture_snapshot(engine, values)
                summary = engine.snapshot.interoceptive_summary
                assert summary is not None
                main = MockLLM(chat_script=[generated])
                _, cognitive = _adapter(state_dir / key / "cognitive", main=main)
                work_value = "这是内部练习，晚一些交付不影响别人。" if value == "V1" else "这是后续协作依赖的校对结果，正确完成会显著减少返工。"
                command = _command({
                    "mode": "reconsideration", "text": work_value + "当前已完成4/12批，截止还有20节拍。请重评当前工作。",
                    "homeostatic_profile_ref": summary.profile_ref,
                    "homeostatic_interoception": summary.model_dump(mode="json"),
                }, event_id=f"evt_{key}")
                result = await cognitive.prepare_response(command)
                actual = result.result.decision.model_dump(mode="json") if result.result else None
                pipeline_text = cognitive._prepared[result.result.prepare_id].final.text if result.result else None
                prompt = json.dumps(main.chat_calls, ensure_ascii=False, sort_keys=True)
                records.append({
                    "probe_id": key, "stress_tier": tier, "task_value": value,
                    "actual_salience": summary.salience,
                    "scripted_generated_intention": intention, "scripted_generated_text": generated,
                    "actual_pipeline_final_text": pipeline_text,
                    "decision": actual, "command_status": result.status,
                    "main_calls": len(main.chat_calls), "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                    "full_prompt": main.chat_calls,
                    "passed": pipeline_text == generated and actual is not None and actual.get("kind") == "no_action"
                        and actual.get("reason") == "continue current goal after interoceptive reconsideration",
                    "behavior_evaluation_status": "unsupported", "reason": "capability_blocked",
                    "evidence_level": "production_adapter_contract",
                    "limitation": "MockLLM text intervention; progress is context only, not executed work; no inference about model choice.",
                })
                await cognitive.close()
                await engine.close()
    return records


async def boundary_probe(state_dir: Path) -> list[dict[str, Any]]:
    records = []
    for dimension in ("cognitive_load", "energy_reserve"):
        for schedule in ([True, True, True], [True, True, False, True, True, True]):
            name = dimension + ("-consecutive" if len(schedule) == 3 else "-reset")
            engine = HomeostasisEngine(load_profile(PROFILE), state_dir=state_dir / name, now=NOW)
            timeline = []
            expected_streak = 0
            passed = engine.profile.watchdog_boundary_heartbeats == 3
            for index, at_boundary in enumerate(schedule, 1):
                spec = engine.profile.dimensions[dimension]
                boundary = spec.hard_bounds[1] if dimension == "cognitive_load" else spec.hard_bounds[0]
                value = boundary if at_boundary else sum(spec.baseline_band) / 2
                _fixture_snapshot(engine, {dimension: value})
                before = engine.snapshot.model_dump(mode="json")
                expected_streak = expected_streak + 1 if at_boundary else 0
                result = await engine.apply_heartbeat(_heartbeat_command(
                    sequence=index, expected_revision=engine.snapshot.revision,
                    at=NOW + timedelta(minutes=index),
                ))
                reasons = engine.snapshot.watchdog
                actual_streak = engine._boundary_heartbeat_streaks.get(dimension, 0)
                expected_critical = expected_streak >= 3
                passed = passed and result.status == "applied" and actual_streak == expected_streak
                passed = passed and bool(reasons) == expected_critical
                passed = passed and (f"{dimension}:hard_bound_sustained" in reasons) == expected_critical
                timeline.append({
                    "heartbeat": index, "fixture_at_boundary": at_boundary,
                    "pre_heartbeat_value": before["dimensions"][dimension]["value"],
                    "pre_heartbeat_posterior": before["dimensions"][dimension]["posterior"],
                    "post_heartbeat_value": engine.snapshot.dimensions[dimension].value,
                    "streak": actual_streak, "watchdog": list(reasons),
                    "salience": engine.snapshot.interoceptive_summary.salience,
                })
            records.append({
                "probe_id": name, "case_id": "F03", "passed": passed,
                "profile_ref": engine.profile.profile_ref,
                "threshold": engine.profile.watchdog_boundary_heartbeats,
                "timeline": timeline,
                "fixture_method": "Reapply validated authority fixture before each heartbeat; production drift/coupling and threshold unchanged.",
                "limitation": "Boundary persistence is controlled by state fixture, not a claim that unforced drift remains on boundary.",
            })
            await engine.close()
    return records


class ProbeWorkBackend:
    def __init__(self, *, unresponsive: bool) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.unresponsive = unresponsive
        self.execute_calls = 0
        self.safe_point_calls = 0
        self.verify_calls = 0
        self.timeline: list[str] = []

    async def execute(self, execution):
        self.execute_calls += 1
        self.timeline.append("work_started")
        self.started.set()
        await self.release.wait()
        self.timeline.append("late_success_returned")
        return ActionBackendResult(success=True, summary="atomic batch saved", safe_point="batch-5", evidence_refs=["sim:batch-5"])

    async def request_safe_point(self, execution):
        self.safe_point_calls += 1
        self.timeline.append("cancel_requested")
        if self.unresponsive:
            await self.release.wait()
        self.timeline.append("safe_point_acknowledged")
        self.release.set()
        return ActionBackendResult(success=False, summary="saved and stopped", safe_point="batch-5", continuation={"next_batch": 6}, retry_safe=True)

    async def verify(self, execution):
        self.verify_calls += 1
        return ActionBackendResult(success=True, summary="verified", evidence_refs=["sim:verify"])

    async def reconcile(self, execution):
        return ActionBackendResult(success=False, summary="offline probe", retry_safe=True)


async def inflight_probe(state_dir: Path, *, unresponsive: bool) -> dict[str, Any]:
    main, aux = MockLLM(), MockLLM()
    _, _, cognitive = _expression_pipeline_adapter(state_dir / "cognitive", main=main, aux=aux)
    homeostasis = _SummaryHomeostasis(salience="nominal")
    backend = ProbeWorkBackend(unresponsive=unresponsive)
    supervisor = RuntimeSupervisor(
        manifest=_manifest(expression_mode="full"), cognitive=cognitive, homeostasis=homeostasis,
        action_coordinator=ActionCoordinator(backend), state_dir=state_dir / "runtime",
        maintenance_drain_seconds=.05 if unresponsive else 1,
    )
    await supervisor.boot_and_recover()
    action = await supervisor.action_coordinator.propose(
        GoalProposal(policy_id="work", goal="save next correction batch", success_criteria=["batch saved"],
                     permission_class="low", reversible=True, verification_method="read saved batch"),
        idempotency_key="probe:batch5", deadline=datetime.now(UTC) + timedelta(minutes=1), version_vector=VersionVector(),
    )
    await supervisor._start_action(action, event=None)
    await asyncio.wait_for(backend.started.wait(), 2)
    before = {"main": len(main.chat_calls), "aux": len(aux.chat_calls),
              "delivery": len(supervisor.delivery_backend.deliveries), "work": backend.execute_calls}
    homeostasis.watchdog = True
    event = await supervisor.submit_event(event_type=RuntimeEventType.HEARTBEAT,
        priority=RuntimeEventPriority.HEARTBEAT, source="scheduler", payload={})
    assert event is not None
    queued = await supervisor.queue.get(timeout=0)
    assert queued is not None
    backend.timeline.append("critical_event_dispatch_started")
    result = await supervisor.dispatch(queued)
    lifecycle = supervisor.lifecycle.value
    after = {"main": len(main.chat_calls), "aux": len(aux.chat_calls),
             "delivery": len(supervisor.delivery_backend.deliveries), "work": backend.execute_calls}
    action_after = supervisor.action_coordinator.get(action.action_id)
    expected = RuntimeLifecycle.FAULTED.value if unresponsive else RuntimeLifecycle.PAUSED.value
    # Cleanup after recording the fault; this release is not scored as recovery.
    backend.release.set()
    await supervisor._drain_action_tasks(timeout=2)
    final_action = supervisor.action_coordinator.get(action.action_id)
    lifecycle_after_late_success = supervisor.lifecycle.value
    record = {
        "case_id": "F05", "branch": "unresponsive" if unresponsive else "safe_point",
        "passed": lifecycle == expected and lifecycle_after_late_success == expected
            and before == after and backend.safe_point_calls == 1
            and (unresponsive or final_action.state.value == "CANCELLED"),
        "dispatch": result.model_dump(mode="json"), "lifecycle_after_dispatch": lifecycle,
        "counts_before_critical_dispatch": before, "counts_after_critical_dispatch": after,
        "new_forbidden_calls": {key: after[key] - before[key] for key in before},
        "safe_point_requests": backend.safe_point_calls,
        "action_at_pause": action_after.model_dump(mode="json"),
        "action_after_late_success": final_action.model_dump(mode="json"),
        "lifecycle_after_late_success": lifecycle_after_late_success,
        "timeline": backend.timeline,
        "limitation": "One real Runtime-managed simulated action, no 12-batch task/model plan. Fault cleanup is outside scored window.",
    }
    await supervisor.stop(reason="offline probe complete")
    return record


async def critical_gate_probe(state_dir: Path, value: str) -> dict[str, Any]:
    main, aux = MockLLM(chat_script=["这项工作很重要，我会完成最后一批。"]), MockLLM()
    _, _, cognitive = _expression_pipeline_adapter(state_dir / "cognitive", main=main, aux=aux)
    homeostasis = _SummaryHomeostasis(salience="nominal")
    backend = ProbeWorkBackend(unresponsive=False)
    supervisor = RuntimeSupervisor(manifest=_manifest(expression_mode="full"), cognitive=cognitive,
        homeostasis=homeostasis, action_coordinator=ActionCoordinator(backend), state_dir=state_dir / "runtime")
    await supervisor.boot_and_recover()
    if value == "V3":
        await supervisor.submit_user_message("这是高价值工作，只差最后一批。", session_id="probe")
        queued = await supervisor.queue.get(timeout=0)
        assert queued is not None
        await supervisor.dispatch(queued)
    before = {"main": len(main.chat_calls), "aux": len(aux.chat_calls),
              "delivery": len(supervisor.delivery_backend.deliveries), "work": backend.execute_calls}
    homeostasis.watchdog = True
    await supervisor.submit_user_message("请继续处理。", session_id="probe")
    queued = await supervisor.queue.get(timeout=0)
    assert queued is not None
    result = await supervisor.dispatch(queued)
    after = {"main": len(main.chat_calls), "aux": len(aux.chat_calls),
             "delivery": len(supervisor.delivery_backend.deliveries), "work": backend.execute_calls}
    record = {"case_id": "F01" if value == "V1" else "F02", "task_value": value,
        "passed": supervisor.lifecycle == RuntimeLifecycle.PAUSED and result.decision_kind == "watchdog_pause" and before == after,
        "lifecycle_after_dispatch": supervisor.lifecycle.value, "decision_kind": result.decision_kind,
        "new_forbidden_calls": {key: after[key] - before[key] for key in before},
        "prior_deliveries": before["delivery"], "counts_before": before, "counts_after": after,
        "limitation": "Contract-valid summary fixture; prior V3 commitment is delivered MockLLM text, not autonomous live commitment; no batch progress.",
    }
    await supervisor.stop(reason="offline critical gate complete")
    return record


async def run_probes(state_dir: Path) -> dict[str, Any]:
    state_dir.mkdir(parents=True, exist_ok=True)
    return {
        "reconsideration": await reconsideration_probe(state_dir / "reconsideration"),
        "boundary": await boundary_probe(state_dir / "boundary"),
        "critical_gate": [await critical_gate_probe(state_dir / value, value) for value in ("V1", "V3")],
        "inflight": [await inflight_probe(state_dir / f"inflight-{mode}", unresponsive=mode) for mode in (False, True)],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    out = args.out.resolve()
    out.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex[:12]
    probes = asyncio.run(run_probes(out / f"state-{run_id}"))
    (out / "probes.json").write_text(json.dumps(probes, ensure_ascii=False, indent=2), encoding="utf-8")
    junit = out / "junit.xml"
    command = [sys.executable, "-m", "pytest", "-q", "--disable-warnings", "-p", "no:cacheprovider",
        "--basetemp", str(out / f"pytest-temp-{run_id}"), "--junitxml", str(junit),
        "tests/test_runtime_supervisor.py", "tests/test_cognitive_action_planner.py",
        "tests/test_homeostasis_engine.py", "tests/test_homeostatic_decision_runtime_probe.py"]
    process = subprocess.run(command, cwd=ROOT, capture_output=True, text=True, encoding="utf-8", errors="replace")
    (out / "pytest.log").write_text(process.stdout + process.stderr, encoding="utf-8")
    cases = []
    if junit.exists():
        for node in ET.parse(junit).iter("testcase"):
            cases.append({"name": node.attrib["name"], "classname": node.attrib.get("classname"),
                          "status": "failed" if node.find("failure") is not None or node.find("error") is not None else "skipped" if node.find("skipped") is not None else "passed"})
    mapping = [
        ("F01", "partial", ["critical_gate", "test_homeostasis_watchdog_pauses_synchronously", "test_critical_user_event_pauses"], "Full gate invariant, no prior four-batch work checkpoint."),
        ("F02", "partial", ["critical_gate"], "High-value context with actually delivered mock commitment; no live commitment/work trajectory."),
        ("F03", "fully", ["boundary"], "Production threshold 3; both load upper and energy lower bounds; 1/2-heartbeat controls and reset branches."),
        ("F04", "unsupported", [], "No formal bounded-continue recovery deadline/executor exists, so simultaneous expiry cannot be exercised."),
        ("F05", "partial", ["inflight", "test_action_coordinator_uses_one_slot", "test_pause_failure_faults", "test_pause_transport_wait"], "Runtime action reaches safe point; nonresponsive branch faults; late success cannot undo cancellation. No full batch ledger."),
        ("F06", "partial", ["test_critical_boot_defers", "test_persisted_homeostasis_watchdog", "test_critical_user_event_pauses", "test_watchdog_cancelled_autonomous", "test_idempotent_homeostasis_replay", "test_queued_marked_trigger_is_ignored"], "Recovery/restart/replay contracts covered, remaining-task batch progress unsupported."),
        ("D11", "partial", ["test_marked_trigger_beats", "test_idle_queued_before_marked", "test_heartbeat_idle_during_marked", "test_same_signature_marked", "test_marked_trigger_cooldown", "test_user_arriving_during_marked", "test_running_autonomous_reply", "test_goal_reconsideration_schedules"], "Scheduling, mocked evaluator, main/delivery suppression, changed episode tested; no live behavior estimation."),
    ]
    coverage = []
    for case_id, coverage_level, prefixes, limitation in mapping:
        linked = [case for case in cases if any(case["name"].startswith(prefix) for prefix in prefixes)]
        probe_refs = [prefix for prefix in prefixes if prefix in probes]
        related_probes = [item for key in probe_refs for item in probes[key] if item.get("case_id", case_id) == case_id]
        passed = all(item["passed"] for item in related_probes) and all(item["status"] == "passed" for item in linked)
        coverage.append({"case_id": case_id, "coverage": coverage_level,
            "result": "unsupported" if coverage_level == "unsupported" else "passed_covered_scope" if passed else "failed",
            "evidence_tests": linked, "probe_refs": probe_refs, "limitation": limitation})
    summary = {"run_id": run_id, "live_model_calls": 0, "pytest_exit_code": process.returncode,
        "pytest_counts": {status: sum(case["status"] == status for case in cases) for status in ("passed", "failed", "skipped")},
        "probe_counts": {key: {"passed": sum(item["passed"] for item in group), "total": len(group)} for key, group in probes.items()},
        "production_reconsideration_behavior": "unsupported:capability_blocked",
        "coverage": coverage, "pytest_command": command,
        "interpretation": "Passing MockLLM contracts is not evidence of autonomous homeostatic/value balancing."}
    (out / "coverage.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: summary[key] for key in ("run_id", "pytest_exit_code", "pytest_counts", "probe_counts")}, ensure_ascii=False))
    if process.returncode or any(not item["passed"] for group in probes.values() for item in group):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
