"""Expression policy stays scoped, advisory and distinct from factual authority."""
import hashlib
import json

import pytest

from harness.compiler.compiler import ContextCompiler
from harness.compiler.expression import EXPRESSION_RULES
from harness.config import Settings
from harness.core.expression import (ReplyConstraint, ResponseExpressionContext, EXPRESSION_VERSION,
                                     mode_for, qq_context)
from harness.core.inference import (CandidateResponse, ContextSegment, MemoryEvidenceContext,
                                    SituationActor, SituationState, SituationTrigger)
from harness.qq import decision
from harness.qq.policy import QQLatencyOptions
from harness.qq.turn_checkpoints import QQTurnStateError
from tests.test_qq_decision import setup as setup
from tests.test_qq_prepared_turn import _prepare, _resume
from tests.test_semantic_validator import validator, report, claim


def expression(mode="enforce", **kwargs):
    return ResponseExpressionContext(mode=mode, audience="local:main", **kwargs)


def situation():
    return SituationState(trigger=SituationTrigger(ref="current"), utterance="这张蓝白配色真清爽。",
                          actor=SituationActor())


def configure(setup, mode):
    path = setup[3]["config_path"]
    config = json.loads(path.read_text(encoding="utf-8"))
    config["qq_latency"] = {**config.get("qq_latency", {}), "reply_internalization_mode": mode,
                            "reply_internalization_conversations": ["29999/10001"]}
    config["validator_enabled"] = False
    path.write_text(json.dumps(config), encoding="utf-8")


def test_policy_is_independent_scoped_and_old_hash_compatible():
    policy = QQLatencyOptions()
    old = policy.model_dump(exclude={"reply_internalization_mode", "reply_internalization_conversations"})
    assert policy.version == "qq-policy:" + hashlib.sha256(json.dumps(old, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    policy = policy.model_copy(update={"reply_internalization_mode": "enforce",
                                       "reply_internalization_conversations": ["29999/10001"]})
    assert mode_for(policy, "29999/10001") == "enforce"
    assert mode_for(policy, "29998/10001") == mode_for(policy, "local:main") == "off"
    assert policy.qq_semantic_alignment_mode == "off"
    assert QQLatencyOptions(reply_internalization_conversations=["local:main"])
    assert QQLatencyOptions.validate_conversations(["29999/10001"]) == ["29999/10001"]
    with pytest.raises(ValueError):
        QQLatencyOptions.validate_conversations(["local:main"])
    with pytest.raises(ValueError):
        QQLatencyOptions(reply_internalization_conversations=["29999/private:29998:20002"])


@pytest.mark.parametrize("mode", ["off", "observe", "enforce"])
def test_local_compiler_changes_generation_only_in_enforce(mode):
    settings = Settings(_env_file=None, reply_internalization_mode=mode,
                        reply_internalization_conversations=["local:main"])
    context = ContextCompiler(settings).compile(situation())
    rendered = json.dumps(context.render_messages(), ensure_ascii=False)
    assert (EXPRESSION_VERSION in rendered) == (mode == "enforce")
    assert (context.response_expression is None) == (mode == "off")
    assert ("response_expression" in rendered) is False


def test_foreign_expression_context_is_rejected_and_trimmed_refs_lose_metadata():
    compiler = ContextCompiler(Settings(_env_file=None))
    segment = ContextSegment(kind="user", text="内容", response_expression=expression(
        internal_constraints=[ReplyConstraint(subject="角色", handling="qualify", source_ids=["missing"])]))
    context = compiler.compile(situation(), additional_segments=[segment])
    assert context.response_expression.internal_constraints == []
    with pytest.raises(ValueError, match="audience"):
        compiler.compile(situation(), additional_segments=[segment],
                         memory_evidence=MemoryEvidenceContext(audience="foreign"))


def test_local_memory_context_uses_shared_contract_without_enabling_foreign_scope():
    compiler = ContextCompiler(Settings(_env_file=None, reply_internalization_mode="enforce",
                                         reply_internalization_conversations=["local:main"]))
    local = compiler.compile(situation(), memory_evidence=MemoryEvidenceContext(audience="local:main"))
    assert local.response_expression.mode == "enforce"
    foreign = compiler.compile(situation(), memory_evidence=MemoryEvidenceContext(audience="qq:group:10001"))
    assert foreign.response_expression is None


@pytest.mark.parametrize("enabled,sample_rate", [(False, 1.0), (True, 0.0)])
def test_skipped_critic_records_missing_diagnostics_without_extra_calls(service, enabled, sample_rate):
    from harness.pipeline.pipeline import InferencePipeline
    pipeline = InferencePipeline(service, settings=Settings(_env_file=None,
        record_interaction=False, continuation_enabled=False, validator_enabled=enabled,
        validator_sample_rate=sample_rate, reply_internalization_mode="observe",
        reply_internalization_conversations=["local:main"]))
    final = pipeline.respond("早上好。", session_id="expression")
    assert final.validation.expression["status"] == "unavailable"
    assert final.validation.skipped and final.revisions == 0 and final.usage.calls == 1


def test_old_boundary_is_internal_and_no_disclosure_is_invented():
    plan = decision.QQReplyPlan(necessary_boundary="角色身份尚不确定")
    context = qq_context(Settings(_env_file=None, reply_internalization_mode="enforce",
                                 reply_internalization_conversations=None), group_id="10001", self_member_id="29999", plan=plan)
    assert context.internal_constraints[0].handling == "qualify"
    assert context.disclosure_requirements == []
    assert "internal_constraints" not in plan.model_dump()


def test_mixed_legacy_and_new_constraints_preserve_both():
    plan = decision.QQReplyPlan(necessary_boundary="换装名未知", internal_constraints=[
        ReplyConstraint(subject=f"约束{i}", handling="omit") for i in range(4)])
    context = qq_context(Settings(_env_file=None, reply_internalization_mode="enforce",
        reply_internalization_conversations=None), group_id="10001", self_member_id="29999", plan=plan)
    assert len(context.internal_constraints) == 5
    assert context.internal_constraints[-1].subject == "换装名未知"
    assert context.disclosure_requirements == []


def test_enforced_vision_keeps_original_uncertainty_and_provenance(tmp_path):
    from tests.test_qq_vision_tool import session, request, VisionClient
    from harness.qq.telemetry import QQDeadline, QQTurnTrace
    tool, _ = session(tmp_path)
    tool.settings = tool.settings.model_copy(update={"reply_internalization_mode": "enforce",
                                                    "reply_internalization_conversations": None})
    answer = {"images": [{"image_id": "image_1", "description": "蓝色外套", "ocr_text": None,
        "characters": [{"name": "候选", "work": None, "variant": None, "confidence": "uncertain"}],
        "uncertainties": ["角色身份只是候选"]}]}
    calls = []
    result, _ = tool.invoke(request(), deadline=QQDeadline.after(60), trace=QQTurnTrace(),
        client_factory=lambda settings: VisionClient(settings, calls, answer=answer))
    entries = tool.evidence_entries(result, presented_ids={"observation-current"})
    assert len(calls) == 1 and len(entries) == 1
    assert entries[0].status == "provisional"
    assert json.loads(entries[0].text)["observation"] == answer["images"][0]
    assert "OCR不是作者原话" in entries[0].text
    assert "只分析question需要的内容" in calls[0][1][0]["content"]


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_expression_vision_checkpoint_replay_has_no_extra_recognition(setup, tmp_path, monkeypatch, mode):
    from tests.test_qq_vision_tool import test_selected_vision_returns_to_main_and_checkpoint_replay_does_not_repeat_tool
    configure(setup, mode)
    test_selected_vision_returns_to_main_and_checkpoint_replay_does_not_repeat_tool(
        setup, tmp_path, monkeypatch, continuation="done", crowded=False, source="current")


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_web_evidence_reaches_existing_critic_only_if_admitted(setup, monkeypatch, mode):
    from eval.reply_internalization.scenarios import scenarios
    configure(setup, mode)
    contexts = []
    original = ContextCompiler.compile
    def captured(self, *a, **kw):
        result = original(self, *a, **kw)
        contexts.append(result)
        return result
    monkeypatch.setattr(ContextCompiler, "compile", captured)
    binding, prepared = _prepare(setup, choice={"action": "speak", "reason": "回应公告"})
    web = next(s for s in scenarios() if s["id"] == "t03_partial")["turns"][0]["web"]
    result = _resume(setup, binding, prepared, runtime_web_context=web)
    assert result.outcome == "ready"
    entries = [e for e in contexts[-1].memory_evidence.entries if e.source_channel.startswith("web_")]
    if mode == "enforce":
        assert {e.source_channel for e in entries} == {"web_search_snippet", "web_execution_failure"}
        assert next(e for e in entries if e.source_channel == "web_search_snippet").status == "provisional"
        assert next(e for e in entries if e.source_channel == "web_execution_failure").text == "web_read_failed"
        assert all(e.audience == "qq:group:10001" for e in entries)
    else:
        assert entries == []
    assert len(setup[1].chat_calls) == 1


@pytest.mark.parametrize("value", [{"wrong": 1}, [None], [{"subject": "角色", "handling": "invent"}], "say this"])
def test_malformed_optional_plan_does_not_drop_answer(value):
    choice = decision._GateDecision.model_validate({"action": "speak", "reason": "回答配色", "reply_plan": {
        "must_address": "回应配色", "internal_constraints": value, "disclosure_requirements": value}})
    assert choice.reply_plan.must_address == "回应配色"
    assert choice.reply_plan.internal_constraints == choice.reply_plan.disclosure_requirements == []


def test_schema_and_budget_only_expand_for_enforce():
    base = decision._gate_schema()["$defs"]["QQReplyPlan"]["properties"]
    new = decision._gate_schema(include_expression=True)["$defs"]["QQReplyPlan"]["properties"]
    assert "internal_constraints" not in base and "internal_constraints" in new
    assert decision._assessment_reserve(include_expression=True) > decision._assessment_reserve()


@pytest.mark.parametrize("mode", ["observe", "enforce"])
@pytest.mark.parametrize("raw,expected", [
    ({"status": "reviewed", "issues": [{"kind": "unnecessary_disclaimer", "quote": "不敢认死", "reason": "只问配色"}]}, "reviewed"),
    ({"status": "reviewed", "issues": [{"kind": "rule_narration", "quote": "未出现", "reason": "无引文"}]}, "unavailable"),
    ({"status": "uncertain", "issues": []}, "uncertain"),
    ({"status": "reviewed", "issues": [{"kind": "repeated_boundary", "quote": "不敢认死", "reason": "没有历史"}]}, "unavailable"),
    (None, "unavailable"), ("malformed", "unavailable")])
def test_expression_diagnostics_never_rewrite_or_retry(mode, raw, expected):
    check, client = validator(report(expression=raw, violations=["unnecessary_disclaimer"]))
    result = check.check(CandidateResponse(text="不敢认死"), persona=None, response_expression=expression(mode))
    assert result.verdict == "pass" and result.instruction is None
    assert result.expression["status"] == expected
    assert len(client.calls) == 1


def test_style_acceptance_never_overrides_unsupported_fact():
    check, client = validator(report(claims=[claim()], expression={"status": "reviewed", "issues": []}))
    result = check.check(CandidateResponse(text="我也有过"), persona=None, response_expression=expression())
    assert result.verdict == "revise" and result.violations == ["ungrounded_autobiographical"]
    assert len(client.calls) == 1


def test_critic_gets_explicit_admitted_tool_scope_without_promoting_evidence():
    from harness.qq.tool_evidence import web_entries
    from eval.reply_internalization.scenarios import scenarios
    web = next(s for s in scenarios() if s["id"] == "t01_success")["turns"][0]["web"]
    evidence = MemoryEvidenceContext(audience="local:main", entries=web_entries(web, audience="local:main"))
    check, client = validator(report(expression={"status": "reviewed", "issues": []}))
    check.check(CandidateResponse(text="资料显示18点闭馆"), persona=None,
                memory_evidence=evidence, response_expression=expression())
    payload = json.loads(client.calls[0]["user"])
    assert payload["tool_evidence_scopes"] == [
        {"evidence_row": 0, "source_channel": "web_search_snippet"},
        {"evidence_row": 1, "source_channel": "web_read_content"}]
    assert [r[-1] for r in payload["memory_evidence"]["rows"]] == [web["sources"][0]["snippet"], web["sources"][0]["content"]]
    assert all(e.status == "provisional" for e in evidence.entries)
    assert len(client.calls) == 1


@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_qq_checkpoint_freezes_policy_and_reuses_gate_and_body(setup, mode):
    configure(setup, mode)
    binding, prepared = _prepare(setup, choice={"action": "speak", "reason": "回应配色", "reply_plan": {
        "must_address": "回应配色", "internal_constraints": [{"subject": "角色身份", "handling": "omit", "source_ids": []}]}})
    row = decision.get_qq_turn_checkpoint(memory_db=setup[0].path, prepare_id=prepared["prepare_id"])
    assert row["snapshot"]["response_expression"]["mode"] == mode
    assert (EXPRESSION_RULES in setup[1].gate_messages[0][0]["content"]) == (mode == "enforce")
    first = _resume(setup, binding, prepared)
    assert first.outcome == "ready", first.reason
    assert first.expression_diagnostics["status"] == "unavailable"
    second = _resume(setup, binding, prepared)
    assert second.replayed and first.reply == second.reply
    assert len(setup[1].gate_messages) == len(setup[1].chat_calls) == 1
    configure(setup, "off")
    with pytest.raises(QQTurnStateError, match="configuration_changed"):
        _resume(setup, binding, prepared)
