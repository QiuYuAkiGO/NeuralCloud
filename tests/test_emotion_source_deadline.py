"""An elapsed source window is a durable rejection, not an HTTP 500 outage."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.qq import direct_source
from harness.runtime import adapters
from harness.runtime.rpc import create_domain_service_app
from tests.test_emotion_runtime import owner
from tests import test_qq_direct_source as source_fixtures


direct = source_fixtures.direct


def command(*, issued_at, deadline):
    return DomainCommandEnvelope(command_type="ObserveEmotion", event_id="source-deadline-event",
        receive_seq=1, correlation_id="source-deadline-correlation", runtime_instance_id="runtime-1",
        manifest_revision=1, idempotency_key="source-deadline-context", issued_at=issued_at, deadline=deadline,
        payload={"context_id": "source-deadline-context", "scope": "local:main", "source_payload": {}})


def test_source_expiring_during_provider_returns_http_rejection_and_maintenance_still_works(
        tmp_path, monkeypatch, direct):
    source = direct.create(images=False)
    clock = {"now": source.request.deadline_at - timedelta(seconds=1.8)}

    class SourceClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return clock["now"].astimezone(tz) if tz else clock["now"].replace(tzinfo=None)

    monkeypatch.setattr(direct_source, "datetime", SourceClock)
    monkeypatch.setattr(adapters, "_utc_now", lambda: clock["now"])

    async def scenario():
        adapter, main, aux = owner(tmp_path / "cognitive")
        request = command(issued_at=clock["now"], deadline=source.request.deadline_at)
        provider_calls = []

        def provider(value):
            assert adapter._lock.locked()
            provider_calls.append(value.command_id)
            clock["now"] = value.deadline + timedelta(milliseconds=250)
            direct.store.update(source.event["event_id"], state="expired")
            direct_source.validate_direct_source(direct.config, source.request)
            raise AssertionError("expired source must not reach emotion appraisal")

        adapter.emotion_input_provider = provider
        app = create_domain_service_app(adapter, service_token="test-token", service_name="cognitive")
        before = await adapter.get_snapshot()
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False),
                base_url="http://test", headers={"Authorization": "Bearer test-token"}) as client:
            response = await client.post("/v1/commands/ObserveEmotion", json=request.model_dump(mode="json"))
            assert response.status_code == 200
            result = response.json()
            assert result["status"] == "rejected" and result["error"] == "command deadline expired"
            assert result["request_hash"] == request.effective_request_hash()
            paused = await client.post("/v1/maintenance/pause", json={"timeout": .2})
            assert paused.status_code == 200 and paused.json() == {"drained": True}
            assert (await client.get("/v1/snapshot")).json() == before.model_dump(mode="json")
        assert provider_calls == [request.command_id]
        assert not main.chat_calls and not aux.calls
        assert not adapter.emotion_service.snapshot()["reservations"]
        state = json.loads(adapter.state_path.read_text(encoding="utf-8"))
        assert state["ledger"]["ObserveEmotion\0source-deadline-context"]["status"] == "rejected"
        assert not adapter._lock.locked()
        await adapter.close()
        restored, _, restored_aux = owner(tmp_path / "cognitive")
        try:
            replay = await restored.observe_emotion(request)
            assert replay.idempotency_hit and replay.status == "rejected"
            assert replay.error == "command deadline expired" and not restored_aux.calls
        finally:
            await restored.close()

    asyncio.run(scenario())


def test_expiry_during_reservation_persist_skips_source_read_and_is_replayable(tmp_path, monkeypatch):
    clock = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(adapters, "_utc_now", lambda: clock["now"])

    async def scenario():
        adapter, main, aux = owner(tmp_path)
        request = command(issued_at=clock["now"], deadline=clock["now"] + timedelta(seconds=1))
        original_persist = adapter._persist_state

        def slow_persist():
            original_persist()
            clock["now"] = request.deadline + timedelta(milliseconds=1)

        def forbidden_provider(value):
            raise AssertionError("source reader must not run after preflight expiry")

        adapter._persist_state = slow_persist
        adapter.emotion_input_provider = forbidden_provider
        try:
            result = await adapter.observe_emotion(request)
            assert result.status == "rejected" and result.error == "command deadline expired"
            replay = await adapter.observe_emotion(request)
            assert replay.idempotency_hit and replay.status == "rejected"
            assert not main.chat_calls and not aux.calls
            assert not adapter.emotion_service.snapshot()["reservations"]
            assert not adapter._lock.locked()
        finally:
            await adapter.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("error,expire", [
    (ValueError("direct_qq_evidence_changed"), True),
    (ValueError("direct_qq_source_expired"), True),
    (OSError("source store unavailable"), True),
    (direct_source.DirectQQSourceExpired("direct_qq_source_expired"), False),
])
def test_real_source_errors_or_unbound_early_expiry_are_not_hidden_as_deadlines(tmp_path, monkeypatch, error, expire):
    clock = {"now": datetime.now(timezone.utc)}
    monkeypatch.setattr(adapters, "_utc_now", lambda: clock["now"])

    async def scenario():
        adapter, main, aux = owner(tmp_path)
        request = command(issued_at=clock["now"], deadline=clock["now"] + timedelta(seconds=1))

        def provider(value):
            if expire:
                clock["now"] = value.deadline + timedelta(milliseconds=1)
            raise error

        adapter.emotion_input_provider = provider
        try:
            with pytest.raises(type(error), match=str(error)):
                await adapter.observe_emotion(request)
            assert (request.command_type, request.idempotency_key) not in adapter._ledger
            assert not main.chat_calls and not aux.calls
            assert not adapter._lock.locked()
        finally:
            await adapter.close()

    asyncio.run(scenario())
