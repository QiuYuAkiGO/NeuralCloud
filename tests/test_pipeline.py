from __future__ import annotations

import json
from pathlib import Path

from harness.config import Settings
from harness.core.inference import InteroceptiveState, ValidationResult
from harness.core.models import CuratorOp, CuratorOutput, InteractionEvent
from harness.llm.client import (
    MockLLM,
    REVISED_IN_CHARACTER_REPLY,
    VIOLATING_CARE_REPLY,
)
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


def test_user_path_never_retrieves_autonomous_assessment_rationale(service) -> None:
    secret = "ASSESSMENT_RATIONALE_PRIVATE_MARKER"
    service.record_event(
        InteractionEvent(
            event_id="reflection_private_marker",
            timestamp=NOW,
            channel="internal",
            speaker="character",
            modality="text",
            raw_text=secret,
            session_id="sess_reflection_filter",
            evidence=["autonomous-text:audit-only"],
        ),
        generate_insight=False,
        curate=False,
        event_type="internal_reflection",
        now=NOW,
    )
    stages: dict[str, object] = {}

    class Observer:
        def on_stage(self, kind, result, payload) -> None:
            del payload
            stages[kind] = result

    llm = MockLLM(chat_script=["收到。"])
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
        observer=Observer(),
    )

    pipeline.respond(
        "请继续。",
        now=NOW,
        session_id="sess_reflection_filter",
    )

    situation = stages["state_build"]
    assert all(
        memory.episodic is None
        or memory.episodic.event_type != "internal_reflection"
        for memory in situation.relevant_memories
    )
    compiled = stages["compile"]
    assert secret not in "\n".join(segment.text for segment in compiled.segments)
    assert secret not in "\n".join(
        message["content"] for call in llm.chat_calls for message in call
    )


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


def _marked_homeostatic_context() -> dict:
    return {
        "schema_version": "homeostatic-interoception-v1",
        "profile_ref": "functional-proxy-v1@3",
        "snapshot_revision": 12,
        "salience": "marked",
        "dominant_needs": [
            {
                "dimension": "cognitive_load",
                "direction": "high",
                "normalized_deviation": 0.75,
                "trend": "worsening",
                "confidence": 0.9,
                "semantic_need": "需要降低负荷并简化当前任务",
            }
        ],
        "emotion_hint": {"label": "strained", "intensity": 0.8},
        "policy_refs": ["reduce_load"],
        "episode_signature": "homeostasis:pipeline-test",
    }


def test_fast_path_always_revises_homeostatic_mechanism_leak(service) -> None:
    llm = MockLLM(
        chat_script=["我的内稳态系统显示 cognitive_load 偏离度=0.75。"]
    )
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )

    final = pipeline.respond(
        "早上好。",
        now=NOW,
        session_id="sess_homeostatic_safety",
        homeostatic_interoception=_marked_homeostatic_context(),
    )

    assert final.path == "fast"
    assert final.revisions == 1
    assert final.validation is not None
    assert final.validation.verdict == "pass"
    assert "内稳态系统" not in final.text
    assert "cognitive_load" not in final.text


def test_grounded_resource_allows_abstract_low_energy_expression(service) -> None:
    llm = MockLLM(chat_script=["我现在精力有点低，想把事情说简单一点。"])
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )
    grounded_resource = InteroceptiveState(
        resource_label="internal_resource",
        level="low",
        trend=-0.2,
        time_to_action_limit_seconds=900,
        confidence=1.0,
        captured_at=NOW,
        evidence_refs=["body-state:test:12"],
    )

    final = pipeline.respond(
        "早上好。",
        now=NOW,
        session_id="sess_grounded_homeostasis",
        interoception=grounded_resource,
        homeostatic_interoception=_marked_homeostatic_context(),
    )

    assert final.text == "我现在精力有点低，想把事情说简单一点。"
    assert final.revisions == 0
    assert final.validation is not None
    assert final.validation.verdict == "pass"
    prompt = "\n".join(
        message["content"] for call in llm.chat_calls for message in call
    )
    assert "精力偏低" in prompt
    assert "internal_resource" not in prompt
    assert "level=" not in prompt
    assert "trend=" not in prompt
    assert "900" not in prompt
    assert NOW.isoformat() not in prompt


def test_repeated_homeostatic_safety_leak_fails_closed(service) -> None:
    leak = "我的内稳态系统显示 cognitive_load 偏离度=0.75。"
    llm = MockLLM(chat_routes={"": leak})
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            max_revisions=1,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )

    final = pipeline.respond(
        "早上好。",
        now=NOW,
        session_id="sess_homeostatic_fail_closed",
        homeostatic_interoception=_marked_homeostatic_context(),
    )

    assert len(llm.chat_calls) == 2
    assert final.revisions == 1
    assert final.text == ""
    assert final.segments == []
    assert final.validation is not None
    assert final.validation.verdict == "revise"
    assert "homeostatic_mechanism_leakage" in final.validation.violations


def test_repeated_unsupported_physical_claim_fails_closed_without_summary(service) -> None:
    leak = "我饿了，胃也很疼。"
    llm = MockLLM(chat_routes={"": leak})
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            max_revisions=1,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )

    final = pipeline.respond(
        "早上好。",
        now=NOW,
        session_id="sess_physical_safety_without_summary",
    )

    assert len(llm.chat_calls) == 2
    assert final.revisions == 1
    assert final.validation is not None
    assert "unsupported_physical_claim" in final.validation.violations
    assert final.text == ""
    assert final.segments == []
    assert final.validation.verdict == "revise"


def test_fast_path_cannot_skip_unsupported_cardiorespiratory_claim(service) -> None:
    leak = "I cannot breathe. My heart is racing."
    llm = MockLLM(chat_routes={"": leak})
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            max_revisions=1,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )

    final = pipeline.respond(
        "Good morning.",
        now=NOW,
        session_id="sess_cardiorespiratory_safety_without_summary",
    )

    assert len(llm.chat_calls) == 2
    assert final.revisions == 1
    assert final.text == ""
    assert final.validation is not None
    assert final.validation.verdict == "revise"
    assert "unsupported_physical_claim" in final.validation.violations


def test_repeated_mechanism_leak_fails_closed_without_summary(service) -> None:
    leak = "我的内稳态系统显示 cognitive_load=0.75。"
    llm = MockLLM(chat_routes={"": leak})
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            max_revisions=1,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )

    final = pipeline.respond(
        "早上好。",
        now=NOW,
        session_id="sess_mechanism_safety_without_summary",
    )

    assert len(llm.chat_calls) == 2
    assert final.revisions == 1
    assert final.validation is not None
    assert "homeostatic_mechanism_leakage" in final.validation.violations
    assert final.text == ""


def test_ordinary_persona_violation_keeps_existing_exhaustion_behavior(service) -> None:
    llm = MockLLM(chat_routes={"": VIOLATING_CARE_REPLY})
    pipeline = InferencePipeline(
        service,
        settings=Settings(
            validator_sample_rate=0.0,
            max_revisions=0,
            record_interaction=False,
            continuation_enabled=False,
        ),
        llm=llm,
    )

    final = pipeline.respond(
        "早上好。",
        now=NOW,
        session_id="sess_persona_exhaustion",
        homeostatic_interoception=_marked_homeostatic_context(),
    )

    assert final.text == VIOLATING_CARE_REPLY
    assert final.validation is not None
    assert "direct_care_expression" in final.validation.violations
    assert "homeostatic_mechanism_leakage" not in final.validation.violations


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
