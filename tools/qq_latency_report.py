"""Read-only QQ latency/coverage evidence; missing markers remain unknown.

Usage: python -m tools.qq_latency_report --trace direct.trace.jsonl --requests model_requests.jsonl
No model calls, tariff estimates, or success inferred from a send attempt.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re

from tools.qq_cache_report import _metrics, _sample

OBSERVATIONS = {"observed_history", "observed_own", "observed_read_only"}
SILENCE = {"natural_wait", "guard_wait", "decision_wait"}
NON_ERRORS = OBSERVATIONS | SILENCE | {"sent", "decision_replayed", "decision_not_sendable"}
COHORTS = ("ordinary_text", "images", "web", "high_queue")
CONVERSATION_KEY = re.compile(r"^qq-scope:[0-9a-f]{64}$")


def _time(value):
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo is not None else None
    except (ValueError, TypeError, AttributeError):
        return None


def _number(value):
    return float(value) if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


def read_rows(paths):
    rows, invalid = [], 0
    for path in paths:
        with Path(path).open(encoding="utf-8") as handle:
            for line in handle:
                try:
                    value = json.loads(line)
                except json.JSONDecodeError:
                    invalid += 1
                    continue
                if isinstance(value, dict):
                    rows.append(value)
                else:
                    invalid += 1
    return rows, invalid


def _latest(rows, identity, *, requests=False):
    chosen = {}
    for index, row in enumerate(rows):
        key = row.get(identity)
        if not isinstance(key, str) or not key:
            continue
        finished = row.get("record_type") == "model_request_finished"
        if requests and row.get("record_type") not in {"model_request_started", "model_request_finished"}:
            continue
        stamp = _time(row.get("completed_at") if requests else row.get("recorded_at"))
        stamp = stamp or _time(row.get("started_at") if requests else row.get("first_seen_at"))
        rank = (int(finished) if requests else 0, stamp.timestamp() if stamp else float("-inf"), index)
        if key not in chosen or rank > chosen[key][0]:
            chosen[key] = (rank, row)
    return [row for _, row in chosen.values()]


def _counters(row):
    return row.get("counters") if isinstance(row.get("counters"), dict) else {}


def _conversation_key(row):
    value = row.get("conversation_key")
    return value if isinstance(value, str) and CONVERSATION_KEY.fullmatch(value) else None


def _configuration(row):
    counters = _counters(row)
    modes = [name for name in ("legacy", "shadow", "coalesced")
             if type(counters.get("scheduler_mode_" + name)) is int and counters["scheduler_mode_" + name] == 1]
    mode = modes[0] if len(modes) == 1 and all(
        counters.get("scheduler_mode_" + name, 0) == 0 for name in ("legacy", "shadow", "coalesced")
        if name != modes[0]) else "unknown"
    images = [name for name in ("all", "one", "off")
              if type(counters.get("image_mode_" + name)) is int and counters["image_mode_" + name] == 1]
    image_mode = images[0] if len(images) == 1 and all(
        counters.get("image_mode_" + name, 0) == 0 for name in ("all", "one", "off")
        if name != images[0]) else "unknown"
    def flag(name):
        value = counters.get(name)
        return value if type(value) is int and value in {0, 1} else None
    return {"cognitive_configuration": (row.get("effective_configuration") or {}).get("version") or "unknown",
            "direct_scheduler": mode, "media_cache_enabled": flag("media_cache_enabled"),
            "media_parallel_enabled": flag("media_parallel_enabled"), "image_mode": image_mode}


def _configuration_id(value):
    return "qq-run-config:" + hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _marker(row, counter, stage):
    value = _number(_counters(row).get(counter))
    if value is not None:
        return value
    values = [_number(item.get("end_offset_ms")) for item in row.get("stages", [])
              if isinstance(item, dict) and item.get("name") == stage and item.get("outcome") == "ok"]
    return min((value for value in values if value is not None), default=None)


def _latencies(values):
    values = sorted(value for value in values if value is not None)
    return {"samples": len(values),
            "p50_ms": values[math.ceil(len(values) * .5) - 1] if values else None,
            "p95_ms": values[math.ceil(len(values) * .95) - 1] if values else None}


def _reason(row):
    terminal = row.get("termination")
    return terminal.get("reason") if isinstance(terminal, dict) else None


def _eligible(row):
    explicit = _counters(row).get("input_reply_eligible")
    if type(explicit) is int and explicit in {0, 1}:
        return bool(explicit)
    if _reason(row) in OBSERVATIONS:
        return False
    # A later failure alone does not prove this was a live send-enabled input.
    return None


def _memberships(row, high_queue_ms):
    counters = _counters(row)
    images, web = counters.get("input_has_images"), counters.get("web_requested")
    images = images if type(images) is int and images in {0, 1} else None
    web = web if type(web) is int and web in {0, 1} else None
    queue = _number(counters.get("protocol_queue_wait_ms"))
    result = []
    if images == 1:
        result.append("images")
    if web == 1:
        result.append("web")
    if queue is not None and queue >= high_queue_ms:
        result.append("high_queue")
    if images == 0 and web == 0 and queue is not None and queue < high_queue_ms:
        result.append("ordinary_text")
    return result


def _span_matches(span, row):
    # Never attribute a model call by wall-clock overlap, model name, or text.
    for key in ("event_id", "decision_id"):
        if row.get(key) and span.get(key) == row[key]:
            return True
    ids = {row.get("trace_id"), *row.get("preparation_counters", {})}
    return any(span.get(key) in ids for key in ("qq_trace_id", "trace_id", "correlation_id") if span.get(key))


def _request_metrics(rows):
    samples = []
    for row in rows:
        raw = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        samples.append(_sample(raw, usage_source=row.get("usage_source") or raw.get("source"),
            status=row.get("status", "unknown"), latencies=[row.get("latency_ms")]))
    return _metrics(samples)


def _emotion_metrics(rows):
    values = {}
    for stage in ("appraisal", "guidance"):
        names = ("model_calls", "results", "fallbacks", "reused_results")
        known = []
        for row in rows:
            counters = _counters(row)
            item = {name: counters.get(f"emotion_{stage}_{name}") for name in names}
            if all(type(value) is int and value >= 0 for value in item.values()):
                known.append(item)
        counts = {name: sum(item[name] for item in known) for name in names}
        resolutions = counts["results"] + counts["reused_results"]
        values[stage] = {**counts, "events_known": len(known), "events_unknown": len(rows) - len(known),
            "fallback_rate": counts["fallbacks"] / counts["results"] if counts["results"] else None,
            "reuse_rate": counts["reused_results"] / resolutions if resolutions else None}
    return values


def _summarize(rows, requests, *, min_hours, min_samples):
    counts = Counter({key: 0 for key in ("reply_verified", "fully_delivered", "partial_verified", "silence",
        "errors", "fallback", "fallback_unknown", "late", "unknown_delivery", "unknown_outcome",
        "first_verification_unknown", "terminal_samples")})
    first, full, moments = [], [], []
    model_rows = [span for span in requests if any(_span_matches(span, row) for row in rows)]
    phases = defaultdict(list)
    stage_times = defaultdict(list)
    for span in model_rows:
        phases[span.get("stage") or span.get("phase") or "unknown"].append(span)
    for row in rows:
        reason = _reason(row)
        counters = _counters(row)
        first_ms = _marker(row, "protocol_first_verified_segment_ms", "protocol.first_verified_segment")
        full_ms = _marker(row, "protocol_fully_verified_ms", "protocol.fully_verified")
        first.append(first_ms)
        full.append(full_ms)
        when = _time(row.get("first_seen_at"))
        if when is not None:
            moments.append(when)
        per_event_stages = Counter()
        for item in row.get("stages", []):
            duration = _number(item.get("duration_ms")) if isinstance(item, dict) else None
            if duration is not None and isinstance(item.get("name"), str):
                per_event_stages[item["name"]] += duration
        for name, duration in per_event_stages.items():
            stage_times[name].append(duration)
        counts["reply_verified"] += int(first_ms is not None)
        counts["fully_delivered"] += int(full_ms is not None)
        counts["partial_verified"] += int(first_ms is not None and full_ms is None)
        counts["silence"] += int(reason in SILENCE)
        counts["errors"] += int(reason is not None and reason not in NON_ERRORS)
        fallback_keys = ("turn_error_fallback", "error_fallback", "protocol_error_fallback", "reply_error_fallback")
        counts["fallback"] += int(any(_number(counters.get(key)) for key in fallback_keys))
        counts["fallback_unknown"] += int(not any(key in counters for key in fallback_keys))
        counts["late"] += int(any(item.get("outcome") == "late_result" for item in row.get("stages", [])
            if isinstance(item, dict)) or (first_ms is not None and first_ms > 60_000))
        counts["unknown_delivery"] += int(reason == "direct_delivery_unknown")
        counts["unknown_outcome"] += int(reason is None)
        counts["first_verification_unknown"] += int(first_ms is None and reason not in SILENCE)
        counts["terminal_samples"] += int(reason is not None)
    hours = (max(moments) - min(moments)).total_seconds() / 3600 if len(moments) > 1 else 0
    missing = []
    if hours < min_hours:
        missing.append("observation_span_below_required_hours")
    if counts["terminal_samples"] < min_samples:
        missing.append("terminal_samples_below_required_count")
    if counts["reply_verified"] < min_samples:
        missing.append("verified_reply_samples_below_required_count")
    if counts["fully_delivered"] < min_samples:
        missing.append("fully_verified_samples_below_required_count")
    configurations = Counter((row.get("effective_configuration") or {}).get("version") or "unknown" for row in rows)
    if configurations.get("unknown"):
        missing.append("effective_configuration_missing")
    if len(set(configurations) - {"unknown"}) > 1:
        missing.append("mixed_effective_configurations")
    direct_configurations = {_configuration_id({key: value for key, value in _configuration(row).items()
                                              if key != "cognitive_configuration"}) for row in rows}
    if any(config["direct_scheduler"] == "unknown" or config["media_cache_enabled"] is None
           or config["media_parallel_enabled"] is None or config["image_mode"] == "unknown"
           for config in map(_configuration, rows)):
        missing.append("direct_configuration_missing")
    if len(direct_configurations) > 1:
        missing.append("mixed_direct_configurations")
    total = len(rows)
    return {"eligible_events": total, "observation_span_hours": round(hours, 6),
            "evidence_status": "pending" if missing else "ready_for_review", "pending_reasons": missing,
            "received_to_first_verified": _latencies(first), "received_to_fully_verified": _latencies(full),
            "outcomes": dict(counts), "reply_coverage": counts["reply_verified"] / total if total else None,
            "full_delivery_coverage": counts["fully_delivered"] / total if total else None,
            "reason_counts": dict(Counter(_reason(row) or "unknown" for row in rows)),
            "configuration_versions": dict(configurations),
            "stage_duration_per_event": {name: _latencies(values) for name, values in sorted(stage_times.items())},
            "model_requests": _request_metrics(model_rows),
            "model_phases": {name: _request_metrics(values) for name, values in sorted(phases.items())},
            "emotion_stages": _emotion_metrics(rows),
            "usage_evidence_status": "available" if model_rows else "pending_no_attributed_physical_requests",
            "model_join_coverage": {
                "events_with_request_identity_match": sum(any(_span_matches(span, row) for span in requests) for row in rows),
                "events": total},
            "quality_status": "pending_natural_canary_persona_review"}


def summarize(trace_rows, request_rows=(), *, high_queue_ms=5000, min_hours=24, min_samples=100,
              conversation_keys=None, configuration_versions=None):
    conversation_keys = set(conversation_keys or [])
    configuration_versions = set(configuration_versions or [])
    if any(not isinstance(key, str) or not CONVERSATION_KEY.fullmatch(key) for key in conversation_keys):
        raise ValueError("invalid conversation key")
    traces = _latest(trace_rows, "trace_id")
    all_event_traces = [row for row in traces if isinstance(row.get("event_id"), str)]
    event_traces = [row for row in all_event_traces
                    if (not conversation_keys or _conversation_key(row) in conversation_keys)
                    and (not configuration_versions or
                         _configuration(row)["cognitive_configuration"] in configuration_versions)]
    # A revised/retried logical event keeps one coverage denominator. Request
    # attempts and older terminal reasons remain separately inspectable.
    events = _latest(event_traces, "event_id")
    requests = _latest(request_rows, "span_id", requests=True)
    requests_before_filter = len(requests)
    if conversation_keys or configuration_versions:
        requests = [span for span in requests if any(_span_matches(span, row) for row in event_traces)]
    eligible = [row for row in events if _eligible(row) is True]
    cohorts = {name: [row for row in eligible if name in _memberships(row, high_queue_ms)] for name in COHORTS}
    assigned = [span for span in requests if any(_span_matches(span, row) for row in eligible)]
    groups = defaultdict(list)
    group_descriptions = {}
    for row in eligible:
        description = _configuration(row)
        identity = _configuration_id(description)
        groups[identity].append(row)
        group_descriptions[identity] = description
    def cohort_view(rows):
        return {name: _summarize([row for row in rows if name in _memberships(row, high_queue_ms)], requests,
                                min_hours=min_hours, min_samples=min_samples) for name in COHORTS}
    return {"schema_version": "qq-latency-report-v1",
            "requirements": {"minimum_observation_hours": min_hours, "minimum_samples_per_cohort": min_samples,
                             "high_queue_ms": high_queue_ms},
            "filters": {"conversation_keys": sorted(conversation_keys),
                "cognitive_configuration_versions": sorted(configuration_versions),
                "excluded_traces": len(all_event_traces) - len(event_traces),
                "excluded_model_requests": requests_before_filter - len(requests),
                "input_traces_without_conversation_key": sum(_conversation_key(row) is None for row in all_event_traces)},
            "deduplication": {"trace_snapshots": len(trace_rows), "unique_traces": len(traces),
                "unique_events": len(events), "repeated_event_attempts": len(event_traces) - len(events),
                "physical_model_requests": len(requests)},
            "unclassified": {"eligibility_unknown": sum(_eligible(row) is None for row in events),
                "observations_excluded": sum(_eligible(row) is False for row in events),
                "cohort_unknown": sum(not _memberships(row, high_queue_ms) for row in eligible),
                "conversation_unknown": sum(_conversation_key(row) is None for row in events),
                "unattributed_model_requests": len(requests) - len(assigned)},
            "totals": _summarize(eligible, requests, min_hours=min_hours, min_samples=min_samples),
            "cohorts": {name: _summarize(rows, requests, min_hours=min_hours, min_samples=min_samples)
                        for name, rows in cohorts.items()},
            "configuration_groups": {identity: {"configuration": group_descriptions[identity],
                "totals": _summarize(rows, requests, min_hours=min_hours, min_samples=min_samples),
                "cohorts": cohort_view(rows)} for identity, rows in sorted(groups.items())},
            "all_physical_model_requests": _request_metrics(requests),
            "attempt_reason_counts": dict(Counter(_reason(row) or "unknown" for row in event_traces)),
            "limitations": [
                "Cohorts may overlap; their totals must not be added together.",
                "Missing eligibility, cohort, verification and usage markers remain unknown.",
                "Cache unknown is not a miss; token coverage is null when input API usage is incomplete.",
                "A terminal sent state alone cannot reconstruct the time of verified receipt.",
                "24 hours is the observed first-seen span, not proof of gap-free collection.",
                "Nested stage durations are not additive; configuration changes block pooled cohort readiness.",
                "Configuration groups include Cognitive version and Direct scheduler/media flags; unknown flags block readiness.",
                "Conversation filters match only the recorded opaque key; old missing keys are never inferred.",
                "Emotion counters describe work observed in each latest event trace; absent stages remain unknown.",
                "Emotion fallback rates use newly committed results; reusing a prior fallback is not another fallback or model call.",
                "Evidence readiness does not establish persona quality or performance improvement.",
                "Late means an explicit late-result stage or first verification beyond the original 60 seconds; fallback is separate."]}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, action="append", required=True)
    parser.add_argument("--requests", type=Path, action="append", default=[])
    parser.add_argument("--high-queue-ms", type=float, default=5000)
    parser.add_argument("--conversation-key", action="append", default=[])
    parser.add_argument("--configuration-version", action="append", default=[])
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if not math.isfinite(args.high_queue_ms) or args.high_queue_ms < 0:
        parser.error("--high-queue-ms must be finite and nonnegative")
    if any(not CONVERSATION_KEY.fullmatch(key) for key in args.conversation_key):
        parser.error("--conversation-key must be a recorded qq-scope: SHA-256 key")
    traces, invalid_traces = read_rows(args.trace)
    requests, invalid_requests = read_rows(args.requests)
    result = summarize(traces, requests, high_queue_ms=args.high_queue_ms,
                       conversation_keys=args.conversation_key, configuration_versions=args.configuration_version)
    result["invalid_jsonl_rows"] = {"traces": invalid_traces, "requests": invalid_requests}
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)


if __name__ == "__main__":
    main()
