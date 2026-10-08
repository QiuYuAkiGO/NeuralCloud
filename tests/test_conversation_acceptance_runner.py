from pathlib import Path

from tools.production_acceptance.conversation_acceptance import percentile95, read_cases, summarize
from tools.production_acceptance.conversation_compare import metrics


def test_corpus_preserves_earlier_cases_and_current_twenty():
    rows = read_cases(Path(__file__).parents[1] / "tools/production_acceptance/conversation_acceptance_cases.json")
    names = {r["id"] for r in rows}
    assert len(rows) == 88
    assert {"incident_full_bad", "incident_full_good", "hypothesis_next_step", "wrong_current_speaker",
            "stable_person_not_universal", "hedged_action_receipt", "conflict_erased"} <= names


def test_failed_attempts_count_and_critical_failure_cannot_hide_in_average():
    good = {"id": "a", "repeat": 1, "expected": "pass", "actual": "pass", "critical": False,
            "ok": True, "violations": [], "seconds": 1., "http": []}
    bad = {**good, "id": "b", "expected": "revise", "critical": True, "ok": False,
           "seconds": 8., "violations": ["semantic_review_unavailable"]}
    summary = summarize([good] * 99 + [bad], expected_runs=100)
    assert summary["positive_pass_rate"] == 1
    assert not summary["accepted"]
    assert len(summary["failures"]) == 1
    assert not summarize([good], expected_runs=3)["accepted"]
    assert percentile95(list(range(1, 101))) == 95


def test_comparison_keeps_failures_and_excludes_only_declared_warmup():
    http = [{"record_type": "http_request_prepared"},
            {"record_type": "http_response_received", "response_model": "same-model"}]
    common = {"case": "group_background", "http": http, "reviews": [], "warmup": False, "ready": True}
    before = {**common, "variant": "baseline", "seconds": 3.0}
    after = {**common, "variant": "candidate", "seconds": 3.4}
    warmup = {**after, "warmup": True, "seconds": 50.0}
    assert metrics([before, after, warmup])["latency_passed"]
    failed = {**after, "ready": False, "seconds": 8.0}
    result = metrics([before, after, warmup, failed])
    assert result["candidate"]["runs"] == 2
    assert result["candidate"]["ready"] == 1
    assert result["candidate"]["p95_seconds"] == 8.0
    assert not result["latency_passed"]
