"""Candidate tool coverage with portable synthetic inputs and no real transport."""
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sqlite3

import httpx
import pytest

from harness.memory.production import ProductionMemoryStore
from harness.production.metering import ProductionUsageSpool
from harness.production.store import ProductionStore
from tools.production_acceptance import model_candidates as runner


SOURCE = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-4-6"
KEY = "SYNTHETIC_MAIN_KEY_ONLY"
CONTENT = '{"kind":"reply","reason":"Synthetic test","text":"test","used_memory_ids":[]}'
USAGE = {"prompt_tokens": 7, "completion_tokens": 9, "total_tokens": 16}


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", lambda *a, **kw: pytest.fail("Real HTTP forbidden"))


@pytest.fixture
def inputs(tmp_path, monkeypatch):
    """Test-only miniature input set; never pretends to be original semantic fixtures."""
    # This transport/fault-injection unit suite uses synthetic inputs with the
    # current implementation. Real v1 comparisons retain their frozen hashes
    # and correctly reject this newer core instead of relabeling old evidence.
    monkeypatch.setattr(runner, "CORE", {name: runner.filehash(SOURCE / name) for name in runner.CORE})
    prior = tmp_path / "prior"
    prior.mkdir()
    specs, rows = [], []
    for name in runner.NAMES:
        directory = prior / name
        directory.mkdir()
        ProductionMemoryStore(directory / "memory.sqlite3")
        payload = {"persona": {"basis": "synthetic acceptance persona"}, "authorized_actions": [],
            "current_event": {"event_id": "current:" + name, "timestamp": datetime.now(timezone.utc).isoformat()},
            "production": {"schema_version": "production-context-v1", "scope": "local:main",
                "body": {"available": True, "snapshot": {"body_instance_id": "synthetic-acceptance-body",
                    "episode_id": "synthetic-episode:" + name}}},
            "memories": [], "source_refs": ["current:" + name]}
        (directory / "prompt-input.json").write_text(runner.canonical(payload), encoding="utf-8")
        digest = runner.digest(payload)
        if name == "long_term_pin":
            monkeypatch.setattr(runner, "ORIGINAL_PIN", digest)
        specs.append({"scenario": name, "payload_sha256": digest})
        rows.append({"scenario": name, "prompt_sha256": digest, "status": "synthetic_not_model_evaluation"})
    (prior / "fixtures.json").write_text(json.dumps({"scenario_version": "synthetic-production-six-v1",
        "source_hashes": runner.ORIGINAL_CORE, "fixtures": specs}))
    (prior / "results.jsonl").write_text("\n".join(json.dumps(row) for row in rows))
    (prior / "summary.json").write_text(json.dumps({"mode": "real_model", "physical_requests": 6,
        "requested_model": "synthetic-prior"}))
    catalog = tmp_path / "catalog.json"
    catalog.write_text(json.dumps({"schema_version": "model-access-readonly-v1", "script_sha256": runner.ACCESS_SCRIPT_HASH,
        "mode": "execute", "physical_requests": 2, "generation_requests": 0, "post_requests": 0,
        "configuration_unchanged": True, "requests": {"primary_model_catalog": {"method": "GET", "http_status": 200,
            "ok": True, "endpoint": runner.ENDPOINT + "/models", "configured_base": runner.ENDPOINT,
            "ids_truncated": False, "model_ids": list(runner.ALLOWED_MODELS), "unique_valid_id_count": 3}}}))
    monkeypatch.setattr(runner, "CATALOG_HASH", runner.filehash(catalog))
    gui = tmp_path / "gui.json"
    gui.write_text(json.dumps({"model": "unchanged-main", "base_url": runner.ENDPOINT, "api_key": KEY,
                               "qq_memory_api_key": "unused-memory", "aux_api_key": "unused-aux"}))
    return prior, catalog, gui


def args(tmp_path, inputs, *, model=MODEL, name="result", mode="--execute"):
    prior, catalog, gui = inputs
    return ["--nc-source", str(SOURCE), "--acceptance-root", str(tmp_path), "--prior-run", str(prior),
        "--catalog-report", str(catalog), "--catalog-sha256", runner.CATALOG_HASH,
        "--gui-config", str(gui), "--model", model, "--run-name", name] + ([mode] if mode else [])


def wire(monkeypatch, *, content=CONTENT, status=200, error_code=None, raw_body=None,
         extras=None, message_extras=None, finish_reason="stop", fail_at=None):
    calls = []

    def respond(request):
        request_body = json.loads(request.content)
        assert request.method == "POST" and str(request.url) == runner.ENDPOINT + "/chat/completions"
        assert request.headers["Authorization"] == "Bearer " + KEY
        assert request_body["model"] in runner.ALLOWED_MODELS
        assert request_body["response_format"] == {"type": "json_object"} and "tools" not in request_body
        index = len(calls)
        calls.append(request)
        effective_status = status if fail_at is None or fail_at == index else 200
        headers = {"Content-Type": "application/json", "X-Request-ID": "safe-request-id",
                   "Authorization": "Bearer " + KEY, "X-API-Key": "SHOULD_NEVER_EXPORT", "Set-Cookie": "PRIVATE_COOKIE"}
        if raw_body is not None:
            return httpx.Response(effective_status, content=raw_body, headers=headers)
        response_body = {"model": request_body["model"], "choices": [{"finish_reason": finish_reason,
            "message": {"content": content, **(message_extras or {})}}], "usage": USAGE, **(extras or {})}
        if effective_status != 200:
            response_body = {"error": {"code": error_code, "message": "Synthetic failure"}, "usage": USAGE}
        return httpx.Response(effective_status, json=response_body, headers=headers)

    def transport(*unused, **kwargs):
        assert kwargs.get("retries", 0) == 0
        return httpx.MockTransport(respond)

    monkeypatch.setattr(httpx, "HTTPTransport", transport)
    return calls


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_dry_run_and_offline_never_read_gui_or_dispatch_real_http(tmp_path, inputs, monkeypatch):
    inputs[2].unlink()
    assert runner.main(args(tmp_path, inputs, name="dry", mode="")) == 0
    assert read(tmp_path / "dry/summary.json")["physical_requests"] == 0
    assert runner.main(args(tmp_path, inputs, name="offline", mode="--offline-check")) == 0
    result = read(tmp_path / "offline/summary.json")
    assert result["physical_requests"] == 0 and result["http_response_captures"] == 6
    assert not list(tmp_path.glob(".model-candidates-v1-quota*"))


def test_six_success_capture_every_http_field_and_private_permissions(tmp_path, inputs, monkeypatch):
    calls = wire(monkeypatch, extras={"provider_field": {"nested": [1, "preserved"]}},
                 message_extras={"reasoning_content": "PRIVATE_REASONING", "unusual": {"content": "alternative"}})
    before = inputs[2].read_bytes()
    assert runner.main(args(tmp_path, inputs)) == 0
    result = read(tmp_path / "result/summary.json")
    assert len(calls) == result["attempts"] == result["usage_receipts"] == result["http_response_captures"] == 6
    assert result["all_structural_checks_passed"] and result["first_response_gate_passed"]
    assert inputs[2].read_bytes() == before and result["configuration_unchanged"]
    private = tmp_path / "result/stable_identity/private-http-response"
    body = (private / "response-body.bin").read_bytes()
    document = read(private / "response.json")
    assert json.loads(body) == document and document["provider_field"] == {"nested": [1, "preserved"]}
    assert document["choices"][0]["message"]["reasoning_content"] == "PRIVATE_REASONING"
    assert document["choices"][0]["message"]["unusual"]["content"] == "alternative"
    assert "PRIVATE_REASONING" not in (tmp_path / "result/summary.json").read_text()
    headers = read(private / "response-headers.json")
    assert headers["x-request-id"] == "safe-request-id"
    assert not {"authorization", "x-api-key", "set-cookie"} & set(headers)
    meta = read(private / "capture-metadata.json")
    assert meta["complete_unmodified_body_saved"] and meta["original_body_sha256"] == hashlib.sha256(body).hexdigest()
    assert meta["alternative_fields_promoted_to_decision"] is False
    if os.name != "nt":
        assert private.stat().st_mode & 0o777 == 0o700
        assert all(path.stat().st_mode & 0o777 == 0o600 for path in private.iterdir())


@pytest.mark.parametrize("status,code,reason", [(401, None, "candidate_configuration_http_401"),
    (402, None, "candidate_configuration_http_402"), (403, None, "candidate_configuration_http_403"),
    (404, None, "candidate_configuration_http_404"), (400, "unsupported_parameter", "candidate_configuration_http_400"),
    (400, "unrecognized", "candidate_unclassified_http_400")])
def test_http_fatal_one_post_one_usage_five_skipped(tmp_path, inputs, monkeypatch, status, code, reason):
    calls = wire(monkeypatch, status=status, error_code=code)
    assert runner.main(args(tmp_path, inputs)) == 1
    result = read(tmp_path / "result/summary.json")
    assert len(calls) == result["attempts"] == result["usage_receipts"] == 1
    assert result["skipped_scenarios"] == 5 and result["fatal_candidate_error"]["reason"] == reason
    assert result["http_response_captures"] == 1 and not result["all_structural_checks_passed"]
    rows = read(tmp_path / "result/results.json")
    assert rows[0]["usage"][0]["usage"]["raw_usage"] == USAGE
    for row in rows[1:]:
        assert row["status"] == "skipped" and row["attempt_count"] == row["usage_receipts"] == 0
        assert row["capture"] is None and row["http_capture"] is None and row["usage"] == []
    assert read(tmp_path / "result/stable_identity/private-http-response/response.json")["error"]["code"] == code


@pytest.mark.parametrize("content,message_extras,expected", [
    ("", {"reasoning_content": CONTENT, "analysis": "Alternative text"}, "ValueError"),
    ('{"kind":"invented"}', {}, "ValidationError"),
    ('{"kind":"reply", "text":"bad "quotes""}', {}, "JSONDecodeError"),
    (None, {"reasoning": CONTENT}, "ValueError"),
])
def test_other_fields_never_repair_empty_or_invalid_decision(tmp_path, inputs, monkeypatch, content, message_extras, expected):
    calls = wire(monkeypatch, content=content, message_extras=message_extras)
    assert runner.main(args(tmp_path, inputs)) == 1
    output = tmp_path / "result"
    result = read(output / "summary.json")
    assert len(calls) == result["attempts"] == result["usage_receipts"] == 1
    assert result["typed_choices"] == 0 and result["skipped_scenarios"] == 5
    first = read(output / "stable_identity/result.json")
    assert first["error_type"] == expected and first["usage"][0]["usage"]["raw_usage"] == USAGE
    response = read(output / "stable_identity/private-http-response/response.json")
    assert response["choices"][0]["message"]["content"] == content
    assert all(response["choices"][0]["message"][key] == value for key, value in message_extras.items())


def test_invalid_outer_http_json_is_saved_without_synthetic_usage(tmp_path, inputs, monkeypatch):
    body = b'{"choices": invalid-json, "reasoning_content": "retained raw"}\xff'
    calls = wire(monkeypatch, raw_body=body)
    assert runner.main(args(tmp_path, inputs)) == 1
    private = tmp_path / "result/stable_identity/private-http-response"
    assert (private / "response-body.bin").read_bytes() == body
    assert not (private / "response.json").exists()
    assert not read(private / "capture-metadata.json")["http_json_valid"]
    result = read(tmp_path / "result/summary.json")
    assert len(calls) == result["attempts"] == result["usage_receipts"] == 1
    usage = read(tmp_path / "result/stable_identity/result.json")["usage"][0]["usage"]
    assert usage["source"] == "unavailable" and not usage.get("raw_usage")


def test_capture_storage_failure_stops_but_retains_actual_usage(tmp_path, inputs, monkeypatch):
    calls = wire(monkeypatch)

    def fail_capture(*unused):
        raise OSError("synthetic diagnostic storage failure")

    monkeypatch.setattr(runner, "capture_http_response", fail_capture)
    assert runner.main(args(tmp_path, inputs)) == 1
    report = read(tmp_path / "result/summary.json")
    assert len(calls) == report["attempts"] == report["usage_receipts"] == 1
    assert report["fatal_candidate_error"]["reason"] == "candidate_http_capture_failed"
    assert report["skipped_scenarios"] == 5
    assert report["http_response_capture_attempts"] == 1 and report["http_response_captures"] == 0
    first = read(tmp_path / "result/stable_identity/result.json")
    assert first["usage"][0]["usage"]["raw_usage"] == USAGE
    assert not first["http_capture"]["capture_saved"]


def test_post_response_metadata_write_failure_preserves_usage_and_stops(tmp_path, inputs, monkeypatch):
    calls = wire(monkeypatch)
    original_write = runner.private_bytes
    writes = []

    def fail_second_metadata_write(path, data):
        if path.name == "request-options.json":
            writes.append(path)
            if len(writes) == 2:
                raise OSError("synthetic response metadata write failure")
        return original_write(path, data)

    monkeypatch.setattr(runner, "private_bytes", fail_second_metadata_write)
    assert runner.main(args(tmp_path, inputs)) == 1
    report = read(tmp_path / "result/summary.json")
    assert len(calls) == report["attempts"] == report["usage_receipts"] == 1
    assert report["skipped_scenarios"] == 5 and report["fatal_candidate_error"]["reason"] == "candidate_http_capture_failed"
    first = read(tmp_path / "result/stable_identity/result.json")
    assert first["usage"][0]["usage"]["raw_usage"] == USAGE
    assert first["http_capture"]["failure_stage"] == "response_metadata_write"


def test_response_credentials_redacted_but_response_consumed_unchanged(tmp_path, inputs, monkeypatch, capsys):
    wire(monkeypatch, extras={"api_key": "OTHER_SENSITIVE_VALUE", "authorization": "Bearer " + KEY,
                             "trace": "upstream echoed " + KEY, KEY: "preserved-first", runner.REDACTED: "preserved-second",
                             "model": KEY})
    assert runner.main(args(tmp_path, inputs)) == 0
    output = tmp_path / "result"
    private = output / "stable_identity/private-http-response"
    assert read(private / "capture-metadata.json")["credential_redaction_applied"]
    assert not read(private / "capture-metadata.json")["complete_unmodified_body_saved"]
    cleaned = read(private / "response.json")
    assert "preserved-first" in cleaned.values() and "preserved-second" in cleaned.values()
    assert KEY not in capsys.readouterr().out
    for path in output.rglob("*"):
        if path.is_file():
            assert all(secret.encode() not in path.read_bytes() for secret in (KEY, "OTHER_SENSITIVE_VALUE", "SHOULD_NEVER_EXPORT", "PRIVATE_COOKIE"))


def test_model_specific_quota_and_usage_replay_are_independent(tmp_path, inputs, monkeypatch):
    calls = wire(monkeypatch)
    assert runner.main(args(tmp_path, inputs)) == 0
    first_usage = read(tmp_path / "result/stable_identity/result.json")["usage"][0]
    store = ProductionStore(tmp_path / "result/production.sqlite3")
    spool = ProductionUsageSpool(tmp_path / "result/cognitive-usage.sqlite3")
    for _ in range(2):
        store.record_usage(first_usage["attempt_id"], first_usage["usage"])
        spool.finish(first_usage["attempt_id"], first_usage["usage"])
    assert len(store.pending_usage()) == spool.status()["pending"] == 6
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM attempts").fetchone()[0] == 6
    with pytest.raises(FileExistsError):
        runner.main(args(tmp_path, inputs, name="cannot-reset-same-model"))
    assert len(calls) == 6
    assert runner.main(args(tmp_path, inputs, model="claude-haiku-4-5-20251001", name="different-model")) == 0
    assert len(calls) == 12 and len(list(tmp_path.glob(".model-candidates-v1-quota*"))) == 2


def test_reject_unlisted_model_before_any_network(tmp_path, inputs):
    with pytest.raises(SystemExit):
        runner.main(args(tmp_path, inputs, model="claude-sonnet-5"))
    assert not list(tmp_path.glob(".model-candidates-v1-quota*"))
