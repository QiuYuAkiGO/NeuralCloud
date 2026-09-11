"""Independent preparation audit: synthetic files only, no monitor or desktop."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from harness.qq import acceptance as module
from tests.test_qq_acceptance import (
    MONO, NOW, add_turn, complete_samples, full_dataset, health,
    setup as setup, start,
)


def preparation(index=1, *, outcome="ok", sequence=1, offset=100, finish=3, counters=None):
    result = {
        "schema_version": "qq-turn-trace-v1", "trace_id": f"qq-trace:{index:032x}",
        "event_id": None, "decision_id": None,
        "first_seen_at": (NOW + timedelta(seconds=offset)).isoformat(), "first_seen_mono": MONO + offset,
        "recorded_at": (NOW + timedelta(seconds=offset + finish)).isoformat(), "elapsed_ms": finish * 1000,
        "counters": {"preparation_attempts": 1, "body_capture": 1, "vision_calls": 1, **(counters or {})},
        "stages": [{"name": "body_capture", "outcome": "ok", "error_type": None,
                    "start_offset_ms": 0, "end_offset_ms": 500, "duration_ms": 500}],
    }
    if sequence is not None:
        result["observation_sequence"] = sequence
    if outcome is not None:
        result["stages"].append({"name": "observation_completed", "outcome": outcome,
                                 "error_type": None if outcome == "ok" else "deadline_expired",
                                 "start_offset_ms": finish * 1000, "end_offset_ms": finish * 1000,
                                 "duration_ms": 0})
    return result


def write_preparations(config, *records):
    config.trace_file.with_suffix(".observation.jsonl").write_text(
        "".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")


def final_report(config):
    complete_samples(config)
    return module.report_run(config.run_dir, now=NOW + timedelta(seconds=module.DURATION_SECONDS))


def test_old_failure_is_baseline_and_successful_preparation_is_not_a_candidate(setup):
    old = preparation(offset=-5, outcome="error")
    write_preparations(setup, old)
    manifest = start(setup)
    fresh = preparation(2)
    write_preparations(setup, old, fresh)
    report = final_report(setup)
    assert manifest["baseline_preparation_trace_ids"] == [old["trace_id"]]
    assert report["status"] == "INSUFFICIENT_SAMPLES"
    assert report["counts"] == {"warm_speak": 0, "cold_speak": 0, "natural_wait": 0}
    assert report["preparation_outcomes"] == {"successful": 1}
    assert report["work_counters"]["body_capture"] == 1
    assert report["physical_work_preparation_count"] == 1
    assert report["stage_seconds"]["body_capture"] == {"count": 1, "p50": .5, "p95": .5, "max": .5}


@pytest.mark.parametrize("outcome", ["error", "expired", "cancelled"])
def test_bounded_recovery_retains_error_and_both_physical_attempts_without_failing(setup, outcome):
    start(setup)
    failed, recovered = preparation(outcome=outcome), preparation(2, finish=12)
    write_preparations(setup, failed, recovered)
    full_dataset(setup)
    report = final_report(setup)
    assert report["status"] == "PASS"
    assert report["preparation_outcomes"] == {"recovered_within_deadline": 1, "successful": 1}
    assert report["work_counters"]["body_capture"] == 2
    entry = report["preparation_attempts"][0]
    assert entry["recovery_trace_id"] == module._opaque(recovered["trace_id"])
    assert entry["outcome"] == outcome
    assert '"observation_sequence":' not in json.dumps(report)
    assert failed["trace_id"] not in json.dumps(report)


@pytest.mark.parametrize("change", ["sequence", "wall_origin", "mono_origin", "late", "earlier"])
def test_only_exact_original_observation_and_later_success_within_ttl_proves_recovery(setup, change):
    start(setup)
    failed, recovered = preparation(outcome="error"), preparation(2, finish=10)
    if change == "sequence":
        recovered["observation_sequence"] += 1
    elif change == "wall_origin":
        recovered["first_seen_at"] = (NOW + timedelta(seconds=101)).isoformat()
    elif change == "mono_origin":
        recovered["first_seen_mono"] += 1
    elif change == "late":
        recovered = preparation(2, finish=61)
    else:
        recovered = preparation(2, finish=2)
    write_preparations(setup, failed, recovered)
    report = final_report(setup)
    assert report["status"] == "FAIL" and "preparation_unrecovered" in report["reasons"]
    assert report["preparation_attempts"][0]["classification"] == "unrecovered_failure"


def test_unlinked_failure_is_explicit_diagnostic_not_a_fabricated_failed_candidate(setup):
    start(setup)
    write_preparations(setup, preparation(outcome="error", sequence=None), preparation(2, sequence=None))
    report = final_report(setup)
    assert report["status"] == "INSUFFICIENT_SAMPLES"
    assert report["preparation_outcomes"] == {"unlinked_failure": 1, "successful": 1}
    assert report["pending_turns"] == 0 and report["turns"] == []
    assert "unlinked_failures_are_diagnostic_only" in report["preparation_association_scope"]


@pytest.mark.parametrize("counter,reason,classification", [
    ("expired_observations", "preparation_expired", "expired"),
    ("expired_carry_origins", "preparation_expired", "expired"),
    ("unresolved_observation_baselines", "preparation_observation_lost", "lost_observation"),
])
def test_explicit_lost_or_expired_observations_never_become_recovered(setup, counter, reason, classification):
    start(setup)
    write_preparations(setup, preparation(outcome="expired", counters={counter: 1}), preparation(2, finish=10))
    report = final_report(setup)
    assert report["status"] == "FAIL" and reason in report["reasons"]
    assert report["preparation_attempts"][0]["classification"] == classification
    assert "recovery_trace_id" not in report["preparation_attempts"][0]
    assert report["work_counters"][counter] == 1 and not report["turns"]


def test_independent_and_two_imported_preparations_share_one_physical_fingerprint(setup):
    start(setup)
    raw = preparation()
    write_preparations(setup, raw, raw)  # Repeated snapshot is not another attempt.
    imported = module._independent_workload(raw)
    traces = []
    for index in range(2):
        trace = add_turn(setup, index, latency=20 + index, first_offset=100)
        trace["counters"].update(imported["counters"])
        trace["counters"]["body_capture"] += 1  # One event-owned capture each.
        trace["preparation_counters"] = imported["preparation_counters"]
        trace["stages"] = imported["stages"] + trace["stages"]
        traces.append(trace)
    setup.trace_file.write_text("".join(json.dumps(trace) + "\n" for trace in traces))
    report = final_report(setup)
    assert report["status"] == "INSUFFICIENT_SAMPLES"
    assert report["work_counters"]["body_capture"] == 3
    assert report["work_counters"]["vision_calls"] == 1
    assert report["stage_seconds"]["body_capture"]["count"] == 1
    assert report["physical_work_preparation_count"] == 1
    assert report["latency_seconds"]["warm_speak"]["count"] == 2


def test_conflicting_raw_and_merged_source_is_an_audit_failure(setup):
    start(setup)
    raw = preparation()
    write_preparations(setup, raw)
    trace = add_turn(setup, 0)
    imported = module._independent_workload(raw)
    imported["preparation_counters"][raw["trace_id"]]["vision_calls"] += 1
    trace["counters"].update(imported["counters"])
    trace["preparation_counters"] = imported["preparation_counters"]
    trace["stages"] = imported["stages"] + trace["stages"]
    setup.trace_file.write_text(json.dumps(trace) + "\n")
    assert "trace_preparation_changed" in final_report(setup)["reasons"]


@pytest.mark.parametrize("damage,reason", [
    ("stage_loss", "preparation_stages_dropped"),
    ("sequence_change", "preparation_trace_identity_or_time_regressed"),
    ("pending", "preparation_unterminated"),
    ("terminal_after_window", "preparation_completed_outside_run_window"),
])
def test_missing_or_inconsistent_terminal_evidence_fails(setup, damage, reason):
    start(setup)
    raw = preparation()
    records = [raw]
    if damage == "stage_loss":
        raw["counters"]["events_dropped"] = 1
    elif damage == "sequence_change":
        later = deepcopy(raw)
        later["observation_sequence"] = 2
        records.append(later)
    elif damage == "pending":
        records = [preparation(outcome=None)]
    else:
        records = [preparation(offset=module.DURATION_SECONDS - 2)]
    write_preparations(setup, *records)
    assert reason in final_report(setup)["reasons"]


def test_removed_baseline_or_previously_sampled_preparation_is_not_silent(setup):
    old = preparation(offset=-10)
    write_preparations(setup, old)
    start(setup)
    raw = preparation(2, offset=5)
    write_preparations(setup, old, raw)
    health(setup, NOW + timedelta(seconds=15))
    assert not module.sample_run(setup.run_dir, now=NOW + timedelta(seconds=15), monotonic=MONO + 15)["reasons"]
    write_preparations(setup)
    health(setup, NOW + timedelta(seconds=30))
    result = module.sample_run(setup.run_dir, now=NOW + timedelta(seconds=30), monotonic=MONO + 30)
    assert {"preparation_baseline_removed", "preparation_records_removed"}.issubset(result["reasons"])


@pytest.mark.parametrize("fault", [None, "unlinked", "wrong_trace", "transport", "expired"])
def test_capture_retry_health_tolerance_requires_linked_original_ttl_and_healthy_observer(setup, fault):
    start(setup)
    raw = preparation(offset=10, outcome="error", sequence=None if fault == "unlinked" else 1)
    if fault == "expired":
        raw["counters"]["expired_observations"] = 1
    write_preparations(setup, raw)
    moment = NOW + timedelta(seconds=15)
    health(setup, moment, state="retrying_capture", automatic_screen_capture=False, automatic_send=False,
           last_error_type="GroundingError", preparation={"trace_id": "wrong" if fault == "wrong_trace" else raw["trace_id"]})
    if fault == "transport":
        value = json.loads(setup.watcher_status_path.read_text())
        value["observer"]["last_error_type"] = "transport_error"
        setup.watcher_status_path.write_text(json.dumps(value))
    sample = module.sample_run(setup.run_dir, now=moment, monotonic=MONO + 15)
    if fault is None:
        assert sample["reasons"] == []
        write_preparations(setup, raw, preparation(2, offset=10, finish=12))
        assert "preparation_unrecovered" not in final_report(setup)["reasons"]
    else:
        assert "worker_flag_disabled" in sample["reasons"] and "worker_error" in sample["reasons"]
