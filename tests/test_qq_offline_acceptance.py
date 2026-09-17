import json

from tools.qq_offline_acceptance import DEFAULT_FIXTURE, run_fixture


def test_fixed_contract_corpus_covers_120_distinct_cases_without_claiming_model_quality():
    fixture = json.loads(DEFAULT_FIXTURE.read_text(encoding="utf-8"))
    assert len({json.dumps(case["input"], sort_keys=True, ensure_ascii=False) for case in fixture["cases"]}) == 120
    result = run_fixture()
    assert result["cases"] == result["passed"] == 120
    assert len(result["categories"]) == 8
    assert result["persona_quality"] == "pending_natural_canary_review"
    assert result["latency_evidence"].startswith("pending_")
