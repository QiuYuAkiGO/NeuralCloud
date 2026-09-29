"""Accounting evidence remains exact across failures and process restarts."""

import json
import threading
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.config import Settings
from harness.llm.client import OpenAICompatClient
from harness.production.metering import (
    ProductionMeteringClient,
    ProductionUsageSpool,
    _connection_settings,
    install_metering_routes,
    production_call_scope,
)
from harness.production.store import ProductionStore


def context():
    return {
        "schema_version": "production-context-v1",
        "configuration": {},
        "body": {"available": False, "snapshot": {}},
        "last_known_body_identity": {"body_instance_id": "original-body", "episode_id": "original-episode"},
    }


def model(handler, *, observed=False):
    client = OpenAICompatClient(
        Settings(llm_model="review-model", llm_base_url="https://model.invalid/v1"),
        request_observer=(lambda event: None) if observed else None,
    )
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(handler))
    return client


def response(usage=None):
    result = {"choices": [{"message": {"content": "completed restricted reply"}}]}
    if usage is not None:
        result["usage"] = usage
    return result


@pytest.mark.parametrize("observed", [False, True])
@pytest.mark.parametrize("failure", ["shape", "late", "status"])
def test_provider_usage_survives_rejected_response(tmp_path, monkeypatch, observed, failure):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    body = response({"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10})
    if failure == "shape":
        body.pop("choices")
    client = model(lambda request: httpx.Response(500 if failure == "status" else 200, json=body), observed=observed)
    if failure == "late":
        calls = 0

        def remaining():
            nonlocal calls
            calls += 1
            if calls > 1:
                raise TimeoutError("late response")
            return 30

        monkeypatch.setattr("harness.llm.deadline.remaining_model_seconds", remaining)
    try:
        with production_call_scope(context(), command_id="c", meter=store):
            with pytest.raises((KeyError, TimeoutError, httpx.HTTPStatusError)):
                client.complete_chat_result([{"role": "user", "content": "private fixture"}])
        usage = store.pending_usage()[0]["usage"]
        assert usage["source"] == "api" and usage["outcome"] == "failed"
        assert usage["raw_usage"] == body["usage"]
        assert usage["episode_id"] == "original-episode"
        assert "private fixture" not in json.dumps(usage)
    finally:
        client.close()


def test_partial_and_missing_counts_never_become_synthetic_raw(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    bodies = iter(
        [
            response(
                {
                    "prompt_tokens": 7,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                    "invalid_tokens": True,
                    "negative_tokens": -1,
                    "text": "not usage",
                }
            ),
            response(),
        ]
    )
    client = model(lambda request: httpx.Response(200, json=next(bodies)))
    try:
        with production_call_scope(context(), command_id="c", meter=store):
            for _ in range(2):
                client.complete_chat_result([{"role": "user", "content": "fixture"}])
        first, second = [row["usage"] for row in store.pending_usage()]
        assert first["raw_usage"] == {"prompt_tokens": 7, "completion_tokens_details.reasoning_tokens": 2}
        assert "total_tokens" not in first and "completion_tokens" not in first
        assert second["source"] == "unavailable" and "raw_usage" not in second
    finally:
        client.close()


def test_usage_spool_replays_after_disconnect_and_reopen(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    wire = []
    available = False
    lost_receipt = True

    def runtime(request):
        nonlocal lost_receipt
        payload = json.loads(request.content)
        wire.append((request.url.path, payload))
        if request.url.path.endswith("model-attempts"):
            receipt = store.reserve_attempt(**payload)
        else:
            if not available:
                raise httpx.ConnectError("runtime offline", request=request)
            receipt = store.record_usage(payload["attempt_id"], payload["usage"])
            if lost_receipt:
                lost_receipt = False
                raise httpx.ReadTimeout("receipt lost after commit", request=request)
        return httpx.Response(200, json=receipt)

    path = tmp_path / "spool.sqlite3"
    meter = ProductionMeteringClient(
        token="fixture", transport=httpx.MockTransport(runtime), spool_path=path, background=False
    )
    client = model(lambda request: httpx.Response(200, json=response({"total_tokens": 11})))
    try:
        with production_call_scope(context(), command_id="c", meter=meter):
            result = client.complete_chat_result([{"role": "user", "content": "private fixture"}])
        assert result.text == "completed restricted reply"
        assert len(wire) == 1  # Completed dialogue never synchronously posts usage.
        meter.flush_pending()
        assert meter.spool.status() == {"pending": 1, "in_flight": 0}
        assert meter.status()["error"] == "ConnectError"
        assert store.pending_usage_count() == 0
        original = meter.spool.pending()[0]
    finally:
        meter.close()
        client.close()
    available = True
    reopened = ProductionMeteringClient(
        token="fixture", transport=httpx.MockTransport(runtime), spool_path=path, background=False
    )
    try:
        reopened.flush_pending()
        assert reopened.spool.status() == {"pending": 0, "in_flight": 0}
        assert reopened.status()["error"] is None
        saved = store.pending_usage()
        assert len(saved) == 1
        assert (saved[0]["attempt_id"], saved[0]["usage"]) == original
        sent = [payload for endpoint, payload in wire if endpoint.endswith("model-usage")]
        assert all(payload == sent[0] for payload in sent)
    finally:
        reopened.close()


def test_no_autonomous_model_call_when_reservation_is_unavailable(tmp_path):
    reservations, calls = [], []

    def offline(request):
        reservations.append(json.loads(request.content)["attempt_id"])
        raise httpx.ConnectError("offline", request=request)

    meter = ProductionMeteringClient(
        token="fixture", transport=httpx.MockTransport(offline), spool_path=tmp_path / "spool.sqlite3", background=False
    )
    client = model(lambda request: calls.append(request) or httpx.Response(200, json=response()))
    try:
        with production_call_scope(context(), command_id="automatic", autonomous=True, meter=meter):
            with pytest.raises(httpx.ConnectError):
                client.complete_chat_result([{"role": "user", "content": "fixture"}])
        assert not calls
        assert len(reservations) == 2 and reservations[0] == reservations[1]
        assert meter.spool.status() == {"pending": 0, "in_flight": 0}
    finally:
        meter.close()
        client.close()


def test_process_recovery_never_publishes_a_live_unknown_payload(tmp_path):
    path = tmp_path / "spool.sqlite3"
    first = ProductionUsageSpool(path, process_epoch="process-a")
    seed = {"body_instance_id": "body", "episode_id": "episode", "source": "unavailable", "outcome": "unknown"}
    first.begin("lost", seed)
    first.begin("late", seed)
    first.checkpoint("late", {**seed, "source": "api", "raw_usage": {"total_tokens": 19}})
    assert first.pending() == []
    same_process = ProductionUsageSpool(path, process_epoch="process-a")
    assert same_process.pending() == []
    restarted = ProductionUsageSpool(path, process_epoch="process-b")
    rows = dict(restarted.pending())
    assert rows["lost"]["source"] == "unavailable" and rows["lost"]["outcome"] == "interrupted"
    assert rows["late"]["raw_usage"] == {"total_tokens": 19}
    with pytest.raises(ValueError, match="identity_conflict"):
        restarted.finish("lost", {**seed, "raw_usage": {"total_tokens": 99}})


def test_reservation_retry_and_real_retry_have_distinct_identities(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    reservations, model_calls = [], []

    def runtime(request):
        payload = json.loads(request.content)
        reservations.append(payload["attempt_id"])
        receipt = store.reserve_attempt(**payload)
        if len(reservations) == 1:
            raise httpx.ReadTimeout("reserved but receipt lost", request=request)
        return httpx.Response(200, json=receipt)

    def provider(request):
        model_calls.append(request)
        if len(model_calls) == 1:
            raise httpx.ReadTimeout("model attempt failed", request=request)
        return httpx.Response(200, json=response())

    meter = ProductionMeteringClient(
        token="fixture", transport=httpx.MockTransport(runtime), spool_path=tmp_path / "spool.sqlite3", background=False
    )
    client = model(provider)
    try:
        with production_call_scope(context(), command_id="automatic", autonomous=True, meter=meter):
            with pytest.raises(httpx.ReadTimeout):
                client.complete_chat_result([{"role": "user", "content": "fixture"}])
            client.complete_chat_result([{"role": "user", "content": "fixture"}])
        assert reservations[0] == reservations[1] != reservations[2]
        assert store.budget()["used"] == len(model_calls) == 2
        assert [item[1]["outcome"] for item in meter.spool.pending()] == ["failed", "completed"]
    finally:
        meter.close()
        client.close()


def test_service_file_credentials_and_configured_port(tmp_path, monkeypatch):
    secret = tmp_path / "service.token"
    secret.write_text("a-secure-service-credential-with-40-characters")
    secret.chmod(0o600)
    monkeypatch.setenv("NC_SERVICE_TOKEN_FILE", str(secret))
    monkeypatch.setenv("NC_SERVICE_TOKEN", "ignored-invalid-env")
    monkeypatch.setenv("NC_COGNITIVE_STATE_PATH", str(tmp_path / "cognitive/state.json"))
    monkeypatch.setenv("NC_RUNTIME_PORT", "8870")
    monkeypatch.delenv("NC_RUNTIME_URL", raising=False)
    monkeypatch.delenv("NC_RUNTIME_HOST", raising=False)
    token, endpoint, path = _connection_settings()
    assert token == secret.read_text()
    assert endpoint == "http://127.0.0.1:8870"
    assert path == tmp_path / "cognitive/production-usage.sqlite3"
    secret.unlink()
    with pytest.raises(OSError):
        _connection_settings()


def test_background_sender_does_not_hold_completed_dialogue(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    transmitting, release, delivered = threading.Event(), threading.Event(), threading.Event()

    def runtime(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("model-attempts"):
            return httpx.Response(200, json=store.reserve_attempt(**payload))
        transmitting.set()
        assert release.wait(3)
        receipt = store.record_usage(payload["attempt_id"], payload["usage"])
        delivered.set()
        return httpx.Response(200, json=receipt)

    meter = ProductionMeteringClient(
        token="fixture", transport=httpx.MockTransport(runtime), spool_path=tmp_path / "spool.sqlite3"
    )
    client = model(lambda request: httpx.Response(200, json=response({"total_tokens": 5})))
    try:
        with production_call_scope(context(), command_id="c", meter=meter):
            result = client.complete_chat_result([{"role": "user", "content": "fixture"}])
        assert result.text == "completed restricted reply"
        assert transmitting.wait(2)
        assert not delivered.is_set()
        release.set()
        assert delivered.wait(2)
    finally:
        release.set()
        meter.close()
        client.close()


def test_authenticated_background_context_route(tmp_path):
    class Runtime:
        store = ProductionStore(tmp_path / "runtime.sqlite3")

        async def context(self, scope, *, autonomous):
            return {"scope": scope, "autonomous": autonomous, "body": {"available": False}}

    app = FastAPI()
    install_metering_routes(app, SimpleNamespace(production_runtime=Runtime(), lifecycle="PAUSED"), token="fixture")
    with TestClient(app) as client:
        path = "/v1/production/context"
        assert client.post(path, json={}).status_code == 401
        headers = {"Authorization": "Bearer fixture"}
        result = client.post(path, json={"scope": "owner", "autonomous": False}, headers=headers)
        assert result.status_code == 200 and result.json() == {
            "scope": "owner",
            "autonomous": False,
            "body": {"available": False},
            "runtime_lifecycle": "PAUSED",
        }
        assert client.post(path, json={"scope": "untrusted"}, headers=headers).status_code == 422
        assert client.post(path, json={"url": "https://untrusted.invalid"}, headers=headers).status_code == 422


@pytest.mark.parametrize("lifecycle", [None, "BOOTING", "PAUSING", "PAUSED", "STOPPING", "STOPPED", "FAULTED"])
def test_pause_and_unknown_lifecycle_reject_autonomous_reservations(tmp_path, lifecycle):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    app = FastAPI()
    supervisor = SimpleNamespace(production_runtime=SimpleNamespace(store=store))
    if lifecycle is not None:
        supervisor.lifecycle = lifecycle
    install_metering_routes(app, supervisor, token="fixture")
    with TestClient(app) as client:
        request = {"attempt_id": "automatic", "purpose": "background", "autonomous": True}
        headers = {"Authorization": "Bearer fixture"}
        result = client.post("/v1/production/model-attempts", json=request, headers=headers)
        assert result.status_code == 409 and store.budget()["used"] == 0
        request.update(attempt_id="user", autonomous=False)
        assert client.post("/v1/production/model-attempts", json=request, headers=headers).status_code == 200
