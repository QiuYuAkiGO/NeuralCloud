"""Live admission and HTTP evidence contracts, exclusively using MockTransport."""

from __future__ import annotations

import json
from threading import Barrier, Event, Lock

import httpx
import pytest

from eval.emotional_expression_diagnostics.budget import BudgetLedger, BudgetStopped
from eval.emotional_expression_diagnostics.fixtures import default_cases
from eval.emotional_expression_diagnostics.models import (
    BudgetSpec, DiagnosticRunSpec, GenerationSpec, PriceSpec, read_jsonl,
)
from eval.emotional_expression_diagnostics.runner import (
    DirectHTTPReplayClient, HTTPReplayFailure, run_experiment,
)


ENDPOINT = "https://provider.example.test/v1/chat/completions"


def live_contract(*, parallel=1):
    return DiagnosticRunSpec(
        source_commit="synthetic-live-safety-test", cases=default_cases()[:2],
        generation=GenerationSpec(model="fixture-provider", endpoint=ENDPOINT, max_tokens=128),
        price=PriceSpec(input_per_million=1, output_per_million=2,
                        source="https://provider.example.test/synthetic-price", verified_at="synthetic-test"),
        budget=BudgetSpec(max_parallel_requests=parallel), live_authorized=True,
    )


def success(request, *, model="fixture-response-v1", finish="stop", content=None):
    payload = json.loads(request.content)
    if content is None:
        content = json.dumps({"owner": "角色", "directions": {}, "trends": {},
                              "unknown_fields": [], "confirmed_causes": []}) if "response_format" in payload else "合成正文"
    return httpx.Response(200, json={
        "model": model, "id": "synthetic-response", "choices": [{"finish_reason": finish, "message": {"content": content}}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 3},
    })


def run_mock(tmp_path, handler, *, parallel=1, observer=None):
    events = []
    spec = live_contract(parallel=parallel)
    client = DirectHTTPReplayClient(endpoint=ENDPOINT, api_key="KEY_NEVER_LOG",
                                    observer=observer or events.append, transport=httpx.MockTransport(handler))
    try:
        result = run_experiment(spec, tmp_path / "run", client=client, live=True, budget_path=tmp_path / "ledger.json")
    finally:
        client.close()
    return result, read_jsonl(tmp_path / "run" / "requests.jsonl"), events


@pytest.mark.parametrize("status", [400, 401, 402, 403, 404, 422, 429, 500, 503])
def test_http_error_metadata_is_saved_before_failure_and_response_is_closed(status):
    events, responses = [], []

    def handler(request):
        response = httpx.Response(status, json={
            "error": {"message": "PRIVATE_PROVIDER_ERROR", "code": "SECRET_CODE"},
            "model": "fixture-response-v1", "id": "synthetic-error",
            "choices": [{"message": {"content": "PRIVATE_ECHOED_INPUT", "reasoning_content": "PRIVATE_REASONING"}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 0},
        })
        responses.append(response)
        return response

    client = DirectHTTPReplayClient(endpoint=ENDPOINT, api_key="KEY_NEVER_LOG",
                                    observer=events.append, transport=httpx.MockTransport(handler))
    try:
        with pytest.raises(HTTPReplayFailure, match=f"provider_http_{status}") as caught:
            client.complete_payload({"model": "fixture", "messages": [], "max_tokens": 1}, request_id="synthetic", metadata={})
    finally:
        client.close()
    assert [row["record_type"] for row in events] == ["http_request_prepared", "http_response_received", "http_request_error"]
    evidence = events[1]
    assert evidence["http_status"] == status and evidence["payload_verified"]
    assert evidence["response_model"] == "fixture-response-v1"
    assert evidence["usage"] == {"prompt_tokens": 10, "completion_tokens": 0}
    assert evidence["response_content"] is None
    assert caught.value.evidence["http_status"] == status
    assert responses[0].is_closed
    assert all(secret not in json.dumps(events) for secret in ("PRIVATE_", "SECRET_CODE", "KEY_NEVER_LOG"))


def test_non_json_response_keeps_status_and_stops_without_logging_body(tmp_path):
    received = []

    def handler(request):
        received.append(request)
        return httpx.Response(200, content=b"PRIVATE_INVALID_JSON")

    result, rows, events = run_mock(tmp_path, handler)
    assert len(received) == 1
    assert result["stop_reason"] == "provider_response_invalid_json"
    assert rows[0]["result"]["http_status"] == 200
    assert rows[0]["result"]["response_json_valid"] is False
    assert result["budget"]["reserved_cny"] != "0"
    assert "PRIVATE_INVALID_JSON" not in json.dumps(events + rows)


def test_first_auth_failure_stops_admission_and_drains_all_in_flight(tmp_path):
    barrier, error_observed, lock = Barrier(4), Event(), Lock()
    received, events = [], []

    def observer(row):
        events.append(row)
        if row.get("http_status") == 401:
            error_observed.set()

    def handler(request):
        with lock:
            index = len(received)
            received.append(request)
        barrier.wait(timeout=10)
        if index == 0:
            return httpx.Response(401, json={"error": {"message": "PRIVATE_CREDENTIAL_ERROR"}})
        assert error_observed.wait(timeout=10)
        return success(request)

    result, rows, _ = run_mock(tmp_path, handler, parallel=4, observer=observer)
    assert len(received) == result["counts"]["attempted"] == 4
    assert len(rows) == 4
    assert result["counts"]["completed"] == 3 and result["counts"]["failed"] == 1
    assert result["stop_reason"] == "provider_http_401"
    assert result["budget"]["stopped"]
    assert len(result["budget"]["reservations"]) == 1
    assert float(result["budget"]["spent_cny"]) > 0
    assert "PRIVATE_CREDENTIAL_ERROR" not in json.dumps(events + rows)
    with pytest.raises(BudgetStopped, match="prior_stop"):
        BudgetLedger(tmp_path / "ledger.json", live_contract().budget, live_contract().price).reserve("later", max_output_tokens=1)


def test_failed_http_with_usage_is_charged_instead_of_assumed_free(tmp_path):
    result, rows, _ = run_mock(tmp_path, lambda request: httpx.Response(429, json={
        "usage": {"prompt_tokens": 100, "completion_tokens": 2}, "error": {"message": "PRIVATE"},
    }))
    assert result["counts"]["attempted"] == 1
    assert result["stop_reason"] == "provider_http_429"
    assert rows[0]["settlement"]["status"] == "settled"
    assert float(result["budget"]["spent_cny"]) == pytest.approx(0.000104)
    assert result["budget"]["reservations"] == {}


def test_transport_failure_does_not_retry_or_discard_reservation(tmp_path):
    received = []

    def handler(request):
        received.append(request)
        raise httpx.ReadTimeout("PRIVATE_EXCEPTION", request=request)

    result, rows, events = run_mock(tmp_path, handler)
    assert len(received) == 1 and result["stop_reason"] == "http_transport_error"
    assert len(result["budget"]["reservations"]) == 1
    assert rows[0]["settlement"]["status"] == "unsettled"
    assert "PRIVATE_EXCEPTION" not in json.dumps(events + rows)


def test_missing_identity_and_identity_drift_stop_without_alias_assumptions(tmp_path):
    received = []

    def handler(request):
        received.append(request)
        return success(request, model=f"fixture-resolved-v{len(received)}")

    result, rows, _ = run_mock(tmp_path, handler)
    assert len(received) == 2
    assert rows[0]["status"] == "completed"  # Resolved model may differ from the request alias.
    assert result["stop_reason"] == "provider_response_identity_changed"
    assert result["backend_identity_drift"]
    missing, missing_rows, _ = run_mock(tmp_path / "missing", lambda request: success(request, model=None))
    assert missing["counts"]["attempted"] == 1
    assert missing_rows[0]["status"] == "invalid_response"
    assert missing["stop_reason"] == "provider_response_identity_missing"


def test_three_invalid_responses_stop_without_adjusting_generation_spec(tmp_path):
    received = []

    def handler(request):
        received.append(json.loads(request.content))
        return success(request, finish="length")

    result, _, _ = run_mock(tmp_path, handler)
    assert result["counts"]["attempted"] == 3
    assert result["stop_reason"] == "consecutive_invalid_responses"
    assert all(payload["max_tokens"] == 128 for payload in received)
    manifest = json.loads((tmp_path / "run" / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["failure_admission_policy"]["automatic_retries"] == 0
    assert manifest["failure_admission_policy"]["consecutive_invalid_responses"] == 3
    assert result["failure_admission_policy"] == manifest["failure_admission_policy"]


def test_schema_valid_incorrect_probe_answers_remain_scored_observations(tmp_path):
    result, rows, _ = run_mock(tmp_path, success)
    probes = [row for row in rows if row["kind"] == "probe"]
    assert result["stop_reason"] is None and result["counts"]["failed"] == 0
    assert probes and all(row["status"] == "completed" for row in probes)
    assert all(row["probe_evaluation"]["valid"] for row in probes)
    assert any(row["probe_evaluation"]["correct_fields"] < 5 for row in probes)


def test_same_model_with_changed_backend_fingerprint_stops(tmp_path):
    received = []

    def handler(request):
        received.append(request)
        response = success(request)
        payload = response.json()
        payload["system_fingerprint"] = f"synthetic-backend-{len(received)}"
        return httpx.Response(200, json=payload)

    result, _, _ = run_mock(tmp_path, handler)
    assert len(received) == 2 and result["backend_identity_drift"]
    assert result["response_models"] == ["fixture-response-v1"]
    assert len(result["response_system_fingerprints"]) == 2
    assert result["stop_reason"] == "provider_response_identity_changed"


def test_mock_transport_is_never_claimed_as_real_network_evidence(tmp_path):
    result, rows, _ = run_mock(tmp_path, lambda request: httpx.Response(403))
    manifest = json.loads((tmp_path / "run" / "run-manifest.json").read_text(encoding="utf-8"))
    assert manifest["live"] is result["live"] is False
    assert manifest["network_opt_in"] is result["network_opt_in"] is True
    assert all(row["live"] is False and row["network_opt_in"] is True for row in rows)


def test_input_cap_is_frozen_per_reservation_even_when_later_spec_differs(tmp_path):
    price = PriceSpec(input_per_million=1, output_per_million=2)
    ledger = BudgetLedger(tmp_path / "ledger.json", BudgetSpec(max_input_tokens=1000), price)
    ledger.reserve("original", max_output_tokens=100)
    later = BudgetLedger(tmp_path / "ledger.json", BudgetSpec(max_input_tokens=256), price)
    result = later.settle("original", {"prompt_tokens": 500, "completion_tokens": 10})
    assert not result["provider_cap_violation"]
    assert later.snapshot()["reservations"] == {}
