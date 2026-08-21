from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from harness.config import Settings
from harness.core.inference import SituationActor, SituationState, SituationTrigger
from harness.core.models import InteractionEvent, ScoreBreakdown, ScoredMemory
from harness.llm.client import MockLLM
from harness.pipeline.parser import rule_parse
from harness.router.gate import ComplexityGate, collect_signals, score_complexity

TZ = ZoneInfo("Asia/Shanghai")
NOW = datetime(2026, 8, 21, 13, 54, tzinfo=TZ)


def _signals(text: str, *, conflict: bool = False):
    parsed = rule_parse(
        InteractionEvent(timestamp=NOW, speaker="user", raw_text=text, session_id="s")
    )
    memories = []
    if conflict:
        memories = [
            ScoredMemory(
                memory_type="episodic",
                ref="evt_04010",
                relevance=0.91,
                why_retrieved=["entity:user", "time:yesterday", "commitment_conflict"],
                score_breakdown=ScoreBreakdown(),
            )
        ]
    return collect_signals(text, parsed, memories)


def test_fast_path_greetings() -> None:
    for text in ("早上好。", "你在干嘛？", "一起吃饭吗？"):
        score, reasons = score_complexity(_signals(text))
        assert score <= 0.15, (text, score, reasons)
        assert "greeting" in reasons


def test_running_example_near_documented_score() -> None:
    score, reasons = score_complexity(_signals("你昨天不是说不会再管我了吗？", conflict=True))
    assert 0.65 <= score <= 0.85, (score, reasons)
    assert "time:yesterday" in reasons
    assert "commitment" in reasons
    assert "retrieval:commitment_conflict" in reasons


def test_distant_memory_probe_is_deep() -> None:
    score, reasons = score_complexity(_signals("你还记得三个月前答应我的事情吗？"))
    assert score >= 0.7, (score, reasons)
    assert "time:distant" in reasons
    assert "memory_probe" in reasons


def test_value_conflict_is_deepest() -> None:
    score, reasons = score_complexity(_signals("如果救我意味着放弃你一直追求的目标呢？"))
    assert score >= 0.85, (score, reasons)
    assert "value_conflict" in reasons


def _situation(text: str) -> SituationState:
    parsed = rule_parse(
        InteractionEvent(timestamp=NOW, speaker="user", raw_text=text, session_id="s")
    )
    return SituationState(
        trigger=SituationTrigger(ref="evt_x"),
        utterance=text,
        parsed=parsed,
        actor=SituationActor(),
    )


def test_gate_splits_fast_and_deep() -> None:
    gate = ComplexityGate(Settings(complexity_threshold=0.5))

    _, fast = gate.evaluate(_situation("早上好。"))
    assert fast.path == "fast"
    assert fast.trigger_subagents == []

    _, deep = gate.evaluate(_situation("你昨天不是说不会再管我了吗？"))
    assert deep.path == "deep"
    assert "social_interpreter" in deep.trigger_subagents


def test_dated_memory_probe_goes_deep() -> None:
    gate = ComplexityGate(Settings(complexity_threshold=0.5))
    _, request = gate.evaluate(_situation("你还记得昨天你说了什么吗？"))
    assert request.path == "deep"
    assert "social_interpreter" in request.trigger_subagents


class _StubGateLLM:
    def __init__(self, payload: dict | None = None, *, raises: bool = False) -> None:
        self.payload = payload if payload is not None else {"path": "deep", "reason": "probe"}
        self.raises = raises
        self.calls = 0
        self.last_system: str | None = None
        self.last_max_tokens: int | None = None

    def complete_json(self, *, system: str, user: str, max_tokens: int | None = None) -> dict:
        self.calls += 1
        self.last_system = system
        self.last_max_tokens = max_tokens
        if self.raises:
            raise ValueError("boom")
        return dict(self.payload)


# "你还记得上次说的那家店吗？": memory_probe(0.18) + question(0.04) = 0.22,
# below the rule threshold and not forced — the gate model's verdict routes it.
LLM_DECIDED_TEXT = "你还记得上次说的那家店吗？"


def test_llm_gate_decides_deep() -> None:
    llm = _StubGateLLM({"path": "deep", "reason": "relationship probe"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, request = gate.evaluate(_situation(LLM_DECIDED_TEXT))
    assert request.path == "deep"
    assert "social_interpreter" in request.trigger_subagents
    assert "llm_decided:deep" in request.reasons
    assert llm.calls == 1


def test_llm_gate_decides_fast() -> None:
    llm = _StubGateLLM({"path": "fast", "reason": "casual question"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, request = gate.evaluate(_situation(LLM_DECIDED_TEXT))
    assert request.path == "fast"
    assert "llm_decided:fast" in request.reasons


def test_llm_failure_falls_back_to_rule() -> None:
    llm = _StubGateLLM(raises=True)
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, request = gate.evaluate(_situation(LLM_DECIDED_TEXT))
    assert request.path == "fast"
    assert not any(reason.startswith("llm_decided") for reason in request.reasons)


def test_pure_greeting_skips_llm() -> None:
    # 锚定全匹配的纯问候没有第二裁判需要捕捉的歧义，整条琐碎闲聊路径零 LLM 调用。
    llm = _StubGateLLM({"path": "deep", "reason": "overcautious"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, request = gate.evaluate(_situation("早上好。"))
    assert request.path == "fast"
    assert llm.calls == 0
    assert not any(reason.startswith("llm_decided") for reason in request.reasons)


def test_llm_may_promote() -> None:
    # 规则侧判 fast 时小模型是第二裁判：其 deep 裁决一定生效。
    llm = _StubGateLLM({"path": "deep", "reason": "hidden edge"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, request = gate.evaluate(_situation("今天天气怎么样？"))
    assert request.path == "deep"
    assert "social_interpreter" in request.trigger_subagents
    assert "llm_decided:deep" in request.reasons


def test_parser_gate_vote_is_reused() -> None:
    # parser 的合并 LLM 调用已产出 gate 投票时，gate 直接复用，不再付出第二次调用。
    llm = _StubGateLLM({"path": "fast", "reason": "should not be consulted"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    situation = _situation(LLM_DECIDED_TEXT)
    assert situation.parsed is not None
    situation.parsed.gate_vote = "deep"
    _, request = gate.evaluate(situation)
    assert request.path == "deep"
    assert "llm_decided:deep" in request.reasons
    assert llm.calls == 0


def test_gate_llm_call_caps_max_tokens() -> None:
    llm = _StubGateLLM({"path": "fast", "reason": "casual question"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5, llm_json_max_tokens=256), llm=llm)
    gate.evaluate(_situation(LLM_DECIDED_TEXT))
    assert llm.calls == 1
    assert llm.last_max_tokens == 256


def test_rule_deep_not_demotable_by_llm() -> None:
    # OR 合并：规则侧（打分越阈）已判 deep 时结果已定，小模型不再被调用，也无法降级。
    llm = _StubGateLLM({"path": "fast", "reason": "regex over-triggered"})
    gate = ComplexityGate(Settings(complexity_threshold=0.2), llm=llm)
    _, request = gate.evaluate(_situation("你还记得我有多难过吗？我真的好难过。"))
    assert request.path == "deep"
    assert "social_interpreter" in request.trigger_subagents
    assert llm.calls == 0


def test_forced_trigger_skips_llm() -> None:
    llm = _StubGateLLM({"path": "fast"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, forced = gate.evaluate(_situation("你昨天不是说不会再管我了吗？"))
    assert forced.path == "deep"
    assert llm.calls == 0  # force_deep 命中后不再调用小模型


def test_mock_llm_uses_rule_path() -> None:
    llm = MockLLM()
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    _, request = gate.evaluate(_situation(LLM_DECIDED_TEXT))
    assert request.path == "fast"
    assert not any(reason.startswith("llm_decided") for reason in request.reasons)
    assert llm.calls == []


def test_person_name_forces_deep() -> None:
    gate = ComplexityGate(Settings(complexity_threshold=0.5))
    _, request = gate.evaluate(_situation("我昨天认识了一个叫阿哲的人。"))
    assert request.path == "deep"
    assert "social_interpreter" in request.trigger_subagents
    assert "person_name" in request.reasons
    assert "forced:documented_trigger" in request.reasons


def test_known_name_from_memory_forces_deep() -> None:
    situation = _situation("林雪最近怎么样？")
    situation.relevant_memories.append(
        ScoredMemory(
            memory_type="episodic",
            ref="evt_lin",
            relevance=0.8,
            why_retrieved=["entity:林雪"],
            score_breakdown=ScoreBreakdown(),
        )
    )
    gate = ComplexityGate(Settings(complexity_threshold=0.5))
    _, request = gate.evaluate(situation)
    assert request.path == "deep"
    assert "person_name" in request.reasons


def test_quoted_title_forces_deep() -> None:
    gate = ComplexityGate(Settings(complexity_threshold=0.5))
    _, request = gate.evaluate(_situation("你看过《三体》吗？"))
    assert request.path == "deep"
    assert "social_interpreter" in request.trigger_subagents
    assert "unknown_entity" in request.reasons


def test_gate_prompt_states_mandatory_name_rule() -> None:
    llm = _StubGateLLM({"path": "deep", "reason": "new person"})
    gate = ComplexityGate(Settings(complexity_threshold=0.5), llm=llm)
    gate.evaluate(_situation(LLM_DECIDED_TEXT))
    system = llm.last_system or ""
    assert "MANDATORY deep path" in system
    assert "person by name" in system
