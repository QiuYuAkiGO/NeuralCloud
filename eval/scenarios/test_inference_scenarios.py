"""Eval scenarios for the online inference path (02-inference-flow.md)."""

from __future__ import annotations

import json
from pathlib import Path

from harness.config import Settings
from harness.core.models import InteractionEvent
from harness.pipeline.pipeline import InferencePipeline
from infra.trace.recorder import JsonlTracer
from tests.factories import NOW, seed_running_example


def test_fast_path_does_not_trigger_subagent(service) -> None:
    pipeline = InferencePipeline(
        service,
        settings=Settings(validator_sample_rate=0.0, record_interaction=False),
    )
    final = pipeline.respond("早上好。", now=NOW, session_id="sess_eval_fast")
    assert final.path == "fast"
    assert final.insights == []
    assert final.revisions == 0


def test_validator_revision_loop(service) -> None:
    seed_running_example(service)
    pipeline = InferencePipeline(
        service,
        settings=Settings(validator_sample_rate=1.0, max_revisions=2, record_interaction=False),
    )
    final = pipeline.respond(
        InteractionEvent(
            event_id="evt_04030",
            timestamp=NOW,
            speaker="user",
            raw_text="你昨天不是说不会再管我了吗？",
            session_id="sess_0881",
        ),
        now=NOW,
    )
    assert final.path == "deep"
    assert final.revisions >= 1
    assert final.validation is not None
    assert final.validation.verdict == "pass"
    assert "对不起" not in final.text
    assert "担心你" not in final.text


def test_inference_trace_complete(service, tmp_path: Path) -> None:
    seed_running_example(service)
    path = tmp_path / "eval_inference.jsonl"
    tracer = JsonlTracer(path, settings=service.settings)
    service.tracer = tracer
    pipeline = InferencePipeline(
        service,
        tracer=tracer,
        settings=Settings(validator_sample_rate=1.0, record_interaction=False),
    )
    pipeline.respond(
        InteractionEvent(
            timestamp=NOW,
            speaker="user",
            raw_text="你昨天不是说不会再管我了吗？",
            session_id="sess_0881",
        ),
        now=NOW,
    )
    records = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    kinds = {row["kind"] for row in records}
    assert {"parse", "state_build", "gate", "interpret", "compile", "generate", "validate", "final"} <= kinds
    staged = [row for row in records if row["kind"] in {"parse", "gate", "compile", "generate", "validate"}]
    assert all("latency_ms" in row for row in staged)
    provenance_compile = next(row for row in records if row["kind"] == "compile")
    assert provenance_compile["path"] == "deep"
