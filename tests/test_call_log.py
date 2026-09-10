from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from harness.core.exceptions import EvidenceRequiredError, PersonaLockedError
from harness.core.models import EpisodicEvent, RetrievalQuery, SemanticFact
from harness.pipeline.pipeline import InferencePipeline
from infra.trace.call_log import list_call_logs
from infra.trace.serialize import dump_value
from tests.factories import NOW, seed_running_example


def _enable_call_log(service, tmp_path: Path) -> None:
    service.settings.call_log_enabled = True
    service.settings.trace_dir = tmp_path


def _payloads(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(path.read_text(encoding="utf-8")) for path in list_call_logs(tmp_path)]


def _has_key(value: Any, key: str) -> bool:
    if isinstance(value, dict):
        if key in value:
            return True
        return any(_has_key(item, key) for item in value.values())
    if isinstance(value, list):
        return any(_has_key(item, key) for item in value)
    return False


def test_dump_value_strips_embedding() -> None:
    dumped = dump_value({"embedding": [0.1, 0.2], "nested": {"embedding": [1.0], "ok": True}})
    assert dumped == {"nested": {"ok": True}}


def test_respond_success_writes_one_call_log(service, tmp_path: Path) -> None:
    _enable_call_log(service, tmp_path)
    pipeline = InferencePipeline(service, settings=service.settings)
    final = pipeline.respond("早上好。", now=NOW, session_id="sess_hi", call_id="run_calllog")
    files = list_call_logs(service.settings)
    assert len(files) == 1
    assert files[0].name == "run_calllog.json"
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["ok"] is True
    assert payload["kind"] == "respond"
    assert payload["call_id"] == "run_calllog"
    assert payload["result"]["text"] == final.text
    kinds = [step["kind"] for step in payload["steps"]]
    assert kinds[0] == "ingest"
    assert "parse" in kinds
    assert "state_build" in kinds
    assert "gate" in kinds
    assert "compile" in kinds
    assert "generate" in kinds
    assert "final" in kinds
    assert "retrieval" in kinds


def test_respond_error_writes_partial_evidence(service, tmp_path: Path) -> None:
    _enable_call_log(service, tmp_path)
    pipeline = InferencePipeline(service, settings=service.settings)

    def boom(*_args: Any, **_kwargs: Any) -> Any:
        raise RuntimeError("parse failed")

    pipeline.parser.parse = boom  # type: ignore[method-assign]
    with pytest.raises(RuntimeError, match="parse failed"):
        pipeline.respond("早上好。", now=NOW, session_id="sess_hi")
    payload, = _payloads(tmp_path)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "RuntimeError"
    assert payload["latency_ms"] >= 0
    assert [step["kind"] for step in payload["steps"]] == ["ingest", "parse"]
    assert payload["steps"][-1]["meta"]["status"] == "error"


def test_respond_nests_record_event(service, tmp_path: Path) -> None:
    _enable_call_log(service, tmp_path)
    pipeline = InferencePipeline(service, settings=service.settings)
    pipeline.respond("早上好。", now=NOW, session_id="sess_hi")
    payloads = _payloads(tmp_path)
    assert [item["kind"] for item in payloads] == ["respond"]
    kinds = [step["kind"] for step in payloads[0]["steps"]]
    assert kinds.count("episodic_write") >= 2
    assert "final" in kinds


def test_query_success_strips_embeddings(service, tmp_path: Path) -> None:
    seed_running_example(service)
    stored = service.stores.episodic.get("evt_04010")
    assert stored is not None and stored.embedding
    assert not _has_key(dump_value(stored), "embedding")
    _enable_call_log(service, tmp_path)
    results = service.query(RetrievalQuery(text="你以后不用管我了", now=NOW, session_id="sess_0881"))
    assert results
    dumped_live = dump_value(results)
    assert not _has_key(dumped_live, "embedding")
    service.wait_background()
    payloads = [item for item in _payloads(tmp_path) if item["kind"] == "query"]
    assert len(payloads) == 1
    assert payloads[0]["ok"] is True
    assert not _has_key(payloads[0], "embedding")
    assert payloads[0]["steps"]
    assert payloads[0]["steps"][0]["kind"] == "retrieval"


def test_failed_memory_write_logs_failure_evidence(service, tmp_path: Path) -> None:
    seed_running_example(service)
    _enable_call_log(service, tmp_path)
    event = service.stores.episodic.get("evt_04010")
    assert event is not None
    broken = EpisodicEvent.model_construct(**{**event.model_dump(), "evidence": []})
    with pytest.raises(EvidenceRequiredError):
        service.record_event(broken, generate_insight=False, curate=False, now=NOW)
    fact = SemanticFact.model_construct(
        fact_id="sem_bad",
        subject="user",
        predicate="likes",
        value="cats",
        confidence=0.8,
        supporting_events=[],
        formed_at=NOW,
    )
    with pytest.raises(EvidenceRequiredError):
        service.force_write_semantic(fact)
    persona = service.get_persona()
    assert persona is not None
    with pytest.raises(PersonaLockedError):
        service.update_persona(persona)
    payloads = _payloads(tmp_path)
    assert {payload["kind"] for payload in payloads} == {"record_event", "force_write_semantic"}
    assert all(payload["ok"] is False for payload in payloads)
    assert all(payload["error"]["type"] == "EvidenceRequiredError" for payload in payloads)
    assert all(payload["latency_ms"] >= 0 for payload in payloads)
