"""The independent daily provider must use the production transport ledger."""

import json

import httpx
import pytest

from harness.memory.revision.provider import DailyRevisionProvider, RevisionProviderError
from harness.production.config import ProductionConfig
from harness.production.maintenance import ProductionMaintenanceDeferred
from harness.production.metering import ProductionMeteringClient, production_call_scope
from harness.production.store import ProductionStore
from tests.test_daily_revision_provider import answer, response, setup
from tests.test_production_metering import context


def test_daily_real_transmissions_use_durable_spool_and_cache_spends_nothing(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    connected = False

    def runtime(request):
        payload = json.loads(request.content)
        if request.url.path.endswith("model-attempts"):
            return httpx.Response(200, json=store.reserve_attempt(**payload))
        if not connected:
            raise httpx.ConnectError("runtime unavailable", request=request)
        return httpx.Response(200, json=store.record_usage(payload["attempt_id"], payload["usage"]))

    meter = ProductionMeteringClient(token="fixture", spool_path=tmp_path / "spool.sqlite3",
        background=False, transport=httpx.MockTransport(runtime))
    try:
        with httpx.Client(transport=httpx.MockTransport(answer)) as client:
            provider = DailyRevisionProvider(config, queue, client)
            with production_call_scope(context(), command_id="daily:run", autonomous=True, meter=meter):
                result = provider.propose(run_id, records, {})
                assert provider.propose(run_id, records, {}) == result
        assert store.budget()["used"] == 3
        assert meter.spool.status() == {"pending": 3, "in_flight": 0}
        meter.flush_pending()
        assert meter.spool.status()["pending"] == 3
        connected = True
        meter.flush_pending()
        meter.flush_pending()
        assert store.pending_usage_count() == 3
        for entry in store.pending_usage():
            usage = entry["usage"]
            assert usage["raw_usage"] == {"prompt_tokens": 50, "completion_tokens": 100, "total_tokens": 150}
            assert usage["source"] == "api" and usage["episode_id"] == "original-episode"
        assert meter.spool.status() == {"pending": 0, "in_flight": 0}
    finally:
        meter.close()


def test_daily_retry_is_a_distinct_attempt_and_partial_usage_stays_raw(tmp_path, monkeypatch):
    config, queue, records, run_id = setup(tmp_path)
    config = config.model_copy(update={"max_attempts": 2})
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    requests = []
    monkeypatch.setattr("harness.memory.revision.provider.time.sleep", lambda delay: None)

    def handler(request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503, json={"usage": {"prompt_tokens": 7, "invalid": True,
                "completion_tokens_details": {"reasoning_tokens": 2}}})
        return answer(request)

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with production_call_scope(context(), command_id="daily:retry", autonomous=True, meter=store):
            DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    entries = store.pending_usage()
    assert len(requests) == store.budget()["used"] == len(entries) == 4
    assert len({entry["attempt_id"] for entry in entries}) == 4
    assert entries[0]["usage"]["raw_usage"] == {
        "prompt_tokens": 7, "completion_tokens_details.reasoning_tokens": 2}


@pytest.mark.parametrize("failure", ["budget", "connection", "paused"])
def test_daily_reservation_refusal_defers_without_model_call_or_local_charge(tmp_path, failure):
    config, queue, records, run_id = setup(tmp_path)
    store = ProductionStore(tmp_path / "runtime.sqlite3", ProductionConfig(autonomous_attempts_per_hour=1))
    store.reserve_attempt("already-spent", purpose="other")

    class Meter:
        def reserve_attempt(self, attempt_id, **kwargs):
            if failure == "connection":
                raise httpx.ConnectError("runtime unavailable")
            if failure == "paused":
                raise httpx.HTTPStatusError("runtime paused", request=httpx.Request("POST", "http://runtime"),
                    response=httpx.Response(409))
            return store.reserve_attempt(attempt_id, **kwargs)

    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("request was never authorized"))) as client:
        with production_call_scope(context(), command_id="daily:wait", autonomous=True, meter=Meter()):
            with pytest.raises(ProductionMaintenanceDeferred):
                DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert queue.status()["usage"] == []
    assert store.budget()["used"] == 1 and store.pending_usage_count() == 0


def test_daily_invalid_completion_keeps_actual_usage(tmp_path):
    config, queue, records, run_id = setup(tmp_path)
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    with httpx.Client(transport=httpx.MockTransport(lambda request: response({}, finish="length"))) as client:
        with production_call_scope(context(), command_id="daily:invalid", autonomous=True, meter=store):
            with pytest.raises(RevisionProviderError):
                DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    usage = store.pending_usage()[0]["usage"]
    assert usage["outcome"] == "failed" and usage["source"] == "api"
    assert usage["raw_usage"]["total_tokens"] == 150


def test_daily_local_daily_limit_does_not_reserve_a_production_attempt(tmp_path):
    config, queue, records, run_id = setup(tmp_path, daily_token_budget=1)
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    with httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("no budget"))) as client:
        with production_call_scope(context(), command_id="daily:local-limit", autonomous=True, meter=store):
            with pytest.raises(RevisionProviderError, match="daily_budget_exceeded"):
                DailyRevisionProvider(config, queue, client).propose(run_id, records, {})
    assert store.budget()["used"] == 0 and store.pending_usage_count() == 0
