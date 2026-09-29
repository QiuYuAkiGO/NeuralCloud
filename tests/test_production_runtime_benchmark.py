import asyncio

import pytest

from tools.benchmark_production_runtime import benchmark, distribution, request_timeout_budget


def test_distribution_uses_nearest_rank_and_empty_is_explicit():
    assert distribution([])["p99"] is None
    assert distribution(range(1, 101))["p99"] == 99


def test_runtime_benchmark_runs_real_loop_and_control_without_network(tmp_path):
    result = asyncio.run(benchmark(tmp_path / "state", heartbeats=3, heartbeat_seconds=.05,
        model_delay=2, pause_trials=2))
    assert result["synthetic_only"]
    assert result["heartbeat_deadline_to_dispatch_ms"]["count"] >= 3
    assert result["heartbeat_under_active_model_ms"]["count"] >= 1
    assert result["pause_request_to_block_ms"]["count"] == 2
    assert result["admission_stayed_blocked"]
    assert result["maximum_active_model_calls"] <= 2
    assert not result["failed_events"]
    assert result["measurement_completed"] and result["benchmark_error"] is None
    assert result["cleanup_error"] is None and result["model_workflow_contract_met"]
    assert result["observed_max_model_calls_per_event"] == 1
    assert not result["linux_acceptance_sample_eligible"]
    assert (tmp_path / "state/runtime/production.sqlite3").exists()


def test_runtime_benchmark_refuses_existing_state(tmp_path):
    with pytest.raises(FileExistsError):
        asyncio.run(benchmark(tmp_path, heartbeats=1, heartbeat_seconds=.01, model_delay=.1))


def test_request_deadline_covers_serial_work_in_both_slots_and_entire_backlog():
    budget = request_timeout_budget(30)
    assert budget["seconds"] == 330
    assert budget["serial_model_calls_allowed"] == 10
    assert budget["model_calls_per_event"] == 1 and budget["includes_queue_wait"]


def test_expired_queued_request_returns_partial_failure_instead_of_waiting_for_heartbeats(tmp_path, monkeypatch):
    from tools import benchmark_production_runtime as tool

    def old_short_budget(delay):
        return {**request_timeout_budget(delay), "seconds": delay * 3}

    monkeypatch.setattr(tool, "request_timeout_budget", old_short_budget)

    async def run():
        # The old 3 * delay request TTL expires after queue wait + one call.
        # Without unexpected-lifecycle detection this waits over 500 seconds.
        return await asyncio.wait_for(benchmark(tmp_path / "short-ttl", heartbeats=10000,
            heartbeat_seconds=.05, model_delay=.3, pause_trials=1), timeout=15)

    result = asyncio.run(run())
    assert not result["target_met"] and not result["measurement_completed"]
    assert not result["linux_acceptance_sample_eligible"]
    assert result["benchmark_error"]["stage"] == "heartbeat_collection"
    assert result["benchmark_error"]["reason"].startswith("benchmark_unexpected_lifecycle:")
    assert result["heartbeat_deadline_to_dispatch_ms"]["count"] < 10000
    assert result["pause_request_to_block_ms"]["count"] == 0
    assert result["cleanup_error"] is None
