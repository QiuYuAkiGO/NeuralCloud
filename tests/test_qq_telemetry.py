"""No models, services or private state: synthetic clocks and temporary JSONL."""
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from harness.embodiment.providers.qq import QQCommunicationError
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace, classify_error


NOW = datetime(2026, 9, 11, 5, tzinfo=timezone.utc)


class Clock:
    def __init__(self, value=100):
        self.value = value

    def __call__(self):
        return self.value


def test_prior_emotion_phase_is_merged_once_on_owner_clock():
    clock = Clock()
    first = QQTurnTrace(event_id="event:synthetic", first_seen_at=NOW, clock=clock)
    with first.stage("emotion.appraisal"):
        clock.value += 2
        first.add_counter("emotion_appraisal_model_calls")
    prior = first.snapshot()
    clock.value += 1
    second = QQTurnTrace(event_id=first.event_id, first_seen_at=NOW, clock=clock)
    second.trace_id = first.trace_id
    with second.stage("cognitive.lock_wait"):
        clock.value += 0.5
    second.merge_prior_phase(prior)
    snapshot = second.snapshot()
    assert snapshot["elapsed_ms"] == 3500
    assert snapshot["counters"]["emotion_appraisal_model_calls"] == 1
    assert [(s["name"], s["start_offset_ms"], s["end_offset_ms"]) for s in snapshot["stages"]] == [
        ("emotion.appraisal", 0, 2000), ("cognitive.lock_wait", 3000, 3500)]
    with pytest.raises(ValueError, match="owner clock"):
        second.merge_prior_phase(prior)
    invalid = {**prior, "event_id": "different"}
    with pytest.raises(ValueError, match="binding"):
        second.merge_prior_phase(invalid)


def test_conversation_metric_key_is_account_scoped_and_immutable():
    trace = QQTurnTrace()
    key = trace.bind_conversation(group_id="10001", self_member_id="29999")
    assert trace.snapshot()["conversation_key"] == key
    assert "10001" not in key and "29999" not in key
    assert trace.bind_conversation(group_id="10001", self_member_id="29999") == key
    other = QQTurnTrace()
    assert other.bind_conversation(group_id="10001", self_member_id="38888") != key
    with pytest.raises(ValueError, match="identity"):
        trace.bind_conversation(group_id="10002", self_member_id="29999")


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


@pytest.mark.parametrize("code", [
    "capability_unavailable", "required_capability_unavailable", "chat_context_changed",
    "active_scope_changed", "scope_not_allowed", "request_expired", "request_time_invalid",
    "provider_activation_changed", "provider_not_activated", "driver_operation_busy",
])
def test_qq_port_errors_have_specific_content_free_categories(code):
    error = QQCommunicationError(code)
    error.args = ("PRIVATE_GROUP_TEXT",)
    trace = QQTurnTrace()
    with pytest.raises(QQCommunicationError):
        with trace.stage("segment_1_delivery"):
            raise error
    snapshot = trace.snapshot()
    assert snapshot["stages"][0]["error_type"] == code
    assert "PRIVATE_GROUP_TEXT" not in json.dumps(snapshot)


@pytest.mark.parametrize("code", ["PRIVATE_GROUP_TEXT", "driver_callback_failed", "unknown_bounded_code",
                                  "x" * 10000, None, ["PRIVATE_GROUP_TEXT"]])
def test_unknown_or_malformed_qq_error_codes_are_never_exposed(code):
    error = QQCommunicationError("PRIVATE_GROUP_TEXT")
    error.code = code
    trace = QQTurnTrace()
    with pytest.raises(QQCommunicationError):
        with trace.stage("segment_1_delivery"):
            raise error
    snapshot = trace.snapshot()
    assert snapshot["stages"][0]["error_type"] == "qq_transport_error"
    assert "PRIVATE_GROUP_TEXT" not in json.dumps(snapshot)


def test_unrelated_exception_cannot_supply_a_qq_telemetry_code():
    error = RuntimeError("PRIVATE_GROUP_TEXT")
    error.code = "capability_unavailable"
    assert classify_error(error) == "model_error"


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


def test_add_cache_usage_records_stage_counters_without_prompt_text():
    from harness.core.inference import TokenUsage

    trace = QQTurnTrace()
    trace.add_cache_usage("gate", TokenUsage(prompt_tokens=100, cache_hit_tokens=80, cache_miss_tokens=20, source="api"))
    trace.add_cache_usage("persona", TokenUsage(prompt_tokens=110, cache_hit_tokens=90, cache_miss_tokens=20, source="api"))
    snapshot = trace.snapshot()
    assert snapshot["counters"] == {
        "gate_cache_hit_tokens": 80, "gate_cache_miss_tokens": 20,
        "persona_cache_hit_tokens": 90, "persona_cache_miss_tokens": 20,
        "gate_prompt_tokens": 100, "gate_completion_tokens": 0,
        "gate_cache_known_prompt_tokens": 100, "gate_cache_unknown_prompt_tokens": 0,
        "gate_usage_calls": 1, "gate_cache_known_calls": 1,
        "persona_prompt_tokens": 110, "persona_completion_tokens": 0,
        "persona_cache_known_prompt_tokens": 110, "persona_cache_unknown_prompt_tokens": 0,
        "persona_usage_calls": 1, "persona_cache_known_calls": 1,
        "gate_input_api_calls": 1, "gate_output_api_calls": 0, "gate_api_usage_calls": 1,
        "persona_input_api_calls": 1, "persona_output_api_calls": 0, "persona_api_usage_calls": 1,
        "gate_estimated_prompt_tokens": 0, "gate_estimated_completion_tokens": 0,
        "persona_estimated_prompt_tokens": 0, "persona_estimated_completion_tokens": 0,
    }
    assert "cache_hit_tokens" in json.dumps(snapshot)
