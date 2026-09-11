"""Synthetic monitor lifecycle and report metrics; no background processes."""
from datetime import timedelta
import json

import pytest

from harness.qq import acceptance as module
from tests.test_qq_acceptance import NOW, MONO, add_turn, change_record, complete_samples, health, setup as setup, start


@pytest.mark.parametrize("build", [None, "synthetic-other-build"])
def test_fixed_worker_build_required_at_start_and_each_sample(setup, build):
    health(setup, NOW, build_hash=build)
    with pytest.raises(module.AcceptanceError, match="preflight"):
        start(setup)
    assert not setup.run_dir.exists()
    health(setup, NOW)
    start(setup)
    health(setup, NOW + timedelta(seconds=15), build_hash=build)
    sample = module.sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)
    assert "worker_build_changed" in sample["reasons"]
    assert module.report_run(setup.run_dir)["status"] == "FAIL"


@pytest.mark.parametrize("error", [OSError, KeyboardInterrupt])
def test_monitor_failure_is_private_durable_and_readable_as_fail(setup, monkeypatch, error):
    monkeypatch.setattr(module.time, "sleep", lambda _: (_ for _ in ()).throw(error("PRIVATE_DIAGNOSTIC")))
    with pytest.raises(error):
        module.run_monitor(setup)
    marker = (setup.run_dir / "monitor_failure.json").read_text()
    assert "PRIVATE_DIAGNOSTIC" not in marker
    result = module.report_run(setup.run_dir, write=True)
    assert result["status"] == "FAIL" and "monitor_failed" in result["reasons"]
    assert result["fully_automatic_e2e_pass"] is False


def test_old_run_refusal_does_not_write_failure_or_change_manifest(setup):
    start(setup)
    before = (setup.run_dir / "manifest.json").read_bytes()
    with pytest.raises(module.AcceptanceError, match="new_private"):
        module.run_monitor(setup)
    assert (setup.run_dir / "manifest.json").read_bytes() == before
    assert not (setup.run_dir / "monitor_failure.json").exists()


def test_stale_samples_cannot_remain_running_without_failure_marker(setup):
    start(setup)
    assert module.report_run(setup.run_dir, now=NOW + timedelta(seconds=20))["status"] == "RUNNING"
    result = module.report_run(setup.run_dir, now=NOW + timedelta(seconds=21))
    assert result["status"] == "FAIL" and "monitor_sampling_stale" in result["reasons"]


def test_latency_stage_and_work_summaries_deduplicate_snapshots(setup):
    start(setup)
    traces = []
    for index, seconds in enumerate((3, 10, 30)):
        trace = add_turn(setup, index, latency=seconds)
        trace["stages"].insert(0, {"name": "gate", "outcome": "ok", "duration_ms": seconds * 100,
                                    "start_offset_ms": 0, "end_offset_ms": seconds * 100})
        trace["counters"].update(model_calls=2, gate_model_calls=1, persona_model_calls=1, precheck_retries=index)
        traces.append(trace)
    # Replace the synthetic fixture stream with valid complete snapshots, then repeat a flush.
    setup.trace_file.write_text("".join(json.dumps(trace) + "\n" for trace in traces + [traces[-1]]))
    complete_samples(setup)
    result = module.report_run(setup.run_dir)
    assert result["latency_seconds"]["warm_speak"] == {"count": 3, "p50": 10, "p95": 30, "max": 30}
    assert result["p95_seconds"]["warm_speak"] == 30
    assert result["stage_seconds"]["gate"] == {"count": 3, "p50": 1, "p95": 3, "max": 3}
    assert result["work_counters"]["model_calls"] == 6
    assert result["work_counters"]["precheck_retries"] == 3
    assert all(len(ref) == 64 for turn in result["turns"] for ref in turn.get("evidence_reference_hashes", []))
    assert "bubble-evidence" not in json.dumps(result)


@pytest.mark.parametrize("changes,reason", [
    ({"click_timing_required": False}, "actual_click_timing_missing"),
    ({"click_acknowledged_at": None}, "actual_click_timing_missing"),
    ({"click_observed_at": (NOW + timedelta(seconds=103)).isoformat()}, "actual_click_timing_missing"),
    ({"click_acknowledged_at": (NOW + timedelta(seconds=101)).isoformat()}, "actual_click_timing_order_invalid"),
    ({"click_acknowledged_at": (NOW + timedelta(seconds=140)).isoformat()}, "actual_click_timing_order_invalid"),
    ({"click_acknowledged_at": None, "click_observed_at": (NOW + timedelta(seconds=103)).isoformat(),
      "click_observed_evidence_ref": "unlinked-ref"}, "actual_click_observation_unlinked"),
])
def test_new_sent_samples_require_one_exact_actual_click_bound(setup, changes, reason):
    start(setup)
    add_turn(setup, 0)
    change_record(setup.ledger_db, "qq_watch_sends", "reservation_id", {"reservation_id": "synthetic-send:0", **changes})
    complete_samples(setup)
    result = module.report_run(setup.run_dir)
    assert result["status"] == "FAIL" and reason in result["reasons"]
    assert result["counts"]["warm_speak"] == 0


def test_observed_click_bound_requires_linked_fresh_bubble_evidence(setup):
    start(setup)
    add_turn(setup, 0)
    change_record(setup.ledger_db, "qq_watch_sends", "reservation_id", {
        "reservation_id": "synthetic-send:0", "click_acknowledged_at": None,
        "click_observed_at": (NOW + timedelta(seconds=103)).isoformat(), "click_observed_evidence_ref": "bubble-evidence"})
    complete_samples(setup)
    result = module.report_run(setup.run_dir)
    assert result["counts"]["warm_speak"] == 1
    assert result["turns"][0]["click_timing_basis"] == "observed"
