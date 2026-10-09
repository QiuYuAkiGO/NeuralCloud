from eval.reply_source_authority.adapters import source_contexts
from eval.reply_source_authority.cases import cases
from harness.core.expression import DisclosureRequirement, ReplyConstraint, ResponseExpressionContext
from harness.core.inference import ReplyUnderstanding, ResponseSemanticContext


def test_source_contexts_preserve_scope_trigger_history_and_nonplan_inputs():
    task = ResponseSemanticContext(mode="enforce", audience="a", trigger_source_ids=["original"],
        understanding=ReplyUnderstanding(acts=["question"], summary="错误的解释", source_ids=["original"]),
        must_address="模型制造的义务")
    expression = ResponseExpressionContext(mode="enforce", audience="a", recent_replies=["实际送达的回复"],
        internal_constraints=[ReplyConstraint(subject="工具失败", handling="qualify")],
        disclosure_requirements=[DisclosureRequirement(subject="无关的限制", reason="answer_material")])
    evidence = object()
    before = {"response_semantics": task, "response_expression": expression, "memory_evidence": evidence,
              "emotion_guidance": "保留真实状态", "concrete_physical_evidence_available": False}
    after = source_contexts(before)
    assert after["memory_evidence"] is evidence
    assert after["emotion_guidance"] == before["emotion_guidance"]
    assert task.understanding is not None and task.must_address and expression.internal_constraints
    assert after["response_semantics"].trigger_source_ids == ["original"]
    assert after["response_semantics"].audience == "a" and after["response_semantics"].mode == "enforce"
    assert after["response_semantics"].understanding is None and not after["response_semantics"].must_address
    assert after["response_expression"].recent_replies == ["实际送达的回复"]
    assert not after["response_expression"].internal_constraints and not after["response_expression"].disclosure_requirements


def test_fixed_pairs_include_true_omissions_and_both_sides_of_factual_risk():
    dataset = cases()
    assert len(dataset) == 24 and sum(row["expected_pass"] for row in dataset) == 12
    for good, bad in zip(dataset[::2], dataset[1::2], strict=True):
        assert good["question"] == bad["question"] and good["evidence"] == bad["evidence"]
        assert good["candidate"] != bad["candidate"]
        assert good["faithful_plan"] != good["drifted_plan"]
    assert next(c for c in dataset if c["id"] == "image_content_bad")["candidate"] == "收到啦。"
    assert next(c for c in dataset if c["id"] == "received_good")["candidate"] == "收到啦。"


def test_removing_model_plan_does_not_remove_semantic_binding_or_fact_invariants():
    import pytest
    from harness.core.inference import MemoryEvidenceContext, MemoryEvidenceEntry
    from harness.validator.semantic import SemanticReview, SemanticReviewError

    evidence = MemoryEvidenceContext(audience="a", entries=[MemoryEvidenceEntry(
        source_id="original", text="图里写了什么？", source_channel="qq_group", audience="a",
        source_conversation_id="a", status="user_claim", kind="current")])
    task = ResponseSemanticContext(mode="enforce", audience="a", trigger_source_ids=["original"])
    kwargs = source_contexts({"response_semantics": task})
    report = SemanticReview.model_validate({"coverage_complete": True, "claims": [], "violations": [],
        "instruction": None, "alignment": {"status": "misaligned", "issues": [{"code": "core_request_missed",
            "evidence_rows": [0], "candidate_quote": None, "reason": "未回应内容问题"}]}})
    result = report.result("收到啦。", evidence_count=1, persona_available=False, evidence=evidence, **kwargs)
    assert result.verdict == "revise" and "reply_semantic_mismatch" in result.violations
    report.alignment.issues[0].evidence_rows = [5]
    with pytest.raises(SemanticReviewError, match="reference_invalid"):
        report.result("收到啦。", evidence_count=1, persona_available=False, evidence=evidence, **kwargs)


def test_body_projection_preserves_all_policies_originals_and_tool_requests():
    import json
    from eval.reply_source_authority.body_replay import body_projection

    gate = {"action": "speak", "reason": "保持谨慎", "reply_plan": {"must_address": "未知身份"},
            "vision_request": {"image_ids": ["image_1"], "question": "画面"}, "addressing": "direct"}
    kwargs = {"payload": json.dumps({"assessment": gate, "web_context": {"failures": ["timeout"]}}),
              "prefix_messages": [{"role": "system", "content": "人格和全部现有规则"},
                                  {"role": "user", "content": "原始发言与完整不确定证据"},
                                  {"role": "assistant", "content": json.dumps(gate)}]}
    result = body_projection(kwargs)
    assert result["prefix_messages"][:2] == kwargs["prefix_messages"][:2]
    projected_gate = json.loads(result["prefix_messages"][2]["content"])
    assert projected_gate["vision_request"] == gate["vision_request"] and projected_gate["addressing"] == "direct"
    assert "reply_plan" not in projected_gate and "reason" not in projected_gate
    assert json.loads(result["payload"])["web_context"] == {"failures": ["timeout"]}


def test_fail_closed_does_not_count_as_a_correct_critic_assessment(tmp_path):
    from eval.qq_brief.common import save
    from eval.reply_source_authority.analyze import critic

    save(tmp_path / "manifest.json", {"arms": ["control"], "plan_states": ["faithful", "drifted"], "repeats": 1})
    save(tmp_path / "cases.json", [{"id": "bad", "expected_pass": False}])
    for state in ("faithful", "drifted"):
        save(tmp_path / "trials" / f"bad-r1-{state}-control" / "result.json", {
            "case_id": "bad", "arm": "control", "plan": state, "repeat": 1, "expected_pass": False,
            "status": "ok", "correct": True, "http_sha256": None,
            "validation": {"verdict": "revise", "violations": ["semantic_review_unavailable"]}})
    report = critic(tmp_path)
    for state in ("faithful", "drifted"):
        metrics = report["metrics"]["control"][state]
        assert metrics["correct"] == 0 and metrics["scored"] == 0 and metrics["unscored"] == 1
        assert metrics["accepted_bad"] == 0


def test_explicit_task_view_uses_exact_admitted_sources(monkeypatch):
    from eval.reply_source_authority.adapters import install
    from harness.core.inference import MemoryEvidenceContext, MemoryEvidenceEntry
    from harness.validator import validator

    # Register restorations before the research-only installer replaces them.
    monkeypatch.setattr(validator.ResponseValidator, "_llm_check", validator.ResponseValidator._llm_check)
    monkeypatch.setattr(validator, "project_semantics", validator.project_semantics)
    evidence = MemoryEvidenceContext(audience="a", entries=[MemoryEvidenceEntry(source_id="original", text="图收到了吗？",
        audience="a", source_conversation_id="a", source_channel="qq_group", status="user_claim", kind="current")])
    task = ResponseSemanticContext(mode="enforce", audience="a", trigger_source_ids=["original"], must_address="错误解释")
    install("source_explicit")
    projected = validator.project_semantics(source_contexts({"response_semantics": task})["response_semantics"], evidence)
    assert projected["source_messages"] == [{"evidence_row": 0, "text": "图收到了吗？"}]
    assert projected["must_address"] == "" and projected["understanding"] is None


def test_portable_replay_inputs_reject_modified_evidence():
    import pytest
    from eval.qq_brief.common import digest
    from eval.reply_source_authority.report_replay import bundled_input

    value = {"candidate": "收到啦。", "evidence": {"entries": [{"text": "原始记录"}]}}
    reference = digest(value)
    bundle = {"inputs": {reference: value}, "trials": {"mechanism": {"trial": reference}}}
    assert bundled_input(bundle, "mechanism", "trial") == value
    value["evidence"]["entries"][0]["text"] = "被改写的记录"
    with pytest.raises(ValueError, match="bundle_hash_mismatch"):
        bundled_input(bundle, "mechanism", "trial")
