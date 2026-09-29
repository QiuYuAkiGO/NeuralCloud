"""Synthetic, offline SQL recall and actual production prompt preparation.

python -m tools.benchmark_production_memory --destination PATH --rows 100000
One million rows is opt-in with --rows 1000000; no models or QQ are invoked.
"""
from __future__ import annotations

import argparse
import asyncio
from contextlib import closing
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import statistics
import sqlite3
import tempfile
import time
from types import SimpleNamespace

from harness.core.models import MemoryScope
from harness.memory.production import Experience, ProductionMemoryStore


def _percentile(values, fraction):
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, max(0, int((len(ordered) - 1) * fraction)))]


def _distribution(values):
    return {"p50": round(statistics.median(values), 3), "p95": round(_percentile(values, .95), 3),
            "max": round(max(values), 3)}


class _ReachedModelBoundary(BaseException):
    """Stop before inference; production must not commit a fake decision."""


class _PromptBoundary:
    def __init__(self):
        self.started = 0
        self.samples = []

    def complete_chat_result(self, messages, **kwargs):
        elapsed = (time.perf_counter() - self.started) * 1000
        payload = json.loads(messages[-1]["content"])
        memories = payload.get("memories", [])
        self.samples.append({"elapsed_ms": elapsed, "memory_entries": sum(len(item["source_ids"]) for item in memories),
                             "message_characters": sum(len(item["content"]) for item in messages)})
        raise _ReachedModelBoundary

    def close(self):
        pass


async def _production_preparation(destination, *, queries, before_prepare=None, after_prepare=None):
    """Run real adapter/facade/prepare/_ask, stopping at the model boundary.

    Copies the SQL benchmark first because real preparation writes source/use
    receipts. No fake model response or commit/fence is counted as compilation.
    """
    from harness.cognitive.production import activate_host
    from harness.cognitive.shared_memory import CognitiveSharedMemory
    from harness.config import Settings
    from harness.contracts.envelope import DomainCommandEnvelope
    from harness.core.models import PersonaCore, PersonaIdentity
    from harness.memory.service import build_inmemory_service
    from harness.memory.shared import SharedMemoryStore
    from harness.pipeline import InferencePipeline
    from harness.production.config import ProductionConfig
    from harness.runtime.adapters import PipelineCognitiveAdapter

    directory = Path(tempfile.mkdtemp(prefix=destination.stem + ".prepare-", dir=destination.parent))
    copied = directory / "production.sqlite3"
    with closing(sqlite3.connect(destination.resolve().as_uri() + "?mode=ro", uri=True)) as source:
        with closing(sqlite3.connect(copied)) as target:
            source.backup(target)
    copied.chmod(0o600)
    model = _PromptBoundary()
    settings = Settings(_env_file=None, production_memory_enabled=True, emotion_mode="off", call_log_enabled=False,
        parser_llm_enabled=False, planner_llm_enabled=False, tier_llm_enabled=False, aux_llm_preload=False,
        continuation_enabled=False, validator_sample_rate=0, cognitive_incremental_persistence_enabled=True)
    memory = build_inmemory_service(settings=settings, llm=model)
    memory.bootstrap_persona(PersonaCore(persona_id="synthetic-persona", created_at=datetime.now(timezone.utc),
                                       identity=PersonaIdentity(name="Synthetic benchmark character")))
    pipeline = InferencePipeline(memory, settings=settings, llm=model)
    session = "synthetic-session"
    adapter = PipelineCognitiveAdapter(pipeline, default_session_id=session,
                                      autonomous_log_dir=directory / "autonomous")
    adapter.production_meter = object()  # No model attempt, meter HTTP or singleton is needed.
    host = SimpleNamespace(chat=SimpleNamespace(settings=settings, active_preset_id="synthetic-v1",
        active_session_id=session, memory=memory, pipeline=pipeline), adapter=adapter)
    host.shared = CognitiveSharedMemory(host, directory / "shared.sqlite3")
    host.shared.store = SharedMemoryStore(directory / "shared.sqlite3", owner_id="benchmark", preset_id="synthetic-v1")
    host.shared._production_stores[("benchmark", "synthetic-v1")] = ProductionMemoryStore(
        copied, owner_id="benchmark", preset_id="synthetic-v1")
    adapter._context_provider = host.shared.local_context
    activate_host(host)
    context = {"schema_version": "production-context-v1", "configuration": ProductionConfig().model_dump(mode="json"),
        "body": {"available": True, "snapshot": {"body_instance_id": "synthetic-body", "episode_id": "synthetic-episode"}},
        "scope": "local:main", "restricted_dialogue": False, "runtime_lifecycle": "RUNNING", "epoch": 0,
        "authorized_action_context": {"runtime_instance_id": "synthetic-runtime"}}
    try:
        for index in range(queries + 5):
            now = datetime.now(timezone.utc)
            event_id = "synthetic-prepare-" + str(index)
            request = DomainCommandEnvelope(command_type="PrepareResponse", event_id=event_id, receive_seq=index + 1,
                correlation_id=event_id, runtime_instance_id="synthetic-runtime", manifest_revision=1,
                idempotency_key=event_id, issued_at=now, deadline=now + timedelta(seconds=60),
                payload={"mode": "production", "production_context": context, "session_id": session,
                         "text": "synthetic" if index % 2 == 0 else f"topic_{index % 100}", "channel": "chat"})
            if before_prepare is not None:
                before_prepare(copied, index)
            model.started = time.perf_counter()
            try:
                await adapter.prepare_response(request)
            except _ReachedModelBoundary:
                pass
            else:
                raise RuntimeError("benchmark_did_not_reach_real_model_boundary")
            if after_prepare is not None:
                after_prepare(copied, index, model.samples[-1])
        measured = model.samples[5:]
        if not measured or not all(item["memory_entries"] > 0 for item in measured):
            raise RuntimeError("benchmark_memory_context_missing")
        return {"path": "PipelineCognitiveAdapter.prepare_response->CognitiveSharedMemory.local_context->production.prepare->_ask",
            "first_prepare_ms": round(model.samples[0]["elapsed_ms"], 3),
            "warm_prepare_ms": _distribution([item["elapsed_ms"] for item in measured]),
            "warmup_prepares": 5, "measured_prepares": queries,
            "memory_entries": {"min": min(item["memory_entries"] for item in measured),
                               "max": max(item["memory_entries"] for item in measured)},
            "message_characters_max": max(item["message_characters"] for item in measured),
            "includes": ["scoped_SQL_recall", "incremental_sync", "source_ingest", "retrieval_and_exposure_receipts",
                         "evidence_segments", "production_payload_budgeting", "schema_and_outbound_messages", "worker_dispatch"],
            "cache_condition": "new_application_objects_and_SQL_copy; process_imports_and_OS_page_cache_may_be_warm; no_drop_caches",
            "excludes": ["application_startup", "HTTP_transport", "QQ_gate_body_context", "external_models", "response_commit"],
            "external_model_calls": 0, "retained_working_copy": str(directory.resolve())}
    finally:
        await adapter.close()


def benchmark(destination, *, rows=100000, batch_size=1000, queries=100, resume=False, context_prepare=True):
    destination = Path(destination)
    if rows < 1 or not 1 <= batch_size <= 10000 or queries < 1:
        raise ValueError("invalid_benchmark_size")
    if destination.exists() and not resume:
        raise ValueError("benchmark_destination_exists_use_new_path_or_explicit_resume")
    store = ProductionMemoryStore(destination, owner_id="benchmark", preset_id="synthetic-v1")
    start_cursor = store.cursor("synthetic:v1")
    if start_cursor > rows:
        raise ValueError("benchmark_rows_smaller_than_existing_source_cursor")
    epoch = datetime(2026, 1, 1, tzinfo=timezone.utc)
    started = time.perf_counter()
    for start in range(start_cursor, rows, batch_size):
        end = min(rows, start + batch_size)
        experiences = [Experience(record_id=f"event-{i}", author_id=f"qq:member:{10000 + i % 1000}",
            text=f"topic_{i % 100} synthetic observation {i}", occurred_at=epoch + timedelta(seconds=i),
            scope=MemoryScope(owner_id="benchmark", channel="qq_group", conversation_id=f"qq:group:{100 + i % 10}",
                              visibility=["local:main", f"qq:group:{100 + i % 10}"]),
            source_refs=(f"synthetic-source:{i}",), independent_key=f"synthetic-source:{i}") for i in range(start, end)]
        store.ingest(experiences, stream="synthetic:v1", cursor=end)
    ingestion_seconds = time.perf_counter() - started
    timings, candidate_counts = [], []
    for index in range(queries + 5):
        diagnostics = {}
        start = time.perf_counter()
        # Alternate common lexical, selective topic and exact person recall.
        query = "synthetic" if index % 3 == 0 else f"topic_{index % 100}"
        store.recall(audience="local:main", query=query,
            subject_ids=(f"qq:member:{10000 + index % 1000}",) if index % 3 == 2 else (),
            now=epoch + timedelta(days=365), diagnostics=diagnostics)
        elapsed = (time.perf_counter() - start) * 1000
        if index >= 5:
            timings.append(elapsed)
            candidate_counts.append(diagnostics["candidate_count"])
    preparation = asyncio.run(_production_preparation(destination, queries=queries)) if context_prepare else None
    return {"schema_version": "production-memory-benchmark-v1", "synthetic_only": True,
        "rows": rows, "rows_inserted": rows - start_cursor, "batch_size": batch_size,
        "ingestion_seconds": round(ingestion_seconds, 3), "database_bytes": destination.stat().st_size,
        "warmup_queries": 5, "measured_queries": queries,
        "recall_ms": _distribution(timings),
        "candidates": {"max": max(candidate_counts), "p95": _percentile(candidate_counts, .95), "limit": 192},
        "production_context_preparation": preparation,
        "excludes": ["network", "models", "embeddings", "cold_os_cache", "QQ_gate_body_context"],
        "destination": str(destination.resolve())}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--destination", required=True)
    parser.add_argument("--rows", type=int, choices=(1000, 10000, 100000, 1000000), default=100000)
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--queries", type=int, default=100)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--sql-only", action="store_true", help="Skip actual production prompt preparation")
    args = parser.parse_args(argv)
    print(json.dumps(benchmark(args.destination, rows=args.rows, batch_size=args.batch_size,
                               queries=args.queries, resume=args.resume, context_prepare=not args.sql_only), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
