"""Only an operator can allocate an unbound usage fact to a verified body."""

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from harness.production.accounting import install_operator_accounting_routes
from harness.production.runtime import ProductionRuntime
from harness.runtime.models import utc_now
from tests.test_production_runtime import FakeSupervisor


class Body:
    def __init__(self):
        self.online = True
        self.calls = []

    async def snapshot(self):
        if not self.online:
            raise TimeoutError("offline")
        return {"body_instance_id": "body-test", "episode_id": "current", "sample_sequence": 5,
            "ledger_revision": 3, "captured_at": utc_now().isoformat(), "physiology_mode": "production_v1",
            "resting": False, "exhausted": False, "energy": {}, "evidence_refs": ["verified-body-proof"]}

    async def record_usage(self, request):
        self.calls.append(request)
        return SimpleNamespace(model_dump=lambda **_: {"inference_id": request.inference_id})


def setup(tmp_path, *, equal_credentials=False):
    supervisor = FakeSupervisor(tmp_path)
    supervisor.production_runtime = runtime = ProductionRuntime(supervisor)
    body = Body()
    runtime.attach_body_provider(body)
    app = FastAPI()
    install_operator_accounting_routes(app, supervisor, token="operator", service_token="operator" if equal_credentials else "service")
    usage = {"source": "api", "raw_usage": {"total_tokens": 29}, "outcome": "completed"}
    runtime.store.reserve_attempt("unknown", purpose="limited-dialogue", autonomous=False)
    runtime.store.record_usage("unknown", usage)
    row = runtime.store.unassigned_usage()[0]
    request = {"assignment_id": "operator-assignment", "expected_usage_hash": row["usage_hash"],
        "body_instance_id": "body-test", "episode_id": "current", "reason": "Operator accepts this prior unbound cost."}
    return runtime, body, TestClient(app), usage, request


def test_service_token_cannot_read_or_assign_and_equal_credentials_fail_closed(tmp_path):
    for equal in (False, True):
        runtime, _, client, _, request = setup(tmp_path / str(equal), equal_credentials=equal)
        for credential in (["service", ""] if not equal else ["operator"]):
            headers = {"Authorization": "Bearer " + credential}
            assert client.get("/v1/production/accounting/unassigned", headers=headers).status_code == 401
            assert client.post("/v1/production/model-usage/unknown/assignment", json=request, headers=headers).status_code == 401
        assert runtime.store.unassigned_usage_count() == 1


def test_assignment_preserves_fact_and_attempt_and_replays_after_settlement(tmp_path):
    runtime, body, client, original, request = setup(tmp_path)
    headers = {"Authorization": "Bearer operator"}
    listing = client.get("/v1/production/accounting/unassigned", headers=headers).json()
    assert listing["unassigned_count"] == 1
    assert listing["verified_current_target"]["episode_id"] == "current"
    assert listing["usage"][0]["usage_hash"] == request["expected_usage_hash"]
    assert runtime.status()["usage_unassigned"] == 1
    first = client.post("/v1/production/model-usage/unknown/assignment", json=request, headers=headers)
    assert first.status_code == 200
    receipt = first.json()
    assert receipt["assignment_basis"] == "operator_allocation_after_unknown_body"
    assert not receipt["occurrence_body_verified"]
    assert receipt["verified_current_body"]["evidence_refs"] == ["verified-body-proof"]
    assert runtime.store.pending_usage()[0]["usage"] == original
    assert runtime.store.record_usage("unknown", original)["durable"]  # Original spool retry is unchanged.
    assert runtime.status()["usage_unassigned"] == 0
    asyncio.run(runtime.flush_usage())
    assert runtime.store.pending_usage_count() == 0
    assert len(body.calls) == 1 and body.calls[0].inference_id == "unknown"
    assert body.calls[0].episode_id == "current" and body.calls[0].quantity == 29
    body.online = False
    replay = client.post("/v1/production/model-usage/unknown/assignment", json=request, headers=headers)
    assert replay.status_code == 200 and replay.json() == receipt
    assert runtime.store.record_usage("unknown", original)["durable"]


def test_assignment_rejects_conflicting_hash_identity_or_body_and_known_episode(tmp_path):
    runtime, body, client, _, request = setup(tmp_path)
    headers = {"Authorization": "Bearer operator"}
    path = "/v1/production/model-usage/unknown/assignment"
    for change in ({"expected_usage_hash": "0" * 64}, {"episode_id": "another"}, {"body_instance_id": "another"}):
        assert client.post(path, json=request | change, headers=headers).status_code == 409
    body.online = False
    assert client.post(path, json=request, headers=headers).status_code == 409
    body.online = True
    assert client.post(path, json=request, headers=headers).status_code == 200
    for change in ({"reason": "different"}, {"assignment_id": "different"}):
        assert client.post(path, json=request | change, headers=headers).status_code == 409
    runtime.store.reserve_attempt("known", purpose="old-episode")
    runtime.store.record_usage("known", {"body_instance_id": "body-test", "episode_id": "old"})
    known = client.post("/v1/production/model-usage/known/assignment", json=request | {"assignment_id": "known"}, headers=headers)
    assert known.status_code == 409 and "cannot_be_reassigned" in known.text
    assert runtime.store.unassigned_usage_count() == 0
    with runtime.store._db() as db:
        assert json.loads(db.execute("SELECT payload FROM usage_outbox WHERE attempt_id='known'").fetchone()[0])["episode_id"] == "old"
