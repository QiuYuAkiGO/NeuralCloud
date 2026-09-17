from datetime import UTC, datetime, timedelta
import json

from tools.qq_latency_report import main, summarize


def trace(number=1, *, when=None, first=1200, full=1800, reason="sent", images=0, web=0, queue=0,
          scheduler="shadow", media_cache=0, media_parallel=0, image_mode="all", conversation="qq-scope:" + "a" * 64):
    when = when or datetime(2026, 9, 16, tzinfo=UTC)
    counters = {"input_reply_eligible": 1, "input_has_images": images, "web_requested": web,
                "image_mode_" + image_mode: 1,
                "protocol_queue_wait_ms": queue, "protocol_error_fallback": 0,
                "scheduler_mode_" + scheduler: 1, "media_cache_enabled": media_cache,
                "media_parallel_enabled": media_parallel}
    if first is not None:
        counters["protocol_first_verified_segment_ms"] = first
    if full is not None:
        counters["protocol_fully_verified_ms"] = full
    return {"trace_id": f"trace-{number}", "event_id": f"event-{number}",
            "conversation_key": conversation,
            "first_seen_at": when.isoformat(), "recorded_at": (when + timedelta(seconds=2)).isoformat(),
            "effective_configuration": {"version": "qq-config:test"},
            "counters": counters, "stages": [], "termination": {"reason": reason, "stage": "protocol.delivery"}}


def span(number=1, *, finished=True, known=True):
    return {"span_id": f"model-{number}", "event_id": f"event-{number}",
            "record_type": "model_request_finished" if finished else "model_request_started",
            "status": "success" if finished else "running", "latency_ms": 100,
            "usage_source": "api", "usage": ({"prompt_tokens": 100, "completion_tokens": 20,
                "cache_hit_tokens": 60 if known else 0, "cache_miss_tokens": 40 if known else 0,
                "cache_status": "known" if known else "unknown", "prompt_tokens_source": "api",
                "completion_tokens_source": "api"} if finished else None)}


def test_latest_snapshot_and_physical_request_finish_are_counted_once():
    old = trace(first=None, full=None, reason=None)
    old["recorded_at"] = "2026-09-16T00:00:01+00:00"
    result = summarize([trace(), old, trace()], [span(finished=False), span(), span(), span(finished=False)])
    assert result["deduplication"]["unique_events"] == 1
    assert result["all_physical_model_requests"]["calls"] == 1
    assert result["totals"]["received_to_first_verified"]["p95_ms"] == 1200
    assert result["totals"]["model_requests"]["cache_hit_tokens"] == 60
    assert result["totals"]["evidence_status"] == "pending"


def test_coverage_includes_silence_errors_and_unknown_not_only_fast_replies():
    rows = [trace(), trace(2, first=None, full=None, reason="natural_wait"),
            trace(3, first=None, full=None, reason="model_timeout"),
            trace(4, first=61000, full=None, reason="direct_delivery_unknown")]
    rows[-1]["counters"]["protocol_error_fallback"] = 1
    totals = summarize(rows)["totals"]
    assert totals["reply_coverage"] == .5 and totals["full_delivery_coverage"] == .25
    assert totals["outcomes"]["silence"] == 1 and totals["outcomes"]["errors"] == 2
    assert totals["outcomes"]["fallback"] == totals["outcomes"]["late"] == 1
    assert totals["outcomes"]["unknown_delivery"] == totals["outcomes"]["partial_verified"] == 1
    assert totals["received_to_fully_verified"]["samples"] == 1


def test_missing_markers_cannot_become_zero_latency_or_plain_text_evidence():
    legacy = trace(first=None, full=None)
    legacy["counters"] = {}
    result = summarize([legacy], [span(2, finished=False)])
    assert result["unclassified"]["eligibility_unknown"] == 1
    assert result["cohorts"]["ordinary_text"]["eligible_events"] == 0
    assert result["totals"]["received_to_first_verified"]["p50_ms"] is None
    assert result["all_physical_model_requests"]["cache_coverage"] is None
    assert result["unclassified"]["unattributed_model_requests"] == 1


def test_unknown_usage_is_never_synthesized_as_cache_miss():
    result = summarize([trace(), trace(2)], [span(), span(2, known=False)])
    usage = result["totals"]["model_requests"]
    assert usage["cache_hit_tokens"] == 60 and usage["cache_miss_tokens"] == 40
    assert usage["cache_unknown_prompt_tokens"] == 100
    assert usage["cache_coverage"] == .5 and usage["hit_rate"] == .6


def test_each_cohort_has_independent_24_hour_and_100_verified_sample_gate():
    start = datetime(2026, 9, 14, tzinfo=UTC)
    rows = [trace(index, when=start + timedelta(minutes=15 * index)) for index in range(100)]
    result = summarize(rows)
    assert result["cohorts"]["ordinary_text"]["evidence_status"] == "ready_for_review"
    assert all(result["cohorts"][cohort]["evidence_status"] == "pending" for cohort in ("images", "web", "high_queue"))
    assert result["totals"]["quality_status"].startswith("pending")
    # A 100-row burst cannot satisfy the duration gate.
    assert summarize([trace(index) for index in range(100)])["totals"]["evidence_status"] == "pending"


def test_cohort_overlap_and_revisions_do_not_double_event_or_request_totals():
    row = trace(images=1, web=1, queue=5000)
    older = trace(first=None, full=None, reason="direct_request_revision_changed")
    older["trace_id"], older["recorded_at"] = "older", "2026-09-16T00:00:01+00:00"
    result = summarize([older, row], [span()])
    assert result["deduplication"]["repeated_event_attempts"] == 1
    assert result["totals"]["eligible_events"] == 1
    assert result["all_physical_model_requests"]["calls"] == 1
    for cohort in ("images", "web", "high_queue"):
        assert result["cohorts"][cohort]["eligible_events"] == 1
    assert result["cohorts"]["ordinary_text"]["eligible_events"] == 0


def test_shared_preparation_join_counts_one_physical_call():
    a, b = trace(), trace(2)
    a["preparation_counters"] = b["preparation_counters"] = {"shared": {"gate_model_calls": 1}}
    model = span()
    model.pop("event_id")
    model["qq_trace_id"] = "shared"
    result = summarize([a, b], [model])
    assert result["totals"]["model_requests"]["calls"] == 1
    assert result["totals"]["model_join_coverage"]["events_with_request_identity_match"] == 2


def test_runtime_correlation_uses_exact_direct_trace_identity():
    row, request = trace(), span()
    request["event_id"] = "runtime-generated-event"
    request["correlation_id"] = row["trace_id"]
    result = summarize([row], [request])
    assert result["totals"]["model_requests"]["calls"] == 1
    assert result["unclassified"]["unattributed_model_requests"] == 0


def test_configuration_rollout_cannot_pool_short_windows_into_a_ready_cohort():
    start = datetime(2026, 9, 14, tzinfo=UTC)
    rows = [trace(index, when=start + timedelta(minutes=15 * index)) for index in range(100)]
    rows[-1]["effective_configuration"]["version"] = "qq-config:new"
    result = summarize(rows)["cohorts"]["ordinary_text"]
    assert result["evidence_status"] == "pending"
    assert "mixed_effective_configurations" in result["pending_reasons"]


def test_emotion_metrics_deduplicate_snapshots_and_keep_reused_fallback_separate():
    old, latest, replay, missing = trace(), trace(), trace(2), trace(3)
    old["recorded_at"] = "2026-09-16T00:00:01+00:00"
    for stage in ("appraisal", "guidance"):
        for row in (old, latest):
            row["counters"].update({f"emotion_{stage}_model_calls": 1, f"emotion_{stage}_results": 1,
                f"emotion_{stage}_fallbacks": 1, f"emotion_{stage}_reused_results": 0})
        replay["counters"].update({f"emotion_{stage}_model_calls": 0, f"emotion_{stage}_results": 0,
            f"emotion_{stage}_fallbacks": 0, f"emotion_{stage}_reused_results": 1})
    result = summarize([old, latest, replay, missing])["totals"]["emotion_stages"]
    for stage in ("appraisal", "guidance"):
        assert result[stage] == {"model_calls": 1, "results": 1, "fallbacks": 1, "reused_results": 1,
            "events_known": 2, "events_unknown": 1, "fallback_rate": 1.0, "reuse_rate": .5}
    unknown = summarize([missing])["totals"]["emotion_stages"]["appraisal"]
    assert unknown["events_unknown"] == 1 and unknown["fallback_rate"] is None
    assert unknown["reuse_rate"] is None


def test_direct_modes_split_ready_cohorts_even_with_identical_cognitive_configuration():
    start = datetime(2026, 9, 14, tzinfo=UTC)
    rows = []
    for group, (scheduler, cache, parallel) in enumerate([
        ("legacy", 0, 0), ("coalesced", 0, 0), ("coalesced", 1, 0), ("coalesced", 1, 1),
    ]):
        rows.extend(trace(100 * group + index, when=start + timedelta(minutes=15 * index),
                          scheduler=scheduler, media_cache=cache, media_parallel=parallel) for index in range(100))
    result = summarize(rows)
    assert "mixed_direct_configurations" in result["totals"]["pending_reasons"]
    assert len(result["configuration_groups"]) == 4
    for group in result["configuration_groups"].values():
        assert group["configuration"]["cognitive_configuration"] == "qq-config:test"
        assert group["totals"]["eligible_events"] == 100
        assert group["cohorts"]["ordinary_text"]["evidence_status"] == "ready_for_review"
        assert group["cohorts"]["images"]["evidence_status"] == "pending"


def test_missing_direct_configuration_blocks_readiness_instead_of_guessing_legacy():
    row = trace()
    for key in ("scheduler_mode_shadow", "media_cache_enabled", "media_parallel_enabled"):
        row["counters"].pop(key)
    result = summarize([row], min_hours=0, min_samples=1)
    assert result["totals"]["pending_reasons"] == ["direct_configuration_missing"]
    group = next(iter(result["configuration_groups"].values()))
    assert group["configuration"]["direct_scheduler"] == "unknown"
    assert group["configuration"]["media_cache_enabled"] is None


def test_image_policy_changes_cannot_be_pooled_as_same_configuration():
    rows = [trace(index, image_mode=mode) for index, mode in enumerate(("all", "one", "off"))]
    result = summarize(rows, min_hours=0, min_samples=1)
    assert "mixed_direct_configurations" in result["totals"]["pending_reasons"]
    assert {group["configuration"]["image_mode"] for group in result["configuration_groups"].values()} == {"all", "one", "off"}
    assert all(group["totals"]["evidence_status"] == "ready_for_review"
               for group in result["configuration_groups"].values())
    rows[0]["counters"].pop("image_mode_all")
    missing = summarize(rows[:1], min_hours=0, min_samples=1)
    assert "direct_configuration_missing" in missing["totals"]["pending_reasons"]
    assert next(iter(missing["configuration_groups"].values()))["configuration"]["image_mode"] == "unknown"


def test_conversation_and_version_filters_use_recorded_keys_and_exact_span_identity():
    key = "qq-scope:" + "a" * 64
    a, b, missing, changed = trace(), trace(2, conversation="qq-scope:" + "b" * 64), trace(3), trace(4)
    missing.pop("conversation_key")
    changed["effective_configuration"]["version"] = "qq-config:new"
    result = summarize([a, b, missing, changed], [span(i) for i in range(1, 5)],
                       conversation_keys=[key], configuration_versions=["qq-config:test"])
    assert result["totals"]["eligible_events"] == 1
    assert result["all_physical_model_requests"]["calls"] == 1
    assert result["filters"] == {"conversation_keys": [key], "cognitive_configuration_versions": ["qq-config:test"],
        "excluded_traces": 3, "excluded_model_requests": 3, "input_traces_without_conversation_key": 1}
    assert summarize([missing])["unclassified"]["conversation_unknown"] == 1
    assert summarize([missing], conversation_keys=[key])["totals"]["eligible_events"] == 0


def test_cli_accepts_exact_canary_key_and_configuration_filter(tmp_path, monkeypatch):
    key = "qq-scope:" + "a" * 64
    inputs, output = tmp_path / "traces.jsonl", tmp_path / "report.json"
    inputs.write_text(json.dumps(trace()) + "\n" + json.dumps(trace(2, conversation=None)), encoding="utf-8")
    monkeypatch.setattr("sys.argv", ["qq_latency_report", "--trace", str(inputs), "--output", str(output),
        "--conversation-key", key, "--configuration-version", "qq-config:test"])
    main()
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["totals"]["eligible_events"] == 1
    assert result["filters"]["input_traces_without_conversation_key"] == 1
