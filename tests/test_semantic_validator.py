import json

import httpx
import pytest

from harness.config import Settings
from harness.core.inference import CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry
from harness.validator.validator import ResponseValidator
from harness.validator.semantic import SemanticReview
from harness.llm.deepseek_structured import StructuredResponseError
from harness.qq.decision import _QQScopedLLM
from harness.qq.telemetry import QQDeadline, QQTurnTrace


def report(**updates):
    return {"coverage_complete": True, "claims": [], "violations": [], "instruction": "", **updates}


def claim(**updates):
    return {"quote": "我也有过", "kind": "autobiographical", "status": "unsupported",
            "basis": "none", "evidence_rows": [], "reason": "没有自身经历来源", **updates}


class Critic:
    _structured = object()
    def __init__(self, response):
        self.response = response
        self.calls = []
    def complete_json(self, **kwargs):
        self.calls.append(kwargs)
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def validator(response):
    client = Critic(response)
    return ResponseValidator(settings=Settings(_env_file=None, validator_enabled=True,
                             validator_sample_rate=0, call_log_enabled=False), llm=client), client


def test_configured_json_critic_covers_fast_path_even_with_zero_sampling():
    check, client = validator(report(claims=[claim()]))
    assert check.should_run("fast")
    result = check.check(CandidateResponse(text="我也有过……明明想加个水"), persona=None, run_llm=False)
    assert result.verdict == "revise" and result.violations == ["ungrounded_autobiographical"]
    assert client.calls[0]["response_format"]["json_schema"]["name"] == "semantic_review"
    assert client.calls[0]["timeout_seconds"] == 8


@pytest.mark.parametrize("response", [report(coverage_complete=False), {},
    ValueError("bad json"), TimeoutError(), httpx.ConnectError("offline"),
    report(claims=[claim(status="supported", basis="general_knowledge")]),
    report(claims=[claim(quote="不存在的片段")]),
    report(claims=[claim(status="supported", basis="evidence", evidence_rows=[0])])])
def test_incomplete_failed_or_invalid_review_never_passes(response):
    check, _ = validator(response)
    assert check.check(CandidateResponse(text="我也有过"), persona=None).verdict == "revise"


def test_complete_evidence_precedes_candidate_and_attribution_is_preserved():
    evidence = MemoryEvidenceContext(audience="group", current_speaker_id="alice", entries=[
        MemoryEvidenceEntry(source_id="s1", text="我去了饭店", audience="group", speaker_id="bob", source_channel="qq_group",
                            status="user_claim", kind="history")])
    check, client = validator(report(claims=[claim(quote="他提到去饭店", kind="attribution",
        status="supported", basis="evidence", evidence_rows=[0])]))
    assert check.check(CandidateResponse(text="他提到去饭店"), persona=None,
                       memory_evidence=evidence).verdict == "pass"
    payload = json.loads(client.calls[0]["user"])
    assert list(payload)[-1] == "text"
    projection = payload["memory_evidence"]
    assert projection["rows"][0][0] != projection["current_speaker_id"]
    assert projection["rows"][0][-1] == "我去了饭店"


def test_semantic_judgment_replaces_keyword_false_positives():
    check, _ = validator(report())
    assert check.check(CandidateResponse(text="假如我口渴了，可以举手示意。"),
                       persona=None).verdict == "pass"


def test_qq_wrapper_keeps_callers_short_timeout():
    client = Critic(report())
    scoped = _QQScopedLLM(client, deadline=QQDeadline.after(60), trace=QQTurnTrace(), timeout=60)
    scoped.complete_json(system="rules", user="data", timeout_seconds=.8)
    assert 0 < client.calls[0]["timeout_seconds"] <= .8


def test_supported_report_cannot_reference_missing_evidence():
    result = SemanticReview.model_validate(report(claims=[claim(status="supported", basis="evidence", evidence_rows=[3])]))
    with pytest.raises(ValueError, match="reference_invalid"):
        result.result("我也有过", evidence_count=1, persona_available=False)


def test_unknown_review_fields_are_discarded_without_changing_verdict():
    from harness.llm.deepseek_structured import validate_response
    from tests.test_deepseek_structured import body
    raw = report(claims=[claim(extra_claim={"verdict": "pass"})], extra_report="pass")
    validate_response(body(json.dumps(raw)), SemanticReview.model_json_schema())
    parsed = SemanticReview.model_validate(raw)
    assert "extra_report" not in parsed.model_dump()
    assert "extra_claim" not in parsed.model_dump()["claims"][0]
    assert parsed.result("我也有过", evidence_count=0, persona_available=False).verdict == "revise"


@pytest.mark.parametrize("change", [{"status": "unknown"}, {"reason": "x" * 401}, {"evidence_rows": [9]}])
def test_extra_fields_do_not_bypass_known_field_or_evidence_checks(change):
    check, _ = validator(report(claims=[claim(**change, extra_claim="pass")], extra_report="pass"))
    assert check.check(CandidateResponse(text="我也有过"), persona=None).verdict == "revise"


def test_review_budget_increases_without_extra_calls_or_deadline_change():
    check, client = validator(report())
    check.check(CandidateResponse(text="hello"), persona=None)
    assert len(client.calls) == 1
    assert client.calls[0]["max_tokens"] == 4096
    assert client.calls[0]["timeout_seconds"] == 8


@pytest.mark.parametrize("speaker", ["bob", None])
def test_supported_verbatim_you_attribution_cannot_reassign_current_author(speaker):
    text = "你刚才说我是杭州人。"
    evidence = MemoryEvidenceContext(audience="group", current_speaker_id="alice", entries=[
        MemoryEvidenceEntry(source_id="s1", text="我是杭州人。", audience="group", source_channel="qq_group",
                            speaker_id=speaker, status="user_claim", kind="current")])
    check, client = validator(report(claims=[claim(quote=text, kind="attribution",
        status="supported", basis="evidence", evidence_rows=[0])]))
    result = check.check(CandidateResponse(text=text), persona=None, memory_evidence=evidence)
    assert result.verdict == "revise" and "attribution_speaker_mismatch" in result.violations
    assert len(client.calls) == 1


def test_malformed_report_repair_reuses_candidate_and_evidence_within_original_deadline():
    check, client = validator(report())
    responses = iter([report(claims=[claim(quote="not in text")]), report()])
    original = client.complete_json

    def complete(**kwargs):
        original(**kwargs)
        return next(responses)

    client.complete_json = complete
    assert check.check(CandidateResponse(text="你好"), persona=None).verdict == "pass"
    assert len(client.calls) == 2
    assert client.calls[0]["user"] == client.calls[1]["user"]
    assert "semantic_quote_not_in_candidate" in client.calls[1]["system"]
    assert all(call["timeout_seconds"] == 8 for call in client.calls)


@pytest.mark.parametrize("previous", [None, "network_error"])
def test_transport_report_repair_uses_existing_two_attempt_budget_and_preserves_older_failures(previous):
    class Responses(Critic):
        def complete_json(self, **kwargs):
            self.calls.append(kwargs)
            if len(self.calls) == 1:
                raise StructuredResponseError("deepseek_structured_json_invalid")
            return report()

    raw = Responses(None)
    scoped = _QQScopedLLM(raw, deadline=QQDeadline.after(60), trace=QQTurnTrace(), timeout=60)
    scoped.failed_error_type = previous
    check = ResponseValidator(settings=Settings(_env_file=None, validator_enabled=True,
        validator_sample_rate=0, call_log_enabled=False), llm=scoped)
    assert check.check(CandidateResponse(text="我愿意先相信你。"), persona=None).verdict == "pass"
    assert len(raw.calls) == 2
    assert raw.calls[0]["user"] == raw.calls[1]["user"]
    assert "deepseek_structured_json_invalid" in raw.calls[1]["system"]
    assert all(call["timeout_seconds"] <= 8 for call in raw.calls)
    assert scoped.failed_error_type is None if previous is None else scoped.failed_error_type is not None


@pytest.mark.parametrize("error,calls", [(StructuredResponseError("deepseek_structured_json_invalid"), 2),
                                      (StructuredResponseError("deepseek_structured_nontext_response"), 1),
                                      (httpx.ConnectError("offline"), 1)])
def test_report_repair_never_exceeds_existing_budget_or_retries_transport_and_refusal(error, calls):
    check, client = validator(error)
    result = check.check(CandidateResponse(text="我愿意先相信你。"), persona=None)
    assert result.verdict == "revise" and result.violations == ["semantic_review_unavailable"]
    assert len(client.calls) == calls


def test_omitted_empty_references_are_neutral_but_never_substantiate_a_fact():
    from harness.llm.deepseek_structured import validate_response
    from tests.test_deepseek_structured import body

    item = claim(quote="我愿意了解你", kind="non_factual", status="not_asserted", basis="none")
    item.pop("evidence_rows")
    raw = report(claims=[item])
    validate_response(body(json.dumps(raw)), SemanticReview.model_json_schema())
    check, client = validator(raw)
    assert check.check(CandidateResponse(text="我愿意了解你"), persona=None).verdict == "pass"
    assert len(client.calls) == 1
    fabricated = report(claims=[{**item, "kind": "shared_history", "status": "supported", "basis": "evidence"}])
    check, _ = validator(fabricated)
    assert check.check(CandidateResponse(text="我愿意了解你"), persona=None).verdict == "revise"
