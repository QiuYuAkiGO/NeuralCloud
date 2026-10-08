"""Reconstruct historical source coverage, without model calls or diary writes.

Run against SQLite backups; timestamps exclude information unavailable at the
original request. This is a reconstruction, not a saved original model prompt.
"""
from __future__ import annotations

import argparse
from contextlib import closing
from datetime import datetime
import json
from pathlib import Path
import sqlite3

from harness.diary.reflection import SCOPE, batches, source_events
from harness.memory.shared import SharedMemoryStore


def replay(shared_path, diary_path, turns_path):
    source = SharedMemoryStore(shared_path)
    def connect(path):
        return sqlite3.connect(Path(path).resolve().as_uri()+"?mode=ro", uri=True)
    with closing(connect(diary_path)) as db:
        diaries = db.execute("SELECT entry_id,written_at FROM entries ORDER BY written_at").fetchall()
    with closing(connect(turns_path)) as db:
        turns = [json.loads(r[0]) for r in db.execute("SELECT record_json FROM turns")]
    result = []
    for turn in sorted(turns, key=lambda r: r["requested_at"]):
        if turn.get("status") != "committed" or turn["trigger"] != "homeostasis":
            continue
        at = datetime.fromisoformat(turn["requested_at"])
        previous = [stamp for _, stamp in diaries if datetime.fromisoformat(stamp) < at]
        start = max(map(datetime.fromisoformat, previous)) if previous else None
        with closing(connect(shared_path)) as db:
            ids = [r[0] for r in db.execute("""SELECT record_id FROM shared_memory_entries
                WHERE julianday(json_extract(record_json,'$.stored_at'))<julianday(?)
                AND julianday(json_extract(record_json,'$.occurred_at'))<julianday(?)
                AND (? IS NULL OR julianday(json_extract(record_json,'$.stored_at'))>julianday(?)) ORDER BY rowid""",
                (at.isoformat(), at.isoformat(), start.isoformat() if start else None, start.isoformat() if start else None))]
        records = []
        for offset in range(0, len(ids), 256):
            records.extend(source.read_exact(SCOPE, ids[offset:offset+256]))
        events = source_events(records)
        groups = list(batches(events))
        assert all(r.occurred_at < at and r.stored_at < at for r in records)
        assert not any(r.kind in {"diary_entry", "board_entry"} for r in records)
        result.append({"day": turn["day"], "requested_at": at.isoformat(), "since": start.isoformat() if start else None,
            "source_records": len(records), "unique_events": len(events), "summary_batches": len(groups),
            "covered_characters": sum(len(p["text"]) for group in groups for p in group),
            "old_diaries_as_new_events": 0, "future_sources": 0,
            "event_days": sorted({p["day"] for group in groups for p in group})})
    return {"kind": "historical_reconstruction_not_original_prompt", "model_calls": 0, "runs": result}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shared", type=Path, required=True)
    parser.add_argument("--diary", type=Path, required=True)
    parser.add_argument("--turns", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = replay(args.shared, args.diary, args.turns)
    args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2)+"\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False))
