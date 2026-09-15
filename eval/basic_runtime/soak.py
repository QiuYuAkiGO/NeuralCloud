"""Read-only wall-clock soak of an already running four-service deployment.

Example: python -m eval.basic_runtime.soak --out-dir eval/reports/basic-runtime
Tokens are read only from NC_CONTROL_TOKEN / NC_SERVICE_TOKEN or token files.
No launch, state reset, chat, heartbeat injection, or deployment mutation occurs.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime
import json
import math
import os
from pathlib import Path
import time
from typing import Any, Callable
from urllib.parse import urlsplit
import uuid

import httpx

from harness.files import replace_path

DIMENSIONS = ("energy_reserve", "cognitive_load", "safety_predictability", "competence", "autonomy", "social_connection", "novelty")
SERVICES = ("runtime", "cognitive", "homeostasis", "gui")
RELEASE_SECONDS = 8 * 60 * 60


@dataclass(frozen=True)
class Config:
    duration_seconds: float = RELEASE_SECONDS
    interval_seconds: float = 30
    timeout_seconds: float = 5
    max_gap_seconds: float = 75
    clock_tolerance_seconds: float = 5
    boundary_seconds: float = 300
    boundary_fraction: float = 0.01

    def __post_init__(self):
        for name in ("duration_seconds", "interval_seconds", "timeout_seconds", "max_gap_seconds", "boundary_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if self.interval_seconds > 30 or self.max_gap_seconds > 75:
            raise ValueError("sampling may not be relaxed beyond 30s / 75s maximum gap")
        if self.timeout_seconds > self.interval_seconds:
            raise ValueError("HTTP timeout must not exceed sampling interval")
        if self.max_gap_seconds < self.interval_seconds:
            raise ValueError("maximum gap must cover sampling interval")
        if self.boundary_seconds > 300:
            raise ValueError("persistent boundary threshold may not exceed 300s")
        if not 0 <= self.clock_tolerance_seconds <= 5 or not 0 < self.boundary_fraction <= 0.05:
            raise ValueError("invalid clock tolerance or boundary fraction")


class RealClock:
    def monotonic(self) -> float:
        return time.monotonic()

    def wall(self) -> datetime:
        return datetime.now(UTC)

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class ObservationError(ValueError):
    pass


def _require(condition: bool, code: str) -> None:
    if not condition:
        raise ObservationError(code)


def _number(value: Any, field: str) -> float:
    _require(isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value), f"invalid:{field}")
    return float(value)


def _timestamp(value: Any, field: str) -> datetime:
    _require(isinstance(value, str), f"missing:{field}")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        _require(result.tzinfo is not None, f"invalid:{field}")
        return result.astimezone(UTC)
    except ValueError:
        raise ObservationError(f"invalid:{field}") from None


class HttpObserver:
    """Only GETs known endpoints; never includes tokens or response text in reports."""

    def __init__(self, urls: dict[str, str], control_token: str, service_token: str, *, timeout: float, transport=None):
        self.urls = urls
        for service in SERVICES:
            parsed = urlsplit(urls[service])
            if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ValueError(f"invalid {service} base URL")
        self._tokens = {"runtime": control_token, "cognitive": service_token, "homeostasis": service_token, "gui": None}
        self.timeout = timeout
        self.client = httpx.Client(timeout=httpx.Timeout(timeout), transport=transport, follow_redirects=False, trust_env=False)
        self.pool = ThreadPoolExecutor(max_workers=6, thread_name_prefix="soak-http")
        self.monitor_since = datetime.now(UTC).isoformat()
        self.monitor_after_seq = 0

    def _get(self, service: str, path: str, params: dict | None = None) -> dict:
        token = self._tokens[service]
        try:
            deadline = time.monotonic() + self.timeout
            with self.client.stream("GET", self.urls[service].rstrip("/") + path, params=params, headers={"Authorization": f"Bearer {token}"} if token else {}) as response:
                response.raise_for_status()
                body = bytearray()
                for chunk in response.iter_bytes():
                    _require(time.monotonic() <= deadline, f"http_deadline:{service}")
                    body.extend(chunk)
                    _require(len(body) <= 8 * 1024 * 1024, f"response_too_large:{service}")
                payload = json.loads(body)
            _require(isinstance(payload, dict), f"invalid:{service}_response")
            return payload
        except httpx.HTTPStatusError as exc:
            raise ObservationError(f"http:{service}:{exc.response.status_code}") from None
        except (httpx.HTTPError, ValueError):
            raise ObservationError(f"unavailable:{service}") from None

    def sample(self) -> dict:
        paths = {"runtime": "/v1/snapshot", "cognitive": "/v1/health", "homeostasis": "/v1/health", "gui": "/api/ops/availability"}
        futures = {service: self.pool.submit(self._get, service, path) for service, path in paths.items()}
        futures["monitor_events"] = self.pool.submit(self._monitor_events)
        futures["monitor_overview"] = self.pool.submit(self._get, "runtime", "/v1/monitor/overview", {"window": "1h"})
        results, failures = {}, []
        for service, future in futures.items():
            try:
                results[service] = future.result()
            except ObservationError as exc:
                failures.append(str(exc))
        if failures:
            raise ObservationError(";".join(failures))
        # Commit the incremental cursor only after every endpoint and page succeeds.
        items = results["monitor_events"]["items"]
        if items:
            self.monitor_after_seq = items[-1]["seq"]
        return results

    def _monitor_events(self) -> dict:
        deadline = time.monotonic() + self.timeout
        after_seq, items = self.monitor_after_seq, []
        for _ in range(20):
            _require(time.monotonic() <= deadline, "monitor_pagination_deadline")
            page = self._get("runtime", "/v1/monitor/events", {
                "since": self.monitor_since, "after_seq": after_seq,
                "topic": "event.accepted", "limit": 500,
            })
            _require(time.monotonic() <= deadline, "monitor_pagination_deadline")
            batch, has_more = page.get("items"), page.get("has_more")
            _require(isinstance(batch, list) and len(batch) <= 500 and isinstance(has_more, bool), "invalid_monitor_page")
            for item in batch:
                seq = item.get("seq") if isinstance(item, dict) else None
                _require(isinstance(seq, int) and not isinstance(seq, bool) and seq > after_seq, "missing_or_invalid_monitor_sequence")
                after_seq = seq
            items.extend(batch)
            if not has_more:
                return {"items": items, "has_more": False, "next_cursor": None}
            _require(bool(batch), "monitor_pagination_no_progress")
            # The API's next_cursor is descending; after_seq requests ascending pages.
        raise ObservationError("monitor_sample_truncated_or_missing")

    def close(self):
        self.pool.shutdown(wait=True)
        self.client.close()


def process_identity(pid: int) -> str:
    """Check liveness and creation identity without optional dependencies."""
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        _require(bool(handle), "process_unavailable")
        try:
            exit_code = wintypes.DWORD()
            _require(bool(kernel.GetExitCodeProcess(handle, ctypes.byref(exit_code))) and exit_code.value == 259, "process_not_running")
            times = [wintypes.FILETIME() for _ in range(4)]
            _require(bool(kernel.GetProcessTimes(handle, *(ctypes.byref(item) for item in times))), "process_identity_unavailable")
            creation = (times[0].dwHighDateTime << 32) + times[0].dwLowDateTime
            return f"{pid}:{creation}"
        finally:
            kernel.CloseHandle(handle)
    try:
        # Linux /proc starttime disambiguates PID reuse; process names may contain spaces.
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
        _require(fields[0] not in {"Z", "X"}, "process_not_running")
        return f"{pid}:{fields[19]}"
    except (OSError, IndexError):
        raise ObservationError("process_identity_unavailable") from None


def normalize(raw: dict, wall: datetime, config: Config) -> dict:
    """Strict whitelist projection of actual public fields, without their defaults."""
    try:
        composite = raw["runtime"]
        runtime, manifest = composite["runtime"], composite["manifest"]
        state = composite["homeostasis"]["payload"]
        profile = state["profile"]
        _require(runtime["lifecycle"] == "RUNNING", "runtime_not_running")
        _require(isinstance(runtime["runtime_instance_id"], str) and bool(runtime["runtime_instance_id"]), "missing:runtime_instance_id")
        health = runtime["health"]
        _require(health["lifecycle"] == "RUNNING", "runtime_health_not_running")
        _require(runtime["agency_state"] == "AVAILABLE", "physiology_or_agency_protection_active")
        for name in ("journal_writable", "cognitive_healthy", "homeostasis_healthy", "llm_healthy", "body_healthy"):
            _require(health[name] is True, f"unhealthy:{name}")
        _require(health["degraded_reasons"] == [], "runtime_degraded")
        for service in ("cognitive", "homeostasis"):
            _require(raw[service]["service"] == service and raw[service]["healthy"] is True, f"unhealthy:{service}")
        _require(raw["gui"]["available"] is True, "gui_runtime_topology_unavailable")
        gui_runtime = raw["gui"]["runtime"]
        _require(gui_runtime["runtime_instance_id"] == runtime["runtime_instance_id"] and gui_runtime["lifecycle"] == "RUNNING", "gui_runtime_mismatch")
        _require(state["watchdog"] == [], "homeostasis_watchdog")
        monitor = raw["monitor_overview"]["monitor"]
        _require(monitor["healthy"] is True and monitor["last_error"] is None, "monitor_unhealthy")
        _require(monitor["ingest_dropped"] == 0, "monitor_events_missing")
        monitor_events = raw["monitor_events"]
        _require(monitor_events["has_more"] is False and isinstance(monitor_events["items"], list), "monitor_sample_truncated_or_missing")
        event_counts: dict[str, int] = {}
        interference = {"user_message", "terminal_continuation", "pause", "resume", "recover", "stop", "emergency_stop", "permission_revoked", "approval_result", "diagnostic"}
        for item in monitor_events["items"]:
            _require(item["topic"] == "event.accepted", "invalid_monitor_event_topic")
            event_type = item["payload"]["event_type"]
            _require(isinstance(event_type, str) and bool(event_type), "missing:monitor_event_type")
            _require(event_type not in interference, f"external_interference:{event_type}")
            event_counts[event_type] = event_counts.get(event_type, 0) + 1
        heartbeat = _number(manifest["heartbeat_seconds"], "heartbeat_seconds")
        _require(heartbeat > 0, "invalid:heartbeat_seconds")
        ages, timestamps = {}, {}
        for name, value in {"runtime": health["last_heartbeat_at"], "homeostasis": state["last_heartbeat_at"]}.items():
            moment = _timestamp(value, f"{name}.last_heartbeat_at")
            age = (wall - moment).total_seconds()
            _require(-config.clock_tolerance_seconds <= age <= heartbeat * 2 + config.interval_seconds, f"stale_or_future_heartbeat:{name}")
            ages[name], timestamps[name] = age, moment.isoformat()
        _require(set(state["dimensions"]) == set(DIMENSIONS) and set(profile["dimensions"]) == set(DIMENSIONS), "missing:dimensions")
        dimensions, sides = {}, {}
        for name in DIMENSIONS:
            factor = state["dimensions"][name]
            low, high = [_number(item, f"{name}.hard_bounds") for item in profile["dimensions"][name]["hard_bounds"]]
            value = _number(factor["value"], f"{name}.value")
            velocity = _number(factor["velocity"], f"{name}.velocity")
            precision = _number(factor["precision"], f"{name}.precision")
            _require(0 <= low < high <= 1 and low <= value <= high and -1 <= velocity <= 1 and 0 <= precision <= 1, f"out_of_bounds:{name}")
            target_low, target_high = [_number(item, f"{name}.target_band") for item in factor["target_band"]]
            _require(factor["dimension"] == name and low <= target_low < target_high <= high, f"invalid_target_band:{name}")
            posterior = factor["posterior"]
            _require(set(posterior) == {"low", "in_band", "high"}, f"invalid_posterior:{name}")
            probabilities = [_number(item, f"{name}.posterior") for item in posterior.values()]
            _require(all(0 <= item <= 1 for item in probabilities) and abs(sum(probabilities) - 1) <= 1e-6, f"invalid_posterior:{name}")
            margin = (high - low) * config.boundary_fraction
            dimensions[name] = value
            sides[name] = "low" if value <= low + margin else "high" if value >= high - margin else None
        embodiment = runtime["embodiment"]
        capabilities = embodiment["effective_capabilities"]
        _require(isinstance(capabilities, list), "missing:embodiment_capabilities")
        physiology = "physiology.body-state.v1" in capabilities
        mode = manifest["embodiment"]["mode"]
        if mode != "off":
            _require(embodiment["state"] == "available", "embodiment_unavailable_or_terminal")
        body_age = None
        if physiology:
            body_age = _number(health["body_state_age_seconds"], "body_state_age_seconds")
            max_age = _number(manifest["embodiment"]["body_state_max_age_seconds"], "body_state_max_age_seconds")
            _require(0 <= body_age <= max_age, "physiology_state_stale")
            _require(bool(health["body_episode_id"]) and _number(health["body_revision"], "body_revision") >= 1, "missing:physiology_identity")
        return {
            "runtime_instance_id": runtime["runtime_instance_id"],
            "lifecycle": runtime["lifecycle"],
            "lifecycle_since": _timestamp(runtime["lifecycle_since"], "lifecycle_since").isoformat(),
            "revisions": {service: _number(raw[service]["revision"], f"{service}.revision") for service in ("cognitive", "homeostasis")},
            "profile_ref": f"{profile['profile_id']}@{profile['revision']}",
            "profile_mode": profile["mode"],
            "heartbeat_seconds": heartbeat,
            "heartbeat_age_seconds": ages,
            "heartbeat_at": timestamps,
            "dimensions": dimensions,
            "boundary_sides": sides,
            "physiology_enabled": physiology,
            "body_state_age_seconds": body_age,
            "services_healthy": {service: True for service in SERVICES},
            "accepted_event_counts": event_counts,
            "monitor_healthy": True,
        }
    except (KeyError, TypeError, IndexError):
        raise ObservationError("missing_or_invalid_required_field") from None


def atomic_summary(path: Path, payload: dict) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("x", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        replace_path(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def failure_context(raw: dict) -> dict:
    """Retain useful terminal/physiology facts even when normalization rejects a sample."""
    try:
        runtime = raw["runtime"]["runtime"]
        factors = raw["runtime"]["homeostasis"]["payload"]["dimensions"]
        return {
            "lifecycle": runtime.get("lifecycle") if runtime.get("lifecycle") in {"RUNNING", "PAUSED", "FAULTED", "STOPPED", "STOPPING"} else None,
            "agency_state": runtime.get("agency_state") if runtime.get("agency_state") in {"AVAILABLE", "DEGRADED", "TERMINAL"} else None,
            "physiology_enabled": "physiology.body-state.v1" in (runtime.get("embodiment") or {}).get("effective_capabilities", []),
            "dimensions": {name: factor["value"] for name, factor in factors.items() if name in DIMENSIONS and isinstance(factor, dict) and isinstance(factor.get("value"), (int, float)) and math.isfinite(factor["value"])},
        }
    except (KeyError, TypeError, AttributeError):
        return {}


def run_soak(observer, output_dir: Path, *, config: Config | None = None, clock=None, pids: dict[str, int] | None = None, pid_probe: Callable[[int], str] = process_identity) -> dict:
    config, clock, pids = config or Config(), clock or RealClock(), pids or {}
    output_dir.mkdir(parents=True, exist_ok=False)
    samples_path, summary_path = output_dir / "samples.jsonl", output_dir / "summary.json"
    started, wall_start = clock.monotonic(), clock.wall()
    failures, gaps, samples, previous, identities, boundary_since = [], [], 0, None, {}, {}
    elapsed, complete, interrupted = 0.0, False, False
    real_clock = type(clock) is RealClock

    def summary() -> dict:
        eligible = complete and elapsed >= RELEASE_SECONDS and config.duration_seconds >= RELEASE_SECONDS and real_clock
        observed_processes = set(pids) == set(SERVICES)
        missing = []
        if not eligible:
            missing.append("eight_hours_of_real_monotonic_time_not_completed")
        if not observed_processes:
            missing.append("four_process_identities_not_observed")
        expected = math.ceil(config.duration_seconds / config.interval_seconds) + 1
        enough = samples >= expected
        if not enough:
            missing.append("insufficient_samples")
        verdict = "FAIL" if failures else "PASS" if eligible and enough and observed_processes else "INCONCLUSIVE" if complete or interrupted else "RUNNING"
        return {
            "schema_version": "basic-runtime-soak-v1", "verdict": verdict,
            "scope": ["eight_hour_online_stability", "undisturbed_homeostatic_stability"],
            "full_milestone_verdict": "NOT_EVALUATED", "duration_eligible": eligible,
            "release_eligible": eligible and enough and observed_processes and not failures,
            "started_at": wall_start.isoformat(), "elapsed_monotonic_seconds": elapsed,
            "requested_duration_seconds": config.duration_seconds, "interval_seconds": config.interval_seconds,
            "clock_source": "system_monotonic" if real_clock else "injected_test_clock",
            "completed": complete, "interrupted": interrupted, "samples": samples,
            "minimum_samples": expected, "processes_observed": sorted(pids),
            "failures": list(failures), "inconclusive_reasons": missing,
            "gaps_seconds": gaps, "max_gap_seconds": config.max_gap_seconds,
            "max_sample_lateness_seconds": config.interval_seconds / 2,
            "boundary_seconds": config.boundary_seconds, "boundary_fraction": config.boundary_fraction,
            "clock_tolerance_seconds": config.clock_tolerance_seconds,
            "artifacts": {"samples": "samples.jsonl", "summary": "summary.json"},
            "observer_read_only": True,
            "interference_check": "runtime_accepted_user_control_events_since_observer_start",
        }

    atomic_summary(summary_path, summary())
    try:
        with samples_path.open("x", encoding="utf-8", newline="\n") as log:
            while True:
                observed, wall = clock.monotonic(), clock.wall()
                elapsed = observed - started
                record = {"sample": samples, "observed_at": wall.isoformat(), "elapsed_monotonic_seconds": elapsed, "issues": []}
                scheduled = min(samples * config.interval_seconds, config.duration_seconds)
                if elapsed - scheduled > config.interval_seconds / 2:
                    record["issues"].append("missed_sample_deadline")
                if elapsed < 0 or abs((wall - wall_start).total_seconds() - elapsed) > config.clock_tolerance_seconds:
                    record["issues"].append("clock_drift_or_suspend")
                if previous is not None:
                    gap = observed - previous["monotonic"]
                    if gap <= 0 or gap > config.max_gap_seconds:
                        gaps.append(gap)
                        record["issues"].append("sample_gap_or_clock_regression")
                try:
                    raw = observer.sample()
                    record["failure_context"] = failure_context(raw)
                    data = normalize(raw, clock.wall(), config)
                    record.pop("failure_context")
                    record["observation"] = data
                    for service, pid in pids.items():
                        identity = pid_probe(pid)
                        if service in identities:
                            _require(identities[service] == identity, f"process_replaced:{service}")
                        identities[service] = identity
                    record["process_identities"] = dict(identities)
                    if previous is not None and previous.get("data") is not None:
                        old = previous["data"]
                        for key in ("runtime_instance_id", "lifecycle_since", "profile_ref", "profile_mode", "physiology_enabled", "heartbeat_seconds"):
                            _require(data[key] == old[key], f"deployment_changed:{key}")
                        for service in ("cognitive", "homeostasis"):
                            _require(data["revisions"][service] >= old["revisions"][service], f"revision_regressed:{service}")
                        for service in ("runtime", "homeostasis"):
                            _require(data["heartbeat_at"][service] >= old["heartbeat_at"][service], f"heartbeat_regressed:{service}")
                    for name, side in data["boundary_sides"].items():
                        if side is None:
                            boundary_since.pop(name, None)
                        elif name not in boundary_since or boundary_since[name][0] != side:
                            boundary_since[name] = (side, observed)
                        elif observed - boundary_since[name][1] >= config.boundary_seconds:
                            record["issues"].append(f"persistent_hard_boundary:{name}:{side}")
                    previous = {"monotonic": observed, "data": data}
                except ObservationError as exc:
                    record["issues"].append(str(exc))
                except Exception:
                    record["issues"].append("observation_or_process_probe_failed")
                samples += 1
                failures.extend({"sample": samples - 1, "code": issue} for issue in record["issues"])
                log.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                log.flush()
                os.fsync(log.fileno())
                complete = elapsed >= config.duration_seconds
                atomic_summary(summary_path, summary())
                if complete or failures:
                    break
                next_sample = min(started + samples * config.interval_seconds, started + config.duration_seconds)
                clock.sleep(max(0, next_sample - clock.monotonic()))
    except KeyboardInterrupt:
        interrupted = True
    except Exception as exc:
        failures.append({"sample": samples, "code": "artifact_or_sampler_failure", "error_type": type(exc).__name__})
    finally:
        atomic_summary(summary_path, summary())
    return summary()


def _token(env_name: str, file: Path | None) -> str:
    value = file.read_text(encoding="utf-8-sig").strip() if file else os.environ.get(env_name, "").strip()
    if not value or "\n" in value or "\r" in value:
        raise ValueError("required token unavailable")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--duration-seconds", type=float, default=RELEASE_SECONDS)
    parser.add_argument("--interval-seconds", type=float, default=30)
    parser.add_argument("--timeout-seconds", type=float, default=5)
    parser.add_argument("--boundary-seconds", type=float, default=300)
    parser.add_argument("--out-dir", type=Path, default=Path("eval/reports/basic_runtime"), help="Parent directory; each run creates its own unique folder")
    parser.add_argument("--control-token-file", type=Path)
    parser.add_argument("--service-token-file", type=Path)
    parser.add_argument("--control-token-env", default="NC_CONTROL_TOKEN")
    parser.add_argument("--service-token-env", default="NC_SERVICE_TOKEN")
    parser.add_argument("--pid", action="append", default=[], metavar="SERVICE=PID", help="Repeat for runtime,cognitive,homeostasis,gui; all four are needed for PASS")
    defaults = {"runtime": 8770, "cognitive": 8771, "homeostasis": 8772, "gui": 8766}
    for service, port in defaults.items():
        parser.add_argument(f"--{service}-url", default=f"http://127.0.0.1:{port}")
    args = parser.parse_args(argv)
    observer = None
    try:
        config = Config(duration_seconds=args.duration_seconds, interval_seconds=args.interval_seconds, timeout_seconds=args.timeout_seconds, boundary_seconds=args.boundary_seconds)
        pids = {}
        for item in args.pid:
            name, value = item.split("=", 1)
            if name not in SERVICES or name in pids or int(value) <= 0:
                raise ValueError("invalid or duplicate service PID")
            pids[name] = int(value)
        if len(set(pids.values())) != len(pids):
            raise ValueError("four-service process IDs must be distinct")
        urls = {service: getattr(args, f"{service}_url") for service in SERVICES}
        observer = HttpObserver(urls, _token(args.control_token_env, args.control_token_file), _token(args.service_token_env, args.service_token_file), timeout=config.timeout_seconds)
        output = args.out_dir / (datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8])
        print(f"Read-only soak started. Reports: {output}", flush=True)
        result = run_soak(observer, output, config=config, pids=pids)
        print(json.dumps({"verdict": result["verdict"], "release_eligible": result["release_eligible"], "summary": str(output / "summary.json")}))
        return 0 if result["verdict"] == "PASS" else 1
    except (ValueError, OSError):
        print("Soak configuration or report directory is invalid; check URLs, PID arguments, token availability and output path.")
        return 2
    finally:
        if observer is not None:
            observer.close()


if __name__ == "__main__":
    raise SystemExit(main())
