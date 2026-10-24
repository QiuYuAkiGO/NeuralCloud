import sqlite3
import sys
import os
import subprocess

import pytest
from fastapi.testclient import TestClient

from neuralcloud.api import create_app
from neuralcloud.contracts import Correction, SourceEvent
from neuralcloud.storage import MemoryStore, Refusal


def event(key="e1", **fields):
    return SourceEvent(
        character_id="misty-test",
        source_instance="fixture",
        event_id=key,
        session_id=fields.pop("session_id", "group-a"),
        occurred_at=1,
        content=fields.pop("content", "秋语喜欢星空，也认识小明"),
        **fields,
    )


def test_global_cjk_alias_and_old_records(store):
    saved = store.ingest(event(aliases=["阿明"]))
    store.ingest(event("e2", session_id="private-b", content="不同会话的记忆"))
    for query in ("秋语", "小明", "阿明"):
        assert store.recall(query)["items"][0]["record_id"] == saved["record_id"]
    assert store.recall("不同会话")["items"][0]["source"]["session_id"] == "private-b"
    assert store.read(saved["record_id"], 1, 2)["content"] == "语喜"


def test_durable_idempotency_and_conflict_survive_restart(store):
    receipt = store.ingest(event())
    assert store.ingest(event()) == receipt
    with pytest.raises(Refusal, match="CONTENT_CONFLICT"):
        store.ingest(event(content="same ID, different body"))
    path = store.path
    store.close()
    resumed = MemoryStore(path, "misty-test")
    try:
        assert resumed.receipt("fixture", "e1") == receipt
        assert resumed.ingest(event()) == receipt
        assert resumed.generation() == 1
    finally:
        resumed.close()


def test_correction_invalidates_derived_memory_and_retraction_preserves_raw_history(store):
    original = store.ingest(event())
    summary = store.ingest(
        event("summary", content="关于秋语的概括", kind="summary", source_ids=[original["record_id"]])
    )
    receipt = store.correct(
        Correction(
            character_id="misty-test",
            source_instance="fixture",
            event_id="fix",
            record_id=original["record_id"],
            expected_revision=1,
            content="秋语更喜欢海洋",
            reason="user correction",
        )
    )
    assert receipt["revision"] == 2
    assert store.read(summary["record_id"])["status"] == "SOURCE_CHANGED"
    with pytest.raises(Refusal, match="SOURCE_UNAVAILABLE"):
        store.ingest(event("stale-derived", kind="summary", source_ids=[summary["record_id"]]))
    assert not store.recall("星空")["items"]
    assert store.recall("海洋")["items"][0]["revision"] == 2
    store.correct(
        Correction(
            character_id="misty-test",
            source_instance="fixture",
            event_id="withdraw",
            record_id=original["record_id"],
            expected_revision=2,
            reason="withdraw",
            retract=True,
        )
    )
    assert store.read(original["record_id"])["status"] == "RETRACTED"
    with pytest.raises(Refusal, match="SOURCE_UNAVAILABLE"):
        store.correct(
            Correction(
                character_id="misty-test",
                source_instance="fixture",
                event_id="revive-derived",
                record_id=summary["record_id"],
                expected_revision=1,
                content="reworded",
                reason="cannot revive withdrawn evidence",
            )
        )
    assert (
        store.db.execute("SELECT count(*) FROM revisions WHERE record_id=?", (original["record_id"],)).fetchone()[0]
        == 3
    )


def test_unknown_format_and_second_owner_fail_closed(store):
    with pytest.raises(RuntimeError, match="ALREADY_OWNED"):
        MemoryStore(store.path, "misty-test")
    path = store.path
    store.close()
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version=999")
    with pytest.raises(Refusal, match="MIGRATION_REQUIRED"):
        MemoryStore(path, "misty-test")


def test_full_database_does_not_acknowledge_failed_write(store):
    confirmed = store.ingest(event())
    current = store.db.execute("PRAGMA page_count").fetchone()[0]
    store.db.execute(f"PRAGMA max_page_count={current + 3}")
    with pytest.raises(sqlite3.OperationalError, match="full"):
        store.ingest(event("full", content="large evidence " * 2000))
    assert store.receipt("fixture", "full")["status"] == "NOT_FOUND"
    assert store.receipt("fixture", "e1") == confirmed


def test_api_auth_bounds_and_absence_of_maintenance_routes(store):
    token = "test-only-token-" + "x" * 32
    client = TestClient(create_app(store, token))
    assert client.get("/v1/health").status_code == 401
    headers = {"Authorization": "Bearer " + token}
    assert client.get("/v1/health", headers=headers).json()["semantic_search"] is False
    reply = client.post("/v1/ingest", headers=headers, json=event().model_dump())
    assert reply.status_code == 200 and reply.json()["status"] == "DURABLE"
    assert client.post("/v1/ingest", headers=headers, content=b"x" * (128 * 1024 + 1)).status_code == 413
    assert client.post("/v1/archive", headers=headers, json={}).status_code == 404
    assert client.post("/v1/candidate", headers=headers, json=event().model_dump()).status_code == 409
    checked = subprocess.run(
        [
            sys.executable,
            "-c",
            "import neuralcloud.api,sys; assert not any(n.startswith(('harness.','gui.','neuralcloud.archive')) for n in sys.modules)",
        ],
        env={**os.environ, "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parents[2] / "src")},
        capture_output=True,
    )
    assert checked.returncode == 0, checked.stderr


def test_backup_includes_wal_confirmed_events(store, tmp_path):
    receipt = store.ingest(event())
    destination = tmp_path / "snapshot.sqlite3"
    store.backup(destination)
    restored = MemoryStore(destination, "misty-test")
    try:
        assert restored.receipt("fixture", "e1") == receipt
    finally:
        restored.close()
