from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
import os
from pathlib import Path

import httpx
import pytest

from eval.basic_runtime.soak import (
    Config,
    DIMENSIONS,
    HttpObserver,
    ObservationError,
    RELEASE_SECONDS,
    SERVICES,
    atomic_summary,
    normalize,
    process_identity,
    run_soak,
)


START = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
PIDS = {name: number for number, name in enumerate(SERVICES, start=1)}


class FakeClock:
    def __init__(self, *, sleep_extra=0, wall_extra=0):
        self.elapsed = 0.0
        self.sleep_extra = sleep_extra
        self.wall_extra = wall_extra
        self.sleeps = 0

    def monotonic(self):
        return self.elapsed

    def wall(self):
        return START + timedelta(seconds=self.elapsed + self.wall_extra * self.sleeps)

    def sleep(self, seconds):
        self.elapsed += seconds + self.sleep_extra
        self.sleeps += 1


def fixture_payload(clock):
    stamp = clock.wall().isoformat()
    lifecycle_since = START.isoformat()
    health = {
        "lifecycle": "RUNNING", "journal_writable": True, "cognitive_healthy": True,
        "homeostasis_healthy": True, "llm_healthy": True, "body_healthy": True,
        "degraded_reasons": [], "last_heartbeat_at": stamp,
        "body_state_age_seconds": None, "body_episode_id": None, "body_revision": None,
    }
    runtime = {
        "runtime_instance_id": "runtime-live", "lifecycle": "RUNNING",
        "lifecycle_since": lifecycle_since, "health": health, "agency_state": "AVAILABLE",
        "embodiment": {"state": "disabled", "effective_capabilities": []},
    }
    state = {
        "watchdog": [], "last_heartbeat_at": stamp,
        "profile": {
            "profile_id": "stable", "revision": 1, "mode": "functional_proxy",
            "dimensions": {name: {"hard_bounds": [0, 1]} for name in DIMENSIONS},
        },
        "dimensions": {name: {
            "dimension": name, "value": 0.5, "velocity": 0, "precision": 0.8,
            "posterior": {"low": 0.1, "in_band": 0.8, "high": 0.1}, "target_band": [0.3, 0.7],
        } for name in DIMENSIONS},
    }
    return {
        "runtime": {
            "runtime": runtime,
            "manifest": {"heartbeat_seconds": 60, "embodiment": {"mode": "off", "body_state_max_age_seconds": 10}},
            "homeostasis": {"service": "homeostasis", "payload": state},
        },
        "cognitive": {"service": "cognitive", "healthy": True, "revision": 1},
        "homeostasis": {"service": "homeostasis", "healthy": True, "revision": clock.sleeps},
        "gui": {"available": True, "runtime": deepcopy(runtime)},
        "monitor_events": {"items": [], "has_more": False},
        "monitor_overview": {"monitor": {"healthy": True, "last_error": None, "ingest_dropped": 0}},
    }


class Observer:
    def __init__(self, clock, mutate=None):
        self.clock, self.mutate = clock, mutate

    def sample(self):
        payload = fixture_payload(self.clock)
        if self.mutate:
            self.mutate(payload, self.clock.sleeps)
        return payload


def run(tmp_path, clock=None, mutate=None, *, duration=60, **kwargs):
    clock = clock or FakeClock()
    return run_soak(
        Observer(clock, mutate), tmp_path / "run", clock=clock,
        config=Config(duration_seconds=duration), pids=PIDS,
        pid_probe=lambda pid: f"{pid}:creation", **kwargs,
    )


def test_short_smoke_writes_complete_artifacts_but_cannot_pass(tmp_path):
    report = run(tmp_path)
    assert report["verdict"] == "INCONCLUSIVE"
    assert not report["release_eligible"]
    assert report["samples"] == report["minimum_samples"] == 3
    assert report["completed"]
    assert report["elapsed_monotonic_seconds"] == 60
    assert report["failures"] == []
    disk = json.loads((tmp_path / "run" / "summary.json").read_text())
    assert disk == report
    samples = [json.loads(line) for line in (tmp_path / "run" / "samples.jsonl").read_text().splitlines()]
    assert len(samples) == 3
    assert samples[-1]["observation"]["dimensions"]["energy_reserve"] == 0.5


def test_eight_hours_on_an_injected_clock_cannot_forge_release_evidence(tmp_path):
    report = run(tmp_path, duration=RELEASE_SECONDS)
    assert report["completed"] and report["samples"] == 961
    assert report["verdict"] == "INCONCLUSIVE"
    assert not report["release_eligible"]
    assert report["clock_source"] == "injected_test_clock"
    assert report["full_milestone_verdict"] == "NOT_EVALUATED"


@pytest.mark.parametrize("kind", ["pause", "missing", "nonfinite", "bounds", "stale", "future", "posterior", "health"])
def test_unhealthy_or_missing_fields_fail_closed(tmp_path, kind):
    def mutate(payload, _):
        runtime = payload["runtime"]["runtime"]
        state = payload["runtime"]["homeostasis"]["payload"]
        if kind == "pause":
            runtime["lifecycle"] = "PAUSED"
        elif kind == "missing":
            del runtime["health"]["last_heartbeat_at"]
        elif kind == "nonfinite":
            state["dimensions"]["energy_reserve"]["value"] = float("nan")
        elif kind == "bounds":
            state["dimensions"]["cognitive_load"]["value"] = 1.1
        elif kind == "stale":
            runtime["health"]["last_heartbeat_at"] = (START - timedelta(seconds=151)).isoformat()
        elif kind == "future":
            state["last_heartbeat_at"] = (START + timedelta(seconds=6)).isoformat()
        elif kind == "posterior":
            state["dimensions"]["energy_reserve"]["posterior"]["low"] = 0.9
        elif kind == "health":
            del payload["cognitive"]["healthy"]

    result = run(tmp_path, mutate=mutate)
    assert result["verdict"] == "FAIL"
    assert result["failures"]
    assert result["samples"] == 1
    assert not result["release_eligible"]


@pytest.mark.parametrize("clock,code", [
    (FakeClock(sleep_extra=90), "sample_gap_or_clock_regression"),
    (FakeClock(sleep_extra=30), "missed_sample_deadline"),
    (FakeClock(wall_extra=6), "clock_drift_or_suspend"),
])
def test_suspend_or_clock_drift_does_not_count_as_healthy_uptime(tmp_path, clock, code):
    result = run(tmp_path, clock=clock, duration=120)
    assert result["verdict"] == "FAIL"
    assert code in [item["code"] for item in result["failures"]]


def test_persistent_boundary_is_rejected_but_one_sample_is_not(tmp_path):
    def stuck(payload, _):
        payload["runtime"]["homeostasis"]["payload"]["dimensions"]["novelty"]["value"] = 0.999

    result = run(tmp_path, mutate=stuck, duration=600)
    assert result["verdict"] == "FAIL"
    assert result["elapsed_monotonic_seconds"] == 300
    assert "persistent_hard_boundary:novelty:high" in [item["code"] for item in result["failures"]]

def test_single_boundary_sample_recovers(tmp_path):
    def transient(payload, number):
        if number == 0:
            payload["runtime"]["homeostasis"]["payload"]["dimensions"]["novelty"]["value"] = 0.999
    report = run(tmp_path, mutate=transient, duration=600)
    assert report["completed"] and not report["failures"]


def test_physically_measured_terminal_protection_is_not_ignored(tmp_path):
    def mutate(payload, _):
        runtime = payload["runtime"]["runtime"]
        runtime["embodiment"] = {"state": "terminal", "effective_capabilities": ["physiology.body-state.v1"]}
        runtime["agency_state"] = "TERMINAL"
        payload["runtime"]["manifest"]["embodiment"]["mode"] = "grounded"
    result = run(tmp_path, mutate=mutate)
    assert result["verdict"] == "FAIL"
    assert "physiology_or_agency_protection_active" in result["failures"][0]["code"]
    sample = json.loads((tmp_path / "run" / "samples.jsonl").read_text())
    assert sample["failure_context"]["agency_state"] == "TERMINAL"
    assert sample["failure_context"]["physiology_enabled"]


def test_stale_physiology_is_not_masked_by_other_healthy_services(tmp_path):
    def mutate(payload, _):
        runtime = payload["runtime"]["runtime"]
        runtime["embodiment"] = {"state": "available", "effective_capabilities": ["physiology.body-state.v1"]}
        runtime["health"].update(body_state_age_seconds=11, body_episode_id="body-1", body_revision=1)
        payload["runtime"]["manifest"]["embodiment"]["mode"] = "grounded"
    report = run(tmp_path, mutate=mutate)
    assert report["verdict"] == "FAIL"
    assert report["failures"][0]["code"] == "physiology_state_stale"


def test_dead_service_process_fails_even_with_cached_healthy_http(tmp_path):
    clock = FakeClock()

    def dead_pid(pid):
        if pid == PIDS["cognitive"]:
            raise ObservationError("process_not_running")
        return str(pid)
    report = run_soak(Observer(clock), tmp_path / "run", clock=clock, pids=PIDS,
                      config=Config(duration_seconds=60), pid_probe=dead_pid)
    assert report["verdict"] == "FAIL"
    assert report["failures"][0]["code"] == "process_not_running"


def test_runtime_restart_and_pid_reuse_fail(tmp_path):
    def restarted(payload, number):
        if number:
            payload["runtime"]["runtime"]["runtime_instance_id"] = "replacement"
            payload["gui"]["runtime"]["runtime_instance_id"] = "replacement"
    report = run(tmp_path, mutate=restarted)
    assert report["verdict"] == "FAIL"
    assert report["failures"][0]["code"] == "deployment_changed:runtime_instance_id"
    clock = FakeClock()
    report = run_soak(Observer(clock), tmp_path / "pid-reused", clock=clock, pids=PIDS,
                      config=Config(duration_seconds=60), pid_probe=lambda pid: f"{pid}:{clock.sleeps}")
    assert report["verdict"] == "FAIL"
    assert report["failures"][0]["code"].startswith("process_replaced:")


def test_interruption_produces_inconclusive_summary(tmp_path):
    class InterruptedClock(FakeClock):
        def sleep(self, _):
            raise KeyboardInterrupt
    report = run(tmp_path, clock=InterruptedClock())
    assert report["interrupted"] and not report["completed"]
    assert report["verdict"] == "INCONCLUSIVE"
    assert not report["release_eligible"]


def test_http_disconnect_is_reported_without_secret_or_response_body(tmp_path):
    urls = {service: f"http://{service}.test" for service in SERVICES}
    token = "secret-that-must-never-be-reported"
    clock = FakeClock()

    def handler(request):
        assert request.method == "GET"
        service = request.url.host.split(".")[0]
        if service == "cognitive":
            raise httpx.ConnectError(token, request=request)
        if request.url.path == "/v1/monitor/events":
            return httpx.Response(200, json=fixture_payload(clock)["monitor_events"])
        if request.url.path == "/v1/monitor/overview":
            return httpx.Response(200, json=fixture_payload(clock)["monitor_overview"])
        return httpx.Response(200, json=fixture_payload(clock)[service])

    observer = HttpObserver(urls, token, token, timeout=1, transport=httpx.MockTransport(handler))
    try:
        result = run_soak(observer, tmp_path / "run", clock=clock, config=Config(duration_seconds=60))
    finally:
        observer.close()
    assert result["verdict"] == "FAIL"
    assert "unavailable:cognitive" in result["failures"][0]["code"]
    assert token not in (tmp_path / "run" / "summary.json").read_text()
    assert token not in (tmp_path / "run" / "samples.jsonl").read_text()


def test_http_observer_uses_existing_read_only_routes_and_incremental_event_cursor():
    from harness.runtime.monitor import _window_seconds

    clock = FakeClock()
    urls = {service: f"http://{service}.test" for service in SERVICES}
    observed_after_seq = []

    def handler(request):
        assert request.method == "GET"
        service = request.url.host.split(".")[0]
        payload = fixture_payload(clock)
        if request.url.path == "/v1/monitor/overview":
            assert _window_seconds(request.url.params["window"]) > 0
            return httpx.Response(200, json=payload["monitor_overview"])
        if request.url.path == "/v1/monitor/events":
            observed_after_seq.append(int(request.url.params["after_seq"]))
            assert request.url.params["topic"] == "event.accepted"
            return httpx.Response(200, json={"has_more": False, "items": [{
                "seq": int(request.url.params["after_seq"]) + 17, "topic": "event.accepted", "payload": {"event_type": "heartbeat"},
            }]})
        expected = {"runtime": "/v1/snapshot", "cognitive": "/v1/health", "homeostasis": "/v1/health", "gui": "/api/ops/availability"}
        assert request.url.path == expected[service]
        return httpx.Response(200, json=payload[service])

    observer = HttpObserver(urls, "control-test", "service-test", timeout=1, transport=httpx.MockTransport(handler))
    try:
        first = normalize(observer.sample(), clock.wall(), Config())
        assert first["accepted_event_counts"] == {"heartbeat": 1}
        observer.sample()
    finally:
        observer.close()
    assert observed_after_seq == [0, 17]


def test_summary_is_replaced_atomically_and_previous_file_survives_failure(tmp_path, monkeypatch):
    path = tmp_path / "summary.json"
    atomic_summary(path, {"verdict": "RUNNING"})
    original = path.read_bytes()

    def fail_replace(source, target):
        assert path.read_bytes() == original
        assert json.loads(Path(source).read_text(encoding="utf-8"))["verdict"] == "FAIL"
        raise OSError("disk problem")
    monkeypatch.setattr("eval.basic_runtime.soak.os.replace", fail_replace)
    with pytest.raises(OSError):
        atomic_summary(path, {"verdict": "FAIL"})
    assert path.read_bytes() == original
    assert list(tmp_path.glob("*.tmp")) == []


def test_real_current_process_identity_is_available():
    assert process_identity(os.getpid()).startswith(f"{os.getpid()}:")


def test_gui_must_be_attached_to_this_runtime():
    clock = FakeClock()
    payload = fixture_payload(clock)
    payload["gui"] = {"available": False}
    with pytest.raises(ObservationError, match="gui_runtime_topology_unavailable"):
        normalize(payload, clock.wall(), Config())


@pytest.mark.parametrize("problem", ["user_message", "pause", "dropped", "truncated"])
def test_interference_or_missing_monitor_events_cannot_pass(tmp_path, problem):
    def mutate(payload, _):
        if problem == "dropped":
            payload["monitor_overview"]["monitor"]["ingest_dropped"] = 1
        elif problem == "truncated":
            payload["monitor_events"]["has_more"] = True
        else:
            payload["monitor_events"]["items"] = [{
                "seq": 1, "topic": "event.accepted", "payload": {"event_type": problem, "text": "private user text"},
            }]
    report = run(tmp_path, mutate=mutate)
    assert report["verdict"] == "FAIL"
    assert "private user text" not in (tmp_path / "run" / "samples.jsonl").read_text()
