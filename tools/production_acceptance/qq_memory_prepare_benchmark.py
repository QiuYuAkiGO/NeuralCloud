"""Offline prepare benchmark. Inputs MUST be isolated SQLite backups, never live DBs.

Example: python -m tools.production_acceptance.qq_memory_prepare_benchmark
  --snapshot /private/snapshot --work /private/new-run --output result.json --samples 30
Snapshot: qq.sqlite3, shared.sqlite3, presets/misty.json, config.json (safe Settings
subset + qq scope), qq-memory.control.json. Writes only below a newly created work
directory; uses MockLLM and never starts transport or background workers.
"""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import math
from pathlib import Path
import shutil
import statistics
import time
from types import SimpleNamespace


def run(snapshot: Path, work: Path, samples: int):
    from gui.presets import PresetLibrary, hydrate_memory
    from harness.cognitive.qq_memory import QQMemoryWorker
    from harness.cognitive.shared_memory import CognitiveSharedMemory
    from harness.config import Settings
    from harness.llm.client import MockLLM
    from harness.memory.service import build_inmemory_service
    from harness.qq.memory import QQVisualMemoryStore
    from harness.qq.telemetry import QQTurnTrace, qq_trace_scope

    if samples < 1 or samples > 1000:
        raise ValueError("samples must be between 1 and 1000")
    shutil.copytree(snapshot, work)  # refuses an existing directory
    config = json.loads((work / "config.json").read_text())
    settings = Settings(_env_file=None, **config["settings"])
    qq = SimpleNamespace(**config["qq"], memory_db=work / "qq.sqlite3")
    library = PresetLibrary(working_dir=work / "presets")
    preset = library.load("misty")
    llm = MockLLM()
    memory = build_inmemory_service(settings=settings, llm=llm)
    hydrate_memory(memory, preset)
    # The live SessionStore is bounded. Apply that same implementation here.
    from harness.pipeline.session import SessionStore
    session = SessionStore(window=settings.session_window)
    session.replace(preset.session_id, preset.transcript)
    chat = SimpleNamespace(settings=settings, library=library, llm=llm, memory=memory,
        active_preset_id=preset.preset_id, active_session_id=preset.session_id,
        _active_builtin_id=preset.builtin_id, _active_preset_name=preset.name,
        user_name=preset.user_name, pipeline=SimpleNamespace(session_store=session))
    host = SimpleNamespace(chat=chat)
    host.shared = CognitiveSharedMemory(host, work / "shared.sqlite3", qq_settings=qq)
    host.qq_memory = QQMemoryWorker(host, control_path=work / "qq-memory.control.json")
    source = QQVisualMemoryStore(qq.memory_db)
    with closing(source._connect()) as db:
        counts = {table: db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
                  for table in ("qq_visual_profiles", "qq_visual_observations", "qq_reply_deliveries", "qq_reply_segment_deliveries")}
        # Prefer the owner's private conversation, which exercises the widest
        # authorized recall, then an allowed group if there is no private row.
        # Use the project's canonical scope serialization.
        from harness.qq.conversation import private_scope_id
        allowed = [private_scope_id(qq.self_member_id, peer) for peer in qq.allowed_private_peer_ids] + qq.allowed_group_ids
        template = None
        for group in allowed:
            row = db.execute("SELECT record_json FROM qq_visual_observations WHERE group_id=? ORDER BY rowid DESC LIMIT 1", (group,)).fetchone()
            if row:
                template = json.loads(row[0])
                break
        if template is None:
            raise ValueError("snapshot has no authorized observation")
        profile = json.loads(db.execute("SELECT record_json FROM qq_visual_profiles WHERE record_id=?", (template["profile_record_id"],)).fetchone()[0])

    def append(index):
        p, o = dict(profile), dict(template)
        p["record_id"] += f":benchmark:{index}"
        p["capture_id"] += f":benchmark:{index}"
        o["record_id"] += f":benchmark:{index}"
        o["capture_id"] += f":benchmark:{index}"
        o["profile_record_id"] = p["record_id"]
        with closing(source._connect()) as db, db:
            db.execute("INSERT INTO qq_visual_profiles VALUES (?,?,?,?,?,?)", (
                p["record_id"], p["group_id"], p["member_id"], p["capture_id"], p["observed_at"], json.dumps(p, ensure_ascii=False)))
            db.execute("INSERT INTO qq_visual_observations VALUES (?,?,?,?,?,?,?,?)", (
                o["record_id"], o["group_id"], o["member_id"], o["capture_id"], o["capture_item_id"],
                o["profile_record_id"], o["observed_at"], json.dumps(o, ensure_ascii=False)))

    start = time.perf_counter()
    host.shared.sync_qq()
    host.shared.sync_local()
    bootstrap_ms = (time.perf_counter() - start) * 1000
    rows = []
    scenarios = []
    def binding_digest(binding):
        material = {"persona": binding.preset.persona.model_dump(mode="json"),
                    "context": binding.context, "source_ids": binding.source_ids,
                    "derived": binding.derived_memory}
        return hashlib.sha256(json.dumps(material, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    try:
        for index in range(samples):
            append(index)
            trace = QQTurnTrace(event_id=f"offline-benchmark:{index}")
            start = time.perf_counter()
            with qq_trace_scope(trace):
                binding = host.shared.qq_binding(group_id=template["group_id"],
                    member_id=template["member_id"], query="还记得我们之前聊过什么吗？")
            elapsed = (time.perf_counter() - start) * 1000
            rows.append({"prepare_ms": elapsed, "stages": trace.snapshot()["stages"],
                         "selected_rows": host.shared.last_import.selected_count,
                         "created_rows": host.shared.last_import.created_count,
                         "binding_sha256": binding_digest(binding)})
        # Rebuild the facade to exercise durable cursors rather than its mtime
        # shortcut; check both query modes and every configured conversation.
        for index, group in enumerate(allowed):
            for query in ("还记得我们之前聊过什么吗？", None):
                host.shared = CognitiveSharedMemory(host, work / "shared.sqlite3", qq_settings=qq)
                trace = QQTurnTrace(event_id=f"offline-scenario:{index}")
                start = time.perf_counter()
                with qq_trace_scope(trace):
                    binding = host.shared.qq_binding(group_id=group, query=query)
                scenarios.append({"scope_index": index, "query_provided": query is not None,
                    "prepare_ms": (time.perf_counter() - start) * 1000,
                    "selected_rows": host.shared.last_import.selected_count,
                    "binding_sha256": binding_digest(binding),
                    "stages": trace.snapshot()["stages"]})
    finally:
        memory.close()
    times = sorted(row["prepare_ms"] for row in rows)
    return {"kind": "offline-production-snapshot", "samples": samples, "source_counts": counts,
            "preset_bytes": (work / "presets/misty.json").stat().st_size,
            "bootstrap_ms": bootstrap_ms, "recall_control": host.qq_memory.control.model_dump(),
            "session_turns": len(session.recent(preset.session_id)),
            "summary_ms": {"min": times[0], "median": statistics.median(times),
                "p95": times[max(0, math.ceil(.95 * len(times)) - 1)], "max": times[-1]},
            "under_1s": sum(value < 1000 for value in times), "rows": rows,
            "restart_scenarios": scenarios,
            "model_calls": 0, "external_sends": 0}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--work", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--samples", type=int, default=30)
    args = parser.parse_args()
    result = run(args.snapshot, args.work, args.samples)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in result.items() if k not in {"rows", "restart_scenarios"}}, ensure_ascii=False))


if __name__ == "__main__":
    main()
