"""Bounded offline archive jobs. Scheduling never runs in normal role startup.

Candidates are explicitly reviewed IDs, not an unconfigured age/LRU guess.
This worker exports only: off-host verification and detachment stay explicit.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
import math
import os
from pathlib import Path
import shutil
import time
import uuid

from cryptography.fernet import InvalidToken

from .archive import MAX_BYTES, ArchiveManager, atomic_new
from .contracts import canonical, digest
from .ownership import Ownership
from .storage import MemoryStore, Refusal

PREFIX = "maintenance.job:"


@dataclass(frozen=True)
class ArchivePolicy:
    min_free_bytes: int
    max_staging_bytes: int
    max_pending_jobs: int = 16
    max_attempts: int = 3
    retry_seconds: int = 60

    def __post_init__(self):
        if any(type(v) is not int for v in asdict(self).values()):
            raise ValueError("integer archive policy required")
        if self.min_free_bytes < 0 or self.max_staging_bytes < MAX_BYTES * 2:
            raise ValueError("invalid storage policy")
        if (
            not 1 <= self.max_pending_jobs <= 32
            or not 1 <= self.max_attempts <= 5
            or not 1 <= self.retry_seconds <= 86400
        ):
            raise ValueError("invalid bounded job policy")


class ArchiveMaintenance:
    def __init__(self, manager: ArchiveManager, root: Path, policy: ArchivePolicy, *, clock=time.time):
        if root.is_symlink() or any(p.is_symlink() for p in root.parents):
            raise Refusal("MAINTENANCE_ROOT_SYMLINK")
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if (root / ".owner").is_symlink():
            raise Refusal("MAINTENANCE_OWNER_SYMLINK")
        self.manager, self.root, self.policy, self.clock = manager, root.resolve(), policy, clock
        self.owner = Ownership(self.root / ".owner")
        try:
            db = manager.store.db
            identity = db.execute("SELECT value FROM metadata WHERE key='database_id'").fetchone()[0]
            expected = {"format": "neuralcloud-archive-maintenance-v1", "database_id": identity}
            marker = self.root / "owner.json"
            if marker.exists():
                if marker.is_symlink() or json.loads(marker.read_text()) != expected:
                    raise Refusal("MAINTENANCE_ROOT_OWNER_MISMATCH")
            else:
                if any(p.name != ".owner" for p in self.root.iterdir()):
                    raise Refusal("MAINTENANCE_ROOT_NOT_EMPTY")
                atomic_new(marker, canonical(expected).encode())
            with manager.store.transaction() as db:
                old = db.execute("SELECT value FROM metadata WHERE key='maintenance.root'").fetchone()
                if old and old[0] != str(self.root):
                    raise Refusal("MAINTENANCE_ROOT_RELOCATION_REQUIRES_REVIEW")
                db.execute("INSERT OR IGNORE INTO metadata VALUES('maintenance.root',?)", (str(self.root),))
        except BaseException:
            self.owner.close()
            raise

    def close(self):
        self.owner.close()

    def jobs(self):
        if self.owner.file.closed:
            raise Refusal("MAINTENANCE_OWNER_CLOSED")
        rows = self.manager.store.db.execute(
            "SELECT value FROM metadata WHERE key>=? AND key<? LIMIT 257", (PREFIX, PREFIX + "\uffff")
        ).fetchall()
        if len(rows) > 256:
            raise Refusal("MAINTENANCE_AUDIT_LIMIT")
        return [json.loads(row[0]) for row in rows]

    def _save(self, job):
        with self.manager.store.transaction() as db:
            db.execute("INSERT OR REPLACE INTO metadata VALUES(?,?)", (PREFIX + job["job_id"], canonical(job)))

    def _now(self):
        if self.owner.file.closed:
            raise Refusal("MAINTENANCE_OWNER_CLOSED")
        value = self.clock()
        if not math.isfinite(value) or value < 0:
            raise Refusal("MAINTENANCE_CLOCK_INVALID")
        with self.manager.store.transaction() as db:
            row = db.execute("SELECT value FROM metadata WHERE key='maintenance.clock'").fetchone()
            value = max(value, float(row[0]) if row else 0)
            db.execute("INSERT OR REPLACE INTO metadata VALUES('maintenance.clock',?)", (str(value),))
        return value

    def enqueue(self, record_ids, *, job_id=None):
        ids = sorted(set(record_ids))
        if not 0 < len(ids) <= 500 or any(not isinstance(i, str) or not 0 < len(i) <= 256 for i in ids):
            raise Refusal("ARCHIVE_BATCH_BOUNDS")
        identifier = str(uuid.UUID(job_id)) if job_id is not None else str(uuid.uuid4())
        jobs = self.jobs()
        for job in jobs:
            if job["job_id"] == identifier:
                if job["record_ids"] != ids:
                    raise Refusal("MAINTENANCE_JOB_ID_CONFLICT")
                return job
        if len(jobs) >= 256 or sum(j["state"] != "EXPORTED" for j in jobs) >= self.policy.max_pending_jobs:
            raise Refusal("MAINTENANCE_QUEUE_FULL")
        # Freeze eligibility/revisions now, with the same durable ID on reentry.
        batch_id = self.manager.plan(ids, batch_id=identifier)
        job = {
            "job_id": identifier,
            "batch_id": batch_id,
            "record_ids": ids,
            "state": "QUEUED",
            "attempts": 0,
            "next_attempt": 0,
            "created_at": self._now(),
            "reason": None,
            "policy_hash": digest(asdict(self.policy)),
        }
        self._save(job)
        return job

    def _path(self, job):
        path = self.root / (str(uuid.UUID(job["batch_id"])) + ".enc")
        if path.is_symlink() or path.resolve().parent != self.root:
            raise Refusal("MAINTENANCE_PATH_UNSAFE")
        return path

    def capacity(self):
        jobs = self.jobs()
        expected = {".owner", "owner.json"} | {self._path(j).name for j in jobs}
        # Bounded inventory of this worker's flat staging directory, never history hydration.
        with os.scandir(self.root) as files:
            for number, entry in enumerate(files):
                if number >= 258 or entry.name not in expected or entry.is_symlink() or not entry.is_file():
                    raise Refusal("MAINTENANCE_STAGING_INVENTORY_REQUIRED")
        staging = sum(self._path(j).stat().st_size for j in jobs if self._path(j).exists())
        path = self.manager.store.path
        wal = Path(str(path) + "-wal")
        db = self.manager.store.db
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        report = {
            "database_bytes": path.stat().st_size,
            "wal_bytes": wal.stat().st_size if wal.exists() else 0,
            "reusable_database_bytes": db.execute("PRAGMA freelist_count").fetchone()[0] * page_size,
            "staging_bytes": staging,
            "staging_free_bytes": shutil.disk_usage(self.root).free,
            "database_free_bytes": shutil.disk_usage(path.parent).free,
            "pending_jobs": sum(j["state"] != "EXPORTED" for j in jobs),
            "policy": asdict(self.policy),
            "local_eviction": "BLOCKED",
        }
        report["optional_collection_should_pause"] = (
            min(report["staging_free_bytes"], report["database_free_bytes"])
            < self.policy.min_free_bytes + MAX_BYTES * 2
            or staging + MAX_BYTES * 2 > self.policy.max_staging_bytes
        )
        return report

    def run_once(self, key: bytes, *, limit=1):
        if type(limit) is not int or not 1 <= limit <= 4:
            raise Refusal("MAINTENANCE_TICK_BOUNDS")
        moment, results = self._now(), []
        for job in sorted(self.jobs(), key=lambda j: (j["created_at"], j["job_id"])):
            if len(results) >= limit:
                break
            if job["state"] in {"EXPORTED", "BLOCKED"} or job["next_attempt"] > moment:
                continue
            if job["policy_hash"] != digest(asdict(self.policy)):
                job.update(state="BLOCKED", reason="POLICY_CHANGED_REVIEW_REQUIRED")
            elif job["attempts"] >= self.policy.max_attempts:
                job.update(state="BLOCKED", reason="EXPORT_ATTEMPT_LIMIT_REACHED")
            elif self.capacity()["optional_collection_should_pause"]:
                job.update(state="PAUSED_CAPACITY", reason="CAPACITY_RESERVE_REQUIRED")
            else:
                job["attempts"] += 1
                self._save(job)
                try:
                    receipt = self.manager.export(job["batch_id"], self._path(job), key)
                    job.update(state="EXPORTED", reason=None, receipt=receipt)
                except Refusal as exc:
                    job.update(state="BLOCKED", reason=exc.code)
                except OSError:
                    job.update(
                        state="BLOCKED" if job["attempts"] >= self.policy.max_attempts else "RETRY",
                        reason="EXPORT_IO_ERROR",
                        next_attempt=moment + self.policy.retry_seconds,
                    )
                except (ValueError, TypeError, InvalidToken):
                    job.update(state="BLOCKED", reason="EXPORT_INVALID_KEY_OR_FILE")
            self._save(job)
            results.append({"job_id": job["job_id"], "state": job["state"], "reason": job["reason"]})
        return results

    def retry(self, job_id, *, expected_hash, evidence):
        job = next((j for j in self.jobs() if j["job_id"] == job_id), None)
        if not job or digest(job) != expected_hash or job["state"] != "BLOCKED":
            raise Refusal("MAINTENANCE_RETRY_CONFLICT")
        if not isinstance(evidence, str) or not 1 <= len(evidence) <= 1024:
            raise Refusal("MAINTENANCE_RETRY_EVIDENCE_REQUIRED")
        if len(job.get("retries", [])) >= 8:
            raise Refusal("MAINTENANCE_RETRY_AUDIT_LIMIT")
        job.setdefault("retries", []).append({"prior_hash": expected_hash, "evidence": evidence, "at": self._now()})
        job.update(state="QUEUED", attempts=0, next_attempt=0, reason=None, policy_hash=digest(asdict(self.policy)))
        self._save(job)
        return job

    def checkpoint(self):
        if self.owner.file.closed:
            raise Refusal("MAINTENANCE_OWNER_CLOSED")
        row = self.manager.store.db.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone()
        return {"busy": row[0], "wal_frames": row[1], "checkpointed_frames": row[2], "vacuum_run": False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=["enqueue", "tick", "status", "checkpoint", "retry"])
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--character", required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--policy", type=Path, required=True)
    parser.add_argument("--request", type=Path)
    parser.add_argument("--key-file", type=Path)
    args = parser.parse_args()
    policy = ArchivePolicy(**json.loads(args.policy.read_text(encoding="utf-8")))
    store = MemoryStore(args.database, args.character)
    worker = None
    try:
        worker = ArchiveMaintenance(ArchiveManager(store), args.root, policy)
        if args.command in {"enqueue", "retry"}:
            if not args.request or args.request.stat().st_size > 262144:
                parser.error("bounded --request JSON required")
            request = json.loads(args.request.read_text(encoding="utf-8"))
            result = getattr(worker, args.command)(**request)
        elif args.command == "tick":
            if not args.key_file or args.key_file.stat().st_size > 1024:
                parser.error("protected --key-file required")
            result = worker.run_once(args.key_file.read_bytes().strip())
        elif args.command == "checkpoint":
            result = worker.checkpoint()
        else:
            result = {"capacity": worker.capacity(), "jobs": worker.jobs()}
        print(json.dumps(result))
    finally:
        if worker:
            worker.close()
        store.close()


if __name__ == "__main__":
    main()
