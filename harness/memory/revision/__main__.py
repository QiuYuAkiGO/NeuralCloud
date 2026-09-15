"""Replay completed days into an isolated copy; never write the input preset/DB."""
from __future__ import annotations

import argparse
from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
import json
from pathlib import Path
import sqlite3
from zoneinfo import ZoneInfo

from gui.presets import atomic_write_json, load_preset_file
from harness.config import Settings
from harness.memory.shared import SharedMemoryStore
from .config import load_config
from .ledger import digest
from .provider import DailyRevisionProvider
from .queue import DailyRevisionQueue
from .reducer import build_revision, revision_context
from .sources import collect_source_window


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--source-db", type=Path, required=True)
    parser.add_argument("--preset", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat, help="Inclusive last completed date")
    parser.add_argument("--self-member-id")
    args = parser.parse_args()
    config = load_config(args.config.resolve())
    if not config.configured:
        parser.error("revision provider credential file is not configured")
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=True)
    working_db, working_preset = output / "sources.sqlite3", output / "revised-preset.json"
    if args.source_db.resolve() == working_db or args.preset.resolve() == working_preset:
        parser.error("output must be isolated from the original sources")
    baseline = load_preset_file(args.preset)
    identity = {"preset_sha256": digest(baseline.model_dump(mode="json")),
                "source_path": str(args.source_db.resolve()), "preset_id": baseline.preset_id}
    manifest_path = output / "input-manifest.json"
    if manifest_path.exists() and json.loads(manifest_path.read_text(encoding="utf-8")) != identity:
        parser.error("output belongs to a different frozen input; use another output directory")
    if not working_db.exists():
        with sqlite3.connect(args.source_db.resolve().as_uri() + "?mode=ro", uri=True) as original:
            with sqlite3.connect(working_db) as copied:
                original.backup(copied)
    atomic_write_json(manifest_path, identity)
    preset = load_preset_file(working_preset) if working_preset.exists() else baseline
    store = SharedMemoryStore(working_db, owner_id="misty", preset_id=preset.preset_id)
    zone = ZoneInfo(config.timezone)
    today = datetime.now(zone).date()
    end = args.end or today - timedelta(days=1)
    start = args.start or end - timedelta(days=config.backfill_days - 1)
    if start > end or end >= today:
        parser.error("date range must contain only completed days")
    queue = DailyRevisionQueue(output / "jobs.sqlite3")
    report = {"source_readonly": True, "start": start.isoformat(), "end": end.isoformat(),
              "model": config.model, "results": [], "coverage_gaps": [], "completed": False}
    provider = DailyRevisionProvider(config, queue)
    failed = False
    try:
        day = start
        while day <= end:
            window = collect_source_window(store, owner_id=store.owner_id, preset_id=preset.preset_id,
                start=datetime.combine(day, time.min, zone), end=datetime.combine(day + timedelta(days=1), time.min, zone),
                self_member_id=args.self_member_id)
            groups = defaultdict(list)
            for source in window.records:
                groups[source.scope.model_dump_json()].append(source)
            report["coverage_gaps"] = sorted(set(report["coverage_gaps"]) | set(window.coverage_gaps))
            if not groups:
                report["results"].append({"day": day.isoformat(), "status": "empty", "source_count": 0})
            for scope_key, sources in groups.items():
                context = revision_context(preset, sources)
                run = queue.enqueue(owner_id=store.owner_id, preset_id=preset.preset_id, day=day.isoformat(),
                    partition=digest(scope_key), sources=[s.model_dump(mode="json") for s in sources],
                    basis={"context": context, "context_digest": digest(context), "watermark": window.watermark},
                    config_digest=digest(config.model_dump(mode="json")))
                entry = {"day": day.isoformat(), "run_id": run["run_id"], "source_count": len(sources)}
                print(json.dumps({"event": "partition_started", **entry}), flush=True)
                try:
                    if run["run_id"] not in preset.daily_revision_state.get("receipts", {}):
                        proposed = provider.propose(run["run_id"], sources, context)
                        preset = build_revision(preset, run, proposed, Settings())
                        atomic_write_json(working_preset, preset.model_dump(mode="json"))
                    receipt = preset.daily_revision_state["receipts"][run["run_id"]]
                    queue.finish(run["run_id"], "applied", receipt=receipt)
                    entry.update(status="applied", effect_count=len(receipt["effect_ids"]))
                except Exception as exc:
                    code = getattr(exc, "error_code", None) or (str(exc) if isinstance(exc, ValueError)
                        and str(exc).startswith("daily_revision_") else type(exc).__name__)
                    entry.update(status="failed", error_code=code)
                    queue.fail(run["run_id"], code, retryable=False)
                    failed = True
                report["results"].append(entry)
                report["usage"] = queue.status().get("usage", [])
                atomic_write_json(output / "report.json", report)
                print(json.dumps({"event": "partition_finished", **entry}), flush=True)
                if failed:
                    break
            if failed:
                break
            day += timedelta(days=1)
        report["completed"] = not failed
        report["finished_at"] = datetime.now(timezone.utc).isoformat()
        atomic_write_json(output / "report.json", report)
    finally:
        provider.close()
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
