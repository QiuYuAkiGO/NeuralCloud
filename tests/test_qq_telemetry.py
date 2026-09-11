"""No models, services or private state: synthetic clocks and temporary JSONL."""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace


NOW = datetime(2026, 9, 11, 5, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value


def test_first_seen_wall_age_is_converted_once_and_expiry_is_monotonic():
    clock = Clock()
    deadline = QQDeadline.from_first_seen(NOW - timedelta(seconds=15), seconds=60, now_at=NOW, clock=clock)
    assert deadline.deadline_mono == 145 and deadline.check() == 45
    clock.value += 44
    assert deadline.remaining() == 1
    clock.value += 1
    assert deadline.expired and deadline.remaining() == 0
    with pytest.raises(QQDeadlineExpired):
        deadline.check()


def test_old_first_seen_is_immediately_expired_and_future_wall_time_does_not_extend_budget():
    clock = Clock()
    old = QQDeadline.from_first_seen(NOW - timedelta(hours=8), seconds=60, now_at=NOW, clock=clock)
    future = QQDeadline.from_first_seen(NOW + timedelta(hours=8), seconds=60, now_at=NOW, clock=clock)
    assert old.expired and future.remaining() == 60


@pytest.mark.parametrize("seconds", [-1, float("nan"), float("inf"), True, "60"])
def test_invalid_deadline_budget_is_rejected(seconds):
    with pytest.raises(ValueError):
        QQDeadline.after(seconds)


def test_unzoned_first_seen_cannot_create_deadline():
    with pytest.raises(ValueError, match="aware"):
        QQDeadline.from_first_seen(datetime(2026, 9, 11), now_at=NOW)


def test_stages_record_offsets_duration_counters_and_original_first_seen():
    clock = Clock()
    trace = QQTurnTrace(event_id="event:synthetic", first_seen_at=NOW, clock=clock)
    clock.value += 1
    with trace.stage("gate"):
        trace.add_counter("gate_model_calls")
        clock.value += 2
    trace.bind_decision("qq-decision:synthetic")
    snapshot = trace.snapshot()
    assert snapshot["first_seen_at"] == NOW.isoformat()
    assert snapshot["first_seen_mono"] == 100 and snapshot["elapsed_ms"] == 3000
    assert snapshot["stages"] == [{"name": "gate", "outcome": "ok", "error_type": None,
        "start_offset_ms": 1000, "end_offset_ms": 3000, "duration_ms": 2000}]
    assert snapshot["counters"] == {"gate_model_calls": 1}
    assert snapshot["decision_id"] == "qq-decision:synthetic"
    snapshot["stages"][0]["name"] = "changed"
    snapshot["counters"]["gate_model_calls"] = 9
    assert trace.snapshot()["stages"][0]["name"] == "gate"
    assert trace.snapshot()["counters"]["gate_model_calls"] == 1


@pytest.mark.parametrize("error,outcome,category", [
    (QQDeadlineExpired("PRIVATE_GROUP_TEXT"), "expired", "deadline_expired"),
    (httpx.ReadTimeout("PRIVATE_GROUP_TEXT"), "error", "model_timeout"),
    (RuntimeError("PRIVATE_GROUP_TEXT"), "error", "model_error"),
    (ValueError("PRIVATE_GROUP_TEXT"), "error", "model_invalid"),
])
def test_errors_store_only_categories_and_preserve_exception(error, outcome, category):
    clock = Clock()
    trace = QQTurnTrace(clock=clock)
    with pytest.raises(type(error)):
        with trace.stage("persona"):
            clock.value += 1
            raise error
    snapshot = trace.snapshot()
    assert snapshot["stages"][0]["outcome"] == outcome
    assert snapshot["stages"][0]["error_type"] == category
    assert "PRIVATE_GROUP_TEXT" not in json.dumps(snapshot)


def test_event_budget_remains_bounded_and_reports_truncation():
    trace = QQTurnTrace(max_events=2)
    for _ in range(1000):
        trace.record("capture")
    snapshot = trace.snapshot()
    assert len(snapshot["stages"]) == 2 and snapshot["counters"]["events_dropped"] == 998


def test_snapshot_flush_is_jsonl_with_stable_trace_id_for_report_consumers(tmp_path):
    clock = Clock()
    trace = QQTurnTrace(event_id="event:synthetic", clock=clock)
    path = tmp_path / "turns.jsonl"
    trace.record("capture")
    trace.flush(path)
    clock.value += 2
    trace.add_counter("model_calls")
    trace.record("gate")
    trace.flush(path)
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2 and rows[0]["trace_id"] == rows[1]["trace_id"]
    assert rows[1]["elapsed_ms"] == 2000 and rows[1]["counters"]["model_calls"] == 1
    assert set(rows[1]) == {"schema_version", "trace_id", "event_id", "decision_id", "first_seen_at",
                           "first_seen_mono", "recorded_at", "elapsed_ms", "stages", "counters"}


@pytest.mark.parametrize("bad", ["member display name", "群聊原文", "a" * 161, "header\nbody"])
def test_raw_content_cannot_be_used_as_trace_identifier(bad):
    with pytest.raises(ValueError):
        QQTurnTrace(event_id=bad)


def test_bad_trace_categories_counters_and_identity_changes_are_rejected():
    trace = QQTurnTrace()
    with pytest.raises(ValueError):
        trace.record("group text with spaces")
    with pytest.raises(ValueError):
        trace.record("gate", error_type="PRIVATE_EXCEPTION_BODY")
    with pytest.raises(ValueError):
        trace.add_counter("calls", -1)
    with pytest.raises(ValueError):
        trace.add_counter("calls", True)
    trace.bind_decision("qq-decision:first")
    with pytest.raises(ValueError):
        trace.bind_decision("qq-decision:second")


def test_flush_does_not_create_missing_private_directory(tmp_path):
    trace = QQTurnTrace()
    with pytest.raises(ValueError):
        trace.flush(tmp_path / "missing" / "trace.jsonl")
    assert not (tmp_path / "missing").exists()
