import uuid

import pytest
from cryptography.fernet import Fernet

from neuralcloud.archive import MAX_BYTES, ArchiveManager
from neuralcloud.contracts import Correction, digest
from neuralcloud.maintenance import ArchiveMaintenance, ArchivePolicy
from neuralcloud.storage import Refusal
from test_memory import event


def worker(store, root, **kw):
    return ArchiveMaintenance(ArchiveManager(store), root, ArchivePolicy(0, MAX_BYTES * 8, **kw), clock=lambda: 100)


def test_export_queue_restarts_without_duplicate_batch_or_online_detachment(store, tmp_path):
    record = store.ingest(event("queued"))["record_id"]
    root, key, identifier = tmp_path / "jobs", Fernet.generate_key(), str(uuid.uuid4())
    first = worker(store, root)
    job = first.enqueue([record], job_id=identifier)
    assert first.enqueue([record], job_id=identifier) == job
    # File fsync and archive receipt completed, job receipt not yet written: restart recovers it.
    first.manager.export(job["batch_id"], root / (job["batch_id"] + ".enc"), key)
    first.close()
    second = worker(store, root)
    try:
        assert second.run_once(key)[0]["state"] == "EXPORTED"
        assert second.run_once(key) == []
        assert store.db.execute("SELECT count(*) FROM archive_batches").fetchone()[0] == 1
        assert store.read(record)["status"] == "ONLINE"
        assert second.capacity()["staging_bytes"] > 0
        assert second.checkpoint()["vacuum_run"] is False
        with pytest.raises(Refusal, match="OFF_HOST"):
            second.manager.detach(job["batch_id"])
        with pytest.raises(Refusal, match="EVICTION_BLOCKED"):
            second.manager.evict(job["batch_id"])
    finally:
        second.close()
    with pytest.raises(Refusal, match="OWNER_CLOSED"):
        second.run_once(key)


def test_capacity_pauses_without_spending_retry_budget_or_deleting_history(store, tmp_path):
    record = store.ingest(event("capacity"))["record_id"]
    root = tmp_path / "jobs"
    policy = ArchivePolicy(10**18, MAX_BYTES * 8)
    maintenance = ArchiveMaintenance(ArchiveManager(store), root, policy, clock=lambda: 100)
    try:
        maintenance.enqueue([record])
        assert maintenance.run_once(Fernet.generate_key())[0]["state"] == "PAUSED_CAPACITY"
        assert maintenance.jobs()[0]["attempts"] == 0
        assert maintenance.capacity()["optional_collection_should_pause"] is True
        assert store.read(record)["status"] == "ONLINE"
        assert not list(root.glob("*.enc"))
    finally:
        maintenance.close()


def test_changed_revision_protection_and_dependency_closure_stop_export(store, tmp_path):
    record = store.ingest(event("changed"))["record_id"]
    maintenance = worker(store, tmp_path / "jobs")
    try:
        maintenance.enqueue([record])
        store.correct(
            Correction(
                character_id=store.character_id,
                source_instance="operator",
                event_id="correction",
                record_id=record,
                expected_revision=1,
                content="updated",
                reason="corrected",
            )
        )
        assert maintenance.run_once(Fernet.generate_key())[0]["reason"] == "ARCHIVE_REVISION_CHANGED"
        derived = store.ingest(event("derived", kind="summary", source_ids=[record]))["record_id"]
        with pytest.raises(Refusal, match="DEPENDENCY_OPEN"):
            maintenance.enqueue([derived])
        with store.transaction() as db:
            db.execute("INSERT INTO protections VALUES(?,?)", (record, "active-activity"))
        with pytest.raises(Refusal, match="ACTIVE_REFERENCE"):
            maintenance.enqueue([record, derived])
    finally:
        maintenance.close()


def test_bounded_io_retry_requires_hash_bound_operator_recovery(store, tmp_path, monkeypatch):
    record = store.ingest(event("retry"))["record_id"]
    now = [100.0]
    maintenance = ArchiveMaintenance(
        ArchiveManager(store),
        tmp_path / "jobs",
        ArchivePolicy(0, MAX_BYTES * 8, max_attempts=2, retry_seconds=10),
        clock=lambda: now[0],
    )
    try:
        maintenance.enqueue([record])
        export = maintenance.manager.export

        def fail(*args, **kwargs):
            raise OSError("synthetic disk failure")

        monkeypatch.setattr(maintenance.manager, "export", fail)
        assert maintenance.run_once(Fernet.generate_key())[0]["state"] == "RETRY"
        assert maintenance.run_once(Fernet.generate_key()) == []
        now[0] = 110
        assert maintenance.run_once(Fernet.generate_key())[0]["state"] == "BLOCKED"
        job = maintenance.jobs()[0]
        with pytest.raises(Refusal, match="RETRY_CONFLICT"):
            maintenance.retry(job["job_id"], expected_hash="stale", evidence="reviewed")
        maintenance.retry(job["job_id"], expected_hash=digest(job), evidence="verified available writable staging")
        monkeypatch.setattr(maintenance.manager, "export", export)
        assert maintenance.run_once(Fernet.generate_key())[0]["state"] == "EXPORTED"
    finally:
        maintenance.close()


def test_partial_or_foreign_ciphertext_is_retained_and_queue_is_bounded(store, tmp_path):
    record = store.ingest(event("partial"))["record_id"]
    maintenance = worker(store, tmp_path / "jobs", max_pending_jobs=1)
    try:
        job = maintenance.enqueue([record])
        target = maintenance.root / (job["batch_id"] + ".enc")
        target.write_bytes(b"partial encrypted output")
        with pytest.raises(Refusal, match="QUEUE_FULL"):
            maintenance.enqueue([record])
        assert maintenance.run_once(Fernet.generate_key())[0]["state"] == "BLOCKED"
        assert target.read_bytes() == b"partial encrypted output"
        assert store.read(record)["status"] == "ONLINE"
    finally:
        maintenance.close()


def test_queue_receipt_loss_reuses_stable_batch_and_limits_each_tick(store, tmp_path):
    maintenance = worker(store, tmp_path / "jobs")
    try:
        identifier = str(uuid.uuid4())
        record = store.ingest(event("lost-queue-receipt"))["record_id"]
        maintenance.manager.plan([record], batch_id=identifier)
        assert maintenance.enqueue([record], job_id=identifier)["batch_id"] == identifier
        other = store.ingest(event("another-job"))["record_id"]
        maintenance.enqueue([other])
        assert len(maintenance.run_once(Fernet.generate_key(), limit=1)) == 1
        assert store.db.execute("SELECT count(*) FROM archive_batches").fetchone()[0] == 2
        with pytest.raises(Refusal, match="BOUNDS"):
            maintenance.run_once(Fernet.generate_key(), limit=5)
    finally:
        maintenance.close()
