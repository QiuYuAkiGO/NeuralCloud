from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess

import pytest

from eval.emotional_expression_diagnostics import cursor_review as cr


def sample(sample_id="blind-001"):
    return {"sample_id": sample_id, "legal_context": "负荷偏高正在缓和；未完成休息动作。",
            "persona": "简洁、诚恳。", "response": "我有点绷着，不过比刚才缓过来些。", "stage": "initial"}


def billing():
    return {"source": "user_confirmation", "on_demand_enabled": False, "existing_quota_only": True,
            "checked_at_utc": datetime.now(timezone.utc).isoformat()}


def output_payload(focus="precision", sample_id="blind-001"):
    return {"sample_id": sample_id, "scores": {**dict.fromkeys(cr.SCORE_KEYS, 4), focus: 4},
            "violations": dict.fromkeys(cr.VIOLATION_KEYS, False), "overexpression": False,
            "evidence": ["‘缓过来些’与负荷正在缓和相符。"]}


def completion(payload=None, *, label="Fable 5.1", extra_events=(), is_error=False, returncode=0, usage=None):
    events = [{"type": "system", "subtype": "init", "model": label, "session_id": "isolated-session"},
              *extra_events,
              {"type": "result", "subtype": "error" if is_error else "success", "is_error": is_error,
               "result": json.dumps(output_payload() if payload is None else payload, ensure_ascii=False),
               "session_id": "isolated-session", "request_id": "request-id", "usage": usage or {"inputTokens": 100}}]
    return subprocess.CompletedProcess([], returncode, "\n".join(json.dumps(e) for e in events), "")


def review():
    return cr.BlindReview(sample_id="blind-001", stage="initial", requested_model="claude-fable-5-1",
                          prompt_hash="hash", focus="precision")


@pytest.fixture
def run_dir(tmp_path):
    (tmp_path / "blind-samples.jsonl").write_text(json.dumps(sample(), ensure_ascii=False) + "\n", encoding="utf-8")
    (tmp_path / "review-authorizations.json").write_text('{"blind-001":true}', encoding="utf-8")
    (tmp_path / "blind-map.json").write_text('{"secret_condition":"must-never-be-sent"}', encoding="utf-8")
    return tmp_path


def fake_preflight_and_reviews(calls, *, catalog=None, fail=None):
    catalog = catalog if catalog is not None else "claude-fable-5-1 - Fable 5.1\nkimi-k3-high - Kimi K3 High\n"

    def run(command, *, cwd, env, timeout):
        calls.append((command, cwd, dict(env)))
        if "status" in command:
            return subprocess.CompletedProcess(command, 0, '{"isAuthenticated":true}', "")
        if "models" in command:
            return subprocess.CompletedProcess(command, 0, catalog, "")
        if fail:
            return fail(command, cwd)
        model = command[command.index("--model") + 1]
        focus = "precision" if model == "claude-fable-5-1" else "chinese_fit"
        return completion(output_payload(focus), label="Fable 5.1" if focus == "precision" else "Kimi K3 High")

    return run


def prepare_live(monkeypatch):
    monkeypatch.setattr(cr, "audited_cli_command", lambda bundle=None: ("audited-node", "index.js"))
    return cr.CursorReviewConfig(billing_evidence=billing())


def test_default_exports_without_any_subprocess_and_excludes_hidden_fields(run_dir):
    data = {**sample(), "condition": "secret-condition", "ground_truth": "private-answer"}
    (run_dir / "blind-samples.jsonl").write_text(json.dumps(data), encoding="utf-8")
    result = cr.review_run(run_dir, run_process=lambda *a, **kw: pytest.fail("unexpected subprocess"))
    exported = (run_dir / "cursor-review-requests.jsonl").read_text(encoding="utf-8")
    assert result["status"] == "exported"
    assert "secret-condition" not in exported and "private-answer" not in exported and "must-never-be-sent" not in exported
    assert not (run_dir / "aux-ratings.jsonl").exists()


@pytest.mark.parametrize("change", [{}, {"on_demand_enabled": True}, {"existing_quota_only": False},
                                    {"source": "assumed"}, {"checked_at_utc": "bad"}])
def test_billing_gate_blocks_without_dispatch(run_dir, change):
    evidence = {} if not change else {**billing(), **change}
    result = cr.review_run(run_dir, live=True, config=cr.CursorReviewConfig(billing_evidence=evidence),
                           run_process=lambda *a, **kw: pytest.fail("paid dispatch before budget verification"))
    assert result["status"] == "blocked" and result["attempted"] == 0


def test_billing_evidence_expires():
    evidence = billing()
    evidence["checked_at_utc"] = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    with pytest.raises(cr.ReviewBlocked, match="expired"):
        cr.validate_billing_evidence(evidence)


def test_private_samples_require_explicit_external_authorization(run_dir):
    (run_dir / "review-authorizations.json").write_text('{"blind-001":false}', encoding="utf-8")
    result = cr.review_run(run_dir, live=True, config=cr.CursorReviewConfig(billing_evidence=billing()),
                           run_process=lambda *a, **kw: pytest.fail("unauthorized sample sent"))
    assert result["status"] == "blocked" and result["skipped_unauthorized"] == 2


def test_live_calls_have_fresh_isolation_no_resume_no_shell_and_fixed_model_ids(run_dir, monkeypatch):
    config = prepare_live(monkeypatch)
    calls = []
    old_read = Path.read_text
    monkeypatch.setenv("CURSOR_API_ENDPOINT", "https://untrusted.example")
    monkeypatch.setenv("UNRELATED_SECRET", "must-not-propagate")
    monkeypatch.setattr(Path, "read_text", lambda self, *a, **kw: pytest.fail("blind map read")
                        if self.name == "blind-map.json" else old_read(self, *a, **kw))

    def checked(command, *, cwd, env, timeout):
        assert "CURSOR_API_ENDPOINT" not in env and "UNRELATED_SECRET" not in env
        settings = json.loads((Path(env["CURSOR_CONFIG_DIR"]) / "cli-config.json").read_text())
        assert settings["permissions"] == {"allow": [], "deny": list(cr.DENIED_PERMISSIONS)}
        assert settings["network"]["useHttp1ForAgent"] is True
        assert json.loads((Path(env["CURSOR_CONFIG_DIR"]) / "mcp.json").read_text()) == {"mcpServers": {}}
        if "--model" in command:
            assert command[command.index("--mode") + 1] == "ask"
            assert command[command.index("--allowed-tools") + 1] == ""
            assert command[command.index("--sandbox") + 1] == "disabled"
            assert "--exclude-workspace-context" not in command and "--disable-project-configs" in command
            assert Path(env["USERPROFILE"]) == cwd.parent / "home"
            assert Path(env["USERPROFILE"]).is_dir()
            assert not {"--resume", "--continue", "--force", "--yolo", "--approve-mcps"} & set(command)
            assert not list(cwd.iterdir())
        return fake_preflight_and_reviews(calls)(command, cwd=cwd, env=env, timeout=timeout)

    result = cr.review_run(run_dir, live=True, config=config, run_process=checked)
    assert result["attempted"] == 2 and result["completed_unverified"] == 2 and result["verified_reviews"] == 0
    assert calls[2][1] != calls[3][1]
    rows = [json.loads(line) for line in (run_dir / "aux-ratings.jsonl").read_text(encoding="utf-8").splitlines()]
    assert rows[1]["requested_model"] == "kimi-k3" and rows[1]["cli_requested_model"] == "kimi-k3-high"
    assert all(row["actual_model"] is None for row in rows)


def test_ancestor_rules_fail_closed_without_reading_contents(tmp_path):
    (tmp_path / "AGENTS.md").write_text("private ancestor instructions")
    workspace = tmp_path / "empty-child"
    workspace.mkdir()
    with pytest.raises(cr.ReviewBlocked, match="ancestor_context"):
        cr._assert_clean_ancestors(workspace)


def test_proxy_uses_existing_windows_route_without_recording_credentials(monkeypatch, tmp_path):
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.setattr(cr, "getproxies", lambda: {"https": "http://private-user:private-pass@localhost:9999"})
    env = cr._isolated_env(tmp_path)
    if cr.os.name == "nt":
        assert env["HTTPS_PROXY"] == "http://private-user:private-pass@localhost:9999"
        assert env["NODE_USE_ENV_PROXY"] == "1"
    monkeypatch.setenv("HTTPS_PROXY", "http://explicit-proxy:9998")
    assert cr._isolated_env(tmp_path)["HTTPS_PROXY"] == "http://explicit-proxy:9998"
    diagnostic = cr._redacted_diagnostic("connection failed http://private-user:private-pass@proxy:9999")
    assert diagnostic == "connection failed [redacted-url]"


def test_fable_high_variant_and_single_model_scope(run_dir, monkeypatch):
    from dataclasses import replace
    config = replace(prepare_live(monkeypatch), models=("claude-fable-5-1",))
    calls = []
    result = cr.review_run(run_dir, live=True, config=config, run_process=fake_preflight_and_reviews(
        calls, catalog="claude-fable-5-1-high - Claude Fable 5.1\nkimi-k3-high - Kimi K3 High\n"))
    assert result["attempted"] == 1
    row = json.loads((run_dir / "aux-ratings.jsonl").read_text(encoding="utf-8"))
    assert row["requested_model"] == "claude-fable-5-1"
    assert row["cli_requested_model"] == "claude-fable-5-1-high" and row["cli_effort"] == "high"


def test_standalone_inspection_never_infers_or_requires_billing(tmp_path, monkeypatch):
    prepare_live(monkeypatch)
    calls = []
    result = cr.inspect_cursor_environment(tmp_path, run_process=fake_preflight_and_reviews(calls))
    assert result["authenticated"] is True
    assert len(calls) == 2 and all("--model" not in call[0] for call in calls)
    assert (tmp_path / "cursor-preflight.json").is_file()
    assert not (tmp_path / "aux-ratings.jsonl").exists()


def test_synthetic_entrypoint_is_one_explicit_model_and_never_overwrites(tmp_path, monkeypatch):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps(billing()))
    output = tmp_path / "smoke"
    calls = []

    def fake_review(directory, **kwargs):
        calls.append(kwargs)
        sample = json.loads((directory / "blind-samples.jsonl").read_text(encoding="utf-8"))
        assert sample["sample_id"] == "synthetic-smoke-001"
        assert set(sample) == {"sample_id", "legal_context", "persona", "response", "stage"}
        return {"status": "completed_unverified", "attempted": 1}

    monkeypatch.setattr(cr, "review_run", fake_review)
    cr.run_synthetic_smoke(output, model="claude-fable-5-1", billing_evidence_file=evidence)
    assert len(calls) == 1 and calls[0]["config"].models == ("claude-fable-5-1",)
    assert calls[0]["config"].max_calls == 1
    with pytest.raises(FileExistsError):
        cr.run_synthetic_smoke(output, model="claude-fable-5-1", billing_evidence_file=evidence)
    assert len(calls) == 1


def test_synthetic_entrypoint_requires_fresh_disabled_overage(tmp_path):
    evidence = tmp_path / "evidence.json"
    evidence.write_text(json.dumps({**billing(), "on_demand_enabled": True}))
    output = tmp_path / "smoke"
    with pytest.raises(cr.ReviewBlocked):
        cr.run_synthetic_smoke(output, model="kimi-k3", billing_evidence_file=evidence)
    assert not output.exists()


def test_explicit_successful_billing_rpc_is_distinct_fresh_evidence():
    evidence = {**billing(), "source": "cursor_billing_rpc_observation",
                "service": "aiserver.v1.DashboardService/GetHardLimit", "http_status": 200,
                "noUsageBasedAllowed": True, "evidence_ref": "private/read-only-billing.json"}
    cr.validate_billing_evidence(evidence)
    for patch in ({"service": "aiserver.v1.DashboardService/GetUsage"}, {"http_status": 403},
                  {"http_status": "200"}, {"noUsageBasedAllowed": False}, {"noUsageBasedAllowed": "true"},
                  {"noUsageBasedAllowed": None}, {"evidence_ref": ""}):
        with pytest.raises(cr.ReviewBlocked, match="rpc_evidence"):
            cr.validate_billing_evidence({**evidence, **patch})
    with pytest.raises(cr.ReviewBlocked, match="expired"):
        cr.validate_billing_evidence({**evidence, "checked_at_utc": (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()})


def test_cleanup_failure_preserves_completed_model_result_and_is_bounded(tmp_path, monkeypatch):
    calls = []

    class LockedDirectory:
        name = str(tmp_path)

        def cleanup(self):
            calls.append("cleanup")
            raise PermissionError("locked")

    monkeypatch.setattr(cr, "_isolation_directory", lambda prefix: LockedDirectory())
    monkeypatch.setattr(cr.time, "sleep", lambda duration: None)
    result = review()
    result.status = "completed_unverified"
    result.scores = {"precision": 4}
    with cr._review_isolation(result):
        pass
    assert calls == ["cleanup"] * 3
    assert result.status == "completed_unverified" and result.scores == {"precision": 4}
    assert result.isolation["cleanup_completed"] is False
    assert result.diagnostics == ["cursor_isolation_cleanup_incomplete:PermissionError"]


@pytest.mark.parametrize("event", [
    {"type": "assistant", "message": {"content": [{"type": "tool_use", "name": "read"}]}},
    {"type": "system", "subtype": "init", "tools": ["Read"]},
    {"type": "assistant", "data": {"readToolCall": {"path": "secret"}}},
])
def test_nested_tool_or_nonempty_advertised_tools_fail_closed(event):
    result = cr.parse_cli_output(completion(extra_events=[event]), review())
    assert result.status == "isolation_violation" and not result.scores


def test_missing_fable_does_not_substitute_or_block_available_kimi(run_dir, monkeypatch):
    calls = []
    result = cr.review_run(run_dir, live=True, config=prepare_live(monkeypatch),
                           run_process=fake_preflight_and_reviews(calls, catalog="kimi-k3-high - Kimi K3 High\n"))
    assert result["status"] == "partial" and result["attempted"] == 1
    assert calls[-1][0][calls[-1][0].index("--model") + 1] == "kimi-k3-high"


def test_selected_label_and_model_generated_claim_never_verify_actual_identity():
    payload = output_payload()
    result = cr.parse_cli_output(completion(payload, extra_events=[{"type": "assistant", "actual_model": "claude-fable-5-1"}]), review())
    assert result.actual_model is None and result.identity_status == "unverified"
    assert result.cli_model_label == "Fable 5.1"


def test_fallback_label_is_suspected_not_false_actual_model_evidence():
    result = cr.parse_cli_output(completion(label="Claude Opus 5"), review())
    assert result.fallback == "suspected" and result.actual_model is None


@pytest.mark.parametrize("mutate", [lambda p: p["scores"].update(precision=True),
                                   lambda p: p["scores"].update(precision=6),
                                   lambda p: p.update(sample_id="other-sample"),
                                   lambda p: p.update(evidence=[]),
                                   lambda p: p["violations"].update(fact_action="false"),
                                   lambda p: p.update(actual_model="self-declared")])
def test_output_schema_is_strict(mutate):
    payload = output_payload()
    mutate(payload)
    result = cr.parse_cli_output(completion(payload), review())
    assert result.status == "invalid_output" and not result.scores


def test_tool_events_reject_review():
    result = cr.parse_cli_output(completion(extra_events=[{"type": "tool_call", "tool_call": {"readToolCall": {}}}]), review())
    assert result.status == "isolation_violation" and not result.scores


def test_timeout_stops_batch_without_retries(run_dir, monkeypatch):
    calls = []
    def timeout(command, cwd):
        raise TimeoutError("arbitrary private prompt must not be logged")
    result = cr.review_run(run_dir, live=True, config=prepare_live(monkeypatch),
                           run_process=fake_preflight_and_reviews(calls, fail=timeout))
    assert result["attempted"] == 1 and result["errors"] == ["cursor_review_timeout"]


def test_quota_error_stops_without_auto_or_other_model_retry(run_dir, monkeypatch):
    calls = []
    def quota(command, cwd):
        return subprocess.CompletedProcess(command, 1, "", "Error: usage limit reached; Bearer private-token-not-for-logs")
    result = cr.review_run(run_dir, live=True, config=prepare_live(monkeypatch),
                           run_process=fake_preflight_and_reviews(calls, fail=quota))
    assert result["attempted"] == 1 and result["errors"] == ["cursor_quota_exhausted"]
    assert "private-token" not in (run_dir / "aux-ratings.jsonl").read_text()


def test_workspace_writes_are_rejected(run_dir, monkeypatch):
    def writes(command, cwd):
        (cwd / "unexpected.txt").write_text("unexpected")
        return completion()
    result = cr.review_run(run_dir, live=True, config=prepare_live(monkeypatch),
                           run_process=fake_preflight_and_reviews([], fail=writes))
    assert result["errors"] == ["cursor_workspace_modified"]


def test_call_cap_prevents_dispatch(run_dir, monkeypatch):
    result = cr.review_run(run_dir, live=True, config=replace(prepare_live(monkeypatch), max_calls=1),
                           run_process=lambda *a, **kw: pytest.fail("dispatch over cap"))
    assert result["errors"] == ["cursor_review_call_cap_exceeded"]


def test_rerun_cannot_silently_spend_again_or_overwrite_ratings(run_dir, monkeypatch):
    (run_dir / "aux-ratings.jsonl").write_text("existing result")
    with pytest.raises(cr.ReviewBlocked, match="already_exists"):
        cr.review_run(run_dir, live=True, config=prepare_live(monkeypatch))
    assert (run_dir / "aux-ratings.jsonl").read_text() == "existing result"


def test_unrecognized_cli_build_is_blocked(tmp_path):
    (tmp_path / "index.js").write_text("unreviewed new cli")
    with pytest.raises(cr.ReviewBlocked, match="audit_mismatch"):
        cr.audited_cli_command(tmp_path)


def test_non_json_or_oversized_cli_output_rejected():
    invalid = subprocess.CompletedProcess([], 0, "not JSON", "")
    assert cr.parse_cli_output(invalid, review()).errors == ["cursor_invalid_event_json"]
    huge = subprocess.CompletedProcess([], 0, "x" * (cr.MAX_OUTPUT_CHARS + 1), "")
    assert cr.parse_cli_output(huge, review()).errors == ["cursor_output_limit_exceeded"]
