"""Task admission, independent meaning review, and bounded QQ correction."""
import json
from pathlib import Path
from datetime import datetime
from types import SimpleNamespace

import pytest

from harness.core.inference import (CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry,
                                    ReplyUnderstanding, ResponseSemanticContext)
from harness.qq import decision
from harness.qq.policy import QQLatencyOptions
from harness.qq.semantic_alignment import mode_for
from harness.qq.telemetry import QQTurnTrace
from tests.test_semantic_validator import report, validator, claim
from tests.test_qq_decision import setup as setup
from tests.test_qq_prepared_turn import _prepare, _resume


def evidence():
    return MemoryEvidenceContext(audience="qq:group:10001", current_speaker_id="alice", entries=[
        MemoryEvidenceEntry(source_id="old", text="不是付费就能出院，还需要继续照护。", audience="qq:group:10001",
            source_channel="qq_group", source_conversation_id="qq:group:10001", status="user_claim", speaker_id="alice"),
        MemoryEvidenceEntry(source_id="new", text="即使没有家属探望，也不能随便让他们离院。", audience="qq:group:10001",
            source_channel="qq_group", source_conversation_id="qq:group:10001", status="user_claim", speaker_id="alice", kind="current")])


def task(mode="enforce", **updates):
    return ResponseSemanticContext(mode=mode, audience="qq:group:10001", trigger_source_ids=["new"],
        understanding=ReplyUnderstanding(acts=["emotion"], summary="只需要安慰孤单。", source_ids=["new"]),
        must_address="安慰孤单", **updates)


def aligned(status="aligned", **updates):
    return {"status": status, "issues": [], **updates}


def mismatch(**updates):
    return aligned("misaligned", issues=[{"code": "meaning_distorted", "evidence_rows": [0, 1],
        "candidate_quote": "孤单和被丢下是两回事", "reason": "原文强调继续照护与离院限制，回复替换成了两个情绪概念。", **updates}])


@pytest.mark.parametrize("mode,expected", [("observe", "pass"), ("enforce", "revise")])
def test_independent_review_gets_original_sources_and_fallible_plan(mode, expected):
    check, client = validator(report(alignment=mismatch()))
    result = check.check(CandidateResponse(text="孤单和被丢下是两回事"), persona=None,
                         memory_evidence=evidence(), response_semantics=task(mode))
    assert result.verdict == expected and result.alignment["status"] == "misaligned"
    sent = json.loads(client.calls[0]["user"])
    assert sent["response_semantics"]["trigger_rows"] == [1]
    assert sent["response_semantics"]["understanding"]["authority"] == "fallible_model_interpretation"
    assert sent["memory_evidence"]["rows"][1][-1] == evidence().entries[1].text
    assert len(client.calls) == 1


@pytest.mark.parametrize("status", ["aligned", "uncertain", "not_applicable"])
def test_short_social_replies_are_not_forced_to_expand(status):
    check, _ = validator(report(alignment=aligned(status)))
    result = check.check(CandidateResponse(text="嗯，我懂。"), persona=None,
                         memory_evidence=evidence(), response_semantics=task())
    assert result.verdict == "pass" and result.instruction is None


def test_alignment_uncertainty_never_waives_factual_error():
    check, _ = validator(report(alignment=aligned("uncertain"), claims=[claim()]))
    result = check.check(CandidateResponse(text="我也有过"), persona=None,
                         memory_evidence=evidence(), response_semantics=task())
    assert result.violations == ["ungrounded_autobiographical"]


@pytest.mark.parametrize("damage", ["missing", "scope", "foreign_source", "missing_trigger", "invalid_quote", "invalid_row", "no_trigger_row"])
def test_bad_alignment_metadata_cannot_pass_enforce(damage):
    source, response = evidence(), report(alignment=mismatch())
    if damage == "missing":
        response.pop("alignment")
    elif damage == "scope":
        source = source.model_copy(update={"audience": "qq:group:99999"})
    elif damage == "missing_trigger":
        source = source.model_copy(update={"entries": source.entries[:1]})
    elif damage == "foreign_source":
        source = source.model_copy(update={"entries": [entry.model_copy(update={"source_conversation_id": "qq:group:99999"})
                                                       for entry in source.entries]})
    else:
        response["alignment"] = mismatch(**{"invalid_quote": {"candidate_quote": "不存在"},
            "invalid_row": {"evidence_rows": [2]}, "no_trigger_row": {"evidence_rows": [0]}}[damage])
    check, client = validator(response)
    result = check.check(CandidateResponse(text="孤单和被丢下是两回事"), persona=None,
                         memory_evidence=source, response_semantics=task())
    assert result.violations == ["semantic_review_unavailable"]
    assert len(client.calls) <= 2


def test_missing_alignment_in_observe_does_not_change_factual_result():
    check, client = validator(report())
    result = check.check(CandidateResponse(text="嗯。"), persona=None,
                         memory_evidence=evidence(), response_semantics=task("observe"))
    assert result.verdict == "pass" and result.alignment["status"] == "unavailable"
    assert len(client.calls) == 1


@pytest.mark.parametrize("value", ["invalid", {"status": "wrong"}, {"status": "misaligned", "issues": [42]}])
def test_malformed_observation_and_reserved_violation_cannot_trigger_a_rewrite(value):
    check, client = validator(report(alignment=value, violations=["reply_semantic_mismatch"]))
    result = check.check(CandidateResponse(text="嗯。"), persona=None,
                         memory_evidence=evidence(), response_semantics=task("observe"))
    assert result.verdict == "pass" and result.alignment["status"] == "unavailable"
    assert len(client.calls) == 1


def test_gate_transport_preserves_usable_plan_when_understanding_is_malformed():
    from jsonschema import validate
    raw = {"action": "speak", "reason": "回应", "reply_plan": {"must_address": "具体问题",
           "understanding": {"summary": "摘要缺少必需来源"}}}
    validate(raw, decision._gate_schema(include_semantics=True))
    choice = decision._GateDecision.model_validate(raw)
    assert choice.reply_plan.must_address == "具体问题"
    assert choice.reply_plan.understanding is None and choice.reply_plan._understanding_invalid


def test_semantic_assessment_budget_is_bounded_and_off_keeps_the_old_reserve():
    original = decision._assessment_reserve()
    extra = decision._assessment_reserve(include_semantics=True)
    interpretation = ReplyUnderstanding(acts=["explanation", "correction", "emotion"],
        summary="\x00" * 240, source_ids=["\x00" * 160] * 8)
    assert extra > original + len(decision._canonical(interpretation.model_dump()))


def test_clipped_source_is_not_recovered_from_gate_summary_or_matching_other_author():
    from harness.core.inference import ContextSegment
    from harness.validator.semantic import project_semantics, SemanticReviewError
    entries = evidence().entries
    other = entries[-1].model_copy(update={"source_id": "other", "speaker_id": "bob"})
    segment = ContextSegment(kind="prefix_user", text=json.dumps({"text": other.text}), source_ids=["other"])
    admitted = MemoryEvidenceContext.bind(audience=task().audience, entries=[*entries, other], segment=segment).admitted(
        [segment], trigger_ref="new")
    assert [e.source_id for e in admitted.entries] == ["other"]
    with pytest.raises(SemanticReviewError, match="not_admitted"):
        project_semantics(task(), admitted)


@pytest.mark.parametrize("spent,expected", [(3, 2), (9, 1)])
def test_report_repairs_share_one_deadline(spent, expected, monkeypatch):
    import harness.llm.deadline as deadline
    now = [100.0]
    monkeypatch.setattr(deadline.time, "monotonic", lambda: now[0])
    check, client = validator(report())
    remaining = []
    def invalid(**options):
        remaining.append(deadline.remaining_model_seconds())
        now[0] += spent
        return report()
    monkeypatch.setattr(client, "complete_json", invalid)
    result = check.check(CandidateResponse(text="嗯。"), persona=None,
                         memory_evidence=evidence(), response_semantics=task())
    assert result.violations == ["semantic_review_unavailable"] and len(remaining) == expected
    assert remaining[0] <= 8
    if expected == 2:
        assert remaining[1] <= remaining[0] - spent


def test_optional_understanding_does_not_invalidate_other_plan_fields():
    plan = decision.QQReplyPlan.model_validate({"intent": "stance", "must_address": "原意",
        "understanding": {"acts": ["invalid"], "summary": "错", "source_ids": ["a"]}})
    assert plan.must_address == "原意" and plan.understanding is None and plan._understanding_invalid
    assert "understanding" not in plan.model_dump()


def test_rollout_is_account_scoped_and_independent_of_latency():
    policy = QQLatencyOptions(qq_semantic_alignment_mode="enforce", qq_latency_conversations=[],
                             qq_semantic_alignment_conversations=["29999/10001"])
    scoped = policy.for_conversation(group_id="10001", self_member_id="29999")
    assert mode_for(scoped, group_id="10001", self_member_id="29999") == "enforce"
    assert mode_for(scoped, group_id="10001", self_member_id="29998") == "off"
    assert mode_for(QQLatencyOptions(), group_id="10001", self_member_id="29999") == "off"


@pytest.mark.parametrize("mode,verdict", [("observe", "pass"), ("enforce", "revise")])
def test_enforce_cannot_bypass_an_unconfigured_critic(mode, verdict):
    from harness.config import Settings
    from harness.validator.validator import ResponseValidator
    check = ResponseValidator(settings=Settings(_env_file=None, validator_enabled=False))
    result = check.check(CandidateResponse(text="嗯"), persona=None, memory_evidence=evidence(), response_semantics=task(mode))
    assert result.verdict == verdict and result.alignment["status"] == "unavailable"


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("mode", ["observe", "enforce"])
def test_pipeline_skip_never_counts_as_alignment_pass(service, enabled, mode):
    from harness.config import Settings
    from harness.pipeline.pipeline import InferencePipeline
    from harness.core.inference import ContextSegment
    pipeline = InferencePipeline(service, settings=Settings(_env_file=None,
        validator_enabled=enabled, validator_sample_rate=0, record_interaction=False))
    original = evidence()
    final = pipeline.prepare_qq_response("请回应原文。", session_id="qq-test", response_semantics=task(mode),
        memory_evidence=original,
        context_segments=[ContextSegment(kind="user", text=original.entries[-1].text, source_ids=["new"])],
        prefix_messages=[{"role": "user", "content": original.entries[-1].text}])
    assert final.validation.alignment["status"] == "unavailable"
    assert final.validation.verdict == ("revise" if mode == "enforce" else "pass")
    assert bool(final.text) is (mode == "observe") and final.revisions == 0


def test_legacy_settings_use_fresh_empty_alignment_allowlist():
    first = QQLatencyOptions.from_settings(SimpleNamespace())
    second = QQLatencyOptions.from_settings(SimpleNamespace())
    assert first.qq_semantic_alignment_conversations == []
    assert first.qq_semantic_alignment_conversations is not second.qq_semantic_alignment_conversations


def test_semantic_context_v1_remains_readable_but_new_context_uses_current_version():
    from harness.qq.semantic_alignment import VERSION
    assert task().version == VERSION
    assert ResponseSemanticContext.model_validate(task().model_dump() | {"version": "reply-semantics-v1"}).version == "reply-semantics-v1"


def configure(setup, monkeypatch, *, mode="enforce", always_wrong=False):
    _, client, _, kwargs, _ = setup
    config = json.loads(kwargs["config_path"].read_text())
    config.update(validator_enabled=True, qq_latency=QQLatencyOptions(qq_semantic_alignment_mode=mode,
        qq_semantic_alignment_conversations=["29999/10001"]).model_dump())
    kwargs["config_path"].write_text(json.dumps(config), encoding="utf-8")
    client._structured = SimpleNamespace(settings=decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])[0])
    reviews = []
    original = client.complete_json
    def review(**options):
        if "response_semantics" not in options.get("user", ""):
            return original(**options)
        payload = json.loads(options["user"])
        reviews.append(payload)
        rows = payload["response_semantics"]["trigger_rows"]
        result = aligned() if len(reviews) > 1 and not always_wrong else aligned("misaligned", issues=[{
            "code": "core_request_missed", "evidence_rows": rows, "candidate_quote": None,
            "reason": "未回应当前成员提出的问题。"}])
        return report(alignment=result)
    monkeypatch.setattr(client, "complete_json", review)
    return reviews


def test_understanding_on_wait_is_preserved_without_body_or_extra_gate(setup, monkeypatch):
    configure(setup, monkeypatch)
    _, client, _, kwargs, records = setup
    summary = {"acts": ["social"], "summary": "当前成员只是确认已听到，没有新请求。",
               "source_ids": [records[-1].record_id]}
    client.choice = json.dumps({"reply_plan": {"understanding": summary, "must_address": ""},
                               "action": "wait", "reason": "这次确认无需再回应。"})
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.outcome == "natural_wait" and result.reply is None
    assert result.reply_plan.understanding.model_dump() == summary
    assert len(client.gate_messages) == 1 and not client.chat_calls
    replay = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert replay.replayed and replay.reply_plan == result.reply_plan
    assert len(client.gate_messages) == 1


@pytest.mark.parametrize("mode,always_wrong,outcome,calls", [
    ("observe", True, "ready", 1), ("enforce", False, "ready", 2), ("enforce", True, "guard_wait", 2)])
@pytest.mark.parametrize("resume", [False, True])
def test_real_pipeline_checks_same_trigger_repairs_once_and_never_sends_error_notice(
        setup, monkeypatch, mode, always_wrong, outcome, calls, resume):
    reviews = configure(setup, monkeypatch, mode=mode, always_wrong=always_wrong)
    if resume:
        binding, prepared = _prepare(setup)
        result = _resume(setup, binding, prepared)
    else:
        result = decision.decide_qq_reply(**setup[3], trigger_member_id="20002", trace=QQTurnTrace())
    assert result.outcome == outcome
    assert len(reviews) == calls and len(setup[1].chat_calls) == calls
    assert len(setup[1].gate_messages) == 1
    assert all(review["response_semantics"]["trigger_rows"] == reviews[0]["response_semantics"]["trigger_rows"] for review in reviews)
    if outcome == "guard_wait":
        assert result.reply is None and result.error_type is None and "reply_semantic_rejected" in result.reason


def test_prepared_turn_cannot_resume_after_semantic_mode_change(setup, monkeypatch):
    from harness.qq.turn_checkpoints import QQTurnStateError
    configure(setup, monkeypatch)
    binding, prepared = _prepare(setup)
    p = setup[3]["config_path"]
    data = json.loads(p.read_text())
    data["qq_latency"]["qq_semantic_alignment_mode"] = "observe"
    p.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(QQTurnStateError, match="configuration_changed"):
        _resume(setup, binding, prepared)
    assert not setup[1].chat_calls


@pytest.mark.parametrize("resume", [False, True])
def test_incident_original_author_and_context_reach_actual_generation_and_critic(setup, monkeypatch, resume):
    from tests.test_qq_decision import add_member
    store, client, _, kwargs, _ = setup
    configure(setup, monkeypatch, mode="observe")
    rows = json.loads((Path(__file__).parent / "fixtures/qq_semantic/incident_20261008.json").read_text(encoding="utf-8"))["messages"]
    source_ids = []
    for row in rows[:19]:
        if row["role"] == "user":
            _, observation = add_member(store, "20002" if row["author"] == "member_1" else "20001",
                text=row["text"], capture=row["id"], moment=datetime.fromisoformat(row["time"]))
            source_ids.append(observation.record_id)
        else:
            decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
                delivered_text=row["text"], evidence_refs=["fixture://" + row["id"]],
                source_record_ids=source_ids[-1:], verification="qq_ui_verified",
                delivered_at=datetime.fromisoformat(row["time"]))
    actual = []
    def inspect(**options):
        payload = json.loads(options["user"])
        actual.append(payload)
        return report(alignment=aligned())
    monkeypatch.setattr(client, "complete_json", inspect)
    kwargs.update(recent_limit_per_member=30, history_limit_per_member=30)
    if resume:
        binding, prepared = _prepare(setup)
        result = _resume(setup, binding, prepared)
    else:
        result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.outcome == "ready" and len(actual) == 1
    payload = actual[0]
    projected = payload["memory_evidence"]
    sources = [dict(zip(projected["columns"], value)) for value in projected["rows"]]
    for source in sources:
        source.update(zip(projected["metadata_columns"], projected["metadata"][source["metadata_id"]]))
    trigger = [sources[i] for i in payload["response_semantics"]["trigger_rows"]]
    assert any(r["text"] == rows[18]["text"] and r["kind"] == "current" and r["status"] == "user_claim"
               and r["speaker_id"] == projected["current_speaker_id"] for r in trigger)
    generated = "\n".join(m["content"] for m in client.chat_calls[0])
    for row in rows[:19]:
        assert row["text"] in generated
        assert any(r["text"] == row["text"] and r["status"] == row["status"] for r in sources)


def test_expression_policy_cache_refresh_does_not_repeat_appraisal():
    from tests.test_emotion_service import FakeAux, request, settings
    from harness.emotion.service import EmotionService
    aux = FakeAux()
    service = EmotionService(settings(), aux)
    frame = service.observe(request())
    original = service.guidance(frame)
    count = len(aux.calls)
    changed = service.guidance(frame, expression_only=True)
    assert changed.revision == original.revision and len(aux.calls) == count + 1
    assert changed.diagnostics["delivery_policy"] == "expression-only-v1"
    assert service.guidance(frame, expression_only=True) == changed and len(aux.calls) == count + 1
