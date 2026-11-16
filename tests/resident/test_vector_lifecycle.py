"""Deterministic test encoder only; never used for model quality measurements."""

import numpy as np
import pytest
from neuralcloud.vector_index import VectorIndex
from neuralcloud.contracts import SourceEvent, Correction


class LifecycleEncoder:
    name = "unit-fixture"
    revision = "1"
    backend = "test-only"

    def chunks(self, text):
        return [(0, len(text), text)]

    def encode(self, texts, query=False):
        return np.array([[1, 0] for _ in texts], dtype="float32")


def add(store, key, text="old text"):
    return store.ingest(
        SourceEvent(
            character_id="misty-test", source_instance="test", event_id=key, session_id="s", occurred_at=1, content=text
        )
    )


def change(store, receipt, **kw):
    return store.correct(
        Correction(
            character_id="misty-test",
            source_instance="test",
            event_id="fix",
            record_id=receipt["record_id"],
            expected_revision=1,
            reason="unit",
            **kw,
        )
    )


def test_catchup_idempotency_reopen_and_revision_filter_before_reembedding(store, tmp_path):
    rec = add(store, "first")
    path = tmp_path / "vectors.sqlite3"
    index = VectorIndex(store.path, path, LifecycleEncoder())
    assert index.sync_batch() == 1 and index.sync_batch() == 0
    assert index.search("question")[0]["revision"] == 1
    change(store, rec, content="new text")
    assert index.search("question") == []
    resumed = VectorIndex(store.path, path, LifecycleEncoder())
    assert resumed.sync_batch() == 1
    assert resumed.search("question")[0]["revision"] == 2


@pytest.mark.parametrize("status", ["ARCHIVED_OFFLINE", "RETRACTED", "SOURCE_CHANGED", "INACTIVE"])
def test_invalid_records_never_return_even_before_worker_sees_change(store, tmp_path, status):
    rec = add(store, "first")
    index = VectorIndex(store.path, tmp_path / "vectors.sqlite3", LifecycleEncoder())
    index.sync_batch()
    with store.transaction() as db:
        db.execute("UPDATE records SET status=? WHERE id=?", (status, rec["record_id"]))
    assert index.search("question") == []


def test_model_version_mismatch_refuses_existing_index(store, tmp_path):
    path = tmp_path / "vectors.sqlite3"
    VectorIndex(store.path, path, LifecycleEncoder())
    encoder = LifecycleEncoder()
    encoder.revision = "2"
    with pytest.raises(ValueError, match="MISMATCH"):
        VectorIndex(store.path, path, encoder)


def test_failed_embedding_does_not_advance_cursor_or_partially_commit(store, tmp_path):
    class Failing(LifecycleEncoder):
        def encode(self, *a, **kw):
            raise RuntimeError("failure")

    add(store, "first")
    index = VectorIndex(store.path, tmp_path / "vectors.sqlite3", Failing())
    with pytest.raises(RuntimeError):
        index.sync_batch()
    index.encoder = LifecycleEncoder()
    assert index.sync_batch() == 1 and len(index.search("query")) == 1


def test_worker_http_contract_auth_health_and_strict_input(store, tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from neuralcloud import vector_index
    import uvicorn
    import sys
    import time

    add(store, "first")
    token = tmp_path / "token"
    token.write_text("x" * 32)
    monkeypatch.setattr(vector_index, "Encoder", lambda *a, **kw: LifecycleEncoder())
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "worker",
            "--source",
            str(store.path),
            "--index",
            str(tmp_path / "index.sqlite3"),
            "--model",
            "fixture",
            "--revision",
            "1",
            "--token-file",
            str(token),
        ],
    )

    def run(app, **kw):
        with TestClient(app) as client:
            assert client.get("/health").status_code == 401
            headers = {"Authorization": "Bearer " + "x" * 32}
            for _ in range(100):
                if client.get("/health", headers=headers).json()["status"] == "READY":
                    break
                time.sleep(0.01)
            result = client.post("/search", headers=headers, json={"query": "fixture"})
            assert result.status_code == 200, result.text
            assert len(result.json()["items"]) == 1
            assert (
                client.post(
                    "/search", headers=headers, json={"query": "fixture", "sql": "DROP TABLE records"}
                ).status_code
                == 422
            )

    monkeypatch.setattr(uvicorn, "run", run)
    vector_index.main()
