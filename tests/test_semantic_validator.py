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
