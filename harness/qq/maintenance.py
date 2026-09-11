"""Explicit, audited engineering maintenance; never starts QQ or sends a reply."""
from __future__ import annotations

import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3

from harness.qq.session import QQSessionSettings
from harness.qq.watch_ledger import LegacyStepConflictRejection, WatchLedger, WatchLedgerError
from harness.qq.watcher import WatcherSettings, worker_lock


def _private_file(path: Path, root: Path) -> Path:
    path = Path(path)
    if not path.is_absolute() or not path.is_relative_to(root) or not path.is_file():
        raise WatchLedgerError("existing absolute private file required")
    resolved = path.resolve(strict=True)
    if not resolved.is_relative_to(root.resolve(strict=True)):
        raise WatchLedgerError("private path escapes the configured directory")
    for part in (path, *path.parents):
        if part.is_symlink() or getattr(part.stat(), "st_file_attributes", 0) & 0x400:
            raise WatchLedgerError("private reparse paths are not permitted")
    return resolved


def maintain_legacy_rejection(*, config_path: Path, reservation_id: str, proof_file: Path,
                             legacy_build_hash: str, legacy_evidence_refs: list[str]) -> dict:
    """Acquire the worker's actual lock, verify no receipt, then audit once.

    The proof file comes from the authenticated read-only Desktop client. Its
    local origin is an operator responsibility, not a cryptographic signature.
    No token contents, messages, model calls or GUI primitives are used here.
    """
    root = Path(os.environ["LOCALAPPDATA"]) / "NeuralCloud" / "QQDesktop"
    config_path, proof_file = (_private_file(path, root) for path in (config_path, proof_file))
    if config_path.stat().st_size > 1024 * 1024 or proof_file.stat().st_size > 1024 * 1024:
        raise WatchLedgerError("maintenance input is too large")
    settings = WatcherSettings.model_validate_json(config_path.read_text(encoding="utf-8"))
    session_path = _private_file(settings.session_config_path, root)
    session = QQSessionSettings.model_validate_json(session_path.read_text(encoding="utf-8"))
    _private_file(settings.state_db, root)
    _private_file(settings.agent_token_file, root)  # Identity/path only; never read.
    memory_db = _private_file(session.memory_db, root)
    if settings.group_id not in session.allowed_group_ids or settings.self_member_id != session.self_member_id:
        raise WatchLedgerError("maintenance configuration scope mismatch")
    proof = LegacyStepConflictRejection.model_validate_json(proof_file.read_text(encoding="utf-8"))
    if (proof.journal.body_instance_id != settings.expected_instance_id
            or proof.journal.environment_revision != settings.expected_revision):
        raise WatchLedgerError("maintenance proof belongs to another desktop")
    lock_path = settings.agent_token_file.with_suffix(".watcher.lock")
    if lock_path.exists():
        _private_file(lock_path, root)
    with worker_lock(lock_path):
        ledger = WatchLedger(settings.state_db, group_id=settings.group_id)
        reservation = ledger.get_send(reservation_id)
        with closing(sqlite3.connect(memory_db.as_uri() + "?mode=ro", uri=True, timeout=3)) as connection:
            connection.execute("PRAGMA query_only=ON")
            if connection.execute("SELECT 1 FROM qq_reply_deliveries WHERE decision_id=?", (reservation.decision_id,)).fetchone():
                raise WatchLedgerError("an existing delivery receipt forbids rejection maintenance")
        ended = ledger.maintain_legacy_step_conflict(reservation_id, proof=proof,
            legacy_build_hash=legacy_build_hash, legacy_evidence_refs=legacy_evidence_refs)
    return {"schema_version": "qq-operator-maintenance-result-v1", "status": ended.status,
            "kind": "legacy_step_conflict_rejection", "automatic_recovery": False,
            "reservation_ref": "id:" + hashlib.sha256(reservation_id.encode()).hexdigest()[:24],
            "delivery_receipt_created": False, "reply_requeued": False,
            "draft_cleanup_performed": False, "fully_automatic_e2e_pass": False}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--reservation-id", required=True)
    parser.add_argument("--proof-file", required=True, type=Path)
    parser.add_argument("--legacy-build-hash", required=True)
    parser.add_argument("--legacy-evidence-ref", required=True, action="append")
    args = parser.parse_args(argv)
    try:
        result = maintain_legacy_rejection(config_path=args.config, reservation_id=args.reservation_id,
            proof_file=args.proof_file, legacy_build_hash=args.legacy_build_hash,
            legacy_evidence_refs=args.legacy_evidence_ref)
        print(json.dumps(result, ensure_ascii=True))
        return 0
    except (ValueError, OSError, sqlite3.Error, KeyError):
        print(json.dumps({"status": "refused", "reason": "maintenance_evidence_or_exclusive_access_unavailable"}))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
