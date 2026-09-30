from datetime import UTC, datetime, timedelta

import pytest

from tools.production_acceptance.launch_metrics import score


def batch(n=200, successful=190, seconds=29):
    start = datetime(2026, 9, 29, tzinfo=UTC)
    plan = [{"request_id": str(i), "category": "ordinary_text", "expected": "reply"} for i in range(n)]
    results = [{"request_id": str(i), "outcome": "reply" if i < successful else "wait",
                "received_at": start.isoformat(), "verified_at": (start + timedelta(seconds=seconds)).isoformat(),
                "decision_id": "decision:" + str(i), "receipt_refs": ["receipt:" + str(i)],
                "verified_segments": 1, "content_valid": True, "authority_valid": True} for i in range(n)]
    return plan, results


def test_wait_never_shrinks_the_frozen_denominator():
    report = score(*batch())
    assert report["ordinary_requests"] == 200 and report["successful_replies"] == 190
    assert report["success_rate"] == .95 and report["pilot_performance_pass"]
    assert not report["target_success_rate_met"]
    assert report["outcomes"]["wait"] == 10
    assert not score(*batch(successful=189))["pilot_performance_pass"]
    assert score(*batch(successful=198))["target_success_rate_met"]


def test_missing_and_duplicate_samples_cannot_improve_success():
    plan, results = batch()
    assert score(plan, results[:185])["successful_replies"] == 185
    assert score(plan, results[:185])["outcomes"]["missing"] == 15
    with pytest.raises(ValueError, match="identity_conflict"):
        score(plan, results + [results[0]])


@pytest.mark.parametrize("change", [{"outcome": "accepted"}, {"outcome": "error"}, {"outcome": "unknown"},
    {"authority_valid": False}, {"content_valid": False}, {"receipt_refs": []},
    {"verified_at": None}, {"verified_segments": 0}, {"required_segments": 2}])
def test_ack_error_or_partial_delivery_is_not_a_reply(change):
    plan, results = batch(n=1, successful=1)
    assert not score(plan, [results[0] | change])["pilot_performance_pass"]


def test_nearest_rank_p95_and_non_200_batch_thresholds():
    plan, results = batch(n=20, successful=20)
    start = datetime.fromisoformat(results[0]["received_at"])
    results[-1]["verified_at"] = (start + timedelta(seconds=55)).isoformat()
    report = score(plan, results)
    assert report["p95_seconds"] == 29 and report["max_seconds"] == 55
    results[-2]["verified_at"] = (start + timedelta(seconds=31)).isoformat()
    assert not score(plan, results)["pilot_performance_pass"]
    assert score(*batch(n=20, successful=19))["pilot_performance_pass"]
    assert not score(*batch(n=20, successful=18))["pilot_performance_pass"]


def test_planned_wait_and_tool_tasks_report_separately():
    plan, results = batch(n=1, successful=1)
    plan += [{"request_id": "wait", "category": "ordinary_text", "expected": "wait"},
             {"request_id": "job", "category": "tool_task", "expected": "task_result"}]
    report = score(plan, results)
    assert report["ordinary_requests"] == 1 and len(report["separate_scenarios"]) == 2
    assert len(report["plan_sha256"]) == len(report["observations_sha256"]) == 64
