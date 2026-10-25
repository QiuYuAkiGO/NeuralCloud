import json
from pathlib import Path
import time

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

from neuralcloud.archive import ArchiveManager, RestoreGrant, sign_restore_grant
from neuralcloud.contracts import Correction
from neuralcloud.storage import Refusal
from neuralcloud.temporary_views import TemporaryViews
from test_archive import restore_approval, verified
from test_memory import event


def setup(store, tmp_path):
    first = store.ingest(event("temp-first", content="synthetic confidential archived content"))
    second = store.ingest(event("temp-second", content="out of scope content"))
    manager = ArchiveManager(store)
    batch = manager.plan([first["record_id"], second["record_id"]])
    path, key = tmp_path / "original.enc", Fernet.generate_key()
    exported = manager.export(batch, path, key)
    verified(manager, batch, path, key, tmp_path)
    manager.detach(batch)
    signer = Ed25519PrivateKey.generate()
    grant = RestoreGrant.model_validate(restore_approval(store, batch, exported, [first["record_id"]], signer)["grant"])
    grant = grant.model_copy(update={"mode": "temporary_view"})
    approval = sign_restore_grant(grant, signer)
    kwargs = {
        "operator_public_key": signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        "target_environment": "isolated-fixture",
    }
    return manager, path, key, approval, kwargs, first["record_id"], second["record_id"]


def test_temporary_scope_expiry_encrypted_files_and_no_online_restoration(store, tmp_path):
    manager, path, key, approval, kwargs, first, second = setup(store, tmp_path)
    now = [150.0]
    root = tmp_path / "views"
    views = TemporaryViews(manager, root, clock=lambda: now[0], monotonic=lambda: 0, sweep_seconds=None)
    try:
        result = views.open(path, key, approval, **kwargs)
        identifier = result["view_id"]
        assert views.read(identifier, first)["content"] == "synthetic confidential archived content"
        assert store.read(first)["status"] == "ARCHIVED_OFFLINE"
        assert not store.recall("confidential")["items"]
        with pytest.raises(Refusal, match="SCOPE"):
            views.read(identifier, second)
        with pytest.raises(Refusal, match="MODE_MISMATCH"):
            manager.restore(path, key, approval, **kwargs, now=150)
        for file in root.iterdir():
            if file.name == ".owner":
                continue
            assert b"confidential archived content" not in file.read_bytes()
        now[0] = 201
        assert views.sweep()["closed"] == 1
        assert not list(root.glob("*.enc"))
        now[0] = 150
        with pytest.raises(Refusal, match="CLOSED"):
            views.read(identifier, first)
        with pytest.raises(Refusal, match="EXPIRED"):
            views.open(path, key, approval, **kwargs)
        assert path.is_file() and store.db.execute("SELECT count(*) FROM revisions").fetchone()[0] == 2
    finally:
        views.close()


def test_temporary_rechecks_latest_tombstones_and_operator_signature(store, tmp_path):
    manager, path, key, approval, kwargs, first, _ = setup(store, tmp_path)
    views = TemporaryViews(manager, tmp_path / "views", clock=lambda: 150, monotonic=lambda: 0, sweep_seconds=None)
    try:
        bad = json.loads(json.dumps(approval))
        bad["grant"]["purpose"] = "unsigned changed purpose"
        with pytest.raises(InvalidSignature):
            views.open(path, key, bad, **kwargs)
        with pytest.raises(Refusal, match="TARGET"):
            views.open(path, key, approval, **{**kwargs, "target_environment": "production"})
        identifier = views.open(path, key, approval, **kwargs)["view_id"]
        store.correct(
            Correction(
                character_id=store.character_id,
                source_instance="operator",
                event_id="withdraw",
                record_id=first,
                expected_revision=1,
                retract=True,
                reason="new withdrawal",
            )
        )
        with pytest.raises(Refusal, match="CORRECTION_CONFLICT"):
            views.read(identifier, first)
        assert not views.keys and not list((tmp_path / "views").glob("*.enc"))
    finally:
        views.close()


def test_process_restart_revokes_views_and_cannot_reuse_consumed_grant(store, tmp_path):
    manager, path, key, approval, kwargs, first, _ = setup(store, tmp_path)
    root = tmp_path / "views"
    views = TemporaryViews(manager, root, clock=lambda: 150, monotonic=lambda: 0, sweep_seconds=None)
    identifier = views.open(path, key, approval, **kwargs)["view_id"]
    # Model a process loss: OS lock released, no graceful cleanup or retained keys.
    views.keys.clear()
    views.owner.close()
    recovered = TemporaryViews(manager, root, clock=lambda: 150, monotonic=lambda: 0, sweep_seconds=None)
    try:
        assert not list(root.glob("*.enc"))
        with pytest.raises(Refusal, match="CLOSED"):
            recovered.read(identifier, first)
        with pytest.raises(Refusal, match="ALREADY_USED"):
            recovered.open(path, key, approval, **kwargs)
    finally:
        recovered.close()
    with pytest.raises(Refusal, match="OWNER_CLOSED"):
        recovered.open(path, key, approval, **kwargs)


def test_cleanup_failure_revokes_key_and_can_retry_without_extending_access(store, tmp_path, monkeypatch):
    manager, path, key, approval, kwargs, first, _ = setup(store, tmp_path)
    now = [150.0]
    views = TemporaryViews(manager, tmp_path / "views", clock=lambda: now[0], monotonic=lambda: 0, sweep_seconds=None)
    try:
        identifier = views.open(path, key, approval, **kwargs)["view_id"]
        real_unlink = Path.unlink

        def fail_unlink(target, *args, **opts):
            if target.parent == views.root:
                raise PermissionError("synthetic file lock")
            return real_unlink(target, *args, **opts)

        monkeypatch.setattr(Path, "unlink", fail_unlink)
        now[0] = 201
        assert views.sweep()["cleanup_pending"] == 1
        assert not views.keys
        with pytest.raises(Refusal, match="CLOSED"):
            views.read(identifier, first)
        monkeypatch.setattr(Path, "unlink", real_unlink)
        assert views.sweep()["closed"] == 1
        assert path.exists()
    finally:
        views.close()


def test_monotonic_expiry_despite_wall_clock_rollback(store, tmp_path):
    manager, path, key, approval, kwargs, first, _ = setup(store, tmp_path)
    wall, elapsed = [150.0], [0.0]
    views = TemporaryViews(
        manager, tmp_path / "views", clock=lambda: wall[0], monotonic=lambda: elapsed[0], sweep_seconds=None
    )
    try:
        identifier = views.open(path, key, approval, **kwargs)["view_id"]
        wall[0], elapsed[0] = 10, 51
        with pytest.raises(Refusal, match="CLOSED"):
            views.read(identifier, first)
    finally:
        views.close()


def test_idle_expiry_sweeper_removes_only_its_generated_copy(store, tmp_path):
    manager, path, key, approval, kwargs, _first, _ = setup(store, tmp_path)
    now = [150.0]
    views = TemporaryViews(manager, tmp_path / "views", clock=lambda: now[0], monotonic=lambda: 0, sweep_seconds=0.02)
    try:
        result = views.open(path, key, approval, **kwargs)
        copy = views.root / (result["view_id"] + ".enc")
        now[0] = 201
        deadline = time.monotonic() + 3
        while copy.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        assert not copy.exists() and not views.keys
        assert path.exists() and views.sweep_error is None
    finally:
        views.close()


def test_view_of_derived_record_cannot_survive_evidence_correction(store, tmp_path):
    source = store.ingest(event("evidence"))["record_id"]
    derived = store.ingest(event("derivation", kind="summary", source_ids=[source]))["record_id"]
    manager = ArchiveManager(store)
    batch = manager.plan([source, derived])
    path, key = tmp_path / "derived.enc", Fernet.generate_key()
    exported = manager.export(batch, path, key)
    verified(manager, batch, path, key, tmp_path)
    manager.detach(batch)
    signer = Ed25519PrivateKey.generate()
    grant = RestoreGrant.model_validate(restore_approval(store, batch, exported, [derived], signer)["grant"])
    grant = grant.model_copy(update={"mode": "temporary_view"})
    kwargs = {
        "operator_public_key": signer.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
        "target_environment": "isolated-fixture",
    }
    source_approval = restore_approval(store, batch, exported, [source], signer, "restore-source-for-view")
    manager.restore(path, key, source_approval, **kwargs, now=150)
    views = TemporaryViews(manager, tmp_path / "views", clock=lambda: 150, monotonic=lambda: 0, sweep_seconds=None)
    try:
        identifier = views.open(path, key, sign_restore_grant(grant, signer), **kwargs)["view_id"]
        assert views.read(identifier, derived)["status"] == "TEMPORARY_VIEW"
        store.correct(
            Correction(
                character_id=store.character_id,
                source_instance="operator",
                event_id="fix-evidence",
                record_id=source,
                expected_revision=1,
                content="new evidence",
                reason="correction",
            )
        )
        with pytest.raises(Refusal, match="CORRECTION_CONFLICT|SOURCE_CORRECTED"):
            views.read(identifier, derived)
        assert not views.keys
    finally:
        views.close()
