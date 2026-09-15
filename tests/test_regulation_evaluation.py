"""Evaluator mathematics and read-only monitor boundaries with isolated fixtures."""
from __future__ import annotations

import asyncio
import copy
from datetime import UTC, datetime, timedelta
import json

import httpx
import pytest

from eval.regulation.monitor import HttpObserver, REQUIRED_SECONDS, build_report, identity, monitor, preflight, sanitize
from eval.regulation.run_eval import DIMENSIONS, default_scenario, run_paired, trajectory_metrics
from harness.homeostasis import load_profile
from harness.homeostasis.models import initial_snapshot


def state_pair():
    profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
    home = initial_snapshot(profile, now=datetime(2026, 9, 12, tzinfo=UTC)).model_dump(mode="json")
    home["profile"] = profile.model_dump(mode="json")
    return {"enabled": True, "goals": [{"id": "private-goal", "statement": "PRIVATE GOAL TEXT", "status": "active"}],
        "episodes": [], "evaluations": [], "budget": {"calls_last_hour": 0, "maximum_calls_per_hour": 4}}, {"payload": home}


def sample(at, *, activity=False):
    goals, home = state_pair()
    if activity:
        goals["episodes"] = [{"id": "episode-one", "status": "completed", "statement": "PRIVATE EPISODE",
            "results": [{"call_id": "call-one", "status": "completed", "at": "2026-09-12T01:00:00Z",
                "evidence_refs": ["evidence://PRIVATE-REF"]}]}]
        goals["evaluations"] = [{"id": "review-one", "outcome": "self_report_only",
            "text": "PRIVATE APPRAISAL", "sources": {"fact": ["PRIVATE-FACT"], "self_report": ["PRIVATE-SELF"], "drift": True}}]
    return {"ok": True, "elapsed_seconds": at, **sanitize(goals, home)}


def test_integral_uses_real_intervals_and_does_not_fill_gaps():
    values = [{"elapsed_seconds": at, "deviations": {key: deviation for key in DIMENSIONS}}
        for at, deviation in ((0, 1.0), (10, 0.0), (100, 1.0))]
    metrics = trajectory_metrics(values, maximum_gap_seconds=75)
    assert metrics["deviation_integral_total_seconds"] == pytest.approx(30)
    assert metrics["observed_interval_seconds"] == 10
    assert metrics["recovery_time_seconds"] == 10
    with pytest.raises(ValueError, match="monotonic"):
        trajectory_metrics(list(reversed(values)))


def test_paired_neutral_appraisal_does_not_assume_expression_gain(tmp_path):
    report = asyncio.run(run_paired(tmp_path / "paired"))
    assert report["matched_initial_and_external_events"]
    assert report["real_model_effectiveness"] == "unverified" and report["release_eligible"] is False
    assert report["enabled_minus_disabled_integral_seconds"] == pytest.approx(0)
    enabled = report["arms"]["enabled"]
    assert enabled["metrics"]["activity_count"] == 1
    assert enabled["metrics"]["duplicate_attempt_count"] == 1
    assert enabled["metrics"]["duplicate_effect_count"] == 0
    assert enabled["metrics"]["wait_count"] == 1
    assert all(value == 0 for value in enabled["attribution"]["self_report"].values())
    assert any(value != 0 for value in enabled["attribution"]["drift"].values())


def test_scripted_suggestion_is_separate_from_external_facts_and_replay(tmp_path):
    case = default_scenario()
    case["attempts"][0]["suggested_impacts"]["cognitive_load"] = -0.1
    case["attempts"][1]["suggested_impacts"]["cognitive_load"] = -0.1
    report = asyncio.run(run_paired(tmp_path / "suggested", scenario=case))
    assert report["arms"]["enabled"]["attribution"]["self_report"]["cognitive_load"] < 0
    assert report["arms"]["enabled"]["attribution"]["external_fixture_fact"] == report["arms"]["disabled"]["attribution"]["external_fixture_fact"]
    assert report["arms"]["enabled"]["metrics"]["duplicate_effect_count"] == 0
    assert report["real_model_effectiveness"] == "unverified"


def test_observer_only_gets_fixed_endpoints_and_excludes_private_text(tmp_path):
    goals, home = state_pair()
    goals["active_episode"] = {"id": "PRIVATE-ID", "statement": "PRIVATE BODY", "results": [{
        "call_id": "PRIVATE-CALL", "status": "completed", "evidence_refs": ["PRIVATE-REF"]}]}
    requests = []

    def respond(request):
        requests.append(request)
        return httpx.Response(200, json=goals if request.url.path == "/v1/goals" else home)

    observer = HttpObserver("http://runtime.test", "PRIVATE-TOKEN", transport=httpx.MockTransport(respond))
    try:
        report = preflight(observer, tmp_path / "preflight")
    finally:
        observer.close()
    assert report["status"] == "ready" and not report["eight_hour_observation_complete"]
    assert [(request.method, request.url.path) for request in requests] == [("GET", "/v1/goals"), ("GET", "/v1/homeostasis/snapshot")]
    assert all(request.headers["Authorization"] == "Bearer PRIVATE-TOKEN" for request in requests)
    output = (tmp_path / "preflight/report.json").read_text()
    assert "PRIVATE" not in output and "private-goal" not in output


@pytest.mark.parametrize("elapsed,real,activity,expected", [
    (60, True, True, "incomplete"), (REQUIRED_SECONDS, False, True, "incomplete"),
    (REQUIRED_SECONDS, True, False, "unverified"), (REQUIRED_SECONDS, True, True, "observed"),
])
def test_soak_classification_requires_real_eight_hours_and_new_activity(elapsed, real, activity, expected):
    rows = [sample(at, activity=activity and at > 0) for at in range(0, elapsed + 1, 30)]
    report = build_report(rows, elapsed_seconds=elapsed, real_clock=real, requested_seconds=elapsed)
    assert report["status"] == expected
    assert report["regulation_effectiveness"] == "unverified"
    assert report["release_eligible"] is False
    assert report["invalid_duplicate_effect_count"] is None
    assert report["new_evidenced_runtime_activity_count"] == int(activity)


def test_old_activity_does_not_count_as_observed_during_monitor():
    rows = [sample(at, activity=True) for at in range(0, REQUIRED_SECONDS + 1, 30)]
    report = build_report(rows, elapsed_seconds=REQUIRED_SECONDS, real_clock=True, requested_seconds=REQUIRED_SECONDS)
    assert report["status"] == "unverified" and report["new_activity_count"] == 0
    assert report["source_counts"]["fact_references"] == 0


def test_gap_disabled_state_and_errors_cannot_pass_observation():
    rows = [sample(at) for at in range(0, REQUIRED_SECONDS + 1, 30)]
    variants = [rows[:10] + rows[14:], copy.deepcopy(rows), rows + [{"ok": False}]]
    variants[1][10]["enabled"] = False
    for variant in variants:
        report = build_report(variant, elapsed_seconds=REQUIRED_SECONDS, real_clock=True, requested_seconds=REQUIRED_SECONDS)
        assert report["status"] == "incomplete"


def test_monitor_writes_bounded_public_samples_and_simulated_clock_stays_incomplete(tmp_path):
    class FakeClock:
        elapsed = 0

        def monotonic(self):
            return self.elapsed

        def wall(self):
            return datetime(2026, 9, 12, tzinfo=UTC) + timedelta(seconds=self.elapsed)

        def sleep(self, seconds):
            self.elapsed += seconds

    class Observer:
        def sample(self):
            return sanitize(*state_pair())

    report = monitor(Observer(), tmp_path / "monitor", duration_seconds=60, interval_seconds=30, clock=FakeClock())
    assert report["status"] == "incomplete" and report["sample_count"] == 3
    lines = (tmp_path / "monitor/samples.jsonl").read_text().splitlines()
    assert len(lines) == 3 and "PRIVATE" not in "\n".join(lines)
    assert [json.loads(line)["elapsed_seconds"] for line in lines] == [0, 30, 60]


def test_preflight_does_not_save_http_error_body(tmp_path):
    observer = HttpObserver("http://runtime.test", "PRIVATE-TOKEN", transport=httpx.MockTransport(
        lambda request: httpx.Response(500, json={"detail": "PRIVATE SERVER ERROR"})))
    try:
        report = preflight(observer, tmp_path / "failed")
    finally:
        observer.close()
    assert report["status"] == "unavailable"
    assert "PRIVATE" not in (tmp_path / "failed/report.json").read_text()


def contribution_sample(at, evaluations, *, episode_ids=()):
    goals, home = state_pair()
    goals["episodes"] = [{"id": episode_id, "status": "active"} for episode_id in episode_ids]
    goals["evaluations"] = evaluations
    return {"ok": True, "elapsed_seconds": at, **sanitize(goals, home)}


def review(episode_id, revision, contributions):
    return {"id": f"review-{episode_id}-{revision}", "episode_id": episode_id,
        "snapshot_revision": revision, "outcome": "improved", "contributions": contributions,
        "causal_effect_verified": True, "text": "PRIVATE APPRAISAL"}


def test_monitor_contributions_use_latest_per_episode_without_summing_repeated_reviews():
    old = review("PRIVATE-EPISODE-A", 2, {"fact": {"cognitive_load": -0.1}})
    latest = review("PRIVATE-EPISODE-A", 4, {"fact": {"cognitive_load": -0.3},
        "self_report": {"cognitive_load": -0.02}})
    other = review("PRIVATE-EPISODE-B", 3, {"fact": {"cognitive_load": -0.4}})
    rows = [contribution_sample(0, [old], episode_ids=["PRIVATE-EPISODE-A"]),
        contribution_sample(30, [old, latest, other]),
        contribution_sample(60, [latest, other, old])]
    report = build_report(rows, elapsed_seconds=60, real_clock=True, requested_seconds=60)
    first, second = identity("PRIVATE-EPISODE-A"), identity("PRIVATE-EPISODE-B")
    assert report["source_contribution_values"]["fact"] == {
        first: {"cognitive_load": -0.3}, second: {"cognitive_load": -0.4}}
    assert report["source_contribution_observed_deltas"]["fact"][first]["cognitive_load"] == pytest.approx(-0.2)
    assert report["source_contribution_observed_deltas"]["fact"][second]["cognitive_load"] == -0.4
    assert report["source_contribution_observed_deltas"]["self_report"][first] is None
    assert report["source_contribution_values"]["drift"] is None
    assert report["status"] == "incomplete" and report["causal_effect_verified"] is False
    assert "PRIVATE" not in json.dumps(report)
    assert all(evaluation["causal_effect_verified"] is False for row in rows for evaluation in row["evaluations"])


def test_monitor_contribution_baseline_uses_latest_initial_review_and_preserves_missing_baseline():
    first = review("existing", 2, {"drift": {"autonomy": 0.1}})
    baseline = review("existing", 3, {"drift": {"autonomy": 0.2}})
    later = review("existing", 5, {"drift": {"autonomy": 0.5}})
    unknown_baseline = review("existing-without-review", 4, {"coupling": {"autonomy": -0.1}})
    missing_revision = review("existing", None, {"drift": {"autonomy": 99}})
    rows = [contribution_sample(0, [baseline, first], episode_ids=["existing", "existing-without-review"]),
        contribution_sample(30, [later, unknown_baseline]),
        contribution_sample(60, [missing_revision])]
    report = build_report(rows, elapsed_seconds=60, real_clock=True, requested_seconds=60)
    assert report["source_contribution_values"]["drift"][identity("existing")]["autonomy"] == 0.5
    assert report["source_contribution_observed_deltas"]["drift"][identity("existing")]["autonomy"] == pytest.approx(0.3)
    assert report["source_contribution_values"]["coupling"][identity("existing-without-review")]["autonomy"] == -0.1
    assert report["source_contribution_observed_deltas"]["coupling"][identity("existing-without-review")] is None


def test_contribution_sanitization_keeps_only_known_psychological_dimensions_and_sources():
    entry = review("PRIVATE-ID", 7, {"fact": {"autonomy": -0.1, "energy": 20, "PRIVATE-DIMENSION": 123},
        "legacy_event": {"social_connection": 0.2}, "PRIVATE-SOURCE": {"autonomy": 999}})
    row = contribution_sample(0, [entry])
    assert row["evaluations"][0]["contributions"] == {
        "fact": {"autonomy": -0.1}, "legacy_event": {"social_connection": 0.2}}
    assert "PRIVATE" not in json.dumps(row)
    assert "energy" not in row["evaluations"][0]["contributions"]["fact"]


@pytest.mark.parametrize("value", [True, float("nan"), float("inf"), "PRIVATE-NUMBER"])
def test_contribution_sanitization_rejects_nonfinite_or_non_numeric_values(value):
    with pytest.raises(ValueError, match="invalid_numeric_snapshot"):
        contribution_sample(0, [review("episode", 1, {"fact": {"autonomy": value}})])
