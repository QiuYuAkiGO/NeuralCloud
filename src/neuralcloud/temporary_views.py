"""Scoped, expiring management views; no role routes and no persistent plaintext.

Only generated encrypted view copies may be removed here. Archive envelopes and
local original revisions are never removed. Keys live only in this process.
"""

from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path
import threading
import time
import uuid

from cryptography.fernet import Fernet

from .archive import MAX_BYTES, ArchiveManager, RestoreGrant, atomic_new
from .contracts import canonical, digest
from .ownership import Ownership
from .storage import Refusal

PREFIX = "temporary.view:"


class TemporaryViews:
    def __init__(
        self, manager: ArchiveManager, root: Path, *, clock=time.time, monotonic=time.monotonic, sweep_seconds=5
    ):
        if sweep_seconds is not None and not 0 < sweep_seconds <= 30:
            raise ValueError("bounded expiry sweep interval required")
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise Refusal("VIEW_ROOT_SYMLINK")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (root / ".owner").is_symlink():
            raise Refusal("VIEW_OWNER_SYMLINK")
        self.manager, self.root, self.clock, self.monotonic = manager, root.resolve(), clock, monotonic
        self.lock = threading.RLock()
        self.keys = {}
        self.stop = threading.Event()
        self.thread = None
        self.sweep_error = None
        self.owner = Ownership(self.root / ".owner")
        try:
            self.anchor_wall, self.anchor_mono = clock(), monotonic()
            identity = manager.store.db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
            marker = self.root / "owner.json"
            expected = {"format": "neuralcloud-temporary-views-v1", "database_id": identity}
            if marker.exists():
                if marker.is_symlink() or json.loads(marker.read_text()) != expected:
                    raise Refusal("VIEW_ROOT_OWNER_MISMATCH")
            else:
                if any(p.name != ".owner" for p in self.root.iterdir()):
                    raise Refusal("VIEW_ROOT_NOT_EMPTY")
                atomic_new(marker, canonical(expected).encode())
            with manager.store.transaction() as db:
                old = db.execute("SELECT value FROM metadata WHERE key='temporary.root'").fetchone()
                if old and old[0] != str(self.root):
                    raise Refusal("VIEW_ROOT_RELOCATION_REQUIRES_REVIEW")
                db.execute("INSERT OR IGNORE INTO metadata VALUES('temporary.root',?)", (str(self.root),))
            # Crash recovery never silently renews approval or recovers old keys.
            for entry in self._entries():
                if entry["state"] != "CLOSED":
                    self._revoke(entry, "PROCESS_RESTART")
            if sweep_seconds is not None:

                def reap():
                    while not self.stop.wait(sweep_seconds):
                        try:
                            self.sweep()
                            self.sweep_error = None
                        except Exception as exc:
                            self.sweep_error = type(exc).__name__

                self.thread = threading.Thread(target=reap, name="archive-view-expiry", daemon=True)
                self.thread.start()
        except BaseException:
            self.owner.close()
            raise

    def _entries(self):
        rows = self.manager.store.db.execute(
            "SELECT value FROM metadata WHERE key>=? AND key<? LIMIT 257", (PREFIX, PREFIX + "\uffff")
        ).fetchall()
        if len(rows) > 256:
            raise Refusal("VIEW_AUDIT_MAINTENANCE_REQUIRED")
        return [json.loads(row[0]) for row in rows]

    def _save(self, entry):
        with self.manager.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (PREFIX + entry["view_id"], canonical(entry)))

    def _now(self):
        if self.stop.is_set():
            raise Refusal("VIEW_OWNER_CLOSED")
        value = max(self.clock(), self.anchor_wall + max(0, self.monotonic() - self.anchor_mono))
        if not math.isfinite(value) or value < 0:
            raise Refusal("VIEW_CLOCK_INVALID")
        with self.manager.store.transaction() as db:
            old = db.execute("SELECT value FROM metadata WHERE key='temporary.clock'").fetchone()
            value = max(value, float(old[0]) if old else 0)
            db.execute("INSERT OR REPLACE INTO metadata VALUES('temporary.clock',?)", (str(value),))
        return value

    def _path(self, identifier):
        identifier = str(uuid.UUID(identifier))
        path = self.root / (identifier + ".enc")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise Refusal("VIEW_PATH_UNSAFE")
        return path

    def _revoke(self, entry, reason):
        # Denial/key disposal precede filesystem cleanup, including failed cleanup.
        self.keys.pop(entry["view_id"], None)
        entry.update(state="CLEANUP_PENDING", reason=reason)
        self._save(entry)
        try:
            path = self._path(entry["view_id"])
            if path.exists():
                if path.stat().st_size > MAX_BYTES * 2:
                    raise Refusal("VIEW_FILE_CHANGED")
                if (
                    entry.get("envelope_hash")
                    and hashlib.sha256(path.read_bytes()).hexdigest() != entry["envelope_hash"]
                ):
                    raise Refusal("VIEW_FILE_CHANGED")
                path.unlink()
            entry["state"] = "CLOSED"
            self._save(entry)
        except (OSError, Refusal):
            # Operator can inspect/retry sweep; failure never extends access.
            pass
        return entry["state"]

    def sweep(self):
        with self.lock:
            moment = self._now()
            counts = {"closed": 0, "cleanup_pending": 0, "active": 0}
            for entry in self._entries():
                if entry["state"] == "CLOSED":
                    continue
                if entry["expires_at"] <= moment or entry["view_id"] not in self.keys:
                    state = self._revoke(entry, "EXPIRED_OR_REVOKED")
                    counts["closed" if state == "CLOSED" else "cleanup_pending"] += 1
                else:
                    counts["active"] += 1
            return counts

    def open(self, path, key, approval, *, operator_public_key, target_environment):
        with self.lock:
            self.sweep()
            grant, data, _ = self.manager.authorize(
                path,
                key,
                approval,
                operator_public_key=operator_public_key,
                target_environment=target_environment,
                mode="temporary_view",
                now=self._now(),
            )
            entries = self._entries()
            # An approval is consumed once, including expiry, process death and failed writes.
            if any(entry["grant_id"] == grant.grant_id for entry in entries):
                raise Refusal("VIEW_GRANT_ALREADY_USED")
            if len(entries) >= 256 or sum(e["state"] != "CLOSED" for e in entries) >= 8:
                raise Refusal("VIEW_CAPACITY_MAINTENANCE_REQUIRED")
            with self.manager.store.transaction() as db:
                self.manager.check_current(db, grant, data)
            selected = set(grant.record_ids)
            revisions = {r["id"]: r["revision"] for r in data["records"]}
            projection = {
                "records": data["records"],
                "sources": [r for r in data["sources"] if r["record_id"] in selected],
                "payloads": {
                    r["record_id"]: json.loads(r["payload"])
                    for r in data["revisions"]
                    if r["record_id"] in selected and r["revision"] == revisions[r["record_id"]]
                },
            }
            raw = canonical(projection).encode()
            if len(raw) > MAX_BYTES:
                raise Refusal("VIEW_TOO_LARGE")
            identifier, view_key = str(uuid.uuid4()), Fernet.generate_key()
            envelope = Fernet(view_key).encrypt(raw)
            entry = {
                "view_id": identifier,
                "grant_id": grant.grant_id,
                "grant_hash": digest(grant),
                "grant": grant.model_dump(),
                "expires_at": grant.expires_at,
                "state": "PREPARING",
                "envelope_hash": hashlib.sha256(envelope).hexdigest(),
            }
            self._save(entry)
            try:
                atomic_new(self._path(identifier), envelope)
                entry["state"] = "ACTIVE"
                self._save(entry)
                self.keys[identifier] = view_key
            except BaseException:
                self._revoke(entry, "OPEN_FAILED")
                raise
            return {"view_id": identifier, "expires_at": grant.expires_at, "record_count": len(selected)}

    def read(self, identifier: str, record_id: str, *, offset=0, limit=16384):
        with self.lock:
            self.sweep()
            if identifier not in self.keys:
                raise Refusal("VIEW_EXPIRED_OR_CLOSED")
            if type(offset) is not int or type(limit) is not int or offset < 0 or not 1 <= limit <= 16384:
                raise Refusal("VIEW_READ_BOUNDS")
            entry = next(e for e in self._entries() if e["view_id"] == identifier)
            grant = RestoreGrant.model_validate(entry["grant"])
            if record_id not in grant.record_ids:
                raise Refusal("VIEW_SCOPE_MISMATCH")
            try:
                path = self._path(identifier)
                if path.stat().st_size > MAX_BYTES * 2:
                    raise Refusal("VIEW_TOO_LARGE")
                envelope = path.read_bytes()
                if hashlib.sha256(envelope).hexdigest() != entry["envelope_hash"]:
                    raise Refusal("VIEW_FILE_CHANGED")
                data = json.loads(Fernet(self.keys[identifier]).decrypt(envelope))
                # Hold the authority transaction until constructing the response.
                with self.manager.store.transaction() as db:
                    self.manager.check_current(db, grant, data)
                    content = data["payloads"][record_id]["content"]
                    result = {
                        "status": "TEMPORARY_VIEW",
                        "record_id": record_id,
                        "content": content[offset : offset + limit],
                        "offset": offset,
                        "total_characters": len(content),
                        "expires_at": grant.expires_at,
                    }
                if self._now() >= grant.expires_at:
                    raise Refusal("VIEW_EXPIRED_OR_CLOSED")
                return result
            except BaseException:
                self._revoke(entry, "READ_INVALIDATED")
                raise

    def close(self):
        self.stop.set()
        if self.thread:
            self.thread.join(timeout=35)
        with self.lock:
            try:
                for entry in self._entries():
                    if entry["state"] != "CLOSED":
                        self._revoke(entry, "CLOSED_BY_OPERATOR")
            finally:
                self.keys.clear()
                self.owner.close()
