"""Causal trace boundaries for long dialogue runs; all model work is offline."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime, timedelta

import pytest

from harness.contracts.decisions import DeliveryEvidence
from harness.contracts.envelope import DomainCommandEnvelope
from harness.core.error_notice import ERROR_FALLBACK_REPLY
from harness.core.models import RetrievalQuery
from harness.runtime.monitor import MonitorStore
from infra.trace.request_spans import current_trace_context, trace_scope
from tests.test_runtime_supervisor import _pipeline_adapter


def _command(kind, number, payload):
    now = datetime.now(UTC)
    return DomainCommandEnvelope[dict](
        command_type=kind, event_id=f"evt_chat_{number}", receive_seq=number,
        correlation_id=f"corr_chat_{number}", runtime_instance_id="babel-trace-fixture",
        manifest_revision=1, idempotency_key=f"{kind}:{number}",
        issued_at=now, deadline=now + timedelta(seconds=60), payload=payload,
    )


def test_prepare_and_commit_bind_causal_context_and_distinct_call_ids(tmp_path, monkeypatch):
    memory, pipeline, adapter = _pipeline_adapter(tmp_path)
    pipeline.settings.call_log_enabled = True
    pipeline.settings.trace_dir = str(tmp_path / "traces")
    prepare_contexts, commit_contexts = [], []
    original_prepare, original_record = pipeline.prepare, memory.record_event

    def prepare(*args, **kwargs):
        prepare_contexts.append((current_trace_context(), kwargs.get("call_id")))
        return original_prepare(*args, **kwargs)

    def record(*args, **kwargs):
        commit_contexts.append(current_trace_context())
        return original_record(*args, **kwargs)

    monkeypatch.setattr(pipeline, "prepare", prepare)
    monkeypatch.setattr(memory, "record_event", record)

    async def scenario():
        command = _command("PrepareResponse", 1, {"text": "你好。"})
        prepared = await adapter.prepare_response(command)
        assert prepared.status == "applied", prepared.error
        assert current_trace_context() == {}
        context, call_id = prepare_contexts[0]
        assert context["command_id"] == command.command_id
        assert context["phase"] == "prepare"
        assert context["correlation_id"] == call_id == command.correlation_id
        evidence = DeliveryEvidence(
            prepare_id=prepared.result.prepare_id, delivered_at=datetime.now(UTC),
            channel="chat", success=True, delivered_segments=prepared.result.decision.segments,
            external_ref="test:babel-delivery",
        )
        commit = _command("CommitInteraction", 1, {
            "prepare_id": prepared.result.prepare_id,
            "evidence": evidence.model_dump(mode="json"),
        })
        result = await adapter.commit_interaction(commit)
        assert result.status == "applied", result.error
        assert commit_contexts
        assert all(item["phase"] == "commit" for item in commit_contexts)
        assert all(item["command_id"] == commit.command_id for item in commit_contexts)
        assert all(item["correlation_id"] == command.correlation_id for item in commit_contexts)
        assert all(item["event_id"] == command.event_id for item in commit_contexts)
        assert current_trace_context() == {}
        await adapter.close()

    asyncio.run(scenario())
    logs = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "traces/calls").rglob("*.json")]
    prepare_log = next(item for item in logs if item["kind"] == "prepare_response")
    assert prepare_log["call_id"] == "corr_chat_1"
    assert prepare_log["correlation_id"] == "corr_chat_1"
    assert any(item["phase"] == "commit" and item["kind"] == "record_event" for item in logs)


def test_failed_prepare_preserves_partial_log_and_failed_monitor_status(tmp_path, monkeypatch):
    memory, pipeline, adapter = _pipeline_adapter(tmp_path)
    pipeline.settings.call_log_enabled = True
    pipeline.settings.trace_dir = str(tmp_path / "traces")

    def fail(_event):
        raise RuntimeError("fixture parser failure")

    monkeypatch.setattr(pipeline.parser, "parse", fail)

    async def scenario():
        result = await adapter.prepare_response(_command("PrepareResponse", 1, {"text": "故事开始。"}))
        assert result.status == "applied", result.error
        assert result.result is not None
        assert result.result.decision.text == ERROR_FALLBACK_REPLY
        assert current_trace_context() == {}
        await adapter.close()

    asyncio.run(scenario())
    path = next((tmp_path / "traces/calls").rglob("corr_chat_1.json"))
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["ok"] is False
    assert payload["result"] is None
    assert any(step["kind"] == "ingest" for step in payload["steps"])
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    try:
        store.append_call_log(path, payload)
        run = store.get_run("corr_chat_1")
        assert run["status"] == "failed" and run["error"]
        assert run["correlation_id"] == "corr_chat_1"
        assert run["event_id"] == "evt_chat_1"
        store.append_event("cognitive.ui", {"run_id": "corr_chat_1", "type": "state", "data": {}})
        assert store.get_run("corr_chat_1")["status"] == "failed"
    finally:
        store.close()


def test_cancelled_prepare_worker_retains_its_turn_context(tmp_path, monkeypatch):
    _, pipeline, adapter = _pipeline_adapter(tmp_path)
    entered, release = threading.Event(), threading.Event()
    observed = []
    original = pipeline.prepare

    def slow_prepare(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        observed.append(current_trace_context())
        return original(*args, **kwargs)

    monkeypatch.setattr(pipeline, "prepare", slow_prepare)

    async def scenario():
        command = _command("PrepareResponse", 1, {"text": "你好。"})
        caller = asyncio.create_task(adapter.prepare_response(command))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            caller.cancel()
            with pytest.raises(asyncio.CancelledError):
                await caller
            assert current_trace_context() == {}
            with trace_scope(correlation_id="unrelated-turn", phase="commit"):
                release.set()
                await asyncio.gather(*tuple(adapter.command_tasks))
            assert observed[0]["correlation_id"] == command.correlation_id
            assert observed[0]["command_id"] == command.command_id
            assert observed[0]["phase"] == "prepare"
        finally:
            release.set()
            await adapter.close()

    asyncio.run(scenario())


def test_retrieval_executor_propagates_each_query_context(tmp_path, monkeypatch):
    memory, _, adapter = _pipeline_adapter(tmp_path)
    observed = []

    def plan(_query):
        observed.append(("planner", current_trace_context()))
        return "plan"

    def embed(_text):
        observed.append(("embedding", current_trace_context()))
        return [0.5]

    monkeypatch.setattr(memory.planner, "plan", plan)
    monkeypatch.setattr(memory.embeddings, "embed_one", embed)
    try:
        for number in (1, 2):
            with trace_scope(correlation_id=f"turn-{number}", phase="prepare", command_id=f"cmd-{number}"):
                result = memory._plan_and_embed(RetrievalQuery(text="塔", now=datetime.now(UTC)))
            assert result == ("plan", [0.5])
            assert current_trace_context() == {}
        for number in (1, 2):
            matching = [item for item in observed if item[1]["correlation_id"] == f"turn-{number}"]
            assert {item[0] for item in matching} == {"planner", "embedding"}
            assert all(item[1]["command_id"] == f"cmd-{number}" for item in matching)
    finally:
        asyncio.run(adapter.close())


def test_delayed_tier_keeps_owning_turn_and_writes_independent_log(tmp_path):
    memory, pipeline, adapter = _pipeline_adapter(tmp_path)
    pipeline.settings.call_log_enabled = True
    pipeline.settings.trace_dir = str(tmp_path / "traces")
    entered, release = threading.Event(), threading.Event()
    observed = []

    def delayed():
        entered.set()
        assert release.wait(5)
        observed.append((current_trace_context(), memory.call_log.active()))
        with memory.call_log.scope("tier_delayed", {}) as call:
            memory.call_log.add_step("tier_result", {"done": True})
            call.set_result({"done": True})

    try:
        with trace_scope(correlation_id="original-turn", command_id="commit-original", phase="commit"):
            with memory.call_log.scope("record_event", {}, call_id="original-call") as call:
                memory._submit_tier(delayed)
                assert entered.wait(2)
                call.set_result({"queued": True})
        with trace_scope(correlation_id="next-turn", command_id="prepare-next", phase="prepare"):
            release.set()
            memory.wait_background()
        assert observed[0][0]["correlation_id"] == "original-turn"
        assert observed[0][0]["phase"] == "commit"
        assert observed[0][1] is None
        logs = [json.loads(path.read_text(encoding="utf-8")) for path in (tmp_path / "traces/calls").rglob("*.json")]
        tier = next(item for item in logs if item["kind"] == "tier_delayed")
        assert tier["correlation_id"] == "original-turn"
        assert tier["command_id"] == "commit-original"
        assert tier["steps"][0]["kind"] == "tier_result"
        assert next(item for item in logs if item["call_id"] == "original-call")["steps"] == []
    finally:
        release.set()
        asyncio.run(adapter.close())


@pytest.mark.parametrize("kind", ["respond", "prepare_response", "prepare_autonomous_response"])
@pytest.mark.parametrize("call_log_first", [True, False])
def test_monitor_merges_sse_and_call_log_by_span_without_replacing_true_correlation(tmp_path, kind, call_log_first):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    meta = {"span_id": "parse-span", "correlation_id": "turn-7", "event_id": "event-7", "phase": "prepare"}
    stage = {"type": "stage", "run_id": "call-7", "stage": "parse", "latency_ms": 4, "data": {}, "meta": meta}
    payload = {
        "kind": kind, "call_id": "call-7", "correlation_id": "turn-7", "event_id": "event-7",
        "ts": datetime.now(UTC).isoformat(), "latency_ms": 20, "ok": True,
        "steps": [{"kind": "parse", "latency_ms": 4, "data": {}, "meta": meta}],
        "result": {"event_id": "event-7", "path": "deep", "usage": {"total_tokens": 15}},
    }
    path = tmp_path / "call-7.json"
    try:
        if call_log_first:
            store.append_call_log(path, payload)
        store.append_event("cognitive.ui", stage)
        store.append_call_log(path, payload)
        store.append_event("cognitive.ui", {"run_id": "call-7", "type": "state", "data": {}})
        run = store.get_run("call-7")
        assert run["status"] == "completed"
        assert run["correlation_id"] == "turn-7"
        assert run["event_id"] == "event-7"
        assert run["total_tokens"] == 15 and run["path"] == "deep"
        assert len(run["stages"]) == 1
        events = store.list_events(correlation_id="turn-7", limit=20)["items"]
        assert sum(item["topic"] == "cognitive.stage.parse" for item in events) == 1
        assert sum(item["topic"] == "cognitive.state" for item in events) == 1
    finally:
        store.close()


def test_partial_call_without_result_stays_running_and_stage_error_is_failed(tmp_path):
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    try:
        store.append_call_log(tmp_path / "partial.json", {
            "kind": "prepare_response", "call_id": "partial", "ts": datetime.now(UTC).isoformat(),
            "steps": [{"kind": "parse", "data": {}}], "result": None,
        })
        assert store.get_run("partial")["status"] == "running"
        assert store.get_run("partial")["completed_at"] is None
        store.append_event("cognitive.ui", {
            "type": "stage", "run_id": "partial", "stage": "generate", "data": None,
            "meta": {"status": "error", "error_type": "TimeoutError"},
        })
        assert store.get_run("partial")["status"] == "failed"
        assert store.get_run("partial")["error"] == "TimeoutError"
    finally:
        store.close()
