"""Focused tests for frozen inputs, isolated real QQ plumbing and honest scoring."""
from argparse import Namespace
from pathlib import Path
import subprocess
import sys

import pytest

from eval.qq_brief.common import digest, file_hash, frozen_scenarios, lexical_metrics, read, save
from eval.qq_brief import runner


ROOT = Path(__file__).resolve().parents[1]


def frozen(tmp_path):
    args = Namespace(run_dir=tmp_path, baseline_root=ROOT, candidate_root=ROOT,
                     production_snapshot=None, credential_file=None)
    runner.freeze(args)
    return read(tmp_path / "manifest.json")


def worker(tmp_path, *, arm="baseline", scenario="s01_memory_present", retry=False):
    command = [sys.executable, str(ROOT / "eval/qq_brief/worker.py"), "--run-dir", str(tmp_path),
               "--arm", arm, "--scenario", scenario, "--repeat", "1", "--mode", "synthetic"]
    if retry:
        command.append("--retry-errors")
    result = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=60)
    assert result.returncode == 0, result.stderr
    return tmp_path / "generation/synthetic" / f"{scenario}-r1-{arm}", result.stdout


def test_suite_is_twelve_independent_four_turn_threads():
    suite = frozen_scenarios()
    assert len(suite) == len({s["id"] for s in suite}) == 12
    assert all(len(s["turns"]) == 4 for s in suite)
    assert {"repeat_only", "new_question", "partial_delivery", "requested_detail", "memory_absent", "scope"} <= {
        turn["focus"] for scenario in suite for turn in scenario["turns"]}
    assert any(s.get("private") for s in suite)
    assert digest(suite) == digest(frozen_scenarios())


def test_primary_only_freeze_preserves_model_and_disables_only_eval_fallback(tmp_path):
    from harness.config import Settings
    from harness.llm.fallback import build_main_client, FallbackLLMClient

    outputs = {}
    for routing in ("configured", "primary-only"):
        folder = tmp_path / routing
        runner.freeze(Namespace(run_dir=folder, baseline_root=ROOT, candidate_root=ROOT,
            production_snapshot=None, credential_file=None, generation_routing=routing))
        outputs[routing] = read(folder / "generation-config.json")
    primary = outputs["primary-only"]
    assert primary["main"] == outputs["configured"]["main"]
    assert primary["aux"] == outputs["configured"]["aux"]
    assert "llm_fallback_config" not in outputs["configured"]["settings"]
    assert primary["settings"]["llm_fallback_config"] is None
    settings = Settings(_env_file=None, **primary["settings"], llm_api_key="test-only",
                        llm_base_url=primary["main"]["base_url"], llm_model=primary["main"]["model"])
    client = build_main_client(settings)
    try:
        assert not isinstance(client, FallbackLLMClient)
        assert client.settings.llm_model == primary["main"]["model"]
    finally:
        client.close()
    manifest = read(tmp_path / "primary-only/manifest.json")
    assert manifest["inputs"]["generation-config.json"] == file_hash(tmp_path / "primary-only/generation-config.json")


def test_freeze_rejects_unknown_generation_routing(tmp_path):
    with pytest.raises(ValueError, match="unknown_generation_routing"):
        runner.freeze(Namespace(run_dir=tmp_path, baseline_root=ROOT, candidate_root=ROOT,
            production_snapshot=None, credential_file=None, generation_routing="random"))


def test_semantic_suite_adds_eight_frozen_threads_and_preserves_author_relations(tmp_path):
    from eval.qq_brief.semantic_scenarios import semantic_scenarios
    added = semantic_scenarios()
    assert len(added) == 8 and all(len(s["turns"]) == 4 for s in added)
    assert all(t["semantic_expectation"] for s in added for t in s["turns"])
    runner.freeze(Namespace(run_dir=tmp_path, baseline_root=ROOT, candidate_root=ROOT,
        production_snapshot=None, credential_file=None, semantic_alignment=True))
    assert read(tmp_path / "manifest.json")["scenario_count"] == 20
    assert read(tmp_path / "scenarios.json")[:12] == frozen_scenarios()
    path, _ = worker(tmp_path, arm="candidate", scenario="s18_multiple_speakers")
    rows = [read(path / f"turn-{i}.json") for i in range(1, 5)]
    assert all(r["status"] == "ok" for r in rows)
    assert [r["decision"]["trigger_member_id"] for r in rows] == ["20004", "20003", "20004", "20003"]


def test_semantic_suite_retains_failed_turns_and_completes_later_opportunities(tmp_path):
    import shutil
    runner.freeze(Namespace(run_dir=tmp_path, baseline_root=ROOT, candidate_root=ROOT,
        production_snapshot=None, credential_file=None, semantic_alignment=True))
    fake = tmp_path / "test-worker"
    fake.mkdir()
    source = (ROOT / "eval/qq_brief/worker.py").read_text(encoding="utf-8")
    source = source.replace("return super().complete_chat_result(messages, **kwargs)",
                            'raise TimeoutError("synthetic_provider_timeout")')
    (fake / "worker.py").write_text(source, encoding="utf-8")
    shutil.copy2(ROOT / "eval/qq_brief/common.py", fake / "common.py")
    for arm in ("baseline", "candidate"):
        command = [sys.executable, str(fake / "worker.py"), "--run-dir", str(tmp_path),
            "--arm", arm, "--scenario", "s13_incident_relations", "--repeat", "1", "--mode", "synthetic"]
        completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert completed.returncode == 0, completed.stderr
        folder = tmp_path / "generation/synthetic" / f"s13_incident_relations-r1-{arm}"
        rows = [read(folder / f"turn-{i}.json") for i in range(1, 5)]
        assert all(r["status"] == "generation_error" and r["continued_after_error"] for r in rows)
        assert not any(r["role"] == "assistant" for r in rows[-1]["transcript"])
        assert all((folder / f"state-{i}.sqlite3").exists() for i in range(1, 5))
        before = [file_hash(folder / f"turn-{i}.json") for i in range(1, 5)]
        repeated = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert repeated.returncode == 0 and before == [file_hash(folder / f"turn-{i}.json") for i in range(1, 5)]
    runner.seal(Namespace(run_dir=tmp_path, mode="synthetic"))
    packet = read(tmp_path / "packets/synthetic/s13_incident_relations-r1.json")
    assert all(r["reply"] is None for rows in packet["variants"].values() for r in rows)



def test_lexical_checks_are_not_semantic_claims():
    assert lexical_metrics("我选安静的。晚上睡觉不容易被吵醒。 ")["within_60_and_2"]
    assert not lexical_metrics("字" * 61)["within_60_and_2"]
    assert not lexical_metrics("一句。二句。三句。")["within_60_and_2"]
    assert lexical_metrics("嗯，我想先试试。")["hesitation_regex"]
    assert lexical_metrics("我不记得那件事。")["forget_regex"]
    assert not lexical_metrics("")["within_60_and_2"]


@pytest.mark.parametrize("http_status", [401, 402, 403, 429, 503])
def test_provider_denial_stops_both_arms_without_erasing_first_failure(tmp_path, http_status):
    import shutil
    runner.freeze(Namespace(run_dir=tmp_path, baseline_root=ROOT, candidate_root=ROOT,
        production_snapshot=None, credential_file=None, semantic_alignment=True))
    fake = tmp_path / "test-worker"
    fake.mkdir()
    source = (ROOT / "eval/qq_brief/worker.py").read_text(encoding="utf-8")
    source = source.replace("return super().complete_chat_result(messages, **kwargs)",
        'from infra.trace.diagnostics import emit_diagnostic\n'
        f'            emit_diagnostic({{"record_type": "http_response_received", "http_status": {http_status}}})\n'
        '            raise RuntimeError("synthetic_provider_denial")')
    (fake / "worker.py").write_text(source, encoding="utf-8")
    shutil.copy2(ROOT / "eval/qq_brief/common.py", fake / "common.py")
    command = [sys.executable, str(fake / "worker.py"), "--run-dir", str(tmp_path),
               "--arm", "candidate", "--scenario", "s13_incident_relations", "--repeat", "1", "--mode", "synthetic"]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert completed.returncode == 0, completed.stderr
    folder = tmp_path / "generation/synthetic/s13_incident_relations-r1-candidate"
    first = read(folder / "turn-1.json")
    assert first["status"] == "generation_error" and (folder / "state-1.sqlite3").exists()
    blocked = http_status != 503
    assert (folder / "turn-2.json").exists() is not blocked
    assert first["continued_after_error"] is not blocked
    for arm in ("baseline", "candidate"):
        assert (tmp_path / f"STOP-{arm}").exists() is blocked
        if blocked:
            assert read(tmp_path / f"STOP-{arm}")["http_status"] == http_status
    if blocked:
        assert first["provider_blocked"]["http_status"] == http_status
        before = file_hash(folder / "turn-1.json")
        rerun = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", timeout=60)
        assert "STOPPED" in rerun.stdout and file_hash(folder / "turn-1.json") == before


def test_freeze_contains_no_credential_contents_and_is_immutable(tmp_path):
    frozen(tmp_path)
    value = read(tmp_path / "generation-config.json")
    assert value["credential_file"] is None
    assert "api_key" not in str(value)
    with pytest.raises(ValueError, match="already_frozen"):
        frozen(tmp_path)


def test_real_qq_chain_uses_own_receipts_resumes_and_seals_anonymously(tmp_path):
    frozen(tmp_path)
    first, _ = worker(tmp_path)
    hashes = {p.name: file_hash(p) for p in first.glob("turn-*.json")}
    assert len(hashes) == 4
    rows = [read(first / f"turn-{i}.json") for i in range(1, 5)]
    assert all(r["status"] == "ok" and r["simulation_only"] and r["mode"] == "synthetic" for r in rows)
    assert rows[1]["history_before"][-1]["text"] == rows[0]["reply"]
    assert rows[1]["decision"]["delivery_refs"]
    assert rows[0]["decision"]["model_call_counts"]
    assert rows[0]["model_requests"] == 0  # No HTTP request disguised as a synthetic score.
    worker(tmp_path)
    assert hashes == {p.name: file_hash(p) for p in first.glob("turn-*.json")}
    worker(tmp_path, arm="candidate")
    runner.seal(Namespace(run_dir=tmp_path, mode="synthetic"))
    packet = read(tmp_path / "packets/synthetic/s01_memory_present-r1.json")
    assert set(packet["variants"]) == {"A", "B"}
    assert "baseline" not in str(packet) and "candidate" not in str(packet)
    runner.analyze(Namespace(run_dir=tmp_path, mode="synthetic"))
    summary = read(tmp_path / "summary-synthetic.json")
    assert all(s["acceptance"] == "not_evaluated" and s["naturalness_mean"] is None for s in summary["arms"].values())


def test_partial_seed_and_private_path_are_exercised_without_delivery_adapters(tmp_path):
    frozen(tmp_path)
    partial, _ = worker(tmp_path, scenario="s07_partial_delivery")
    assert read(partial / "turn-1.json")["status"] == "ok"
    import sqlite3
    with sqlite3.connect(partial / "state-0.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM qq_reply_segment_deliveries").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM qq_reply_deliveries").fetchone()[0] == 0
    private, _ = worker(tmp_path, scenario="s12_private_intent")
    result = read(private / "turn-4.json")
    assert result["status"] == "ok"
    assert result["decision"]["group_id"] == "private:29999:20003"


def test_interrupted_turn_never_silently_reissues_request(tmp_path):
    frozen(tmp_path)
    folder = tmp_path / "generation/synthetic/s01_memory_present-r1-baseline"
    folder.mkdir(parents=True)
    save(folder / "turn-1.inflight.json", {"interrupted": True})
    _, output = worker(tmp_path)
    assert "interrupted_request" in output and not (folder / "turn-1.json").exists()


def test_rating_schema_rejects_missing_semantic_evidence():
    with pytest.raises(ValueError):
        runner.validate_rating({"turns": []})
    rating = {"turns": [{"turn": i, **{label: {**{field: False for field in runner.BOOL_FIELDS},
        "naturalness": 4, "boundary_at_end": None, "note": "具体证据"} for label in ("A", "B")}}
        for i in range(1, 5)]}
    assert runner.validate_rating(rating) is rating
    rating["turns"][0]["A"]["first_answer"] = "true"
    with pytest.raises(ValueError, match="boolean"):
        runner.validate_rating(rating)


def test_running_summary_ignores_inflight_markers(tmp_path):
    frozen(tmp_path)
    save(tmp_path / "generation/live/s01_memory_present-r1-baseline/turn-1.inflight.json", {"started": True})
    runner.analyze(Namespace(run_dir=tmp_path, mode="live"))
    result = read(tmp_path / "summary-live.json")
    assert result["arms"]["baseline"]["recorded_turns"] == 0
    assert result["arms"]["baseline"]["acceptance"] == "not_evaluated"


def test_judge_interruption_requires_explicit_retry(tmp_path, monkeypatch):
    import httpx
    packet = {"packet_id": "test", "variants": {"A": [], "B": []}}
    path = tmp_path / "packets/live/test.json"
    save(path, packet)
    save(tmp_path / "packets/live/seal.json", {"packets": {"test": file_hash(path)},
                                                "judge_system_sha256": digest(runner.JUDGE_SYSTEM)})
    save(tmp_path / "judge/test.inflight.json", {"request_uncertain": True})
    monkeypatch.setattr(runner, "verify_seal", lambda *args: read(tmp_path / "packets/live/seal.json"))
    calls = []

    class FakeClient:
        def __init__(self, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def get(self, path):
            return httpx.Response(200, request=httpx.Request("GET", "https://api.moonshot.cn/v1/models"),
                                  json={"data": [{"id": "kimi-k3"}]})

        def post(self, *args, **kwargs):
            calls.append(args)
            raise AssertionError("An interrupted request must not be silently repeated")

    monkeypatch.setattr(httpx, "Client", FakeClient)
    monkeypatch.setenv("MOONSHOT_API_KEY", "synthetic-private-key")
    runner.judge(Namespace(run_dir=tmp_path, retry_errors=False))
    assert not calls
    assert (tmp_path / "judge/test.inflight.json").exists()


def test_stop_candidate_is_checked_before_state_or_network(tmp_path):
    frozen(tmp_path)
    runner.stop(Namespace(run_dir=tmp_path, arm="candidate"))
    folder, output = worker(tmp_path, arm="candidate")
    assert "STOPPED" in output
    assert not folder.exists()
    baseline, _ = worker(tmp_path, arm="baseline")
    assert read(baseline / "turn-4.json")["status"] == "ok"


def test_reuse_preserves_completed_outputs_and_failed_attempt_evidence(tmp_path):
    original, target = tmp_path / "original", tmp_path / "target"
    frozen(original)
    folder, _ = worker(original)
    save(folder / "prior-attempts/first/error.json", {"synthetic_error": "preserved"})
    frozen(target)
    args = Namespace(run_dir=target, from_run=original, arm="baseline", mode="synthetic")
    runner.reuse(args)
    copied = target / "generation/synthetic" / folder.name
    assert file_hash(copied / "turn-4.json") == file_hash(folder / "turn-4.json")
    assert read(copied / "prior-attempts/first/error.json") == {"synthetic_error": "preserved"}
    runner.reuse(args)
    provenance = read(target / "provenance/reuse-baseline-synthetic.json")
    assert provenance["jobs"][folder.name]["requests_reissued"] is False
    runner.verify_reuse(target)
    save(copied / "prior-attempts/first/error.json", {"synthetic_error": "changed"})
    with pytest.raises(ValueError, match="reused_evidence"):
        runner.verify_reuse(target)


@pytest.fixture
def sealed_pair(tmp_path):
    frozen(tmp_path)
    worker(tmp_path)
    worker(tmp_path, arm="candidate")
    runner.seal(Namespace(run_dir=tmp_path, mode="synthetic"))
    return tmp_path


@pytest.mark.parametrize("target", ["packet", "input", "http", "generation", "mapping"])
def test_seal_rejects_changed_evidence(sealed_pair, target):
    root = sealed_pair
    paths = {"packet": root / "packets/synthetic/s01_memory_present-r1.json",
             "input": root / "scenarios.json",
             "http": root / "generation/synthetic/s01_memory_present-r1-baseline/turn-1.http.jsonl",
             "generation": root / "generation/synthetic/s01_memory_present-r1-baseline/turn-1.json",
             "mapping": root / "private/synthetic-arm-map.json"}
    with paths[target].open("a", encoding="utf-8") as stream:
        stream.write(" ")
    with pytest.raises(ValueError, match="changed|mismatch"):
        runner.verify_seal(root, "synthetic")


def test_seal_rejects_changed_source(sealed_pair, monkeypatch):
    monkeypatch.setattr(runner, "source_hashes", lambda root: {"harness/changed.py": "different"})
    with pytest.raises(ValueError, match="source_changed"):
        runner.verify_seal(sealed_pair, "synthetic")


def test_successful_rating_is_verified_before_judge_skips_it(tmp_path, monkeypatch):
    import httpx
    sealed = {"packets": {"test": "expected-current-packet-hash"}}
    monkeypatch.setattr(runner, "verify_seal", lambda *args: sealed)
    save(tmp_path / "judge/test.json", {"rating": {"turns": []}, "packet_sha256": "stale-packet"})
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: pytest.fail("No API access before evidence validation"))
    with pytest.raises(ValueError, match="rating_packet_hash"):
        runner.judge(Namespace(run_dir=tmp_path, retry_errors=False))


@pytest.mark.parametrize("metric", ["known_scope_canary_leaks", "repeated_full_boundary", "boundary_not_at_end", "stance_evasion"])
def test_required_behavior_metrics_independently_block_pass(metric):
    summary = {"known_scope_canary_leaks": 0, "ordinary_within_60_and_2": 1.0, "ordinary_first_answer": 1.0,
        **{key: 0 for key in ("ordinary_hesitation", "ordinary_full_relationship_cycle", "explicit_forget",
            "unsupported_facts", "scope_leaks", "missed_new_questions", "repeated_full_boundary",
            "boundary_not_at_end", "stance_evasion")}}
    assert runner.acceptance(summary, complete=True) == "pass"
    summary[metric] = 1
    assert runner.acceptance(summary, complete=True) == "fail"
    if metric == "known_scope_canary_leaks":
        assert runner.acceptance(summary, complete=False) == "fail"


def test_judge_rebind_records_actual_time_and_prior_hash_before_first_judge(tmp_path):
    frozen(tmp_path)
    manifest = read(tmp_path / "manifest.json")
    manifest["judge_system_sha256"] = "prior-rubric-hash"
    save(tmp_path / "manifest.json", manifest)
    runner.bind_judge(Namespace(run_dir=tmp_path, reason="Clarify already-approved exceptions"))
    updated = read(tmp_path / "manifest.json")
    assert updated["judge_system_sha256"] == digest(runner.JUDGE_SYSTEM)
    assert updated["inputs"] == manifest["inputs"] and updated["sources"] == manifest["sources"]
    records = list((tmp_path / "provenance").glob("judge-rubric-rebind-*.json"))
    assert len(records) == 1
    proof = read(records[0])
    assert proof["prior_judge_system_sha256"] == "prior-rubric-hash" and proof["captured_at"]
    assert "not original freeze" in proof["timing"]
    save(tmp_path / "judge/started.request.json", {})
    with pytest.raises(ValueError, match="already_started"):
        runner.bind_judge(Namespace(run_dir=tmp_path, reason="Too late"))


def test_attempt_inventory_retains_failures_and_all_attempt_requests(tmp_path):
    for scenario, index in (("s01", 3), ("s02", 1)):
        folder = tmp_path / "generation/live" / f"{scenario}-r1-baseline"
        save(folder / f"turn-{index}.json", {"status": "ok", "turn": index})
        (folder / f"turn-{index}.http.jsonl").write_text(
            '{"record_type":"http_request_prepared","request_id":"latest-' + scenario + '"}\n', encoding="utf-8")
        failure = {"status": "generation_error", "turn": index, "error": {"error_type": "model_invalid"}}
        for duplicate in ("first", "copied-again"):
            archive = folder / "prior-attempts" / duplicate
            save(archive / f"turn-{index}.json", failure)
            (archive / f"turn-{index}.http.jsonl").write_text(
                '{"record_type":"http_request_prepared","request_id":"prior-' + scenario + '"}\n', encoding="utf-8")
    value = runner.attempt_inventory(tmp_path, mode="live", arm="baseline")
    assert value["prior_failures"] == 2
    assert value["prior_failure_types"] == {"model_invalid": 2}
    assert value["initial_unattempted_after_failure"] == 4
    assert value["http_model_requests_latest"] == 2
    assert value["http_model_requests_prior_attempts"] == 2
    assert value["http_model_requests_all_attempts"] == 4


def test_semantic_acceptance_retry_keeps_initial_failure_and_latency(tmp_path):
    from eval.qq_brief.semantic_acceptance import summarize
    row = {"scenario_id": "s13", "repeat": 1, "turn": 1, "status": "ok", "reply": "理解了。",
        "input": {"semantic_expectation": "解释", "must_answer": True, "focus": "answer"},
        "trace": {"counters": {}}, "latency_ms": 10, "model_requests": 5,
        "reviewed_candidates": ["理解了。"]}
    failed = row | {"status": "generation_error", "latency_ms": 100, "error": {"error_type": "model_timeout"}}
    folder = tmp_path / "generation/live/s13-r1-candidate"
    save(folder / "prior-attempts/first/turn-1.json", failed)
    save(folder / "turn-1.json", row)
    failures = runner.initial_generation_failures(tmp_path, mode="live", arm="candidate")
    score = {"semantic_pass": True}
    result = summarize({a: [row] for a in ("baseline", "candidate")},
        {a: [(row, score)] for a in ("baseline", "candidate")}, expected_turns=1,
        initial_failures={"baseline": [], "candidate": failures})
    assert result["arms"]["candidate"]["semantic_passes"] == 0
    assert result["arms"]["candidate"]["p95_latency_ms"] == 100
    assert result["human_review_queue"][0]["initial_generation_failure"] == failed["error"]
    assert not result["rollout_enforce_allowed"]


def test_legacy_judge_payload_is_byte_equivalent_and_has_no_effort():
    packet = {"packet_id": "test", "variants": {"A": [], "B": []}}
    from eval.qq_brief.common import canonical
    expected = {"model": "kimi-k3", "messages": [{"role": "system", "content": runner.JUDGE_SYSTEM},
                {"role": "user", "content": canonical(packet)}], "max_tokens": 8192}
    assert runner._judge_job(packet) == expected
    assert canonical(runner._judge_job(packet)) == canonical(expected)
    assert runner.bound_judge_config({}) == {"max_tokens": 8192}


@pytest.mark.parametrize("config", [
    {"max_tokens": 0}, {"max_tokens": True}, {"max_tokens": "8192"},
    {"max_tokens": 8192, "reasoning_effort": "medium"},
    {"max_tokens": 8192, "reasoning_effort": None}, {"max_tokens": 8192, "temperature": 1},
])
def test_invalid_judge_parameters_rejected(config):
    with pytest.raises(ValueError, match="judge_"):
        runner._judge_job({}, config)


def test_configured_freeze_seal_and_tampering(tmp_path):
    args = Namespace(run_dir=tmp_path, baseline_root=ROOT, candidate_root=ROOT,
                     production_snapshot=None, credential_file=None,
                     judge_max_tokens=16384, judge_reasoning_effort="high")
    runner.freeze(args)
    manifest = read(tmp_path / "manifest.json")
    config = {"max_tokens": 16384, "reasoning_effort": "high"}
    assert manifest["judge_config"] == config
    assert manifest["judge_config_sha256"] == digest(config)
    runner.seal(Namespace(run_dir=tmp_path, mode="synthetic"))
    sealed = runner.verify_seal(tmp_path, "synthetic")
    assert sealed["judge_config"] == config
    assert sealed["judge_config_sha256"] == digest(config)
    manifest["judge_config"]["max_tokens"] = 8192
    save(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="judge_config_hash"):
        runner.verify_seal(tmp_path, "synthetic")
    manifest["judge_config_sha256"] = digest(manifest["judge_config"])
    save(tmp_path / "manifest.json", manifest)
    with pytest.raises(ValueError, match="sealed_judge_config"):
        runner.verify_seal(tmp_path, "synthetic")


def test_old_manifest_and_seal_without_judge_config_remain_compatible(tmp_path):
    frozen(tmp_path)
    manifest = read(tmp_path / "manifest.json")
    manifest.pop("judge_config")
    manifest.pop("judge_config_sha256")
    save(tmp_path / "manifest.json", manifest)
    runner.seal(Namespace(run_dir=tmp_path, mode="synthetic"))
    sealed = runner.verify_seal(tmp_path, "synthetic")
    assert "judge_config" not in sealed
    assert runner.bound_judge_config(sealed) == {"max_tokens": 8192}


@pytest.mark.parametrize("config", [None, {"max_tokens": 16384, "reasoning_effort": "high"}])
def test_serial_judge_sends_sealed_config_and_validates_retained_request(tmp_path, monkeypatch, config):
    import httpx
    from eval.qq_brief.common import canonical
    packet_path = tmp_path / "packets/live/test.json"
    packet = {"packet_id": "test", "variants": {"A": [], "B": []}}
    save(packet_path, packet)
    sealed = {"packets": {"test": file_hash(packet_path)}, "manifest_sha256": "manifest"}
    if config is not None:
        sealed.update(judge_config=config, judge_config_sha256=digest(config))
    rating = {"turns": [{"turn": i, **{label: {**{field: False for field in runner.BOOL_FIELDS},
        "naturalness": 4, "boundary_at_end": None, "note": "test"} for label in ("A", "B")}}
        for i in range(1, 5)]}
    sent = []
    def transport(request):
        if request.method == "GET":
            return httpx.Response(200, json={"data": [{"id": "kimi-k3"}]})
        import json
        sent.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "kimi-k3", "choices": [
            {"message": {"content": canonical(rating)}}]})
    client_class = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: client_class(
        **kwargs, transport=httpx.MockTransport(transport)))
    monkeypatch.setattr(runner, "verify_seal", lambda *args: sealed)
    monkeypatch.setenv("MOONSHOT_API_KEY", "synthetic-private-key")
    runner.judge(Namespace(run_dir=tmp_path, retry_errors=False))
    assert sent == [runner._judge_job(packet, config)]
    assert runner.validated_rating(tmp_path, "test", sealed) == rating
    runner.judge(Namespace(run_dir=tmp_path, retry_errors=False))
    assert len(sent) == 1
    changed = {"max_tokens": 32768, "reasoning_effort": "high"}
    sealed.update(judge_config=changed, judge_config_sha256=digest(changed))
    with pytest.raises(ValueError, match="rating_run_binding|rating_request_hash"):
        runner.validated_rating(tmp_path, "test", sealed)
