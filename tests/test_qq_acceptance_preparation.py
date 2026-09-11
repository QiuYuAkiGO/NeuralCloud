"""Shared preparation is counted once, with no live evidence or model calls."""
from copy import deepcopy
import json

import pytest

from harness.qq import acceptance as module
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_acceptance import add_turn, complete_samples, setup as setup, start


SOURCE = "qq-trace:" + "a" * 32
SECOND = "qq-trace:" + "b" * 32


def shared_trace(source=SOURCE, *, offset=0):
    # Two equal-duration captures are distinct physical operations (source index).
    stage = {"name": "body_capture", "outcome": "ok", "error_type": None, "duration_ms": 100,
             "source_preparation_id": source}
    return {"counters": {"body_capture": 3, "vision_calls": 2, "model_calls": 1},
            "preparation_counters": {source: {"body_capture": 2, "vision_calls": 2}},
            "stages": [dict(stage, source_stage_index=index, source_start_offset_ms=index * 100,
                            source_end_offset_ms=(index + 1) * 100,
                            start_offset_ms=offset + index * 100, end_offset_ms=offset + (index + 1) * 100)
                       for index in range(2)] + [
                {"name": "body_capture", "duration_ms": 250, "outcome": "ok"}]}


def test_shared_counters_and_equal_named_stages_count_physical_work_once():
    stages, counts, sources = {}, {}, {}
    module._add_workload(shared_trace(), stages, counts, sources)
    module._add_workload(shared_trace(offset=500), stages, counts, sources)
    assert counts["body_capture"] == 4  # Shared two + one event-owned capture each.
    assert counts["vision_calls"] == 2 and counts["model_calls"] == 2
    assert stages["body_capture"] == [.25, .1, .1, .25]
    assert len(sources) == 1


def test_distinct_preparation_attempts_remain_distinct():
    stages, counts, sources = {}, {}, {}
    module._add_workload(shared_trace(), stages, counts, sources)
    module._add_workload(shared_trace(SECOND), stages, counts, sources)
    assert counts["vision_calls"] == 4 and counts["body_capture"] == 6
    assert len(stages["body_capture"]) == 6 and len(sources) == 2


def test_real_merge_preparation_contract_preserves_source_identity_across_origins():
    clock = [10.0]
    prep = QQTurnTrace(first_seen_mono=10, clock=lambda: clock[0])
    prep.add_counter("body_capture", 2)
    for _ in range(2):
        beginning = clock[0]
        clock[0] += .1
        prep.record("body_capture", started_mono=beginning)
    prep.record("initial_capture", started_mono=10)
    prep.record("initial_self_verification", started_mono=10.1)
    stages, counts, sources = {}, {}, {}
    for origin in (8, 9):
        event = QQTurnTrace(event_id=f"synthetic-event:{origin}", first_seen_mono=origin, clock=lambda: clock[0])
        event.add_counter("body_capture")
        assert event.merge_preparation(prep)
        module._add_workload(event.snapshot(), stages, counts, sources)
    assert counts["body_capture"] == 4 and len(stages["body_capture"]) == 2
    assert len(stages["initial_capture"]) == len(stages["initial_self_verification"]) == 1
    assert len(sources) == 1


@pytest.mark.parametrize("field", ["counter", "name", "outcome", "duration", "source_time"])
def test_conflicting_shared_source_fails_without_mutating_aggregates(field):
    stages, counts, sources = {}, {}, {}
    module._add_workload(shared_trace(), stages, counts, sources)
    before = deepcopy((stages, counts, sources))
    changed = shared_trace(offset=999)
    if field == "counter":
        changed["preparation_counters"][SOURCE]["vision_calls"] = 1
    elif field == "duration":
        changed["stages"][0]["duration_ms"] = 99
        changed["stages"][0]["source_end_offset_ms"] = 99
    elif field == "source_time":
        changed["stages"][0]["source_start_offset_ms"] = 1
        changed["stages"][0]["source_end_offset_ms"] = 101
    else:
        changed["stages"][0][field] = "vision" if field == "name" else "error"
    with pytest.raises(module.AcceptanceError, match="trace_preparation_changed"):
        module._add_workload(changed, stages, counts, sources)
    assert (stages, counts, sources) == before


@pytest.mark.parametrize("broken", ["unlinked", "duplicate_index", "negative_own", "boolean_count", "invalid_offset"])
def test_malformed_preparation_cannot_hide_or_duplicate_work(broken):
    trace = shared_trace()
    if broken == "unlinked":
        trace["preparation_counters"] = {}
    elif broken == "duplicate_index":
        trace["stages"][1]["source_stage_index"] = 0
    elif broken == "negative_own":
        trace["counters"]["body_capture"] = 1
    elif broken == "boolean_count":
        trace["preparation_counters"][SOURCE]["vision_calls"] = True
    else:
        trace["stages"][0]["source_start_offset_ms"] = float("nan")
    stages, counts, sources = {}, {}, {}
    with pytest.raises(module.AcceptanceError):
        module._add_workload(trace, stages, counts, sources)
    assert stages == counts == sources == {}


def test_report_deduplicates_preparation_but_retains_each_delivery_latency(setup):
    start(setup)
    traces = []
    for index in range(2):
        trace = add_turn(setup, index, latency=20 + index)
        prep = shared_trace(offset=index * 500)
        trace["stages"] = prep["stages"] + trace["stages"]
        trace["counters"].update(prep["counters"])
        trace["preparation_counters"] = prep["preparation_counters"]
        traces.append(trace)
    setup.trace_file.write_text("".join(json.dumps(trace) + "\n" for trace in traces + [traces[-1]]))
    complete_samples(setup)
    report = module.report_run(setup.run_dir)
    assert report["work_counters"]["body_capture"] == 4
    assert report["work_counters"]["vision_calls"] == 2
    assert report["stage_seconds"]["body_capture"]["count"] == 4
    assert report["physical_work_preparation_count"] == 1
    assert report["latency_seconds"]["warm_speak"]["count"] == 2
    assert report["latency_seconds"]["warm_speak"]["max"] == 21
    assert SOURCE not in json.dumps(report)


def test_later_snapshot_cannot_change_imported_counters(setup):
    start(setup)
    trace = add_turn(setup, 0)
    prep = shared_trace()
    trace["stages"] = prep["stages"] + trace["stages"]
    trace["counters"].update(prep["counters"])
    trace["preparation_counters"] = prep["preparation_counters"]
    later = deepcopy(trace)
    later["preparation_counters"][SOURCE]["vision_calls"] = 1
    setup.trace_file.write_text(json.dumps(trace) + "\n" + json.dumps(later) + "\n")
    _, problems = module._traces(setup.trace_file)
    assert "trace_identity_or_time_regressed" in problems
