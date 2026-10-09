import json

import httpx
import pytest

from harness.config import Settings
from harness.core.inference import CandidateResponse, MemoryEvidenceContext, MemoryEvidenceEntry
from harness.validator.validator import ResponseValidator
from harness.validator.semantic import SemanticReview
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


@pytest.mark.parametrize("author,current,expected", [
    ("alice", "alice", "pass"), ("bob", "alice", "revise"),
    (None, "alice", "revise"), ("alice", None, "revise"), (None, None, "revise"),
])
def test_verbatim_second_person_attribution_cannot_override_bound_author(author, current, expected):
    evidence = MemoryEvidenceContext(audience="group", current_speaker_id=current, entries=[
        MemoryEvidenceEntry(source_id="source", text="吓哭了", audience="group", speaker_id=author,
                            source_channel="qq_group", status="user_claim", kind="current")])
    check, _ = validator(report(claims=[claim(quote="但你说吓哭了", kind="attribution",
        status="supported", basis="evidence", evidence_rows=[0])]))
    result = check.check(CandidateResponse(text="但你说吓哭了"), persona=None, memory_evidence=evidence)
    assert result.verdict == expected
    if expected == "revise":
        assert "attribution_speaker_mismatch" in result.violations


@pytest.mark.parametrize("text,kind,status,basis,expected", [
    ("你说：‘吓哭了’。", "attribution", "supported", "evidence", "revise"),
    ("另一位群友说吓哭了", "attribution", "supported", "evidence", "pass"),
    ("他说你说吓哭了", "attribution", "supported", "evidence", "pass"),
    ("你说吓哭了？", "non_factual", "not_asserted", "none", "pass"),
    ("你说很害怕", "attribution", "supported", "evidence", "pass"),
])
def test_author_guard_only_checks_asserted_exact_direct_quotes(text, kind, status, basis, expected):
    evidence = MemoryEvidenceContext(audience="group", current_speaker_id="alice", entries=[
        MemoryEvidenceEntry(source_id="source", text="吓哭了", audience="group", speaker_id="bob",
                            source_channel="qq_group", status="user_claim", kind="current")])
    check, _ = validator(report(claims=[claim(quote=text, kind=kind, status=status, basis=basis,
        evidence_rows=[0] if basis == "evidence" else [])]))
    assert check.check(CandidateResponse(text=text), persona=None, memory_evidence=evidence).verdict == expected


def test_same_quote_from_multiple_members_preserves_current_speakers_statement():
    evidence = MemoryEvidenceContext(audience="group", current_speaker_id="alice", entries=[
        MemoryEvidenceEntry(source_id=author, text="吓哭了", audience="group", speaker_id=author,
                            source_channel="qq_group", status="user_claim", kind="current") for author in ["alice", "bob"]])
    check, _ = validator(report(claims=[claim(quote="你说吓哭了", kind="attribution",
        status="supported", basis="evidence", evidence_rows=[1])]))
    assert check.check(CandidateResponse(text="你说吓哭了"), persona=None, memory_evidence=evidence).verdict == "pass"


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


def test_invalid_critic_report_retried_without_changing_candidate_or_evidence(caplog):
    check, client = validator(None)
    responses = iter([report(claims=[claim(quote="不存在的片段")]),
                      report(claims=[claim(quote="你好", kind="non_factual", status="not_asserted")])])
    def complete(**kwargs):
        client.calls.append(kwargs)
        return next(responses)
    client.complete_json = complete
    scoped = _QQScopedLLM(client, deadline=QQDeadline.after(60), trace=QQTurnTrace(), timeout=60)
    check.llm = scoped
    assert check.check(CandidateResponse(text="你好"), persona=None).verdict == "pass"
    assert len(client.calls) == 2
    assert client.calls[0]['user'] == client.calls[1]['user']
    assert 'semantic_quote_not_in_candidate' in client.calls[1]['system']
    assert scoped.failed_error_type is None
    assert 'semantic_quote_not_in_candidate' in caplog.text


def test_invalid_critic_retry_still_rejects_ungrounded_reply():
    check, client = validator(None)
    responses = iter([report(claims=[claim(quote="错引")]), report(claims=[claim()])])
    client.complete_json = lambda **kwargs: next(responses)
    result = check.check(CandidateResponse(text="我也有过"), persona=None)
    assert result.verdict == 'revise'
    assert result.violations == ['ungrounded_autobiographical']


@pytest.mark.parametrize('primary_offline', [False, True])
def test_qq_body_fallback_completes_semantic_validation(service, primary_offline):
    from harness.llm.client import ChatResult, RoutingLLMClient, MockLLM
    from harness.llm.health import ProviderHealth
    from harness.pipeline.pipeline import InferencePipeline
    from tests.test_main_reply_fallback import wrapper, status_error, Stub

    fallback, main, alternate = wrapper(status_error(503))
    alternate.result = ChatResult(text='你好。[END]')
    health = ProviderHealth(main.settings, start=False, transport=httpx.MockTransport(
        lambda request: httpx.Response(200, json={'data': [{'id': main.settings.llm_model}]})))
    fallback.health = health
    if not primary_offline:
        health.probe()
        health.probe()
    critic = Stub(ChatResult(text=json.dumps(report(claims=[claim(quote='错引')]))))
    original = critic.complete_chat_result
    def review(messages, **kwargs):
        if critic.calls:
            critic.result = ChatResult(text=json.dumps(report(claims=[claim(
                quote='你好', kind='non_factual', status='not_asserted')])))
        return original(messages, **kwargs)
    critic.complete_chat_result = review
    routed = RoutingLLMClient(fallback, MockLLM(), critic)
    scoped = _QQScopedLLM(routed, deadline=QQDeadline.after(60), trace=QQTurnTrace(), timeout=60)
    config = Settings(_env_file=None, validator_enabled=True, continuation_enabled=False, record_interaction=False)
    try:
        for _ in range(2):
            final = InferencePipeline(service, settings=config, llm=scoped).prepare_qq_response(
                '请打个招呼。', session_id='fallback-test', context_segments=[],
                memory_evidence=MemoryEvidenceContext(audience='qq:group:10001', entries=[]),
                prefix_messages=[{'role': 'system', 'content': '请简短回应问候。'},
                                 {'role': 'user', 'content': '你好'},
                                 {'role': 'assistant', 'content': '{"action":"speak"}'}])
            assert final.text.strip() and final.validation.verdict == 'pass'
            assert scoped.failed_error_type is None
            # Healthy model-list probes must not reopen failed generation.
            if not primary_offline:
                health.probe()
                health.probe()
        assert len(main.calls) == (0 if primary_offline else 1)
        assert len(alternate.calls) == 2
    finally:
        routed.close()


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
