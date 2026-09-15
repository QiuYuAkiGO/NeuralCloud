"""Linux board contract tests use temporary SQLite and in-process HTTP only."""

import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from fastapi.testclient import TestClient
import httpx
import pytest
from pydantic import ValidationError

from harness.board.service import LocalBoardDriver, create_app
from harness.board.store import BoardStore, BoardStoreError, text_digest
from harness.embodiment.board_contracts import (
    BoardAppendRequest, BoardEvidenceRequest, BoardReadRequest, BoardReconcileRequest, BoardScope,
)
from harness.embodiment.capabilities import BOARD_APPEND, CapabilityRequirement, EmbodimentState, HealthStatus
from harness.embodiment.providers.board import BOARD_CAPABILITIES, BoardClient, BoardError, BoardProvider
from harness.research.manifest import EmbodimentCondition
from harness.embodiment.schema import schema_catalog

TOKEN = "9acf70a21fb83dce" * 4


def test_versioned_board_schema_exposes_operations_and_matches_export():
    schema = schema_catalog()["expression.board.v1"]
    exported = Path(__file__).resolve().parents[1] / "contracts/embodiment/expression.board.v1.schema.json"
    assert json.loads(exported.read_text(encoding="utf-8")) == schema
    assert {item["$ref"].split("/")[-1] for item in schema["anyOf"]} == {
        "BoardReadRequest", "BoardReadResult", "BoardAppendRequest", "BoardReceipt",
        "BoardReconcileRequest", "BoardReconcileResult", "BoardEvidenceRequest", "BoardEntry", "BoardStatus",
    }
    assert schema["$defs"]["BoardReadRequest"]["properties"]["latest"]["default"] is False
    assert schema["$defs"]["BoardAppendRequest"]["additionalProperties"] is False


def request(scope, **kwargs):
    now = datetime.now(UTC)
    return BoardAppendRequest(scope=scope, entry_id="entry:one", decision_id="decision:one",
        text="雨声有一点像敲键盘。\n今天想把这个感觉留下来。", title="雨", source_event_ids=("event:one",),
        requested_at=now, deadline=now + timedelta(seconds=30)).model_copy(update=kwargs)


def readonly(scope, model=BoardReadRequest, **kwargs):
    now = datetime.now(UTC)
    return model(scope=scope, requested_at=now, deadline=now + timedelta(seconds=10), **kwargs)


def reconcile(original):
    return readonly(original.scope, BoardReconcileRequest, entry_id=original.entry_id,
                    decision_id=original.decision_id, text_sha256=text_digest(original.text))


def condition(scope, mode="active"):
    return EmbodimentCondition(mode=mode, body_instance_id=scope.body_instance_id if mode != "off" else None,
        capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES])


@pytest.fixture
def store(tmp_path):
    value = BoardStore(tmp_path / "board" / "board.sqlite3", BoardScope(body_instance_id="synthetic-linux-board"))
    yield value
    value.close()


def test_real_transaction_receipt_idempotency_and_restart(store):
    original = request(store.scope)
    receipt = store.append(original)
    assert receipt.text_sha256 == hashlib.sha256(original.text.encode()).hexdigest()
    assert receipt.created_at >= original.requested_at and receipt.sequence == 1
    assert store.append(original) == receipt
    path, scope = store.path, store.scope
    store.close()
    reopened = BoardStore(path, scope)
    try:
        assert reopened.reconcile(reconcile(original)).receipt == receipt
        assert reopened.append(original) == receipt
        entries = reopened.read(readonly(scope)).entries
        assert len(entries) == 1 and entries[0].text == original.text
        assert entries[0].source_event_ids == original.source_event_ids
    finally:
        reopened.close()


@pytest.mark.parametrize("changes", [
    {"text": "changed"}, {"title": "changed"}, {"source_event_ids": ("event:other",)},
    {"entry_id": "entry:other"}, {"decision_id": "decision:other"},
])
def test_id_collision_never_rewrites_or_duplicates(store, changes):
    original = request(store.scope)
    receipt = store.append(original)
    with pytest.raises(BoardStoreError, match="idempotency_conflict"):
        store.append(original.model_copy(update=changes))
    assert store.read(readonly(store.scope)).entries[0].receipt == receipt
    assert len(store.read(readonly(store.scope)).entries) == 1


def test_concurrent_same_decision_is_one_commit(store):
    original = request(store.scope)
    with ThreadPoolExecutor(max_workers=8) as pool:
        receipts = list(pool.map(store.append, [original] * 20))
    assert all(receipt == receipts[0] for receipt in receipts)
    assert len(store.read(readonly(store.scope)).entries) == 1


def test_latest_reads_recent_entries_without_changing_default_pagination(store):
    for number in range(1, 6):
        store.append(request(store.scope, entry_id=f"entry:{number}", decision_id=f"decision:{number}"))
    first = store.read(readonly(store.scope, limit=2)).entries
    latest = store.read(readonly(store.scope, limit=2, latest=True)).entries
    assert [item.receipt.sequence for item in first] == [1, 2]
    assert [item.receipt.sequence for item in latest] == [4, 5]


def test_store_identity_is_bound_across_restart(store):
    with pytest.raises(BoardStoreError, match="identity_mismatch"):
        BoardStore(store.path, store.scope.model_copy(update={"body_instance_id": "different-body"}))
    with pytest.raises(BoardStoreError, match="scope_not_allowed"):
        store.append(request(store.scope.model_copy(update={"board_id": "different-board"})))


@pytest.mark.parametrize("field,value", [("text", "tampered"), ("title", "tampered"), ("evidence_ref", "tampered")])
def test_evidence_detects_corruption_of_body_and_metadata(store, field, value):
    original = request(store.scope)
    store.append(original)
    with sqlite3.connect(store.path) as db:
        db.execute(f"UPDATE board_entries SET {field}=?", (value,))
    with pytest.raises(BoardStoreError, match="evidence_corrupt"):
        store.evidence(readonly(store.scope, BoardEvidenceRequest, entry_id=original.entry_id))


def test_missing_reconciliation_is_readonly_and_wrong_hash_conflicts(store):
    original = request(store.scope)
    assert store.reconcile(reconcile(original)).status == "not_found"
    assert store.read(readonly(store.scope)).entries == ()
    store.append(original)
    with pytest.raises(BoardStoreError, match="idempotency_conflict"):
        store.reconcile(reconcile(original).model_copy(update={"text_sha256": "f" * 64}))


def test_expired_and_invalid_requests_never_write(store):
    original = request(store.scope)
    with pytest.raises(BoardStoreError, match="request_expired"):
        store.append(original.model_copy(update={"requested_at": original.requested_at - timedelta(seconds=60),
                                                "deadline": original.requested_at - timedelta(seconds=30)}))
    with pytest.raises(ValidationError):
        BoardAppendRequest.model_validate(original.model_dump() | {"text": "  "})
    with pytest.raises(ValidationError):
        BoardReconcileRequest.model_validate(reconcile(original).model_dump() | {"deadline": original.requested_at + timedelta(seconds=50)})
    assert store.read(readonly(store.scope)).entries == ()


def test_service_authentication_all_routes_and_durable_http_receipt(store):
    original = request(store.scope)
    app = create_app(store=store, token=TOKEN)
    with TestClient(app) as client:
        for route in ("/health", "/v1/capabilities", "/v1/status"):
            assert client.get(route).status_code == 401
        for route in ("read", "append", "reconcile", "evidence"):
            assert client.post("/v1/board/" + route, json={}).status_code == 401
        headers = {"Authorization": "Bearer " + TOKEN}
        assert client.get("/health", headers=headers).json()["status"] == "healthy"
        result = client.post("/v1/board/append", headers=headers, json=original.model_dump(mode="json"))
        assert result.status_code == 200
        replay = client.post("/v1/board/append", headers=headers, json=original.model_dump(mode="json"))
        assert replay.json() == result.json()
        conflict = client.post("/v1/board/append", headers=headers,
                               json=original.model_copy(update={"text": "changed"}).model_dump(mode="json"))
        assert conflict.status_code == 409 and "changed" not in conflict.text
        read = client.post("/v1/board/read", headers=headers, json=readonly(store.scope).model_dump(mode="json"))
        assert len(read.json()["entries"]) == 1


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_raw_http_cannot_bypass_server_negotiation(store, mode):
    app = create_app(store=store, token=TOKEN, condition=condition(store.scope, mode))
    with TestClient(app) as client:
        result = client.post("/v1/board/append", headers={"Authorization": "Bearer " + TOKEN},
                             json=request(store.scope).model_dump(mode="json"))
        assert result.status_code == 503 and result.json()["detail"] == "capability_unavailable"
        assert not store.read(readonly(store.scope)).entries


def test_unhealthy_store_refuses_write(store):
    async def scenario():
        provider = BoardProvider(condition(store.scope), LocalBoardDriver(store), store.scope)
        assert (await provider.activate()).embodiment.state == EmbodimentState.AVAILABLE
        store.close()
        assert (await provider.status()).embodiment.health.status == HealthStatus.UNAVAILABLE
        with pytest.raises(BoardError, match="capability_unavailable"):
            await provider.append(request(store.scope))
    asyncio.run(scenario())


def test_initially_offline_body_can_negotiate_after_startup_and_health_recovers(store):
    async def scenario():
        class RecoveringDriver(LocalBoardDriver):
            online = False
            writable = True

            async def capabilities(self):
                if not self.online:
                    raise ConnectionError()
                return await super().capabilities()

            async def health(self):
                value = await super().health()
                if not self.writable:
                    value = value.model_copy(update={"status": HealthStatus.UNAVAILABLE})
                return value

        driver = RecoveringDriver(store)
        provider = BoardProvider(condition(store.scope), driver, store.scope)
        assert (await provider.activate()).embodiment.state == EmbodimentState.UNAVAILABLE
        driver.online = True
        assert (await provider.activate()).embodiment.state == EmbodimentState.AVAILABLE
        driver.writable = False
        with pytest.raises(BoardError, match="capability_unavailable"):
            await provider.append(request(store.scope))
        driver.writable = True
        assert not (await provider.read(readonly(store.scope))).entries
        assert (await provider.append(request(store.scope))).sequence == 1
    asyncio.run(scenario())


def test_provider_requires_activation_and_freezes_scope_condition_and_revision(store):
    async def scenario():
        driver = LocalBoardDriver(store)
        provider = BoardProvider(condition(store.scope), driver, store.scope)
        with pytest.raises(BoardError, match="not_activated"):
            await provider.append(request(store.scope))
        assert (await provider.activate()).embodiment.effective_capabilities == set(BOARD_CAPABILITIES)
        provider.manager.condition = provider.manager.condition.model_copy(update={"mode": "observe"})
        with pytest.raises(BoardError, match="activation_changed"):
            await provider.append(request(store.scope))
        assert not store.read(readonly(store.scope)).entries
    asyncio.run(scenario())


def test_http_client_provider_full_negotiation_and_independent_evidence(store):
    async def scenario():
        app = create_app(store=store, token=TOKEN)
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            client = BoardClient("http://127.0.0.1:8793", TOKEN, transport=transport)
            provider = BoardProvider(condition(store.scope), client, store.scope)
            try:
                assert (await provider.activate()).embodiment.state == EmbodimentState.AVAILABLE
                original = request(store.scope)
                receipt = await provider.append(original)
                assert (await provider.reconcile(reconcile(original))).receipt == receipt
                assert (await provider.read(readonly(store.scope))).entries[0].text == original.text
            finally:
                await provider.close()
    asyncio.run(scenario())


def test_lost_append_response_only_reconciles_never_repeats_write(store):
    async def scenario():
        class LostReply(LocalBoardDriver):
            calls = 0

            async def append(self, request):
                self.calls += 1
                await super().append(request)
                raise TimeoutError()

        driver = LostReply(store)
        provider = BoardProvider(condition(store.scope), driver, store.scope)
        await provider.activate()
        original = request(store.scope)
        with pytest.raises(BoardError, match="outcome_unknown"):
            await provider.append(original)
        assert original.entry_id in (await provider.status()).uncertain_entries
        assert not provider.manager.supports(BOARD_APPEND)
        with pytest.raises(BoardError, match="capability_unavailable"):
            await provider.append(original)
        result = await provider.reconcile(reconcile(original))
        assert result.status == "committed" and driver.calls == 1
        assert not (await provider.status()).uncertain_entries
        assert provider.manager.supports(BOARD_APPEND)
    asyncio.run(scenario())


def test_not_found_does_not_clear_uncertain_append(store):
    async def scenario():
        class UnknownWrite(LocalBoardDriver):
            async def append(self, request):
                raise TimeoutError()

        provider = BoardProvider(condition(store.scope), UnknownWrite(store), store.scope)
        await provider.activate()
        original = request(store.scope)
        with pytest.raises(BoardError, match="outcome_unknown"):
            await provider.append(original)
        assert (await provider.reconcile(reconcile(original))).status == "not_found"
        assert (await provider.status()).uncertain_entries == (original.entry_id,)
        assert not store.read(readonly(store.scope)).entries
    asyncio.run(scenario())


@pytest.mark.parametrize("url", ["http://example.com", "https://127.0.0.1", "http://token@localhost", "http://localhost/path"])
def test_client_never_sends_private_credential_to_untrusted_origin(url):
    with pytest.raises(ValueError, match="loopback"):
        BoardClient(url, TOKEN)
