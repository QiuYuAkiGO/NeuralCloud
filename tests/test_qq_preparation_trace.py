"""Preparation timing uses synthetic work and temporary evidence files only."""
from contextlib import contextmanager
from datetime import timedelta
import json

import pytest

from harness.qq.telemetry import QQDeadlineExpired, QQTurnTrace
from harness.qq.watcher import QQWatcher
from tests.test_qq_watcher import NOW, setup as setup
from tests.test_qq_watcher_unattended import scenario as scenario


class Clock:
    value = 100.0

    def __call__(self):
        return self.value


def measured_preparation(clock):
    source = QQTurnTrace(first_seen_at=NOW, first_seen_mono=clock(), clock=clock)
    source.add_counter("body_capture", 2)
    for _ in range(2):
        with source.stage("body_capture"):
            clock.value += .25
    source.record("observation_completed")
    return source


def test_shared_preparation_offsets_ids_and_same_name_requests_are_preserved_once():
    clock = Clock()
    source = measured_preparation(clock)
    targets = [QQTurnTrace(event_id=f"synthetic:{index}", first_seen_at=NOW - timedelta(seconds=index),
                           first_seen_mono=100-index, clock=clock) for index in (1, 2)]
    for target in targets:
        assert target.merge_preparation(source)
        assert target.merge_preparation(source) is False
        value = target.snapshot()
        assert value["counters"]["body_capture"] == 2
        assert value["preparation_counters"] == {source.trace_id: {"body_capture": 2}}
        stages = value["stages"]
        assert [stage["source_stage_index"] for stage in stages] == [0, 1, 2]
        assert [stage["source_start_offset_ms"] for stage in stages] == [0, 250, 500]
        assert all(stage["source_preparation_id"] == source.trace_id for stage in stages)
    first, second = [target.snapshot()["stages"] for target in targets]
    assert second[0]["start_offset_ms"] - first[0]["start_offset_ms"] == 1000
    assert first[0]["duration_ms"] == first[1]["duration_ms"] == 250
    value["preparation_counters"][source.trace_id]["body_capture"] = 999
    assert targets[-1].snapshot()["preparation_counters"][source.trace_id]["body_capture"] == 2


def test_real_second_preparation_is_new_work_and_overflow_is_explicit():
    clock = Clock()
    target = QQTurnTrace(event_id="synthetic:event", first_seen_mono=99, clock=clock, max_events=5)
    first = measured_preparation(clock)
    second = measured_preparation(clock)
    target.merge_preparation(first)
    target.merge_preparation(second)
    value = target.snapshot()
    assert len(value["preparation_counters"]) == 2
    assert value["counters"]["body_capture"] == 4
    assert value["counters"]["events_dropped"] == 1


def instrument(desktop, *, fail=None, holder=None):
    state = {"trace": None}
    original_capture, original_group = desktop.capture, desktop.verify_group

    @contextmanager
    def turn(*, deadline=None, trace=None):
        previous = state["trace"]
        state["trace"] = trace
        assert deadline is not None and trace is not None
        try:
            yield
        finally:
            state["trace"] = previous

    def capture():
        trace = state["trace"]
        with trace.stage("body_capture"):
            trace.add_counter("body_capture")
            if holder:
                holder[0].heartbeat()
            if fail == "capture":
                raise QQDeadlineExpired("synthetic private body failure")
            return original_capture()

    def group(**kwargs):
        trace = state["trace"]
        trace.add_counter("accessibility_projection")
        trace.add_counter("accessibility_fallback")
        trace.add_counter("synthetic_ax_unavailable")
        trace.add_counter("vision_calls")
        with trace.stage("vision"):
            if fail == "group":
                raise QQDeadlineExpired("synthetic private model failure")
        return original_group(**kwargs)

    def self_profile():
        trace = state["trace"]
        trace.add_counter("self_verification")
        if fail == "self":
            raise QQDeadlineExpired("synthetic private profile failure")

    desktop.turn, desktop.capture, desktop.verify_group, desktop.verify_self = turn, capture, group, self_profile


@pytest.mark.parametrize("phase,stage", [("capture", "initial_capture"), ("group", "group_verification"),
                                        ("self", "initial_self_verification")])
def test_preparation_failures_are_separate_and_keep_calls_and_fixed_error(setup, phase, stage):
    settings, desktop, decide, calls, receipts = setup
    instrument(desktop, fail=phase)
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    with pytest.raises(QQDeadlineExpired):
        watcher.cycle()
    assert not calls and not receipts and not watcher._trace_file.exists()
    path = watcher._trace_file.with_suffix(".observation.jsonl")
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(records) == 1 and records[0]["event_id"] is None
    assert any(item["name"] == stage and item["outcome"] == "expired" for item in records[0]["stages"])
    assert records[0]["counters"]["body_capture"] == 1
    if phase != "capture":
        assert records[0]["counters"]["vision_calls"] == 1
        assert records[0]["counters"]["synthetic_ax_unavailable"] == 1
    status = watcher.status["preparation"]
    assert status["outcome"] == "expired" and status["last_error_type"] == "QQDeadlineExpired"
    assert "synthetic private" not in path.read_text() + json.dumps(watcher.status)


def test_baseline_preparation_has_live_status_but_never_a_candidate_trace(setup):
    settings, desktop, decide, calls, receipts = setup
    holder = []
    instrument(desktop, holder=holder)
    watcher = QQWatcher(settings, desktop=desktop, decide=decide)
    holder.append(watcher)
    watcher.cycle()
    status = json.loads(settings.status_file.read_text())
    assert status["preparation"]["outcome"] == "running"
    assert status["preparation"]["phase"] == "capture"
    assert status["preparation"]["counters"]["body_capture"] == 1
    assert watcher.status["preparation"]["outcome"] == "ok"
    assert not calls and not receipts and not watcher._trace_file.exists()


def test_old_baseline_uses_current_local_origin_but_new_change_keeps_old_first_seen(scenario):
    state = scenario
    state.watcher._observer_baselined = False
    state.watcher.ledger.start_run(now=state.clock.wall())
    state.clock.advance(120)
    state.watcher.cycle()
    assert state.watcher._observer_baselined
    rows = [json.loads(line) for line in state.watcher._trace_file.with_suffix(".observation.jsonl").read_text().splitlines()]
    assert rows[-1]["first_seen_at"] == state.clock.wall().isoformat()
    arrival = state.append("synthetic new candidate")
    state.clock.advance(61)
    captures = state.desktop.captures
    state.watcher.cycle()
    assert state.desktop.captures == captures + 1
    assert state.watcher._observer_sequence == arrival.sequence
    rows = [json.loads(line) for line in state.watcher._trace_file.with_suffix(".observation.jsonl").read_text().splitlines()]
    expired = [row for row in rows if row["counters"].get("expired_observations")]
    assert expired[-1]["first_seen_mono"] == arrival.first_seen_mono
    assert expired[-1]["observation_sequence"] == arrival.sequence
    assert not state.calls


def test_batch_links_shared_preparation_and_real_attribution_retry_is_separate(scenario):
    state = scenario
    instrument(state.desktop)
    state.watcher.cycle()
    state.append("synthetic first")
    state.append("synthetic second")
    state.desktop.fail_attribution = 1
    state.watcher.cycle()
    traces = list(state.watcher._traces.values())
    assert len(traces) == 2
    first_sources = [set(trace.snapshot()["preparation_counters"]) for trace in traces]
    assert first_sources[0] == first_sources[1]
    first_origin = traces[0].first_seen_mono
    state.clock.advance(1)
    state.watcher.cycle()
    assert traces[0].first_seen_mono == first_origin
    assert len(traces[0].snapshot()["preparation_counters"]) == 2
