"""Isolated production scheduling/control measurement with synthetic slow models.

Uses the real RuntimeSupervisor, ProductionRuntime.run, cognitive adapter,
durable journal, and authenticated ASGI control routes. No live model, QQ,
service socket, or production state is used. Five-second heartbeat p99 needs
at least 100 samples; short runs are explicitly smoke tests.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import math
from pathlib import Path
import platform
import statistics
import tempfile
import threading
import time
from types import SimpleNamespace

import httpx

from harness.config import Settings
from harness.core.models import PersonaCore, PersonaIdentity
from harness.memory.service import build_inmemory_service
from harness.pipeline import InferencePipeline
from harness.production.config import ProductionConfig
from harness.research.manifest import ExperimentManifest
from harness.runtime import PassiveHomeostasisAdapter, PipelineCognitiveAdapter, RuntimeSupervisor
from harness.runtime.control import ControlPlane, create_control_app
from harness.runtime.models import RuntimeEventType, RuntimeLifecycle, utc_now
from harness.runtime.scheduler import RealTimeScheduler


def distribution(values):
    if not values:
        return {"count": 0, "p50": None, "p95": None, "p99": None, "max": None}
    ordered = sorted(values)
    def percentile(fraction):
        return round(ordered[max(0, math.ceil(len(ordered) * fraction) - 1)], 3)
    return {"count": len(values), "p50": round(statistics.median(values), 3),
        "p95": percentile(.95), "p99": percentile(.99), "max": round(max(values), 3)}


class SlowModel:
    def __init__(self, delay):
        self.delay = delay
        self.lock = threading.Lock()
        self.pending = set()
        self.started = []
        self.maximum_active = 0
        self.calls_by_event = {}

    @property
    def active(self):
        with self.lock:
            return len(self.pending)

    def release_pending(self):
        with self.lock:
            for event in self.pending:
                event.set()

    def complete_chat_result(self, messages, **kwargs):
        release = threading.Event()
        event_id = json.loads(messages[-1]["content"]).get("current_event", {}).get("event_id", "unbound")
        with self.lock:
            self.pending.add(release)
            self.started.append(time.perf_counter())
            self.maximum_active = max(self.maximum_active, len(self.pending))
            self.calls_by_event[event_id] = self.calls_by_event.get(event_id, 0) + 1
        try:
            release.wait(self.delay)
            return SimpleNamespace(text='{"kind":"wait","reason":"synthetic benchmark result"}')
        finally:
            with self.lock:
                self.pending.discard(release)


class MeasuredScheduler(RealTimeScheduler):
    def make_due_event(self):
        due = self.next_deadline()
        event = super().make_due_event()
        if event is not None:
            event["payload"]["benchmark_due_monotonic"] = due
        return event


class SyntheticBody:
    def __init__(self):
        self.sequence = 0

    async def snapshot(self):
        self.sequence += 1
        return {"body_instance_id": "benchmark-body", "episode_id": "benchmark-episode",
            "physiology_mode": "production_v1", "sample_sequence": self.sequence,
            "captured_at": utc_now().isoformat(), "resting": False, "exhausted": False,
            "energy": {"terminal": False}}


def manifest(heartbeat_seconds):
    return ExperimentManifest.model_validate({
        "experiment_id": "production-runtime-benchmark", "revision": 1, "control_mode": "development_debug",
        "condition": "llm_top3", "persona_version": "synthetic-v1", "homeostatic_profile": "passive-v1",
        "heartbeat_seconds": max(1, math.ceil(heartbeat_seconds)), "policy_horizon": 3, "random_seed": 1,
        "initial_snapshot_ref": "synthetic://empty", "model_version": "synthetic-slow-model",
        "prompt_version": "embodied-production-v1", "schema_version": "state-v1", "a_version": "A-v1",
        "b_version": "B-v1", "c_version": "C-v1", "precision_version": "precision-v1",
        "coupling_version": "coupling-v1", "priority_policy": "runtime-v1", "environment": "isolated-benchmark",
        "retention": "synthetic-only", "watchdog_action": "pause_preserve_state",
        "production": ProductionConfig(autonomous_enabled=False).model_dump(mode="json")})


def request_timeout_budget(model_delay, *, queued_load_target=8, foreground_slots=2):
    # This production wait choice makes one model call per event. Include the
    # entire fixed backlog even if the two admitted jobs compute serially.
    serial_calls = queued_load_target + foreground_slots
    return {"seconds": max(60, serial_calls * model_delay + 30),
        "queued_load_target": queued_load_target, "foreground_slots": foreground_slots,
        "model_calls_per_event": 1, "serial_model_calls_allowed": serial_calls,
        "fixed_margin_seconds": 30, "includes_queue_wait": True}


async def wait_until(predicate, *, timeout, failure, abort=None):
    async with asyncio.timeout(timeout):
        while not predicate():
            if abort is not None and (reason := abort()):
                raise RuntimeError(reason)
            if failure is not None and failure.done():
                await failure
                raise RuntimeError("benchmark_runtime_stopped")
            await asyncio.sleep(.001)


async def benchmark(state_dir, *, heartbeats=120, heartbeat_seconds=5, model_delay=30, pause_trials=3):
    if heartbeats < 1 or heartbeat_seconds <= 0 or model_delay <= 0 or pause_trials < 1:
        raise ValueError("benchmark_arguments_invalid")
    state_dir = Path(state_dir)
    state_dir.mkdir(parents=True, exist_ok=False)
    model = SlowModel(model_delay)
    settings = Settings(_env_file=None, parser_llm_enabled=False, planner_llm_enabled=False,
        tier_llm_enabled=False, validator_sample_rate=0, continuation_enabled=False, call_log_enabled=False,
        cognitive_incremental_persistence_enabled=True, emotion_mode="off")
    memory = build_inmemory_service(settings=settings, llm=model)
    memory.bootstrap_persona(PersonaCore(persona_id="benchmark-persona", created_at=utc_now(),
        identity=PersonaIdentity(name="Synthetic benchmark")))
    cognitive = PipelineCognitiveAdapter(InferencePipeline(memory, settings=settings, llm=model),
        state_path=state_dir / "cognitive/state.json")
    scheduler = MeasuredScheduler(heartbeat_seconds)
    timeout_budget = request_timeout_budget(model_delay)
    supervisor = RuntimeSupervisor(manifest=manifest(heartbeat_seconds), cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=state_dir / "runtime", scheduler=scheduler,
        command_timeout_seconds=timeout_budget["seconds"], maintenance_drain_seconds=max(5, model_delay + 2))
    production = supervisor.production_runtime
    production.attach_body_provider(SyntheticBody())
    cognitive.production_meter = SimpleNamespace(reserve_attempt=production.store.reserve_attempt,
        record_usage=production.store.record_usage, context=production.context_snapshot)
    events, heartbeat_lags, heartbeat_queue_lags, loaded_heartbeats = {}, [], [], []
    user_dispatches, failures, lifecycle_times = [], [], []
    heartbeat_collection = True

    async def observe(topic, value):
        now = time.perf_counter()
        if topic == "event.accepted":
            events[value["event_id"]] = {**value, "accepted_monotonic": now}
        elif topic == "event.processing":
            event = events.get(value["event_id"], {})
            if event.get("event_type") == RuntimeEventType.USER_MESSAGE.value:
                user_dispatches.append(now)
            if heartbeat_collection and event.get("event_type") == RuntimeEventType.HEARTBEAT.value:
                due = event["payload"].get("benchmark_due_monotonic")
                if due is not None:
                    lag = max(0, (time.monotonic() - due) * 1000)
                    heartbeat_lags.append(lag)
                    heartbeat_queue_lags.append(max(0, (now - event["accepted_monotonic"]) * 1000))
                    if model.active:
                        loaded_heartbeats.append(lag)
        elif topic == "event.completed" and (value.get("status") == "failed"
                or value.get("detail", {}).get("error_type")):
            failures.append({"event_type": events.get(value["event_id"], {}).get("event_type"),
                "error_type": value.get("detail", {}).get("error_type")})
        elif topic == "lifecycle":
            lifecycle_times.append((value["current"], now))

    supervisor.add_listener(observe)
    await supervisor.boot_and_recover()
    runner = asyncio.create_task(supervisor.run())
    running = True
    sequence = 0
    async def feed():
        nonlocal sequence
        while running:
            if supervisor.lifecycle == RuntimeLifecycle.RUNNING and supervisor.queue.qsize() < 8:
                sequence += 1
                await supervisor.submit_user_message("synthetic load", session_id="benchmark-" + str(sequence % 2))
            await asyncio.sleep(min(.02, heartbeat_seconds / 4))
    feeder = asyncio.create_task(feed())
    token = "synthetic-benchmark-control-token"
    app = create_control_app(ControlPlane(supervisor, token=token))
    pause_samples = []
    started = time.perf_counter()
    stage, benchmark_error, cleanup_error = "heartbeat_collection", None, None

    def require_running():
        if feeder.done():
            return "benchmark_feeder_stopped"
        if production._timer is not None and production._timer.done():
            return "benchmark_heartbeat_producer_stopped"
        if supervisor.lifecycle != RuntimeLifecycle.RUNNING:
            return "benchmark_unexpected_lifecycle:" + supervisor.lifecycle.value
        return None

    try:
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://benchmark.invalid",
                                    headers={"Authorization": "Bearer " + token}) as client:
            await wait_until(lambda: len(heartbeat_lags) >= heartbeats,
                timeout=heartbeats * heartbeat_seconds + max(30, model_delay), failure=runner,
                abort=require_running)
            heartbeat_collection = False
            for trial in range(pause_trials):
                stage = "pause_trial:" + str(trial + 1)
                await wait_until(lambda: model.active >= 1, timeout=model_delay + 10, failure=runner,
                    abort=require_running)
                before = time.perf_counter()
                response = await client.post("/v1/pause", json={"reason": "synthetic latency measurement"})
                response.raise_for_status()
                ack = time.perf_counter()
                await wait_until(lambda: any(state == RuntimeLifecycle.PAUSING.value and stamp >= before
                    for state, stamp in lifecycle_times), timeout=10, failure=runner)
                blocked = next(stamp for state, stamp in lifecycle_times
                    if state == RuntimeLifecycle.PAUSING.value and stamp >= before)
                await supervisor.submit_user_message("paused synthetic canary", session_id="benchmark-canary")
                await asyncio.sleep(.2)
                observed = time.perf_counter()
                new_calls = sum(blocked < stamp <= observed for stamp in model.started)
                new_jobs = sum(blocked < stamp <= observed for stamp in user_dispatches)
                # Measure admission blocking before releasing simulated inflight
                # calls; drain completion is a separate, explicitly assisted value.
                model.release_pending()
                await wait_until(lambda: supervisor.lifecycle in {RuntimeLifecycle.PAUSED, RuntimeLifecycle.FAULTED},
                    timeout=max(10, model_delay + 5), failure=runner)
                pause_samples.append({"trial": trial + 1, "request_to_block_ms": (blocked - before) * 1000,
                    "request_to_http_ack_ms": (ack - before) * 1000,
                    "request_to_drain_ms": (time.perf_counter() - before) * 1000,
                    "new_model_calls_after_block": new_calls, "new_user_jobs_after_block": new_jobs,
                    "paused_observation_seconds": .2, "terminal_state": supervisor.lifecycle.value})
                if supervisor.lifecycle != RuntimeLifecycle.PAUSED:
                    break
                if trial + 1 < pause_trials:
                    resumed = await client.post("/v1/resume", json={"reason": "next synthetic trial"})
                    resumed.raise_for_status()
                    await wait_until(lambda: supervisor.lifecycle == RuntimeLifecycle.RUNNING,
                        timeout=max(10, model_delay + 5), failure=runner)
        stage = "completed"
    except Exception as exc:
        benchmark_error = {"stage": stage, "error_type": type(exc).__name__,
            "reason": str(exc) if str(exc).startswith("benchmark_") else "benchmark_stage_failed",
            "lifecycle": supervisor.lifecycle.value}
    finally:
        running = False
        feeder.cancel()
        await asyncio.gather(feeder, return_exceptions=True)
        model.release_pending()
        try:
            async with asyncio.timeout(max(15, model_delay + 10)):
                if supervisor.lifecycle != RuntimeLifecycle.STOPPED:
                    await supervisor.stop(reason="isolated benchmark cleanup")
                await asyncio.wait_for(runner, 10)
        except (TimeoutError, RuntimeError) as exc:
            cleanup_error = type(exc).__name__
            # This process owns only its new synthetic state. Closing admission
            # releases a waiting runner without touching any external service.
            await supervisor.queue.close()
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        if supervisor.lifecycle == RuntimeLifecycle.STOPPED:
            supervisor.recovery.close()
    heartbeat = distribution(heartbeat_lags)
    pauses = distribution([sample["request_to_block_ms"] for sample in pause_samples])
    complete = len(heartbeat_lags) >= heartbeats and len(pause_samples) == pause_trials
    max_event_calls = max(model.calls_by_event.values(), default=0)
    model_contract_met = max_event_calls <= timeout_budget["model_calls_per_event"] and "unbound" not in model.calls_by_event
    blocked_safely = len(pause_samples) == pause_trials and all(
        not sample["new_model_calls_after_block"] and not sample["new_user_jobs_after_block"]
        and sample["terminal_state"] == RuntimeLifecycle.PAUSED.value for sample in pause_samples)
    return {"schema_version": "production-runtime-benchmark-v1", "synthetic_only": True,
            "scope": "ProductionRuntime.run + RuntimeSupervisor + PipelineCognitiveAdapter + ASGI control",
            "platform": platform.platform(), "python": platform.python_version(),
            "elapsed_seconds": round(time.perf_counter() - started, 3), "heartbeat_seconds": heartbeat_seconds,
            "simulated_model_delay_seconds": model_delay, "model_calls": len(model.started),
            "maximum_active_model_calls": model.maximum_active, "queued_load_target": 8,
            "request_timeout_budget": timeout_budget, "observed_max_model_calls_per_event": max_event_calls,
            "model_workflow_contract_met": model_contract_met,
            "measurement_completed": complete, "benchmark_error": benchmark_error, "cleanup_error": cleanup_error,
            "requested_heartbeats": heartbeats, "requested_pause_trials": pause_trials,
            "heartbeat_deadline_to_dispatch_ms": heartbeat,
            "heartbeat_ingress_to_dispatch_ms": distribution(heartbeat_queue_lags),
            "heartbeat_under_active_model_ms": distribution(loaded_heartbeats),
            "pause_request_to_block_ms": pauses, "pause_trials": pause_samples,
            "admission_stayed_blocked": blocked_safely, "failed_events": failures,
            "target_met": complete and model_contract_met and not benchmark_error and not cleanup_error
                and blocked_safely and not failures and pauses["max"] <= 500 and heartbeat["p99"] <= 250,
            "linux_acceptance_sample_eligible": complete and not benchmark_error and not cleanup_error
                and platform.system() == "Linux" and heartbeat_seconds == 5
                and len(loaded_heartbeats) >= 100 and model_delay > heartbeat_seconds,
            "drain_measurement": "simulated inflight calls explicitly released after paused observation; not natural provider drain latency",
            "excludes": ["real LLM", "four-process RPC", "QQ send", "real MistyAI", "production memory size", "cold OS cache"]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--heartbeats", type=int, default=120)
    parser.add_argument("--heartbeat-seconds", type=float, default=5)
    parser.add_argument("--model-delay", type=float, default=30)
    parser.add_argument("--pause-trials", type=int, default=3)
    args = parser.parse_args()
    if args.output.exists():
        parser.error("output already exists")
    with tempfile.TemporaryDirectory(prefix="nc-production-runtime-benchmark-") as temporary:
        result = asyncio.run(benchmark(args.state_dir or Path(temporary) / "state", heartbeats=args.heartbeats,
            heartbeat_seconds=args.heartbeat_seconds, model_delay=args.model_delay, pause_trials=args.pause_trials))
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write("\n")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["target_met"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
