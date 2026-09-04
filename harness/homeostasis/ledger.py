"""Durable, bounded-cache command ledger for the homeostasis service."""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from pathlib import Path
from typing import Any


class CommandLedger:
    """Store idempotency results in SQLite while caching only a bounded tail.

    Older deployments persisted one growing JSON object after every command.
    The legacy file is imported transactionally on first use and intentionally
    retained as a rollback artifact, but it is no longer rewritten.
    """

    _MIGRATION_KEY = "legacy_json_v1_imported"

    def __init__(
        self,
        state_dir: Path | str,
        *,
        cache_size: int = 512,
        legacy_filename: str = "command-ledger.json",
        snapshot_filename: str = "snapshot.json",
        filename: str = "command-ledger.sqlite3",
    ) -> None:
        if cache_size < 1:
            raise ValueError("cache_size must be positive")
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.state_dir / filename
        self.legacy_path = self.state_dir / legacy_filename
        self.snapshot_path = self.state_dir / snapshot_filename
        self.cache_size = cache_size
        self._cache: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._initialise()
        self._warm_cache()

    @property
    def cached_entries(self) -> int:
        return len(self._cache)

    def get(self, key: str) -> dict[str, Any] | None:
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
            return cached

        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT request_hash, result_json FROM command_results WHERE command_key = ?",
                (key,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        entry = {"request_hash": str(row[0]), "result": json.loads(str(row[1]))}
        self._remember(key, entry)
        return entry

    def load_snapshot(self) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT snapshot_json FROM homeostasis_state WHERE singleton = 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        return payload if isinstance(payload, dict) else None

    def commit(
        self,
        key: str,
        *,
        request_hash: str,
        result: dict[str, Any],
        snapshot: dict[str, Any],
    ) -> None:
        encoded = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        encoded_snapshot = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        revision = int(snapshot["revision"])
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO command_results(command_key, request_hash, result_json)
                    VALUES (?, ?, ?)
                    ON CONFLICT(command_key) DO UPDATE SET
                        request_hash = excluded.request_hash,
                        result_json = excluded.result_json
                    """,
                    (key, request_hash, encoded),
                )
                connection.execute(
                    """
                    INSERT INTO homeostasis_state(singleton, revision, snapshot_json)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        revision = excluded.revision,
                        snapshot_json = excluded.snapshot_json
                    """,
                    (revision, encoded_snapshot),
                )
        finally:
            connection.close()
        self._remember(key, {"request_hash": request_hash, "result": result})

    def save_snapshot(self, snapshot: dict[str, Any]) -> None:
        encoded = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
        revision = int(snapshot["revision"])
        connection = self._connect()
        try:
            with connection:
                connection.execute(
                    """
                    INSERT INTO homeostasis_state(singleton, revision, snapshot_json)
                    VALUES (1, ?, ?)
                    ON CONFLICT(singleton) DO UPDATE SET
                        revision = excluded.revision,
                        snapshot_json = excluded.snapshot_json
                    """,
                    (revision, encoded),
                )
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.path, timeout=30.0)

    def _initialise(self) -> None:
        connection = self._connect()
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=FULL")
            with connection:
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS command_results (
                        command_key TEXT PRIMARY KEY,
                        request_hash TEXT NOT NULL,
                        result_json TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS metadata (
                        key TEXT PRIMARY KEY,
                        value TEXT NOT NULL
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS homeostasis_state (
                        singleton INTEGER PRIMARY KEY CHECK (singleton = 1),
                        revision INTEGER NOT NULL,
                        snapshot_json TEXT NOT NULL
                    )
                    """
                )
            self._migrate_legacy_json(connection)
        finally:
            connection.close()

    def _migrate_legacy_json(self, connection: sqlite3.Connection) -> None:
        migrated = connection.execute(
            "SELECT 1 FROM metadata WHERE key = ?",
            (self._MIGRATION_KEY,),
        ).fetchone()
        if migrated is not None:
            return

        entries: list[tuple[str, str, str]] = []
        if self.legacy_path.is_file():
            try:
                payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = {}
            if isinstance(payload, dict):
                for command_key, raw_entry in payload.items():
                    if not isinstance(raw_entry, dict):
                        continue
                    request_hash = raw_entry.get("request_hash")
                    result = raw_entry.get("result")
                    if not isinstance(request_hash, str) or not isinstance(result, dict):
                        continue
                    entries.append(
                        (
                            str(command_key),
                            request_hash,
                            json.dumps(result, ensure_ascii=False, separators=(",", ":")),
                        )
                    )

        snapshot: dict[str, Any] | None = None
        if self.snapshot_path.is_file():
            try:
                raw_snapshot = json.loads(self.snapshot_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw_snapshot = None
            if isinstance(raw_snapshot, dict) and isinstance(raw_snapshot.get("revision"), int):
                snapshot = raw_snapshot

        with connection:
            if entries:
                connection.executemany(
                    """
                    INSERT OR IGNORE INTO command_results(command_key, request_hash, result_json)
                    VALUES (?, ?, ?)
                    """,
                    entries,
                )
            if snapshot is not None:
                connection.execute(
                    """
                    INSERT OR IGNORE INTO homeostasis_state(singleton, revision, snapshot_json)
                    VALUES (1, ?, ?)
                    """,
                    (
                        int(snapshot["revision"]),
                        json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
                    ),
                )
            connection.execute(
                "INSERT OR REPLACE INTO metadata(key, value) VALUES (?, ?)",
                (self._MIGRATION_KEY, "true"),
            )

    def _warm_cache(self) -> None:
        connection = self._connect()
        try:
            rows = connection.execute(
                """
                SELECT command_key, request_hash, result_json
                FROM command_results
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (self.cache_size,),
            ).fetchall()
        finally:
            connection.close()
        for command_key, request_hash, result_json in reversed(rows):
            self._remember(
                str(command_key),
                {"request_hash": str(request_hash), "result": json.loads(str(result_json))},
            )

    def _remember(self, key: str, entry: dict[str, Any]) -> None:
        self._cache[key] = entry
        self._cache.move_to_end(key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
