"""Read-only prefix-cache hit report from QQ traces or monitor_runs."""
from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# V4 Flash USD per 1M tokens after 2026-08-16: (hit, miss, output)
_FLASH_OFFPEAK = (0.007, 0.22, 0.66)
_FLASH_PEAK = (0.014, 0.44, 1.32)


def peak_hour(moment: datetime) -> bool:
    hour = moment.astimezone(timezone.utc).hour
    return 1 <= hour < 4 or 6 <= hour < 10


def _rates(moment: datetime) -> tuple[float, float, float]:
    return _FLASH_PEAK if peak_hour(moment) else _FLASH_OFFPEAK


def _hour_key(iso: str) -> str:
    return iso[:13] if iso else "unknown"


def _load_traces(path: Path) -> list[dict]:
    latest: dict[str, dict] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            trace_id = row.get("trace_id")
            if not isinstance(trace_id, str):
                continue
            latest[trace_id] = row
    return list(latest.values())


def summarize_traces(rows: list[dict]) -> dict:
    hours: dict[str, dict[str, int]] = defaultdict(lambda: {
        "turns": 0, "gate_hit": 0, "gate_miss": 0, "persona_hit": 0, "persona_miss": 0,
    })
    for row in rows:
        counters = row.get("counters") if isinstance(row.get("counters"), dict) else {}
        hour = _hour_key(row.get("recorded_at") or row.get("first_seen_at") or "")
        bucket = hours[hour]
        bucket["turns"] += 1
        bucket["gate_hit"] += int(counters.get("gate_cache_hit_tokens") or 0)
        bucket["gate_miss"] += int(counters.get("gate_cache_miss_tokens") or 0)
        bucket["persona_hit"] += int(counters.get("persona_cache_hit_tokens") or 0)
        bucket["persona_miss"] += int(counters.get("persona_cache_miss_tokens") or 0)
    return {"hours": dict(hours), "totals": _totals(hours)}


def summarize_monitor(path: Path) -> dict:
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    hours: dict[str, dict[str, int]] = defaultdict(lambda: {
        "turns": 0, "gate_hit": 0, "gate_miss": 0, "persona_hit": 0, "persona_miss": 0,
        "hit": 0, "miss": 0, "prompt": 0, "completion": 0,
    })
    try:
        rows = connection.execute(
            "SELECT started_at, prompt_tokens, completion_tokens, cache_hit_tokens, cache_miss_tokens FROM monitor_runs"
        ).fetchall()
    except sqlite3.OperationalError:
        rows = connection.execute(
            "SELECT started_at, prompt_tokens, completion_tokens, 0 AS cache_hit_tokens, 0 AS cache_miss_tokens FROM monitor_runs"
        ).fetchall()
    finally:
        connection.close()
    for row in rows:
        hour = _hour_key(row["started_at"] or "")
        bucket = hours[hour]
        bucket["turns"] += 1
        bucket["hit"] += int(row["cache_hit_tokens"] or 0)
        bucket["miss"] += int(row["cache_miss_tokens"] or 0)
        bucket["prompt"] += int(row["prompt_tokens"] or 0)
        bucket["completion"] += int(row["completion_tokens"] or 0)
    return {"hours": dict(hours), "totals": _totals(hours)}


def _totals(hours: dict[str, dict[str, int]]) -> dict[str, float | int]:
    hit = miss = completion = turns = 0
    cost = 0.0
    for hour, bucket in hours.items():
        turns += bucket.get("turns", 0)
        hour_hit = bucket.get("hit") or (bucket.get("gate_hit", 0) + bucket.get("persona_hit", 0))
        hour_miss = bucket.get("miss") or (bucket.get("gate_miss", 0) + bucket.get("persona_miss", 0))
        hour_out = bucket.get("completion", 0)
        hour_prompt = int(bucket.get("prompt") or 0)
        if hour_hit + hour_miss == 0 and hour_prompt:
            hour_miss = hour_prompt
        hit += hour_hit
        miss += hour_miss
        completion += hour_out
        try:
            moment = datetime.fromisoformat(hour + ":00:00+00:00")
        except ValueError:
            moment = datetime.now(timezone.utc)
        rates = _rates(moment)
        cost += hour_hit * rates[0] / 1_000_000
        cost += hour_miss * rates[1] / 1_000_000
        cost += hour_out * rates[2] / 1_000_000
    prompt = hit + miss
    return {
        "turns": turns,
        "cache_hit_tokens": hit,
        "cache_miss_tokens": miss,
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "hit_rate": (hit / prompt) if prompt else 0.0,
        "estimated_usd": round(cost, 6),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trace", type=Path, help="direct.trace.jsonl")
    parser.add_argument("--monitor", type=Path, help="observability.sqlite3")
    args = parser.parse_args()
    if not args.trace and not args.monitor:
        parser.error("Provide --trace and/or --monitor")
    report: dict = {}
    if args.trace:
        report["trace"] = summarize_traces(_load_traces(args.trace))
    if args.monitor:
        report["monitor"] = summarize_monitor(args.monitor)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
