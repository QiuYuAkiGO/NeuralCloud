"""Read-only monitoring of existing goal and homeostasis endpoints.

Only numeric state, bounded statuses, counts and hashed identities are written.
No goal statement, dialogue, appraisal text, token or raw error body is saved.
"""
from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import math
from pathlib import Path
import time
from urllib.parse import urlsplit

import httpx

from eval.regulation.run_eval import DIMENSIONS, trajectory_metrics

REQUIRED_SECONDS = 8 * 60 * 60
MAXIMUM_GAP_SECONDS = 75
CONTRIBUTION_SOURCES = ("fact", "self_report", "drift", "legacy_event", "coupling")
STATUSES = frozenset({"active", "observing", "queued", "planned", "pending", "running", "executing", "completed", "succeeded", "committed", "cancelled", "abandoned", "paused", "blocked", "waiting", "wait", "deferred", "reviewing", "unknown", "failed", "error", "improved", "unchanged", "worsened", "stable", "self_report_only", "effect_pending", "rejected", "uncertain"})


def identity(value) -> str:
    return hashlib.sha256(str(value).encode()).hexdigest()[:20]


def status(value) -> str:
    return value if value in STATUSES else "unknown"


def finite(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError("invalid_numeric_snapshot")
    return float(value)


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _rows(value):
    return value if isinstance(value, list) else []


def sanitize(goals: dict, homeostasis: dict) -> dict:
    home = homeostasis.get("payload", homeostasis)
    factors = home["dimensions"]
    values, deviations = {}, {}
    for key in DIMENSIONS:
        factor = factors[key]
        value = finite(factor["value"])
        low, high = [finite(bound) for bound in factor["target_band"]]
        bounds = home["profile"]["dimensions"][key]["hard_bounds"]
        hard_low, hard_high = [finite(bound) for bound in bounds]
        if not hard_low < low < high < hard_high:
            raise ValueError("invalid_snapshot_bounds")
        values[key] = value
        deviations[key] = min(1.0, max(0.0, (low - value) / (low - hard_low), (value - high) / (hard_high - high)))
    episodes = list(_rows(goals.get("episodes")))
    if isinstance(goals.get("active_episode"), dict):
        episodes.append(goals["active_episode"])
    unique = {str(row.get("id", row.get("episode_id"))): row for row in episodes if isinstance(row, dict)}
    activity = {}
    for episode in unique.values():
        for result in _rows(episode.get("results")):
            if not isinstance(result, dict):
                continue
            refs = _rows(result.get("evidence_refs"))
            key = identity([episode.get("id", episode.get("episode_id")), result.get("call_id"), result.get("at"), result.get("status")])
            activity[key] = {"id": key, "status": status(result.get("status")), "evidence_count": len(refs),
                "reference_hashes": [identity(ref) for ref in refs[:32]]}
    evaluations = []
    for row in _rows(goals.get("evaluations")):
        if not isinstance(row, dict):
            continue
        sources = _mapping(row.get("sources"))
        contributions = {}
        for source in CONTRIBUTION_SOURCES:
            values_by_dimension = _mapping(_mapping(row.get("contributions")).get(source))
            values_by_dimension = {key: finite(values_by_dimension[key]) for key in DIMENSIONS if key in values_by_dimension}
            if values_by_dimension:
                contributions[source] = values_by_dimension
        evaluations.append({"id": identity(row.get("id")),
            "episode_id": identity(row["episode_id"]) if row.get("episode_id") else None,
            "snapshot_revision": int(finite(row["snapshot_revision"])) if row.get("snapshot_revision") is not None else None,
            "contributions": contributions,
            "causal_effect_verified": False,
            "outcome": status(row.get("outcome")),
            "fact_count": len(_rows(sources.get("fact"))), "self_report_count": len(_rows(sources.get("self_report"))),
            "drift_present": sources.get("drift") is True, "evidence_count": len(_rows(row.get("evidence_refs")))})
    budget = _mapping(goals.get("budget"))
    return {"enabled": goals.get("enabled") is True, "homeostasis_revision": int(finite(home["revision"])),
        "values": values, "deviations": deviations,
        "affect": {key: finite(home["core_affect"][key]) for key in ("valence", "arousal", "dominance", "confidence")},
        "emotion": home.get("emotion", {}).get("label") if home.get("emotion", {}).get("label") in {"strained", "lonely", "engaged", "settled", "attentive"} else "unknown",
        "goals": [{"id": identity(row.get("id", row.get("goal_id"))), "status": status(row.get("status"))} for row in _rows(goals.get("goals")) if isinstance(row, dict)],
        "episode_statuses": [{"id": identity(key), "status": status(value.get("status"))} for key, value in unique.items()],
        "activities": list(activity.values()), "evaluations": evaluations,
        "budget": {key: finite(budget[key]) if budget.get(key) is not None else None for key in ("calls_last_hour", "maximum_calls_per_hour")}}


class HttpObserver:
    def __init__(self, runtime_url: str, token: str, *, timeout: float = 5, transport=None):
        parsed = urlsplit(runtime_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in {"", "/"}:
            raise ValueError("invalid_runtime_url")
        if not token.strip():
            raise ValueError("control_token_required")
        if not 0 < timeout <= 30:
            raise ValueError("invalid_http_timeout")
        self.base_url = runtime_url.rstrip("/")
        self.timeout = timeout
        self.client = httpx.Client(headers={"Authorization": f"Bearer {token}"}, timeout=timeout,
            follow_redirects=False, trust_env=False, transport=transport)

    def _get(self, path):
        deadline = time.monotonic() + self.timeout
        with self.client.stream("GET", self.base_url + path) as response:
            if response.status_code != 200:
                raise ValueError(f"http_status_{response.status_code}")
            body = bytearray()
            for chunk in response.iter_bytes():
                if time.monotonic() > deadline:
                    raise ValueError("snapshot_read_deadline")
                body.extend(chunk)
                if len(body) > 8 * 1024 * 1024:
                    raise ValueError("snapshot_too_large")
            value = json.loads(body)
            if not isinstance(value, dict):
                raise ValueError("invalid_snapshot_object")
            return value

    def sample(self):
        try:
            return sanitize(self._get("/v1/goals"), self._get("/v1/homeostasis/snapshot"))
        except httpx.HTTPError:
            raise ValueError("runtime_transport_unavailable") from None
        except (KeyError, TypeError):
            raise ValueError("invalid_snapshot_contract") from None

    def close(self):
        self.client.close()


class RealClock:
    def monotonic(self):
        return time.monotonic()

    def wall(self):
        return datetime.now(UTC)

    def sleep(self, seconds):
        time.sleep(seconds)


def contribution_summary(good):
    """Keep cumulative episode traces separate; repeated reviews are not additive."""
    initial, latest = {}, {}
    for index, sample in enumerate(good):
        for evaluation in sample["evaluations"]:
            episode_id = evaluation.get("episode_id")
            if not episode_id or not evaluation.get("contributions"):
                continue
            previous = latest.get(episode_id)
            revision = evaluation.get("snapshot_revision")
            previous_revision = previous.get("snapshot_revision") if previous else None
            if previous_revision is not None and (revision is None or revision < previous_revision):
                continue
            latest[episode_id] = evaluation
            if index == 0:
                initial[episode_id] = evaluation
    initial_episodes = {row["id"] for row in good[0].get("episode_statuses", [])} if good else set()
    totals = {source: {} for source in CONTRIBUTION_SOURCES}
    changes = {source: {} for source in CONTRIBUTION_SOURCES}
    for episode_id, evaluation in latest.items():
        baseline = initial.get(episode_id)
        for source, values in evaluation["contributions"].items():
            totals[source][episode_id] = values
            before = baseline.get("contributions", {}).get(source) if baseline else None
            if before is not None and all(key in before for key in values):
                changes[source][episode_id] = {key: value - before[key] for key, value in values.items()}
            elif episode_id not in initial_episodes and episode_id not in initial:
                changes[source][episode_id] = values
            else:
                changes[source][episode_id] = None
    return ({source: rows or None for source, rows in totals.items()},
            {source: rows or None for source, rows in changes.items()})


def build_report(samples, *, elapsed_seconds, real_clock, requested_seconds, interrupted=False):
    good = [row for row in samples if row.get("ok")]
    errors = len(samples) - len(good)
    initial_activities = {row["id"] for row in good[0]["activities"]} if good else set()
    activity = {row["id"]: row for sample in good[1:] for row in sample["activities"] if row["id"] not in initial_activities}
    initial_evaluations = {row["id"] for row in good[0]["evaluations"]} if good else set()
    evaluations = {row["id"]: row for sample in good[1:] for row in sample["evaluations"] if row["id"] not in initial_evaluations}
    evidence_activity = sum(row["status"] in {"completed", "succeeded", "committed"} and row["evidence_count"] > 0 for row in activity.values())
    gaps = [current["elapsed_seconds"] - previous["elapsed_seconds"] for previous, current in zip(good, good[1:])]
    if good:
        gaps += [good[0]["elapsed_seconds"], elapsed_seconds - good[-1]["elapsed_seconds"]]
    complete = bool(real_clock and good and all(row["enabled"] for row in good) and elapsed_seconds >= REQUIRED_SECONDS and good[-1]["elapsed_seconds"] - good[0]["elapsed_seconds"] >= REQUIRED_SECONDS and not errors and max(gaps, default=0) <= MAXIMUM_GAP_SECONDS and not interrupted)
    result = "incomplete" if not complete else "observed" if evidence_activity else "unverified"
    contribution_totals, contribution_changes = contribution_summary(good)
    return {"schema_version": "regulation-readonly-monitor-v1", "status": result,
        "requested_seconds": requested_seconds, "required_seconds": REQUIRED_SECONDS, "elapsed_seconds": elapsed_seconds,
        "real_monotonic_clock": real_clock, "eight_hour_observation_complete": complete,
        "sample_count": len(samples), "valid_samples": len(good), "error_samples": errors,
        "maximum_gap_seconds": max(gaps, default=None), "interrupted": interrupted,
        "runtime_enabled_all_samples": bool(good) and all(row["enabled"] for row in good),
        "new_activity_count": len(activity), "new_evidenced_runtime_activity_count": evidence_activity,
        "activity_verification": "Runtime reports evidence references; this observer does not independently read body evidence.",
        "regulation_effectiveness": "unverified", "causal_effect_verified": False, "release_eligible": False,
        "metrics": trajectory_metrics(good, maximum_gap_seconds=MAXIMUM_GAP_SECONDS) if good else None,
        "source_counts": {"fact_references": sum(row["fact_count"] for row in evaluations.values()),
            "self_report_references": sum(row["self_report_count"] for row in evaluations.values()),
            "evaluations_with_drift": sum(row["drift_present"] for row in evaluations.values())},
        "source_contribution_values": contribution_totals,
        "source_contribution_observed_deltas": contribution_changes,
        "source_contribution_basis": "Latest Runtime cumulative traces per episode, with deltas from the latest available review in the first valid sample; overlapping episodes are not additive or independently causal.",
        "invalid_duplicate_effect_count": None,
        "limitations": ["Observation only; no service start, state mutation, injected heartbeat or message sending.",
            "No private statements, dialogue, appraisal text, raw references or tokens are retained.",
            "Two snapshots cannot establish causal attribution or independently prove absence of duplicate side effects.",
            "Contribution values use the latest Runtime trace per episode, not a sum of repeated reviews; missing baselines remain null.",
            "A baseline review may precede monitoring; reported contribution deltas are not guaranteed to arise entirely inside the observation interval.",
            "Runtime contribution traces do not independently verify behavioral benefit; invalid duplicate effects remain unavailable.",
            "A short or simulated-clock run never satisfies the eight-hour observation requirement."]}


def preflight(observer, out_dir: Path):
    out_dir.mkdir(parents=True, exist_ok=False)
    try:
        sample = observer.sample()
        report = {"schema_version": "regulation-preflight-v1", "status": "ready" if sample["enabled"] else "disabled",
            "eight_hour_observation_complete": False, "regulation_effectiveness": "unverified", "sample": sample}
    except (ValueError, httpx.HTTPError) as exc:
        report = {"schema_version": "regulation-preflight-v1", "status": "unavailable", "error_type": type(exc).__name__,
            "eight_hour_observation_complete": False, "regulation_effectiveness": "unverified"}
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report


def monitor(observer, out_dir: Path, *, duration_seconds=REQUIRED_SECONDS, interval_seconds=30, clock=None):
    if not 0 < duration_seconds <= 7 * 86400 or not 0 < interval_seconds <= 30:
        raise ValueError("invalid_monitor_duration_or_interval")
    clock = clock or RealClock()
    real_clock = type(clock) is RealClock
    out_dir.mkdir(parents=True, exist_ok=False)
    started = clock.monotonic()
    samples, interrupted = [], False
    try:
        with (out_dir / "samples.jsonl").open("x", encoding="utf-8") as stream:
            while True:
                try:
                    sample = {"ok": True, **observer.sample()}
                except (ValueError, httpx.HTTPError) as exc:
                    sample = {"ok": False, "error_type": type(exc).__name__}
                sample.update(elapsed_seconds=clock.monotonic() - started, observed_at=clock.wall().isoformat())
                samples.append(sample)
                stream.write(json.dumps(sample, ensure_ascii=False) + "\n")
                stream.flush()
                elapsed = clock.monotonic() - started
                report = build_report(samples, elapsed_seconds=elapsed, real_clock=real_clock, requested_seconds=duration_seconds)
                (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
                observation_elapsed = sample["elapsed_seconds"] - samples[0]["elapsed_seconds"]
                if observation_elapsed >= duration_seconds:
                    break
                clock.sleep(min(interval_seconds, duration_seconds - observation_elapsed))
    except KeyboardInterrupt:
        interrupted = True
    report = build_report(samples, elapsed_seconds=clock.monotonic() - started, real_clock=real_clock,
        requested_seconds=duration_seconds, interrupted=interrupted)
    (out_dir / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    return report
