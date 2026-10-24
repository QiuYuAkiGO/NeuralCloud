import pytest
from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from neuralcloud.archive import ArchiveManager, decode, verify_at_destination
from neuralcloud.contracts import Correction
from neuralcloud.storage import Refusal
from test_memory import event


def restore_approval(store, batch, exported, ids, signer, nonce="approval-fixture-001"):
    from neuralcloud.archive import RestoreGrant, sign_restore_grant

    return sign_restore_grant(
        RestoreGrant(
            grant_id=nonce,
            character_id=store.character_id,
            database_id=store.db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0],
            target_environment="isolated-fixture",
            batch_id=batch,
            envelope_hash=exported["envelope_hash"],
            record_ids=ids,
            purpose="synthetic restore acceptance",
            issued_at=100,
            expires_at=200,
        ),
        signer,
    )


def test_restore_requires_scoped_unexpired_operator_grant_and_is_idempotent(store, tmp_path):
    first, second = (store.ingest(event(key)) for key in ("restore-one", "restore-two"))
    manager = ArchiveManager(store)
    batch = manager.plan([first["record_id"], second["record_id"]])
    target, key = tmp_path / "restore.enc", Fernet.generate_key()
    exported = manager.export(batch, target, key)
    verified(manager, batch, target, key, tmp_path)
    manager.detach(batch)
    signer = Ed25519PrivateKey.generate()
    kwargs = {
        "operator_public_key": signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        "target_environment": "isolated-fixture",
    }
    approval = restore_approval(store, batch, exported, [first["record_id"]], signer)
    with pytest.raises(Refusal, match="EXPIRED"):
        manager.restore(target, key, approval, **kwargs, now=201)
    with pytest.raises(Refusal, match="TARGET_MISMATCH"):
        manager.restore(target, key, approval, **{**kwargs, "target_environment": "production"}, now=150)
    result = manager.restore(target, key, approval, **kwargs, now=150)
    assert manager.restore(target, key, approval, **kwargs, now=151) == result
    assert result["record_count"] == 1
    assert store.read(first["record_id"])["status"] == "ONLINE"
    assert store.read(second["record_id"])["status"] == "ARCHIVED_OFFLINE"
    store.correct(
        Correction(
            character_id=store.character_id,
            source_instance="operator",
            event_id="withdraw-offline",
            record_id=second["record_id"],
            expected_revision=1,
            retract=True,
            reason="new withdrawal",
        )
    )
    withdrawn = restore_approval(store, batch, exported, [second["record_id"]], signer, "approval-fixture-002")
    with pytest.raises(Refusal, match="CORRECTION_CONFLICT"):
        manager.restore(target, key, withdrawn, **kwargs, now=150)


def test_restore_does_not_resurrect_summary_after_evidence_correction(store, tmp_path):
    source = store.ingest(event("original"))
    derived = store.ingest(event("summary", kind="summary", source_ids=[source["record_id"]]))
    manager = ArchiveManager(store)
    batch = manager.plan([source["record_id"], derived["record_id"]])
    target, key = tmp_path / "derived.enc", Fernet.generate_key()
    exported = manager.export(batch, target, key)
    verified(manager, batch, target, key, tmp_path)
    manager.detach(batch)
    signer = Ed25519PrivateKey.generate()
    kwargs = {
        "operator_public_key": signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        "target_environment": "isolated-fixture",
        "now": 150,
    }
    grant = restore_approval(store, batch, exported, [derived["record_id"]], signer)
    with pytest.raises(Refusal, match="SOURCE_CORRECTED_OR_OFFLINE"):
        manager.restore(target, key, grant, **kwargs)
    grant_source = restore_approval(store, batch, exported, [source["record_id"]], signer, "approval-source-001")
    manager.restore(target, key, grant_source, **kwargs)
    store.correct(
        Correction(
            character_id=store.character_id,
            source_instance="operator",
            event_id="fix-source",
            record_id=source["record_id"],
            expected_revision=1,
            content="corrected evidence",
            reason="new correction",
        )
    )
    with pytest.raises(Refusal, match="SOURCE_CORRECTED_OR_OFFLINE"):
        manager.restore(target, key, grant, **kwargs)


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
    with pytest.raises(Refusal, match="SOURCE_UNAVAILABLE"):
        store.ingest(event("offline-derived", kind="summary", source_ids=[record["record_id"]]))
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
