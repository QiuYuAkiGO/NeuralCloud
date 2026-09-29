"""Embedding inference has the same production budget and usage boundary."""

import httpx
import pytest

from harness.config import Settings
from harness.llm.embeddings import GuardedEmbedding, OpenAICompatEmbedding
from harness.production.config import ProductionConfig
from harness.production.maintenance import ProductionMaintenanceDeferred
from harness.production.metering import production_call_scope
from harness.production.store import ProductionStore
from tests.test_production_metering import context


@pytest.mark.parametrize("invalid", [False, True])
def test_embedding_records_actual_usage_before_vector_validation(tmp_path, invalid):
    store = ProductionStore(tmp_path / "runtime.sqlite3")
    client = OpenAICompatEmbedding(Settings(embedding_model="embedder"))
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={
        "usage": {"prompt_tokens": 7, "total_tokens": 7},
        **({} if invalid else {"data": [{"index": 0, "embedding": [1, 0]}]})})))
    try:
        with production_call_scope(context(), command_id="embeddings", autonomous=True, meter=store):
            result = GuardedEmbedding(client).embed(["private text"])
        assert result == ([None] if invalid else [[1, 0]])
        entry = store.pending_usage()[0]
        assert entry["usage"]["raw_usage"] == {"prompt_tokens": 7, "total_tokens": 7}
        assert entry["usage"]["model"] == "embedder"
        assert entry["usage"]["outcome"] == ("failed" if invalid else "completed")
        assert store.budget()["used"] == 1
    finally:
        client.close()


def test_guarded_embedding_propagates_maintenance_budget_refusal(tmp_path):
    store = ProductionStore(tmp_path / "runtime.sqlite3", ProductionConfig(autonomous_attempts_per_hour=1))
    store.reserve_attempt("already-used", purpose="other")
    client = OpenAICompatEmbedding(Settings())
    client._client.close()
    client._client = httpx.Client(transport=httpx.MockTransport(lambda request: pytest.fail("no reserved budget")))
    try:
        with production_call_scope(context() | {"maintenance": True}, command_id="embeddings", autonomous=True, meter=store):
            with pytest.raises(ProductionMaintenanceDeferred):
                GuardedEmbedding(client).embed(["fixture"])
        assert store.pending_usage_count() == 0
    finally:
        client.close()


@pytest.mark.parametrize("native", [False, True])
def test_preload_inference_is_metered_without_inventing_native_counts(tmp_path, monkeypatch, native):
    from harness.llm.client import preload_aux_model

    store = ProductionStore(tmp_path / "runtime.sqlite3")
    raw = {"prompt_eval_count": 3, "eval_count": 1} if native else {"prompt_tokens": 3, "total_tokens": 4}
    body = raw if native else {"usage": raw}
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)))
    monkeypatch.setattr("harness.llm.client.httpx.Client", lambda **kwargs: client)
    with production_call_scope(context(), command_id="preload", autonomous=True, meter=store):
        assert preload_aux_model(Settings(aux_llm_base_url="http://127.0.0.1:11434/v1" if native else "http://model/v1"))
    assert store.budget()["used"] == 1
    assert store.pending_usage()[0]["usage"]["raw_usage"] == raw


def test_model_listing_does_not_consume_model_attempts(tmp_path, monkeypatch):
    from harness.llm.client import list_endpoint_models

    store = ProductionStore(tmp_path / "runtime.sqlite3")
    client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={"data": [{"id": "model"}]})))
    monkeypatch.setattr("harness.llm.client.httpx.Client", lambda **kwargs: client)
    with production_call_scope(context(), command_id="listing", autonomous=True, meter=store):
        assert list_endpoint_models("http://model/v1") == ["model"]
    assert store.budget()["used"] == store.pending_usage_count() == 0
