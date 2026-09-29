"""No network: current _ask, v2 commits, native wire and durable quota tests."""
import hashlib
import json
import sqlite3

import httpx
import pytest

from tools.production_acceptance import evidence_v2 as runner


KEY = "synthetic-evidence-private-key-123456789"


def arguments(tmp_path, *, name="run", mode="--offline-check"):
    values = ["--acceptance-root", str(tmp_path), "--run-name", name]
    if mode:
        values.append(mode)
    if mode == "--execute":
        config = tmp_path / "synthetic-config.json"
        if not config.exists():
            config.write_text(json.dumps({"model": "unchanged-main", "base_url": "https://api.apiyi.com/v1", "api_key": KEY}), encoding="utf-8")
        values += ["--gui-config", str(config)]
    return runner.parser().parse_args(values)


def factory(mutator=None):
    calls = []
    def make(name, original):
        def respond(request):
            calls.append((name, request))
            assert request.method == "POST" and str(request.url) == runner.ENDPOINT
            assert request.headers["anthropic-version"] == "2023-06-01"
            assert "authorization" not in request.headers
            body = json.loads(request.content)
            assert body["model"] == runner.MODEL and body["max_tokens"] == 4096
            assert body["tool_choice"] == {"type": "tool", "name": runner.TOOL}
            assert len(body["tools"]) == 1 and "response_format" not in body
            response = original(request)
            return mutator(name, request, response) if mutator else response
        return httpx.MockTransport(respond)
    return calls, make


def test_preview_never_reads_config_or_constructs_network(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "candidate_config", lambda *args: pytest.fail("config read"))
    monkeypatch.setattr(httpx, "HTTPTransport", lambda **kwargs: pytest.fail("network transport"))
    summary = runner.run(arguments(tmp_path, mode=None))
    assert summary["dispatch_attempts"] == summary["durable_usage_receipts"] == summary["quota_claims"] == 0
    assert summary["configuration_unchanged"] and summary["source_hashes_unchanged"]
    assert all(row["status"] == "preview" for row in summary["results"])
    with pytest.raises(FileExistsError):
        runner.run(arguments(tmp_path, mode=None))


def test_six_real_synthetic_commits_and_complete_capture(tmp_path):
    calls, wire = factory()
    summary = runner.run(arguments(tmp_path), transport_factory=wire)
    assert summary["accepted"] == summary["dispatch_attempts"] == summary["durable_usage_receipts"] == 6
    assert summary["real_network_attempts"] == 0 and len(calls) == 6
    assert all(row["original_sources_unchanged"] for row in summary["results"])
    for name, request in calls:
        saved = (tmp_path / "run" / name / "http-request-body.bin").read_bytes()
        metadata = json.loads((tmp_path / "run" / name / "http-request-metadata.json").read_text(encoding="utf-8"))
        assert saved == request.content and metadata["sha256"] == hashlib.sha256(saved).hexdigest()
    assert all(row["usage"]["raw_usage"] == {"input_tokens": 100, "output_tokens": 50} for row in summary["usage"])
    for name in runner.NAMES:
        directory = tmp_path / "run" / name
        raw = json.loads((directory / "private-http-response/response-body.bin").read_bytes())
        tool_input = json.loads((directory / "native-tool-input.json").read_text(encoding="utf-8"))
        assert raw["content"][0]["input"] == tool_input
        with sqlite3.connect(directory / "memory.sqlite3") as db:
            assert db.execute("SELECT count(*) FROM pm_evidence_manifests").fetchone()[0] >= 1
        assert json.loads((directory / "result.json").read_text(encoding="utf-8"))["http_capture"]["capture_saved"]
    before = json.loads((tmp_path / "run/future/post-recall.json").read_text(encoding="utf-8"))
    after = json.loads((tmp_path / "run/future/after-effective-recall.json").read_text(encoding="utf-8"))
    assert "seeded-preference" in [entry["record_id"] for entry in before["entries"]]
    assert "seeded-preference" not in [entry["record_id"] for entry in after["entries"]]
    assert all(summary["results"][-1]["checks"].values())


def test_quota_cannot_reset_by_run_name_and_config_is_read_only(tmp_path):
    args = arguments(tmp_path, mode="--execute")
    before = args.gui_config.read_bytes()
    calls, wire = factory()
    summary = runner.run(args, transport_factory=wire)
    assert summary["quota_claims"] == 6 and len(calls) == 6
    assert args.gui_config.read_bytes() == before and summary["configuration_unchanged"]
    with pytest.raises(ValueError, match="experiment_already_started"):
        runner.run(arguments(tmp_path, name="different-name", mode="--execute"), transport_factory=wire)
    assert len(calls) == 6


@pytest.mark.parametrize("failure", ["wrong_tool", "two_tools", "truncated", "http400", "timeout"])
def test_first_response_gate_stops_without_retry_and_keeps_usage(tmp_path, failure):
    def alter(name, request, response):
        if failure == "timeout":
            raise httpx.ReadTimeout("synthetic timeout", request=request)
        body = response.json()
        if failure == "http400":
            return httpx.Response(400, json={"error": {"code": "unknown_vendor_error"}, "usage": {"output_tokens": 11}})
        if failure == "wrong_tool":
            body["content"][0]["name"] = "execute_real_action"
        elif failure == "two_tools":
            body["content"].append(body["content"][0])
        else:
            body["stop_reason"] = "max_tokens"
        return httpx.Response(200, json=body)
    calls, wire = factory(alter)
    summary = runner.run(arguments(tmp_path), transport_factory=wire)
    assert len(calls) == summary["dispatch_attempts"] == summary["quota_claims"] == summary["durable_usage_receipts"] == 1
    assert summary["results"][0]["status"] == "rejected"
    assert all(row["status"] == "skipped" for row in summary["results"][1:])
    if failure == "timeout":
        assert summary["usage"][0]["usage"]["source"] == "unavailable"
    else:
        assert summary["usage"][0]["usage"]["source"] == "api"


def test_rejected_memory_group_does_not_erase_received_usage(tmp_path):
    def alter(name, request, response):
        body = response.json()
        value = body["content"][0]["input"]
        value["memory_updates"][0]["claims"][0]["anchors"][0]["quote"] = "fabricated quote"
        return httpx.Response(200, json=body)
    _, wire = factory(alter)
    summary = runner.run(arguments(tmp_path), transport_factory=wire)
    assert summary["results"][0]["reason"] == "production_memory_proposals_rejected"
    assert summary["durable_usage_receipts"] == 1
    with sqlite3.connect(tmp_path / "run/correction/memory.sqlite3") as db:
        assert db.execute("SELECT count(*) FROM pm_v2_tasks WHERE status='pending'").fetchone()[0] == 1
        assert db.execute("SELECT count(*) FROM pm_v2_versions").fetchone()[0] == 1  # Seed only.


def test_capture_write_failure_preserves_provider_counts(tmp_path, monkeypatch):
    monkeypatch.setattr(runner, "capture_http_response", lambda *args: (_ for _ in ()).throw(OSError("synthetic full disk")))
    _, wire = factory()
    summary = runner.run(arguments(tmp_path), transport_factory=wire)
    assert summary["results"][0]["reason"] == "native_response_capture_failed"
    assert summary["usage"][0]["usage"]["raw_usage"]["output_tokens"] == 50
    assert summary["durable_usage_receipts"] == summary["dispatch_attempts"] == 1


@pytest.mark.parametrize("http_status", [200, 500])
def test_second_capture_failure_stops_remaining_paid_attempts(tmp_path, monkeypatch, http_status):
    original = runner.capture_http_response
    captured = []
    def capture(response, directory, secrets):
        captured.append(directory.name)
        if len(captured) == 2:
            raise OSError("synthetic storage failure")
        return original(response, directory, secrets)
    monkeypatch.setattr(runner, "capture_http_response", capture)
    def alter(name, request, response):
        return httpx.Response(http_status if name == "quoted" else 200, json=response.json())
    calls, wire = factory(alter)
    summary = runner.run(arguments(tmp_path), transport_factory=wire)
    assert len(calls) == summary["dispatch_attempts"] == summary["durable_usage_receipts"] == 2
    assert summary["results"][1]["fatal"]["reason"] == "native_response_capture_failed"
    assert all(row["status"] == "skipped" for row in summary["results"][2:])


def test_complete_private_capture_scrubs_secret_keys_headers_and_does_not_promote_thinking(tmp_path):
    def alter(name, request, response):
        body = response.json()
        body["authorization"] = KEY
        body["content"].insert(0, {"type": "thinking", "thinking": "non-final diagnostic reasoning"})
        return httpx.Response(200, json=body, headers={"set-cookie": KEY, "x-request-id": "safe-id", "server": KEY})
    _, wire = factory(alter)
    summary = runner.run(arguments(tmp_path, mode="--execute"), transport_factory=wire)
    assert summary["accepted"] == 6
    for name in runner.NAMES:
        directory = tmp_path / "run" / name
        response = json.loads((directory / "private-http-response/response.json").read_text(encoding="utf-8"))
        assert response["content"][0]["thinking"] == "non-final diagnostic reasoning"
        assert "non-final diagnostic reasoning" not in (directory / "diagnostic-chat-result.json").read_text(encoding="utf-8")
        headers = json.loads((directory / "private-http-response/response-headers.json").read_text(encoding="utf-8"))
        assert "set-cookie" not in headers and headers["x-request-id"] == "safe-id"
        for path in directory.rglob("*"):
            if path.is_file():
                assert KEY.encode() not in path.read_bytes(), path


def test_secret_echo_in_proposal_is_not_persisted_in_memory(tmp_path):
    def alter(name, request, response):
        body = response.json()
        body["content"][0]["input"]["memory_updates"][0]["claims"][0]["statement"] = KEY
        return httpx.Response(200, json=body)
    _, wire = factory(alter)
    summary = runner.run(arguments(tmp_path, mode="--execute"), transport_factory=wire)
    assert summary["results"][0]["reason"] == "native_secret_echo_rejected"
    assert summary["durable_usage_receipts"] == 1
    for path in (tmp_path / "run").rglob("*"):
        if path.is_file():
            assert KEY.encode() not in path.read_bytes(), path
