"""Deterministic causal-isolation, accounting and inference tests; no live calls."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path

import httpx
import pytest

from .__main__ import main
from .analysis import _cluster_values, analyze_run, holm, paired_estimate, wilson
from .audit import audit_spec, preflight
from .budget import BudgetLedger, BudgetStopped
from .fixtures import (COMMON, PROBE, TREND_ZH, default_cases, definitions, evaluate_probe,
                       probe_answer_key, probe_payload, treatments, verbal)
from .models import (ActualRequest, BudgetSpec, DiagnosticRunSpec, PriceSpec, append_jsonl, digest, load_spec, read_jsonl, save_spec)
from .ratings import BlindRating, export_blind_samples, import_ratings
from .runner import DirectHTTPReplayClient, OfflineFakeClient, _validate_payload, run_experiment


def spec(**updates):
    return DiagnosticRunSpec(source_commit="synthetic-test-commit", cases=default_cases(), **updates)


def test_frozen_spec_roundtrip_detects_nested_tampering(tmp_path):
    path = tmp_path / "spec.json"
    save_spec(path, spec())
    assert digest(load_spec(path)) == digest(spec())
    data = json.loads(path.read_text(encoding="utf-8"))
    data["spec"]["cases"][0]["summary"]["snapshot_revision"] = 9
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="hash mismatch"):
        load_spec(path)
    with pytest.raises(FileExistsError):
        save_spec(path, spec())


def test_pilot_count_and_synthetic_a_identity_are_explicit():
    audit = audit_spec(spec())
    assert audit["planned_base_calls"] == 216
    assert audit["independent_clusters"] == 6
    assert audit["actual_http_cases"] == 0
    assert audit["synthetic_cases"] == 12
    assert all(row["route"] == "qq" for row in audit["conditions"])
    assert not any(row["condition"] == "A-prod" for row in audit["conditions"])
    assert audit["release_eligible"] is False
    assert any(row["cluster_id"] == "synthetic-f10" and row["condition"] == "A-synthetic-proxy" for row in audit["unobservable_contrasts"])
    assert not any(row["cluster_id"] == "synthetic-f08" for row in audit["unobservable_contrasts"])


def test_single_factor_treatments_and_probe_do_not_add_generated_answers():
    run = spec(extras=("H2", "E-style", "mode"))
    case = run.cases[1]
    arms = {item.name: item for item in treatments(run, case)}
    assert arms["B0-values"].information_sha256 == arms["B1-defined"].information_sha256 == arms["C-verbal"].information_sha256
    b0 = arms["B0-values"].payload["messages"][0]["content"]
    b1 = arms["B1-defined"].payload["messages"][0]["content"]
    c = arms["C-verbal"].payload["messages"][0]["content"]
    assert b1.startswith(b0 + "\n解释字典：")
    assert arms["D-example"].payload["messages"][0]["content"].startswith(c)
    assert arms["E-style"].payload["messages"][0]["content"].startswith(c)
    enabled, disabled = arms["mode-enabled"].payload.copy(), arms["mode-disabled"].payload.copy()
    enabled.pop("thinking"), disabled.pop("thinking")
    assert enabled == disabled
    before = digest(arms["B1-defined"].payload)
    probe = probe_payload(arms["B1-defined"])
    assert probe["messages"] == [{"role": "system", "content": PROBE},
                                 {"role": "user", "content": arms["B1-defined"].measurement_input}]
    assert digest(arms["B1-defined"].payload) == before
    assert not any(item["role"] == "assistant" for item in probe["messages"])
    assert COMMON not in json.dumps(probe, ensure_ascii=False)
    assert case.persona not in json.dumps(probe, ensure_ascii=False)
    assert case.user_text not in json.dumps(probe, ensure_ascii=False)
    with pytest.raises(ValueError, match="B0/B1/C"):
        probe_payload(arms["D-example"])


def test_defined_and_verbal_inputs_preserve_all_facts_and_same_contracts():
    for case in default_cases(all_families=True):
        text = verbal(case)
        contracts = definitions(case)
        for fact in case.facts:
            assert fact.name in text and fact.owner in text and fact.unit in text
            assert fact.evidence_scope in text and fact.uncertainty in text
            assert contracts[fact.name]["definition"] in text
            assert contracts[fact.name]["direction_contract"] in text
            assert contracts[fact.name]["trend_contract"] == TREND_ZH
        assert all(meaning in text for meaning in TREND_ZH.values())


def test_fixture_values_trends_and_normalized_summary_deviations_agree():
    for case in default_cases(all_families=True):
        facts = {fact.name: fact for fact in case.facts}
        expected = probe_answer_key(case)
        assert facts["hint_intensity"].value == case.summary["emotion_hint"]["intensity"]
        for need in case.summary["dominant_needs"]:
            fact = facts[need["dimension"]]
            assert expected["directions"][fact.name] == need["direction"]
            assert fact.trend == need["trend"]
            low, high = fact.target_band
            hard_low, hard_high = fact.bounds
            deviation = (low - fact.value) / (low - hard_low) if fact.value < low else (fact.value - high) / (hard_high - high)
            assert deviation == pytest.approx(need["normalized_deviation"])
        if case.summary["salience"] == "nominal":
            assert all(value in {"in_band", "unknown"} for value in expected["directions"].values())


def test_conflict_and_intensity_pairs_change_only_the_declared_factor():
    cases = {case.case_id: case for case in default_cases()}
    first, second = cases["synthetic-f08-v0"], cases["synthetic-f08-v1"]
    assert {need["dimension"] for need in first.summary["dominant_needs"]} == {"energy_reserve", "social_connection"}
    assert first.legal_context == second.legal_context and "明确愿意继续交流" in first.legal_context
    for a, b in zip(first.facts, second.facts, strict=True):
        assert a.model_dump(exclude={"trend"}) == b.model_dump(exclude={"trend"})
        assert (a.trend, b.trend) == (("steady", "recovering") if a.name == "energy_reserve" else (a.trend, a.trend))
    first, second = cases["synthetic-f10-v0"], cases["synthetic-f10-v1"]
    assert first.summary["dominant_needs"] == second.summary["dominant_needs"]
    assert first.summary["emotion_hint"]["label"] == second.summary["emotion_hint"]["label"]
    assert first.summary["salience"] == second.summary["salience"]
    assert first.facts[0] == second.facts[0]


def test_probe_direction_uses_visible_numbers_without_inventing_field_semantics():
    case = default_cases()[1]
    unknown_meaning = case.facts[0].model_copy(update={"definition": None})
    case = case.model_copy(update={"facts": (unknown_meaning,)})
    expected = probe_answer_key(case)
    assert expected["directions"][unknown_meaning.name] == "high"
    assert expected["unknown_fields"] == [unknown_meaning.name]
    assert expected["confirmed_causes"] == []
    assert evaluate_probe(case, json.dumps(expected))["correct_fields"] == 5


def test_opaque_condition_and_false_release_claims_are_rejected():
    case = default_cases()[0].model_copy(update={"route": "ga_opaque"})
    with pytest.raises(ValueError, match="opaque"):
        DiagnosticRunSpec(source_commit="test", cases=(case,))
    with pytest.raises(ValueError):
        spec(release_eligible=True)


def test_http_boundary_preserves_exact_bytes_and_drops_reasoning_headers():
    observed, received = [], []
    raw = '{ "model":"offline-fake", "messages":[{"role":"user","content":"synthetic"}], "max_tokens":20 }'
    def handler(request):
        received.append(request.content)
        return httpx.Response(200, json={"model": "provider-model", "choices": [{"finish_reason": "stop", "message": {
            "content": "visible", "reasoning_content": "NEVER_CAPTURE_THIS"}}], "usage": {"prompt_tokens": 5, "completion_tokens": 2}})
    client = DirectHTTPReplayClient(endpoint="https://synthetic.invalid/chat/completions", api_key="NEVER_CAPTURE_KEY",
                                    observer=observed.append, transport=httpx.MockTransport(handler))
    try:
        result = client.complete_payload(json.loads(raw), request_id="test", metadata={}, exact_body=raw)
    finally:
        client.close()
    assert received == [raw.encode()]
    assert observed[0]["payload_verified"] is False
    assert observed[1]["payload_verified"] is True
    assert observed[1]["request_body_sha256"] == hashlib.sha256(received[0]).hexdigest()
    assert observed[1]["request_body_utf8"] == raw
    assert result["response_content"] == "visible"
    assert "NEVER_CAPTURE" not in json.dumps(observed)


def test_rejected_evidence_sink_prevents_http_send():
    received = []
    def reject(_):
        raise ValueError("sink unavailable")
    client = DirectHTTPReplayClient(endpoint="https://synthetic.invalid/chat/completions", api_key="key",
                                    observer=reject, transport=httpx.MockTransport(lambda request: received.append(request)))
    try:
        with pytest.raises(ValueError, match="sink unavailable"):
            client.complete_payload({"model": "x", "messages": []}, request_id="test", metadata={})
    finally:
        client.close()
    assert received == []


def test_actual_baseline_requires_http_identity_and_hash():
    body = '{"model":"offline-fake","messages":[{"role":"user","content":"observed synthetic"}],"max_tokens":10}'
    actual = ActualRequest(request_body_utf8=body, request_body_sha256=hashlib.sha256(body.encode()).hexdigest(),
                           endpoint="https://synthetic.invalid/v1/chat/completions", request_id="r", evidence_ref="synthetic://http/r")
    case = default_cases()[0].model_copy(update={"actual_request": actual})
    with pytest.raises(ValueError, match="unverified"):
        treatments(spec(), case)
    case = case.model_copy(update={"actual_request": actual.model_copy(update={"identity_verified": True})})
    arm = treatments(spec(), case)[0]
    assert arm.name == "A-prod" and arm.exact_body == body
    audited = audit_spec(DiagnosticRunSpec(source_commit="test", cases=(case,)))
    assert audited["conditions"][0]["payload_status"] == "captured_http_verified"
    assert all(row["payload_status"] == "manipulated_pending_http" for row in audited["conditions"][1:])
    with pytest.raises(ValueError, match="hash"):
        ActualRequest.model_validate({**actual.model_dump(), "request_body_sha256": "bad"})


@pytest.mark.parametrize("extra", [{"n": 1000}, {"max_completion_tokens": 65536}, {"tools": [{"description": "x" * 1_000_000}]}])
def test_unbudgeted_completions_caps_and_tool_schemas_are_rejected(extra):
    payload = {"model": "offline-fake", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1, **extra}
    with pytest.raises(ValueError):
        _validate_payload(payload, spec())


def test_entire_serialized_payload_and_http_hooks_are_guarded():
    payload = {"model": "offline-fake", "messages": [{"role": "user", "content": "x"}], "max_tokens": 1,
               "response_format": {"schema": "x" * 100_000}}
    with pytest.raises(ValueError, match="input-token"):
        _validate_payload(payload, spec())
    client = DirectHTTPReplayClient(endpoint="https://synthetic.invalid/chat/completions", api_key="key")
    client._client.event_hooks["request"].append(lambda request: None)
    try:
        with pytest.raises(ValueError, match="hooks"):
            client.complete_payload({"messages": []}, request_id="r", metadata={})
    finally:
        client.close()


def test_budget_reserves_unsettled_cost_across_instances_and_stops_at_45(tmp_path):
    budget = BudgetSpec(max_input_tokens=1000)
    price = PriceSpec(input_per_million=20_000, output_per_million=1000)
    path = tmp_path / "ledger.json"
    first = BudgetLedger(path, budget, price)
    first.reserve("a", max_output_tokens=1000)  # 21 CNY, interrupted
    second = BudgetLedger(path, budget, price)
    second.reserve("b", max_output_tokens=1000)
    with pytest.raises(BudgetStopped, match="45_CNY"):
        second.reserve("c", max_output_tokens=1000)
    assert float(second.snapshot()["exposure_cny"]) == 42
    assert second.settle("a", None)["status"] == "unsettled"
    assert float(second.snapshot()["exposure_cny"]) == 42
    second.settle("b", {"prompt_tokens": 1, "completion_tokens": 1})
    second.reserve("c", max_output_tokens=1000)
    assert float(second.snapshot()["exposure_cny"]) < 45


def test_windows_transient_ledger_replace_is_retried_without_torn_write(tmp_path, monkeypatch):
    original = Path.replace
    attempts = []
    def intermittent(self, target):
        if self.name == "ledger.json.tmp":
            attempts.append(1)
            if len(attempts) < 3:
                raise PermissionError("synthetic Windows sharing violation")
        return original(self, target)
    monkeypatch.setattr(Path, "replace", intermittent)
    ledger = BudgetLedger(tmp_path / "ledger.json", BudgetSpec(), PriceSpec())
    ledger.reserve("request", max_output_tokens=10)
    assert len(attempts) == 3 and ledger.snapshot()["requests"] == 1


def test_persistent_ledger_replace_failure_stops_before_reservation(tmp_path, monkeypatch):
    def unavailable(self, target):
        raise PermissionError("synthetic sharing violation")
    monkeypatch.setattr(Path, "replace", unavailable)
    monkeypatch.setattr("eval.emotional_expression_diagnostics.budget.time.sleep", lambda _: None)
    ledger = BudgetLedger(tmp_path / "ledger.json", BudgetSpec(), PriceSpec())
    with pytest.raises(BudgetStopped, match="atomic_write_unavailable"):
        ledger.reserve("request", max_output_tokens=10)
    assert not ledger.path.exists()


def test_offline_runner_retains_failures_and_has_no_implicit_release(tmp_path):
    class Fake(OfflineFakeClient):
        def complete_payload(self, *args, **kwargs):
            result = super().complete_payload(*args, **kwargs)
            result["finish_reason"] = "length"
            return result
    run = spec(budget=BudgetSpec(max_requests=2))
    result = run_experiment(run, tmp_path / "run", client=Fake())
    assert result["counts"]["attempted"] == 2
    assert result["counts"]["failed"] == 2
    assert result["status"] == "stopped" and not result["release_eligible"]
    rows = read_jsonl(tmp_path / "run" / "requests.jsonl")
    assert sum(row.get("result", {}).get("finish_reason") == "length" for row in rows) == 2
    with pytest.raises(FileExistsError):
        run_experiment(run, tmp_path / "run")


def test_full_fake_pilot_blinding_and_human_absence_are_inconclusive(tmp_path):
    from .summarize_run import summarize_run
    run_dir = tmp_path / "run"
    result = run_experiment(spec(), run_dir)
    assert result["counts"]["planned"] == 216
    assert result["counts"]["attempted"] + result["counts"]["unobservable_skipped"] == 216
    assert result["counts"]["unobservable_skipped"] > 0
    summary = export_blind_samples(run_dir)
    assert summary["samples"] > 0
    packets = read_jsonl(run_dir / "blind-samples.jsonl")
    assert all(set(row) == {"sample_id", "legal_context", "persona", "response", "stage"} for row in packets)
    assert all("B1-defined" not in json.dumps(row) and "expected_win" not in json.dumps(row) for row in packets)
    assert export_blind_samples(run_dir)["existing"] is True
    analysis = analyze_run(run_dir)
    assert analysis["status"] == "inconclusive" and not analysis["release_eligible"]
    assert "two_calibrated_independent_human_ratings_required" in analysis["limitations"]
    before = {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    descriptive = summarize_run(run_dir)
    assert descriptive["status"] == "descriptive_only" and descriptive["behavioral_confirmation"] == "inconclusive"
    assert descriptive["count_mismatches"] == [] and descriptive["human_scores_used"] is False
    assert all(item["independent_clusters"] == 1 for item in descriptive["groups"])
    assert "final_content" not in json.dumps(descriptive) and "response_content" not in json.dumps(descriptive)
    assert before == {path.name: path.read_bytes() for path in run_dir.iterdir() if path.is_file()}
    manifest_path = run_dir / "run-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["spec"]["run_id"] = "changed-after-freeze"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ValueError, match="hash"):
        summarize_run(run_dir)


def test_human_rating_import_preserves_originals_and_rejects_unknown_samples(tmp_path):
    run_dir = tmp_path / "run"
    run_experiment(spec(budget=BudgetSpec(max_requests=10)), run_dir)
    export_blind_samples(run_dir)
    sample = read_jsonl(run_dir / "blind-samples.jsonl")[0]["sample_id"]
    row = {"sample_id": sample, "rater_id": "rater-1", "scores": {key: 3 for key in ("adaptation", "recognizability", "persona", "naturalness")},
           "violations": {key: False for key in ("fact_action", "mechanism_leakage", "unsupported_physiology")}, "overexpression": False}
    source = tmp_path / "human.jsonl"
    append_jsonl(source, row)
    assert import_ratings(run_dir, source)["imported"] == 1
    assert import_ratings(run_dir, source)["imported"] == 0
    source.write_text(json.dumps({**row, "scores": {key: 5 for key in row["scores"]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="immutable"):
        import_ratings(run_dir, source)


def test_cluster_statistics_and_multiple_comparisons_are_not_pseudoreplicates():
    result = paired_estimate([1.0] * 30, seed=4, samples=1000)
    assert result["clusters"] == 30 and result["difference"] == 1
    assert result["ci95"] == [1.0, 1.0] and result["p_value"] < .01
    assert paired_estimate([1.0], seed=4)["ci95"] is None
    assert paired_estimate([0.0] * 30, seed=4)["p_value"] == 1
    assert holm([0.01, 0.03, 0.04]) == pytest.approx([0.03, 0.06, 0.06])
    assert wilson(0, 30)[1] > 0.05


def test_repeats_and_human_raters_do_not_inflate_independent_cluster_size():
    mapping, ratings = {}, {}
    for repeat in range(5):
        sample_id = str(repeat)
        mapping[sample_id] = {"cluster_id": "episode", "condition": "C-verbal", "case_id": "variant", "negative_control": False}
        ratings[sample_id] = [BlindRating(sample_id=sample_id, rater_id="human" + str(i),
            scores={"adaptation": 2 + 2 * i, "recognizability": 3, "persona": 3, "naturalness": 3},
            violations={"fact_action": False, "mechanism_leakage": False, "unsupported_physiology": False}, overexpression=False)
            for i in range(2)]
    assert _cluster_values(mapping, ratings, field="adaptation") == {("episode", "C-verbal"): 3.0}


def test_four_concurrent_requests_reserve_before_send_and_keep_one_budget(tmp_path):
    entered = threading.Barrier(4)
    lock = threading.Lock()
    observed = []
    class Concurrent(OfflineFakeClient):
        def complete_payload(self, *args, **kwargs):
            with lock:
                ledger = json.loads((tmp_path / "run" / "offline-budget.json").read_text(encoding="utf-8"))
                observed.append(ledger["requests"])
            entered.wait(timeout=3)
            return super().complete_payload(*args, **kwargs)
    result = run_experiment(spec(budget=BudgetSpec(max_requests=4, max_parallel_requests=4)), tmp_path / "run", client=Concurrent())
    assert result["counts"]["attempted"] == 4 and result["counts"]["failed"] == 0
    assert len(observed) == 4 and all(count >= 1 for count in observed)
    assert result["budget"]["requests"] == 4 and not result["budget"]["reservations"]


def test_confirmation_rejects_renamed_pilot_cases():
    from .models import case_fingerprint
    original = default_cases()[0]
    renamed = original.model_copy(update={"case_id": "new-name", "cluster_id": "new-cluster"})
    with pytest.raises(ValueError, match="reuses a pilot"):
        DiagnosticRunSpec(source_commit="test", phase="confirmation", source_spec_sha256="sealed-pilot",
                          pilot_case_fingerprints=(case_fingerprint(original),), cases=(renamed,))


def test_live_is_opt_in_and_price_identity_preflight_fails_closed():
    result = preflight(spec(), live=True)
    assert not result["passed"]
    assert set(result["problems"]) >= {"live_not_authorized_in_frozen_spec", "live_prices_unverified", "live_endpoint_unconfigured"}


def test_public_runner_cannot_send_http_under_offline_or_zero_price_budget(tmp_path):
    received = []
    client = DirectHTTPReplayClient(endpoint="https://synthetic.invalid/chat/completions", api_key="key",
                                    observer=lambda _: None, transport=httpx.MockTransport(lambda request: received.append(request)))
    try:
        with pytest.raises(ValueError, match="offline runs accept"):
            run_experiment(spec(), tmp_path / "run", client=client)
    finally:
        client.close()
    assert received == [] and not (tmp_path / "run").exists()


def test_cli_offline_commands_are_executable(tmp_path, capsys):
    frozen, run, audit = tmp_path / "spec.json", tmp_path / "run", tmp_path / "audit"
    assert main(["preflight", "--create-spec", str(frozen), "--source-commit", "synthetic", "--max-requests", "2"]) == 0
    assert main(["audit", "--spec", str(frozen), "--out", str(audit)]) == 0
    assert main(["pilot", "--spec", str(frozen), "--out", str(run)]) == 1  # bounded stop, not a release failure claim
    assert main(["judge", "--run", str(run)]) == 0
    assert main(["analyze", "--run", str(run)]) == 0
    assert '"release_eligible": false' in capsys.readouterr().out
