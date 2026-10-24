import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from neuralcloud.archive import ArchiveManager, decode, verify_at_destination
from neuralcloud.contracts import Correction
from neuralcloud.storage import Refusal
from test_memory import event


def verified(manager, batch, target, key, root):
    receipts, vaults = [], {}
    for number in range(2):
        signer = Ed25519PrivateKey.generate()
        domain, host = f"synthetic-device-{number}", f"synthetic-vault-{number}"
        receipts.append(
            verify_at_destination(target, key, signer, host_id=host, failure_domain=domain, maintenance_root=root)
        )
        vaults[domain] = (host, signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))
    manager.accept_verification(batch, receipts, trusted_vaults=vaults, origin_host="synthetic-origin")
    return receipts, vaults


def test_export_retry_restore_and_detach_keep_local_originals(store, tmp_path):
    record = store.ingest(event())
    manager = ArchiveManager(store)
    batch = manager.plan([record["record_id"]])
    target, key = tmp_path / "batch.enc", Fernet.generate_key()
    first = manager.export(batch, target, key)
    assert manager.export(batch, target, key) == first
    assert decode(target, key)["revisions"][0]["record_id"] == record["record_id"]
    with pytest.raises(Refusal, match="VERIFICATION_REQUIRED"):
        manager.detach(batch)
    verified(manager, batch, target, key, tmp_path)
    manager.detach(batch)
    manager.detach(batch)
    assert store.read(record["record_id"])["status"] == "ARCHIVED_OFFLINE"
    assert not store.recall("秋语")["items"]
    with pytest.raises(Refusal, match="EVICTION_BLOCKED"):
        manager.evict(batch)
    assert store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 1


def test_corrupt_export_and_single_failure_domain_cannot_detach(store, tmp_path):
    record = store.ingest(event())
    manager = ArchiveManager(store)
    batch = manager.plan([record["record_id"]])
    target, key = tmp_path / "batch.enc", Fernet.generate_key()
    manager.export(batch, target, key)
    signer = Ed25519PrivateKey.generate()
    receipt = verify_at_destination(
        target, key, signer, host_id="vault", failure_domain="same-physical-disk", maintenance_root=tmp_path
    )
    vaults = {"same-physical-disk": ("vault", signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw))}
    with pytest.raises(Refusal, match="INDEPENDENT_RECOVERY_COPY"):
        manager.accept_verification(batch, [receipt, receipt], trusted_vaults=vaults, origin_host="origin")
    raw = target.read_bytes()
    target.write_bytes(raw[:30] + b"corruption" + raw[40:])
    with pytest.raises(InvalidToken):
        decode(target, key)
    assert store.read(record["record_id"])["status"] == "ONLINE"


def test_changed_revision_and_active_evidence_block_detachment(store, tmp_path):
    record = store.ingest(event())
    manager = ArchiveManager(store)
    store.protect(record["record_id"], "activity:waiting")
    with pytest.raises(Refusal, match="ACTIVE_REFERENCE"):
        manager.plan([record["record_id"]])
    store.protect(record["record_id"], "activity:waiting", False)
    batch = manager.plan([record["record_id"]])
    target, key = tmp_path / "batch.enc", Fernet.generate_key()
    manager.export(batch, target, key)
    verified(manager, batch, target, key, tmp_path)
    store.correct(
        Correction(
            character_id="misty-test",
            source_instance="fixture",
            event_id="late",
            record_id=record["record_id"],
            expected_revision=1,
            content="新更正",
            reason="late",
        )
    )
    with pytest.raises(Refusal, match="REVISION_CHANGED"):
        manager.detach(batch)
    assert store.read(record["record_id"])["content"] == "新更正"


def test_open_dependency_and_pinned_history_are_retained(store):
    record = store.ingest(event())
    store.ingest(event("derived", kind="summary", source_ids=[record["record_id"]]))
    manager = ArchiveManager(store)
    with pytest.raises(Refusal, match="DEPENDENCY_OPEN"):
        manager.plan([record["record_id"]])
    pinned = store.ingest(event("pinned", pinned=True))
    with pytest.raises(Refusal, match="RETAINED"):
        manager.plan([pinned["record_id"]])
