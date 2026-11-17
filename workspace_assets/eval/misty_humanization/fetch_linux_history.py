"""Read-only paginated Linux monitoring export; credentials remain in memory."""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path

import httpx


def dump(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", default="2026-09-15T04:08:40.396121Z")
    parser.add_argument("--output", type=Path, default=Path("data/evaluations/misty-humanization-20260915/linux-api"))
    args = parser.parse_args()
    access_path = Path(os.environ["LOCALAPPDATA"]) / "NeuralCloud/LinuxDeployment/access.json"
    access = json.loads(access_path.read_text(encoding="utf-8-sig"))
    cutoff = datetime.now(timezone.utc).isoformat()
    out = args.output
    manifest = {"created_at": cutoff, "cutoff_utc": cutoff, "delta_since_utc": args.since,
                "read_only": True, "credentials_exported": False, "queries": [],
                "coverage_notes": ["API monitor is a retained, lossy-by-design projection, not a full database backup.",
                                   "Historical delivery query covers all rows still retained by the API.",
                                   "Journal rows are deduplicated by runtime_instance_id and record_seq."]}
    client = httpx.Client(base_url=access["url"].rstrip("/"),
                         auth=httpx.BasicAuth(access["username"], access["password"]), timeout=90)
    del access
    all_events = {}

    def pages(path, params):
        cursor = None
        rows = []
        seen_cursors = set()
        count = 0
        while True:
            query = {**params, "limit": 500}
            if cursor:
                query["cursor"] = cursor
            response = client.get(path, params=query)
            response.raise_for_status()
            page = response.json()
            rows.extend(page["items"])
            count += 1
            if not page["has_more"]:
                break
            cursor = page["next_cursor"]
            if not cursor or cursor in seen_cursors:
                raise RuntimeError("Pagination did not advance")
            seen_cursors.add(cursor)
        manifest["queries"].append({"path": path, "params": params, "pages": count,
                                     "row_count": len(rows), "has_more": False,
                                     "oldest_observed_at": min((r.get("observed_at", "") for r in rows), default=None),
                                     "newest_observed_at": max((r.get("observed_at", "") for r in rows), default=None)})
        print(json.dumps({"path": path, "filter": params, "pages": count, "rows": len(rows), "has_more": False}), flush=True)
        return rows

    try:
        # The event endpoint supports a fixed cutoff while the journal facade does not.
        delta = pages("/api/ops/events", {"since": args.since, "until": cutoff})
        for row in delta:
            all_events[row["seq"]] = row
        deliveries = pages("/api/ops/journal", {"since": "1970-01-01T00:00:00Z", "record_type": "reply_delivery"})
        for row in deliveries:
            all_events[row["seq"]] = row
        runs = pages("/api/ops/runs", {"since": args.since})
        dump(out / "runs.json", runs)
        correlations = {r.get("correlation_id") for r in deliveries}
        correlations.update(r.get("correlation_id") for r in runs)
        correlations.discard(None)
        for correlation in sorted(correlations):
            rows = pages("/api/ops/events", {"since": "1970-01-01T00:00:00Z", "until": cutoff,
                                             "correlation_id": correlation})
            for row in rows:
                all_events[row["seq"]] = row
        # Save only authenticated GET responses needed for history evaluation.
        state = client.get("/api/debug/state")
        manifest["debug_state_status"] = state.status_code
        if state.is_success:
            value = state.json()
            dump(out / "debug-state.json", value)
            manifest["debug_state_top_level_keys"] = list(value) if isinstance(value, dict) else []
        status_response = client.get("/api/status")
        status_response.raise_for_status()
        ui = status_response.json()
        dump(out / "status-history.json", {k: ui[k] for k in
             ("session", "persona", "speakers", "presets", "shared_memory") if k in ui})
        board_response = client.get("/api/board")
        manifest["board_status"] = board_response.status_code
        if board_response.is_success:
            dump(out / "board.json", board_response.json())
        overview_response = client.get("/api/ops/overview", params={"window": "1h"})
        if overview_response.is_success:
            manifest["monitor_retention_and_health"] = overview_response.json().get("monitor", {})
        manifest["context_snapshots_fetched_at"] = datetime.now(timezone.utc).isoformat()
        journal = {}
        for row in all_events.values():
            if row.get("topic", "").startswith("journal."):
                record = dict(row["payload"])
                record.pop("integrity", None)
                journal[(record.get("runtime_instance_id"), record["record_seq"])] = record
        journal_path = out / "runtime/journal/runtime-linux-api.jsonl"
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        sorted_journal = sorted(journal.values(), key=lambda x: (x.get("recorded_at", ""), x["record_seq"]))
        journal_path.write_text("".join(json.dumps(x, ensure_ascii=False) + "\n" for x in sorted_journal), encoding="utf-8")
        events_path = out / "monitor-events.jsonl"
        events_path.write_text("".join(json.dumps(all_events[k], ensure_ascii=False) + "\n" for k in sorted(all_events)), encoding="utf-8")
        manifest.update({"journal_unique_records": len(journal), "monitor_unique_events": len(all_events),
                         "journal_types": dict(Counter(x["record_type"] for x in sorted_journal)),
                         "event_topics": dict(Counter(x["topic"] for x in all_events.values())),
                         "journal_sha256": hashlib.sha256(journal_path.read_bytes()).hexdigest(),
                         "journal_oldest_at": sorted_journal[0]["recorded_at"] if sorted_journal else None,
                         "journal_newest_at": sorted_journal[-1]["recorded_at"] if sorted_journal else None})
        dump(out / "manifest.json", manifest)
        print(json.dumps({k: manifest[k] for k in ("cutoff_utc", "journal_unique_records", "monitor_unique_events", "journal_types", "event_topics")}), flush=True)
    finally:
        client.close()


if __name__ == "__main__":
    main()
