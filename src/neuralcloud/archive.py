"""Bounded management-plane archives. Never imported by the role HTTP service.

Eviction of production originals is intentionally unavailable until the deployment's
side-copy inventory and independent recovery gates have been accepted.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import uuid

from cryptography.fernet import Fernet
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey

from .contracts import canonical, digest
from .storage import MemoryStore, Refusal

MAX_BYTES = 8 * 1024 * 1024


def atomic_new(path: Path, data: bytes):
    with path.open("xb") as out:
        out.write(data)
        out.flush()
        os.fsync(out.fileno())
    if os.name != "nt":
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)


def decode(path: Path, key: bytes):
    if path.stat().st_size > MAX_BYTES * 2:
        raise Refusal("ARCHIVE_TOO_LARGE")
    raw = Fernet(key).decrypt(path.read_bytes())
    if len(raw) > MAX_BYTES:
        raise Refusal("ARCHIVE_TOO_LARGE")
    data = json.loads(raw)
    if data.get("format") != "neuralcloud-cold-v1":
        raise Refusal("ARCHIVE_FORMAT_UNSUPPORTED")
    ids = {row["id"] for row in data["records"]}
    if not 0 < len(ids) <= 500 or len(ids) != len(data["records"]):
        raise Refusal("ARCHIVE_INVALID_IDS")
    seen = set()
    for row in data["revisions"]:
        pair = row["record_id"], row["revision"]
        if pair in seen or row["record_id"] not in ids or digest(json.loads(row["payload"])) != row["hash"]:
            raise Refusal("ARCHIVE_HASH_OR_REFERENCE_INVALID")
        seen.add(pair)
    if any((r["id"], r["revision"]) not in seen for r in data["records"]):
        raise Refusal("ARCHIVE_CURRENT_REVISION_MISSING")
    if any(r["record_id"] not in ids or r["source_id"] not in ids for r in data["sources"]):
        raise Refusal("ARCHIVE_DEPENDENCY_OPEN")
    return data


def verify_at_destination(
    path: Path, key: bytes, signing_key: Ed25519PrivateKey, *, host_id: str, failure_domain: str, maintenance_root: Path
):
    """Run ON the configured vault/backup host. A local check is not off-host proof.

    Signing keys and approved host/domain identities are operator-owned. The
    verifier actually decrypts and restores every revision into temporary SQLite.
    No signing key or cold decryption key is ever distributed to the role.
    """
    data = decode(path, key)
    with tempfile.TemporaryDirectory(prefix="nc-restore-", dir=maintenance_root) as temporary:
        db = sqlite3.connect(Path(temporary) / "restore.sqlite3")
        try:
            db.execute(
                "CREATE TABLE restored(id TEXT, revision INTEGER, payload TEXT, hash TEXT, PRIMARY KEY(id,revision))"
            )
            for row in data["revisions"]:
                db.execute(
                    "INSERT INTO restored VALUES(?,?,?,?)",
                    (row["record_id"], row["revision"], row["payload"], row["hash"]),
                )
            db.commit()
            if db.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise Refusal("RESTORE_FAILED")
            for payload, expected in db.execute("SELECT payload,hash FROM restored"):
                if digest(json.loads(payload)) != expected:
                    raise Refusal("RESTORE_HASH_FAILED")
        finally:
            db.close()
    payload = {
        "version": 1,
        "batch_id": data["batch_id"],
        "character_id": data["character_id"],
        "envelope_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
        "record_count": len(data["records"]),
        "revision_count": len(data["revisions"]),
        "host_id": host_id,
        "failure_domain": failure_domain,
        "verified_at": time.time(),
        "restore_verified": True,
    }
    return {"payload": payload, "signature": base64.b64encode(signing_key.sign(canonical(payload).encode())).decode()}


class ArchiveManager:
    def __init__(self, store: MemoryStore):
        self.store = store

    def _eligible(self, db, ids):
        rows = []
        selected = set(ids)
        for record_id in ids:
            row = db.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            if not row or row["status"] != "ONLINE" or row["pinned"]:
                raise Refusal("ARCHIVE_RETAINED_OR_UNAVAILABLE")
            if db.execute("SELECT 1 FROM protections WHERE record_id=? LIMIT 1", (record_id,)).fetchone():
                raise Refusal("ARCHIVE_ACTIVE_REFERENCE")
            # Conservative closed-component selection: no evidence can silently disappear.
            refs = db.execute(
                "SELECT source_id FROM sources WHERE record_id=? UNION SELECT record_id FROM sources WHERE source_id=?",
                (record_id, record_id),
            ).fetchall()
            if any(ref[0] not in selected for ref in refs):
                raise Refusal("ARCHIVE_DEPENDENCY_OPEN")
            rows.append(dict(row))
        return rows

    def plan(self, record_ids):
        ids = sorted(set(record_ids))
        if not 0 < len(ids) <= 500:
            raise Refusal("ARCHIVE_BATCH_BOUNDS")
        batch_id = str(uuid.uuid4())
        with self.store.transaction() as db:
            rows = self._eligible(db, ids)
            plan = {
                "format": "neuralcloud-cold-v1",
                "batch_id": batch_id,
                "character_id": self.store.character_id,
                "created_at": time.time(),
                "high_water": self.store.generation(),
                "records": rows,
            }
            db.execute("INSERT INTO archive_batches VALUES(?,'PLANNED',?,NULL,NULL)", (batch_id, canonical(plan)))
        return batch_id

    def export(self, batch_id, target: Path, key: bytes):
        # Export is bounded and can be repeated after a crash between file fsync and receipt commit.
        with self.store.transaction() as db:
            batch = db.execute("SELECT * FROM archive_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch or batch["state"] not in {"PLANNED", "SNAPSHOTTED", "EXPORTED"}:
                raise Refusal("ARCHIVE_STATE")
            data = json.loads(batch["plan"])
            ids = [r["id"] for r in data["records"]]
            if self._eligible(db, ids) != data["records"]:
                raise Refusal("ARCHIVE_REVISION_CHANGED")
            data["revisions"] = []
            data["sources"] = []
            size = len(canonical(data).encode())
            for record_id in ids:
                for row in db.execute("SELECT * FROM revisions WHERE record_id=? ORDER BY revision", (record_id,)):
                    entry = dict(row)
                    size += len(canonical(entry).encode())
                    if size > MAX_BYTES - 65536:
                        raise Refusal("ARCHIVE_BATCH_TOO_LARGE")
                    data["revisions"].append(entry)
                data["sources"].extend(
                    dict(row)
                    for row in db.execute("SELECT * FROM sources WHERE record_id=? ORDER BY source_id", (record_id,))
                )
            raw = canonical(data).encode()
            if len(raw) > MAX_BYTES:
                raise Refusal("ARCHIVE_BATCH_TOO_LARGE")
            db.execute("UPDATE archive_batches SET state='SNAPSHOTTED' WHERE id=?", (batch_id,))
        # Never overwrite an existing file or a different snapshot.
        if target.exists():
            if decode(target, key) != data:
                raise Refusal("ARCHIVE_TARGET_CONFLICT")
        else:
            atomic_new(target, Fernet(key).encrypt(raw))
        envelope_hash = hashlib.sha256(target.read_bytes()).hexdigest()
        with self.store.transaction() as db:
            db.execute(
                "UPDATE archive_batches SET state='EXPORTED',envelope_hash=? WHERE id=?", (envelope_hash, batch_id)
            )
        return {"batch_id": batch_id, "state": "EXPORTED", "envelope_hash": envelope_hash}

    def accept_verification(self, batch_id, receipts, *, trusted_vaults, origin_host):
        """trusted_vaults maps an independently audited physical failure domain
        to (host_id, Ed25519 public key). Distinct drive letters are not evidence.
        """
        with self.store.transaction() as db:
            batch = db.execute("SELECT * FROM archive_batches WHERE id=?", (batch_id,)).fetchone()
            if not batch or batch["state"] not in {"EXPORTED", "VERIFIED_OFF_HOST"}:
                raise Refusal("ARCHIVE_STATE")
            domains = set()
            for receipt in receipts:
                payload = receipt["payload"]
                domain = payload["failure_domain"]
                trusted = trusted_vaults.get(domain)
                if not trusted or trusted[0] != payload["host_id"] or payload["host_id"] == origin_host:
                    raise Refusal("UNTRUSTED_VAULT")
                Ed25519PublicKey.from_public_bytes(trusted[1]).verify(
                    base64.b64decode(receipt["signature"], validate=True), canonical(payload).encode()
                )
                if (
                    payload["version"] != 1
                    or payload["batch_id"] != batch_id
                    or payload["character_id"] != self.store.character_id
                    or payload["envelope_hash"] != batch["envelope_hash"]
                    or payload["restore_verified"] is not True
                    or not 0 <= time.time() - payload["verified_at"] <= 7 * 86400
                ):
                    raise Refusal("VAULT_VERIFICATION_MISMATCH")
                domains.add(domain)
            if len(domains) < 2:
                raise Refusal("INDEPENDENT_RECOVERY_COPY_REQUIRED")
            db.execute(
                "UPDATE archive_batches SET state='VERIFIED_OFF_HOST',verification=? WHERE id=?",
                (canonical(receipts), batch_id),
            )

    def detach(self, batch_id):
        with self.store.transaction() as db:
            batch = db.execute("SELECT * FROM archive_batches WHERE id=?", (batch_id,)).fetchone()
            if batch and batch["state"] == "DETACHED_ONLINE":
                return
            if not batch or batch["state"] != "VERIFIED_OFF_HOST":
                raise Refusal("OFF_HOST_VERIFICATION_REQUIRED")
            expected = json.loads(batch["plan"])["records"]
            if self._eligible(db, [r["id"] for r in expected]) != expected:
                raise Refusal("ARCHIVE_REVISION_CHANGED")
            for row in expected:
                db.execute("UPDATE records SET status='ARCHIVED_OFFLINE',batch_id=? WHERE id=?", (batch_id, row["id"]))
                db.execute("DELETE FROM search WHERE record_id=?", (row["id"],))
                db.execute(
                    "INSERT INTO changes(record_id,revision,operation,received_at) VALUES(?,?,'ARCHIVE',?)",
                    (row["id"], row["revision"], time.time()),
                )
            db.execute("UPDATE archive_batches SET state='DETACHED_ONLINE' WHERE id=?", (batch_id,))

    def evict(self, batch_id):
        # No caller can accidentally turn partial detachment into an erasure claim.
        raise Refusal("LOCAL_EVICTION_BLOCKED_PENDING_DEPLOYMENT_COPY_INVENTORY")
