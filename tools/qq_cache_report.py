"""Read-only cache accounting. Prefer --requests for physical model calls.

Unknown cache usage is never a cache miss. Historical traces and monitor rows
remain readable, but cannot supply model identity or missing token counts.
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import sqlite3


def _count(value) -> int:
    return value if type(value) is int and value >= 0 else 0


def _hour_key(iso: str) -> str:
    try:
        moment = datetime.fromisoformat(iso.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            return "unknown_timezone"
        return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")
    except (ValueError, TypeError, AttributeError):
        return "unknown"


def _jsonl(path: Path):
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, dict):
                yield row


def _load_traces(path: Path) -> list[dict]:
    latest: dict[str, dict] = {}
    for row in _jsonl(path):
        if isinstance(row.get("trace_id"), str):
            latest[row["trace_id"]] = row
    return list(latest.values())


def _usage(raw: dict) -> dict:
    """Normalize legacy positive partitions; legacy zeroes remain unknown."""
    prompt = _count(raw.get("prompt_tokens"))
    hit, miss = _count(raw.get("cache_hit_tokens")), _count(raw.get("cache_miss_tokens"))
    known = raw.get("cache_known_prompt_tokens")
    if type(known) is not int or known < 0:
        known = hit + miss
    if known > prompt or hit + miss != known:
        known = hit = miss = 0
    return {"prompt_tokens": prompt, "completion_tokens": _count(raw.get("completion_tokens")),
            "cache_hit_tokens": hit, "cache_miss_tokens": miss,
            "cache_known_prompt_tokens": known, "cache_unknown_prompt_tokens": max(0, prompt - known)}


def _sample(raw: dict, *, calls: int = 1, usage_calls: int | None = None, **metadata) -> dict:
    raw = raw if isinstance(raw, dict) else {}
    usage_calls = int(bool(raw)) if usage_calls is None else usage_calls
    source = metadata.get("usage_source") or raw.get("source") or "unknown"
    input_source = raw.get("prompt_tokens_source") or (source if "prompt_tokens" in raw else "unknown")
    output_source = raw.get("completion_tokens_source") or (source if "completion_tokens" in raw else "unknown")
    input_api_calls = usage_calls if input_source == "api" else 0
    output_api_calls = usage_calls if output_source == "api" else 0
    cache = _usage(raw)
    known_calls = (calls if raw.get("cache_status") == "known" or
                   (cache["cache_known_prompt_tokens"] > 0 and not cache["cache_unknown_prompt_tokens"]) else 0)
    return {**cache, "calls": calls, "usage_calls": usage_calls,
            "api_usage_calls": usage_calls if "api" in {input_source, output_source} else 0,
            "input_api_calls": input_api_calls, "output_api_calls": output_api_calls,
            "estimated_prompt_tokens": cache["prompt_tokens"] if input_source == "estimate" else 0,
            "estimated_completion_tokens": cache["completion_tokens"] if output_source == "estimate" else 0,
            "cache_known_calls": known_calls,
            "input_usage_unknown_calls": max(0, calls - input_api_calls),
            "output_usage_unknown_calls": max(0, calls - output_api_calls),
            "usage_source": source, "latencies": [], **metadata}


_SUM_FIELDS = ("calls", "usage_calls", "api_usage_calls", "input_api_calls", "output_api_calls",
               "estimated_prompt_tokens", "estimated_completion_tokens", "cache_known_calls", "input_usage_unknown_calls",
               "output_usage_unknown_calls", "prompt_tokens", "completion_tokens",
               "cache_hit_tokens", "cache_miss_tokens", "cache_known_prompt_tokens", "cache_unknown_prompt_tokens")


def _metrics(samples: list[dict]) -> dict:
    totals = {key: sum(_count(row.get(key)) for row in samples) for key in _SUM_FIELDS}
    prompt, known = totals["prompt_tokens"], totals["cache_known_prompt_tokens"]
    totals["cache_unknown_calls"] = totals["calls"] - totals["cache_known_calls"]
    for field in ("api_usage", "input_api", "output_api"):
        totals[field + "_call_coverage"] = totals[field + "_calls"] / totals["calls"] if totals["calls"] else None
    totals["hit_rate"] = totals["cache_hit_tokens"] / known if known else None
    totals["cache_coverage_of_recorded_input"] = known / prompt if prompt else None
    totals["cache_coverage"] = (known / prompt if prompt and not totals["input_usage_unknown_calls"] else None)
    totals["prompt_tokens_complete"] = totals["input_usage_unknown_calls"] == 0
    totals["completion_tokens_complete"] = totals["output_usage_unknown_calls"] == 0
    totals["total_tokens"] = prompt + totals["completion_tokens"]
    latency = sorted(float(value) for row in samples for value in row.get("latencies", [])
                     if type(value) in (int, float) and math.isfinite(value) and value >= 0)
    totals["latency_ms"] = {"samples": len(latency), "total": round(sum(latency), 3),
                            "mean": round(sum(latency) / len(latency), 3) if latency else None,
                            "p50": latency[math.ceil(len(latency) * .5) - 1] if latency else None,
                            "p95": latency[math.ceil(len(latency) * .95) - 1] if latency else None}
    totals["status_counts"] = dict(Counter(row.get("status", "unknown") for row in samples))
    # Prices vary by model, provider and time. There is no implicit tariff.
    totals["estimated_usd"] = None
    totals["cost_status"] = "not_estimated_no_explicit_pricing"
    return totals


def _report(samples: list[dict], *, turns: int | None = None, source: str) -> dict:
    hours, groups = defaultdict(list), defaultdict(list)
    dimensions = ("stage", "request_model", "response_model", "role", "usage_source")
    for sample in samples:
        hours[_hour_key(sample.get("started_at", ""))].append(sample)
        groups[tuple(sample.get(key) or "unknown" for key in dimensions)].append(sample)
    totals = _metrics(samples)
    if turns is not None:
        totals["turns"] = turns
    return {"schema_version": "cache-report-v2", "source": source,
            "totals": totals, "hours": {hour: _metrics(rows) for hour, rows in sorted(hours.items())},
            "groups": [{**dict(zip(dimensions, key)), **_metrics(rows)} for key, rows in sorted(groups.items())]}


def summarize_requests(path: Path) -> dict:
    # A span is one physical attempt. Replayed terminal lines must not double
    # count it, and an interrupted start remains an unknown-usage call.
    latest: dict[str, dict] = {}
    for row in _jsonl(path):
        kind, span = row.get("record_type"), row.get("span_id")
        if kind not in {"model_request_started", "model_request_finished"} or not isinstance(span, str):
            continue
        if kind == "model_request_finished" or span not in latest:
            latest[span] = row
    samples = []
    for row in latest.values():
        raw = row.get("usage") if isinstance(row.get("usage"), dict) else {}
        samples.append(_sample(raw, started_at=row.get("started_at", ""),
            stage=row.get("stage") or row.get("phase"), request_model=row.get("request_model") or row.get("model"),
            response_model=row.get("response_model"), role=row.get("role"),
            usage_source=row.get("usage_source") or raw.get("source"), status=row.get("status", "unknown"),
            latencies=[row["latency_ms"]] if "latency_ms" in row else []))
    return _report(samples, source="physical_requests")


def summarize_traces(rows: list[dict]) -> dict:
    samples = []
    for row in rows:
        counters = row.get("counters") if isinstance(row.get("counters"), dict) else {}
        for stage in ("gate", "persona"):
            prefix = f"{stage}_"
            if not any(key.startswith(prefix) for key in counters):
                continue
            raw = {key.removeprefix(prefix): value for key, value in counters.items() if key.startswith(prefix)}
            # Old counters supply only a lower bound on input. Mark the missing
            # total/output as unknown rather than inventing a complete sample.
            has_prompt = "prompt_tokens" in raw
            if not has_prompt:
                raw["prompt_tokens"] = _count(raw.get("cache_hit_tokens")) + _count(raw.get("cache_miss_tokens"))
            calls = _count(raw.get("model_calls")) or _count(raw.get("usage_calls")) or 1
            usage_calls = _count(raw.get("usage_calls"))
            latencies = [event.get("duration_ms") for event in row.get("stages", [])
                         if isinstance(event, dict) and event.get("name") == f"{stage}.model_call"]
            sample = _sample(raw, calls=calls, usage_calls=usage_calls,
                started_at=row.get("first_seen_at") or row.get("recorded_at") or "", stage=stage,
                request_model=row.get("model"), role="unknown", status="unknown", latencies=latencies,
                api_usage_calls=_count(raw.get("api_usage_calls")),
                input_api_calls=_count(raw.get("input_api_calls")), output_api_calls=_count(raw.get("output_api_calls")),
                input_usage_unknown_calls=max(0, calls - _count(raw.get("input_api_calls"))),
                output_usage_unknown_calls=max(0, calls - _count(raw.get("output_api_calls"))),
                estimated_prompt_tokens=_count(raw.get("estimated_prompt_tokens")),
                estimated_completion_tokens=_count(raw.get("estimated_completion_tokens")),
                cache_known_calls=_count(raw.get("cache_known_calls")))
            if not has_prompt:
                sample["input_usage_unknown_calls"] = calls
            if "completion_tokens" not in raw:
                sample["output_usage_unknown_calls"] = calls
            samples.append(sample)
    return _report(samples, turns=len(rows), source="legacy_turn_traces")


def summarize_monitor(path: Path) -> dict:
    # mode=ro prevents accidental creation/migration of production databases.
    connection = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in connection.execute("PRAGMA table_info(monitor_runs)")}
        wanted = ("started_at", "prompt_tokens", "completion_tokens", "cache_hit_tokens", "cache_miss_tokens",
                  "cache_known_prompt_tokens", "cache_status", "latency_ms", "status")
        selected = [name for name in wanted if name in columns]
        rows = [dict(row) for row in connection.execute(f"SELECT {', '.join(selected)} FROM monitor_runs")]
    finally:
        connection.close()
    samples = [_sample(row, started_at=row.get("started_at", ""), stage="turn_aggregate",
                       status=row.get("status", "unknown"),
                       latencies=[row["latency_ms"]] if row.get("latency_ms") is not None else []) for row in rows]
    report = _report(samples, turns=len(rows), source="legacy_monitor_runs")
    report["limitations"] = ["Monitor rows count turns, not physical model attempts; model/stage identity is unavailable."]
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requests", type=Path, help="model_requests.jsonl (preferred)")
    parser.add_argument("--trace", type=Path, help="direct.trace.jsonl")
    parser.add_argument("--monitor", type=Path, help="observability.sqlite3")
    args = parser.parse_args()
    if not (args.requests or args.trace or args.monitor):
        parser.error("Provide --requests, --trace and/or --monitor")
    report = {}
    if args.requests:
        report["requests"] = summarize_requests(args.requests)
    if args.trace:
        report["trace"] = summarize_traces(_load_traces(args.trace))
    if args.monitor:
        report["monitor"] = summarize_monitor(args.monitor)
    # Different sources overlap; never sum their totals together.
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
