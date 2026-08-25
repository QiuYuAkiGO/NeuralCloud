from __future__ import annotations

import json
from pathlib import Path

from harness.config import Settings
from harness.core.inference import ValidationResult
from harness.core.models import CuratorOp, CuratorOutput, InteractionEvent
from harness.llm.client import REVISED_IN_CHARACTER_REPLY
from harness.pipeline.gateway import Gateway
from harness.pipeline.pipeline import InferencePipeline
from infra.trace.recorder import JsonlTracer
from tests.factories import NOW, seed_running_example


def test_running_example_end_to_end(service) -> None:
    seed_running_example(service)
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            complexity_threshold=0.5,
            validator_sample_rate=1.0,
            max_revisions=2,
            record_interaction=True,
            context_token_budget=3000,
            compiler_top_memories=5,
            session_window=10,
        ),
    )
    event = InteractionEvent(
        event_id="evt_04030",
        timestamp=NOW,
        speaker="user",
        raw_text="你昨天不是说不会再管我了吗？",
        session_id="sess_0881",
        evidence=["chat:sess_0881:evt_04030"],
    )
    final = pipeline.respond(event, now=NOW)
    assert final.path == "deep"
    assert final.complexity_score >= 0.5
    assert final.insights
    assert final.insights[0].constraint == "avoid_explicit_psychological_explanation"
    assert final.revisions >= 1
    assert "对不起" not in final.text
    assert "担心你" not in final.text
    assert "寻求安全感" not in final.text
    assert final.validation is not None
    assert final.validation.verdict == "pass"
    assert final.text == REVISED_IN_CHARACTER_REPLY
    assert service.stores.episodic.get("evt_04030") is not None
    assert final.usage.calls >= 2
    assert final.usage.revision_calls >= 1
    assert final.usage.total_tokens > final.usage.revision_tokens > 0
    assert final.latency_ms >= 0.0


def test_greeting_uses_fast_path(service) -> None:
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            complexity_threshold=0.5,
            validator_sample_rate=0.0,
            record_interaction=False,
        ),
    )
    final = pipeline.respond("早上好。", now=NOW, session_id="sess_hi")
    assert final.path == "fast"
    assert final.insights == []
    assert final.revisions == 0
    assert final.validation is not None
    assert final.validation.skipped is True
    assert final.usage.calls == 1
    assert final.usage.revision_calls == 0
    assert final.usage.total_tokens > 0
    assert final.latency_ms >= 0.0


class _SpyValidator:
    def __init__(self) -> None:
        self.memory_empty_seen: list[bool] = []

    def should_run(self, path: str) -> bool:
        return True

    def check(self, candidate, *, persona, insights=None, memory_empty=False) -> ValidationResult:
        self.memory_empty_seen.append(memory_empty)
        return ValidationResult(verdict="pass", violations=[], severity=0.0, instruction=None)


def test_memory_empty_flag_reaches_validator(service) -> None:
    spy = _SpyValidator()
    pipeline = InferencePipeline(
        service,
        settings=Settings(record_interaction=False, continuation_enabled=False),
        validator=spy,
    )
    final = pipeline.respond("你好。", now=NOW, session_id="sess_spy_empty")
    assert final.text
    assert spy.memory_empty_seen == [True]


def test_memory_present_flag_is_false(service) -> None:
    seed_running_example(service)
    spy = _SpyValidator()
    pipeline = InferencePipeline(
        service,
        settings=Settings(record_interaction=False, continuation_enabled=False),
        validator=spy,
    )
    final = pipeline.respond("你好。", now=NOW, session_id="sess_spy_full")
    assert final.text
    assert spy.memory_empty_seen == [False]


def test_respond_survives_invalid_curator_op(service) -> None:
    class BadCurator:
        def propose(self, event, *, persona, open_candidates):
            return CuratorOutput(ops=[CuratorOp(op="UPDATE", type="candidate")])

    service.curator = BadCurator()
    pipeline = InferencePipeline(
        service,
        settings=Settings(record_interaction=True, validator_sample_rate=0.0),
    )
    final = pipeline.respond("早上好。", now=NOW, session_id="sess_curator")
    assert final.text
    assert final.path == "fast"


def test_session_window_keeps_recent_turns(service) -> None:
    pipeline = InferencePipeline(
        service,
        settings=Settings(session_window=4, record_interaction=False, validator_sample_rate=0.0),
    )
    pipeline.respond("早上好。", now=NOW, session_id="sess_w")
    pipeline.respond("你在干嘛？", now=NOW, session_id="sess_w")
    turns = pipeline.session_store.recent("sess_w")
    assert len(turns) == 4
    assert turns[0].role == "user"
    assert turns[-1].role == "character"


def test_custom_speaker_name_in_session_and_event(service) -> None:
    pipeline = InferencePipeline(
        service,
        settings=Settings(record_interaction=True, validator_sample_rate=0.0),
    )
    pipeline.respond("你好。", now=NOW, session_id="sess_name", speaker="阿宁")
    turns = pipeline.session_store.recent("sess_name")
    assert turns[0].role == "user"
    assert turns[0].speaker == "阿宁"
    character_turns = [turn for turn in turns if turn.role == "character"]
    assert character_turns
    assert character_turns[0].speaker == "Vaelith"
    stored = next(
        event
        for event in service.stores.episodic.list_all()
        if "阿宁" in event.participants and event.session_id == "sess_name"
    )
    assert "user" in stored.participants
    assert "character" in stored.participants


def test_gateway_standardizes_raw_text() -> None:
    event = Gateway().ingest("你好", session_id="sess_g", speaker="user")
    assert event.raw_text == "你好"
    assert event.session_id == "sess_g"
    assert event.channel == "chat"
    assert event.speaker == "user"


def test_trace_covers_inference_stages(service, tmp_path: Path) -> None:
    seed_running_example(service)
    path = tmp_path / "inference.jsonl"
    tracer = JsonlTracer(path, settings=service.settings)
    service.tracer = tracer
    pipeline = InferencePipeline(
        service,
        tracer=tracer,
        settings=Settings(
            complexity_threshold=0.5,
            validator_sample_rate=1.0,
            max_revisions=2,
            record_interaction=False,
        ),
    )
    pipeline.respond(
        InteractionEvent(
            event_id="evt_04030",
            timestamp=NOW,
            speaker="user",
            raw_text="你昨天不是说不会再管我了吗？",
            session_id="sess_0881",
        ),
        now=NOW,
    )
    kinds = [json.loads(line)["kind"] for line in path.read_text(encoding="utf-8").splitlines()]
    for required in ("parse", "state_build", "gate", "interpret", "compile", "generate", "validate", "final"):
        assert required in kinds, kinds
    rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    assert any(row.get("latency_ms", -1) >= 0 for row in rows)
    final_row = next(row for row in rows if row.get("kind") == "final")
    assert final_row.get("total_latency_ms", -1) >= 0
