"""All expression routes consume a prepared brief without observing again."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from harness.cognitive import board, capabilities, goals
from harness.cognitive.autonomous import _assessment_messages
from harness.compiler.compiler import ContextCompiler, command_emotion_context
from harness.config import Settings
from harness.core.inference import ContextSegment, StructuredInsight
from harness.core.models import RelationshipState
from harness.llm.client import MockLLM
from harness.pipeline.pipeline import InferencePipeline
from harness.validator.validator import rule_check, validate_emotion_expression, validate_voice_emotion
from tests.factories import vaelith
from tests.test_capability_cognition import FakeLLM, command, host, offer
from tests.test_compiler import NOW, _homeostatic_summary, _situation
from tests.test_goal_cognition import goal_command, start_choice


BRIEF = "这句话让你非常生气，暂时直白而坚定地说明边界。"


def test_compiler_uses_one_prepared_brief_and_removes_competing_emotion_parameters():
    situation = _situation()
    situation.relationship = RelationshipState(target="user", closeness=0.2, updated_at=NOW)
    context = ContextCompiler(Settings(emotion_mode="on")).compile(
        situation, homeostatic_interoception=_homeostatic_summary(),
        emotion_guidance={"text": BRIEF, "context_id": "emotion:1"},
        insights=[StructuredInsight(recommended_stance="OLD_STANCE", constraint="keep_facts")],
        additional_segments=[ContextSegment(kind="emotion_guidance", text="DUPLICATE_BRIEF")],
    )
    messages = json.dumps(context.render_messages(), ensure_ascii=False)
    assert sum(segment.kind == "emotion_guidance" for segment in context.segments) == 1
    assert BRIEF in messages and "性格特质与表达习惯描述通常状态" in messages
    assert all(token not in messages for token in ("OLD_STANCE", "DUPLICATE_BRIEF", "closeness=", "当前心理", "需要低压力"))
    assert any(entry.source_ids == ["emotion:1"] for entry in context.provenance)
    assert "keep_facts" in messages


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_inactive_or_shadow_preserves_old_prompt(mode):
    context = ContextCompiler(Settings(emotion_mode=mode)).compile(
        _situation(), homeostatic_interoception=_homeostatic_summary(), emotion_guidance=BRIEF)
    messages = json.dumps(context.render_messages(), ensure_ascii=False)
    assert BRIEF not in messages and "当前心理" in messages
    assert any(segment.kind == "homeostatic_interoception" for segment in context.segments)


def test_on_keeps_critical_guard_and_falls_back_without_a_second_model_call():
    compiler = ContextCompiler(Settings(emotion_mode="on"))
    context = compiler.compile(_situation(), homeostatic_interoception=_homeostatic_summary())
    assert any(segment.kind == "emotion_guidance" for segment in context.segments)
    assert not any(segment.kind == "psych" for segment in context.segments)
    with pytest.raises(ValueError, match="critical"):
        compiler.compile(_situation(), homeostatic_interoception=_homeostatic_summary(salience="critical"),
                         emotion_guidance=BRIEF)


def test_prepare_and_revision_reuse_guidance_without_observing_or_committing(service):
    llm = MockLLM(chat_script=["我的内稳态 cognitive_load 偏离度=0.75。"],
                  chat_routes={"Revision instruction": "这句话让我很生气。"})
    pipeline = InferencePipeline(service, settings=Settings(emotion_mode="on", validator_sample_rate=0,
        continuation_enabled=False, record_interaction=False), llm=llm)
    pipeline.emotion_service = SimpleNamespace(observe=lambda value: pytest.fail("prepare must not observe"))
    before = pipeline.session_store.snapshot()
    result = pipeline.prepare("你好。", now=NOW, session_id="emotion-session", emotion_guidance=BRIEF)
    assert result.text == "这句话让我很生气。" and result.revisions == 1
    assert pipeline.session_store.snapshot() == before
    assert all(sum(message["content"].count(BRIEF) for message in call) == 1 for call in llm.chat_calls)


def _owner(mode="on", output=None):
    owner = host(FakeLLM(output))
    owner.chat.settings = Settings(emotion_mode=mode)
    owner.chat.memory.get_persona = lambda: vaelith()
    owner.guidance_calls = []

    async def guidance(request):
        owner.guidance_calls.append(request.command_id)
        return SimpleNamespace(text=BRIEF, context_id="emotion:1")

    owner.adapter = SimpleNamespace(emotion_guidance=guidance, _ledger={})
    return owner


@pytest.mark.parametrize("mode", ["on", "shadow"])
@pytest.mark.parametrize("route", ["capability", "goal"])
def test_direct_decisions_keep_tool_data_and_use_only_visible_emotion(mode, route):
    owner = _owner(mode, start_choice(action=True) if route == "goal" else None)
    request = goal_command() if route == "goal" else command()
    request.payload.update(emotion_context_ref="emotion:1", emotion_revision=1)
    asyncio.run((goals.prepare if route == "goal" else capabilities.prepare)(owner, request))
    messages = owner.chat.llm.calls[0][0]
    payload = json.loads(messages[1]["content"])
    assert payload["available_offers"][0]["offer_id"] == offer().offer_id
    assert "verified-shared-recall" in messages[1]["content"]
    assert ("homeostatic_summary" in payload) is (mode == "shadow")
    system = "\n".join(item["content"] for item in messages if item["role"] == "system")
    assert (BRIEF in system) is (mode == "on")
    assert BRIEF not in messages[0]["content"]
    if mode == "on":
        assert messages[-1]["role"] == "system" and BRIEF in messages[-1]["content"]
    assert owner.guidance_calls == [request.command_id]
    assert "emotion_context_ref" not in payload and "emotion_revision" not in payload


def test_only_runtime_emotion_fields_are_removed_before_strict_validation():
    payload = {"emotion_context_ref": "emotion:1", "emotion_revision": 2, "unknown": "reject me"}
    clean, reference = command_emotion_context(None, payload)
    assert clean == {"unknown": "reject me"} and reference == "emotion:1"
    assert "emotion_context_ref" in payload
    owner = _owner()
    request = command(unknown="reject me", emotion_context_ref="emotion:1")
    with pytest.raises(capabilities.CapabilitySelectionError, match="invalid_request"):
        asyncio.run(capabilities.prepare(owner, request))
    assert not owner.guidance_calls


def test_autonomous_assessment_uses_the_same_brief_without_raw_psychology(service):
    pipeline = InferencePipeline(service, settings=Settings(emotion_mode="on"))
    messages = _assessment_messages(pipeline=pipeline, evaluated_at=NOW, session_id="test", channel="chat",
        homeostasis={}, policies=[], homeostatic_interoception=_homeostatic_summary(), emotion_guidance=BRIEF)
    text = json.dumps(messages, ensure_ascii=False)
    assert text.count(BRIEF) == 1 and "近期心理" not in text and "亲近=" not in text


@pytest.mark.parametrize("route", ["capability", "goal"])
def test_direct_voice_script_is_guarded_before_rendering(route):
    voice = offer(channel="voice", capability_id="expression.voice.render.v1")
    choice = {"kind": "act", "offer_id": voice.offer_id, "arguments": {"script": {"segments": [
        {"kind": "speech", "segment_id": "s1", "text": "我的内稳态 cognitive_load 偏离度=0.75。",
         "style": "angry", "strength": "high"}]}}}
    output = {**start_choice(action=True), "choice": choice} if route == "goal" else choice
    owner = _owner(output=output)
    request = goal_command([voice]) if route == "goal" else command([voice])
    request.payload.update(emotion_context_ref="emotion:1", emotion_revision=1)
    with pytest.raises(ValueError, match="emotion_expression_rejected"):
        asyncio.run((goals.prepare if route == "goal" else capabilities.prepare)(owner, request))


def test_board_uses_prepared_emotion_and_preserves_shared_source_data(monkeypatch):
    owner = _owner(output={"action": "write", "title": "", "text": "这句话让我很生气。"})
    owner.shared.board_context = lambda **kwargs: {"entries": [{"record_id": "source:1", "text": "verified-board-context"}]}
    monkeypatch.setattr(board, "_settings", lambda *_: SimpleNamespace(
        autonomous_enabled=True, user_model_seconds=20, autonomous_model_seconds=20, model_max_tokens=512))
    request = command()
    request = request.model_copy(update={"payload": {
        "entry_id": "entry:1", "source_event_id": request.event_id, "body_instance_id": "body:1", "board_id": "board:1",
        "deadline_at": request.deadline.isoformat(), "trigger": "user_request", "request_text": "写下来",
        "homeostatic_summary": _homeostatic_summary(), "emotion_context_ref": "emotion:1", "emotion_revision": 1}})
    result = asyncio.run(board.prepare(owner, request))
    assert result["decision"]["text"] == "这句话让我很生气。"
    messages = owner.chat.llm.calls[0][0]
    assert sum(item["content"].count(BRIEF) for item in messages if item["role"] == "system") == 1
    assert BRIEF not in messages[0]["content"]
    assert messages[-1]["role"] == "system" and BRIEF in messages[-1]["content"]
    assert "homeostatic_summary" not in messages[1]["content"] and "verified-board-context" in messages[1]["content"]


def test_strong_style_is_allowed_but_physical_fabrication_is_not():
    validate_voice_emotion({"script": {"segments": [{"kind": "speech", "text": "别再这样说了。",
        "style": "angry", "strength": "high"}]}}, persona=vaelith(), guidance=BRIEF)
    with pytest.raises(ValueError, match="emotion_expression_rejected"):
        validate_emotion_expression("我饿得胃疼。", persona=vaelith(), guidance=BRIEF)


def test_emotion_override_preserves_explicit_values_and_rejects_new_mechanism_names():
    persona = vaelith()
    persona.identity.values.append("不直接表达关心")
    assert "direct_care_expression" in rule_check("我很在乎你。", persona=persona, insights=[],
                                                  emotion_guidance=BRIEF).violations
    with pytest.raises(ValueError, match="emotion_expression_rejected"):
        validate_emotion_expression("我的情绪权重为0.9。", persona=persona, guidance=BRIEF)
