"""Isolated recovery scaling benchmark; no services, network, or real state.

Run with python -m tools.benchmark_runtime_recovery --output <report.json>.
OS file caches are not flushed: results explicitly describe warm-cache runs.
"""

from __future__ import annotations

import argparse
import json
import statistics
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.runtime.journal import RuntimeJournal, RuntimeJournalRecord, _canonical_json, _payload_checksum
from harness.runtime.models import (
    ActionExecution,
    ActionExecutionState,
    RuntimeEvent,
    RuntimeEventPriority,
    RuntimeEventType,
)
from harness.runtime.recovery import RecoveryStore


def write_fixture(path, count):
    now = datetime.now(UTC)
    previous = ""
    event = action = None
    with path.open("wb") as handle:
        for i in range(1, count + 1):
            cycle = (i - 1) % 20
            event_id, kind, data = None, "noise", {"payload": "x" * 128}
            if cycle == 0:
                event = RuntimeEvent(
                    event_id=f"event-{i}",
                    receive_seq=i,
                    correlation_id=f"event-{i}",
                    event_type=RuntimeEventType.USER_MESSAGE,
                    priority=RuntimeEventPriority.USER,
                    source="benchmark",
                    source_time=now,
                    received_at=now,
                    idempotency_key=f"event-{i}",
                    manifest_revision=1,
                    payload={"text": "fixture"},
                )
                event_id, kind, data = event.event_id, "event_accepted", {"event": event.model_dump(mode="json")}
            elif cycle == 1:
                event_id, kind, data = (
                    event.event_id,
                    "processing_started",
                    {
                        "event_type": "user_message",
                        "source": "benchmark",
                        "first_receive_seq": event.receive_seq,
                        "last_receive_seq": event.receive_seq,
                    },
                )
            elif cycle == 2:
                action = ActionExecution(
                    action_id=f"action-{i}",
                    goal_id=f"goal-{i}",
                    goal="fixture",
                    idempotency_key=f"action-{i}",
                    state=ActionExecutionState.READY,
                    reversible=True,
                    verification_method="fixture",
                    deadline=now + timedelta(days=1),
                    created_at=now,
                    updated_at=now,
                )
                event_id, kind, data = event.event_id, "action_proposed", {"action": action.model_dump(mode="json")}
            elif cycle == 3:
                action = action.model_copy(update={"state": ActionExecutionState.SUCCEEDED})
                kind, data = "action_completed", {"action": action.model_dump(mode="json")}
            elif cycle == 4:
                event_id, kind, data = event.event_id, "processing_completed", {"status": "completed"}
            payload = RuntimeJournalRecord(
                record_seq=i,
                record_type=kind,
                runtime_instance_id="benchmark",
                character_id="misty",
                manifest_revision=1,
                recorded_at=now,
                event_id=event_id,
                receive_seq=event.receive_seq if event_id else None,
                data=data,
                prev_hash=previous,
                checksum="0" * 64,
            ).hash_payload()
            previous = _payload_checksum(payload)
            handle.write((_canonical_json({**payload, "checksum": previous}) + "\n").encode())


def measure(root, mode):
    started = time.perf_counter()
    store = RecoveryStore(root / "recovery.sqlite3", identity={"character": "misty", "manifest": "benchmark"})
    journal = RuntimeJournal(
        root / "journal",
        runtime_instance_id="benchmark",
        character_id="misty",
        manifest_revision=1,
        fsync=False,
        recovery_store=store,
        recovery_mode=mode,
    )
    pending = (
        sum(1 for _ in store.pending_events())
        + sum(1 for _ in store.pending_commands())
        + sum(1 for _ in store.action_rows())
    )
    duration = time.perf_counter() - started
    return (
        journal,
        store,
        {
            "seconds": duration,
            "replayed_records": journal.recovered_records,
            "pending": pending,
            "retained_records": journal.retained_records,
            "mode": store.mode,
        },
    )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--counts", default="10000,100000,1000000")
    parser.add_argument("--repeats", type=int, default=5)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = {
        "cache_condition": "OS cache not flushed; warm filesystem cache",
        "scope": "journal open + projection restore + pending queries; excludes service RPC and background audit",
        "results": [],
    }
    for count in map(int, args.counts.split(",")):
        with tempfile.TemporaryDirectory(prefix="misty-recovery-benchmark-") as directory:
            root = Path(directory)
            (root / "journal").mkdir()
            path = root / "journal/runtime-000001.jsonl"
            write_fixture(path, count)
            journal, store, full = measure(root, "strict")
            store.commit()
            for _ in range(1000):
                journal.append("tail_fixture")
            store.close()
            fast = []
            for _ in range(args.repeats):
                journal, store, result = measure(root, "fast")
                fast.append(result)
                store.close()  # keep the same committed cut for every trial
            row = dict(
                records=count,
                bytes=sum(p.stat().st_size for p in (root / "journal").glob("*.jsonl")),
                strict=full,
                fast=fast,
                fast_median_seconds=statistics.median(r["seconds"] for r in fast),
            )
            report["results"].append(row)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(report, indent=2), encoding="utf-8")
            print(json.dumps(row), flush=True)


if __name__ == "__main__":
    main()
