"""Append-only SQLite board with transaction-bound, independently readable receipts."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import threading

from harness.embodiment.board_contracts import (
    BoardAppendRequest, BoardEntry, BoardEvidenceRequest, BoardReadRequest, BoardReadResult,
    BoardReceipt, BoardReconcileRequest, BoardReconcileResult, BoardScope,
)


class BoardStoreError(RuntimeError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def utc_now() -> datetime:
    return datetime.now(UTC)


def text_digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def evidence_reference(scope, *, entry_id, decision_id, title, text_sha256, source_event_ids, created_at):
    digest = text_digest(json.dumps({
        "scope": scope.model_dump(), "entry_id": entry_id, "decision_id": decision_id,
        "title": title, "text_sha256": text_sha256, "source_event_ids": source_event_ids,
        "created_at": created_at,
    }, sort_keys=True, ensure_ascii=False, separators=(",", ":")))
    return "board-evidence://sha256/" + digest


class BoardStore:
    """One immutable scope per file; historical writes never become new actions."""

    def __init__(self, path: str | Path, scope: BoardScope, *, clock: Callable[[], datetime] = utc_now):
        self.path = Path(path).resolve()
        self.scope = BoardScope.model_validate(scope.model_dump())
        self._clock = clock
        self._lock = threading.RLock()
        self._closed = False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        if os.name != "nt":
            os.chmod(self.path.parent, 0o700)
        self._db = sqlite3.connect(self.path, timeout=2, check_same_thread=False, isolation_level=None)
        self._db.row_factory = sqlite3.Row
        try:
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._db.executescript("""
                CREATE TABLE IF NOT EXISTS board_identity (
                    singleton INTEGER PRIMARY KEY CHECK(singleton=1), scope_json TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS board_entries (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    entry_id TEXT NOT NULL UNIQUE,
                    decision_id TEXT NOT NULL UNIQUE,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL,
                    text_sha256 TEXT NOT NULL,
                    source_event_ids_json TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    evidence_ref TEXT NOT NULL
                );
            """)
            encoded = self.scope.model_dump_json()
            self._db.execute("INSERT OR IGNORE INTO board_identity VALUES (1, ?)", (encoded,))
            identity = self._db.execute("SELECT scope_json FROM board_identity WHERE singleton=1").fetchone()
            if BoardScope.model_validate_json(identity[0]) != self.scope:
                raise BoardStoreError("board_identity_mismatch")
            if os.name != "nt":
                for item in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")):
                    if item.exists():
                        os.chmod(item, 0o600)
        except BaseException:
            self._db.close()
            self._closed = True
            raise

    def close(self):
        with self._lock:
            if not self._closed:
                self._db.close()
                self._closed = True

    def _scope(self, scope: BoardScope):
        if scope != self.scope:
            raise BoardStoreError("scope_not_allowed")
        if self._closed:
            raise BoardStoreError("board_store_unavailable")

    def _request(self, request):
        self._scope(request.scope)
        moment = self._clock()
        if request.requested_at > moment + timedelta(seconds=2):
            raise BoardStoreError("request_time_invalid")
        if moment >= request.deadline:
            raise BoardStoreError("request_expired")

    def healthy(self) -> bool:
        with self._lock:
            if self._closed:
                return False
            try:
                identity = self._db.execute("SELECT scope_json FROM board_identity WHERE singleton=1").fetchone()
                return (
                    identity is not None and BoardScope.model_validate_json(identity[0]) == self.scope
                    and bool(os.access(self.path, os.W_OK)) and bool(os.access(self.path.parent, os.W_OK))
                )
            except (sqlite3.Error, ValueError):
                return False

    def _entry(self, row) -> BoardEntry:
        sources = tuple(json.loads(row["source_event_ids_json"]))
        ref = evidence_reference(self.scope, entry_id=row["entry_id"], decision_id=row["decision_id"],
            title=row["title"], text_sha256=row["text_sha256"], source_event_ids=sources, created_at=row["created_at"])
        if text_digest(row["text"]) != row["text_sha256"] or ref != row["evidence_ref"]:
            raise BoardStoreError("board_evidence_corrupt")
        return BoardEntry(
            receipt=BoardReceipt(scope=self.scope, entry_id=row["entry_id"], decision_id=row["decision_id"],
                text_sha256=row["text_sha256"], created_at=row["created_at"], evidence_ref=row["evidence_ref"],
                sequence=row["sequence"]),
            text=row["text"], title=row["title"], source_event_ids=sources,
        )

    def append(self, request: BoardAppendRequest) -> BoardReceipt:
        request = BoardAppendRequest.model_validate(request.model_dump())
        with self._lock:
            self._request(request)
            if not self.healthy():
                raise BoardStoreError("board_store_unavailable")
            self._db.execute("BEGIN IMMEDIATE")
            try:
                # These two uniqueness constraints also prevent a fresh entry ID
                # from replaying a historical cognition decision after restart.
                rows = self._db.execute(
                    "SELECT * FROM board_entries WHERE entry_id=? OR decision_id=?",
                    (request.entry_id, request.decision_id),
                ).fetchall()
                if rows:
                    entry = self._entry(rows[0])
                    if (len(rows) != 1 or entry.receipt.entry_id != request.entry_id
                            or entry.receipt.decision_id != request.decision_id or entry.text != request.text
                            or entry.title != request.title or entry.source_event_ids != request.source_event_ids):
                        raise BoardStoreError("board_idempotency_conflict")
                    self._db.execute("COMMIT")
                    return entry.receipt
                self._request(request)  # Include waiting for the SQLite write lock.
                now = self._clock()
                digest = text_digest(request.text)
                ref = evidence_reference(self.scope, entry_id=request.entry_id, decision_id=request.decision_id,
                    title=request.title, text_sha256=digest, source_event_ids=request.source_event_ids, created_at=now.isoformat())
                self._db.execute(
                    "INSERT INTO board_entries(entry_id,decision_id,title,text,text_sha256,source_event_ids_json,created_at,evidence_ref) VALUES (?,?,?,?,?,?,?,?)",
                    (request.entry_id, request.decision_id, request.title, request.text, digest,
                     json.dumps(request.source_event_ids, ensure_ascii=False), now.isoformat(), ref),
                )
                row = self._db.execute("SELECT * FROM board_entries WHERE entry_id=?", (request.entry_id,)).fetchone()
                self._db.execute("COMMIT")  # FULL WAL sync completes before exposing the receipt.
                return self._entry(row).receipt
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def read(self, request: BoardReadRequest) -> BoardReadResult:
        request = BoardReadRequest.model_validate(request.model_dump())
        with self._lock:
            self._request(request)
            ordering = "DESC" if request.latest else "ASC"
            rows = self._db.execute(f"SELECT * FROM board_entries WHERE sequence>? ORDER BY sequence {ordering} LIMIT ?",
                                    (request.after_sequence, request.limit)).fetchall()
            if request.latest:
                rows.reverse()
            self._request(request)
            return BoardReadResult(scope=self.scope, entries=tuple(self._entry(row) for row in rows), observed_at=self._clock())

    def reconcile(self, request: BoardReconcileRequest) -> BoardReconcileResult:
        request = BoardReconcileRequest.model_validate(request.model_dump())
        with self._lock:
            self._request(request)
            rows = self._db.execute("SELECT * FROM board_entries WHERE entry_id=? OR decision_id=?",
                                    (request.entry_id, request.decision_id)).fetchall()
            receipt = None
            if rows:
                receipt = self._entry(rows[0]).receipt
                if (len(rows) != 1 or receipt.entry_id != request.entry_id or receipt.decision_id != request.decision_id
                        or receipt.text_sha256 != request.text_sha256):
                    raise BoardStoreError("board_idempotency_conflict")
            self._request(request)
            return BoardReconcileResult(scope=self.scope, entry_id=request.entry_id, decision_id=request.decision_id,
                                        status="committed" if receipt else "not_found", receipt=receipt)

    def evidence(self, request: BoardEvidenceRequest) -> BoardEntry:
        request = BoardEvidenceRequest.model_validate(request.model_dump())
        with self._lock:
            self._request(request)
            row = self._db.execute("SELECT * FROM board_entries WHERE entry_id=?", (request.entry_id,)).fetchone()
            if row is None:
                raise BoardStoreError("board_entry_not_found")
            self._request(request)
            return self._entry(row)
