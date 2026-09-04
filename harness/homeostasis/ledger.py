"""Durable, bounded-cache command ledger for the homeostasis service."""

from __future__ import annotations

import json
import sqlite3
from collections import OrderedDict
from copy import deepcopy
from pathlib import Path
from typing import Any, Iterator

from harness.contracts.envelope import DomainCommandResult
from harness.homeostasis.models import HomeostaticSnapshot, HomeostaticTransitionTrace


class CommandLedgerError(RuntimeError):
    pass


class CommandLedgerConflictError(CommandLedgerError):
    pass


class StaleSnapshotError(CommandLedgerError):
    pass


class LegacyMigrationError(CommandLedgerError):
    pass


class CommandLedger:
    """Store idempotency results in SQLite while caching only a bounded tail.

    Older deployments persisted one growing JSON object after every command.
    The legacy file is imported transactionally on first use and intentionally
    retained as a rollback artifact, but it is no longer rewritten.
    """

    _MIGRATION_KEY = "legacy_json_v2_imported"
    _TRACE_BACKFILL_KEY = "transition_traces_v1_backfilled"

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
            return deepcopy(cached)

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
        return deepcopy(entry)

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

    def load_engine_state(self) -> dict[str, Any]:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT engine_state_json FROM homeostasis_state WHERE singleton = 1"
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return {}
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise CommandLedgerError("stored engine state must be an object")
        return payload

    def load_trace(self, revision: int) -> dict[str, Any] | None:
        connection = self._connect()
        try:
            row = connection.execute(
                "SELECT trace_json FROM transition_traces WHERE revision = ?",
                (revision,),
            ).fetchone()
        finally:
            connection.close()
        if row is None:
            return None
        payload = json.loads(str(row[0]))
        if not isinstance(payload, dict):
            raise CommandLedgerError("stored transition trace must be an object")
        return payload

    def has_traces(self) -> bool:
        connection = self._connect()
        try:
            return (
                connection.execute("SELECT 1 FROM transition_traces LIMIT 1").fetchone()
                is not None
            )
        finally:
            connection.close()

    def iter_traces(self) -> Iterator[dict[str, Any]]:
        connection = self._connect()
        try:
            rows = connection.execute(
                "SELECT trace_json FROM transition_traces ORDER BY revision"
            )
            for row in rows:
                payload = json.loads(str(row[0]))
                if not isinstance(payload, dict):
                    raise CommandLedgerError("stored transition trace must be an object")
                yield payload
        finally:
            connection.close()

    def commit(
        self,
        key: str,
        *,
        request_hash: str,
        result: dict[str, Any],
        snapshot: dict[str, Any],
        engine_state: dict[str, Any],
        trace: dict[str, Any],
    ) -> None:
        validated_result = DomainCommandResult[dict[str, Any]].model_validate(result)
        validated_snapshot = HomeostaticSnapshot.model_validate(snapshot)
        validated_trace = HomeostaticTransitionTrace.model_validate(trace)
        payload_revision, payload_trace = self._validate_result_payload(validated_result)
        result = validated_result.model_dump(mode="json")
        snapshot = validated_snapshot.model_dump(mode="json")
        trace = validated_trace.model_dump(mode="json")
        encoded = self._encode(result)
        encoded_snapshot = self._encode(snapshot)
        encoded_engine_state = self._encode(engine_state)
        encoded_trace = self._encode(trace)
        revision = validated_snapshot.revision
        if validated_result.request_hash != request_hash:
            raise ValueError("result request hash does not match ledger request hash")
        if payload_revision != revision:
            raise ValueError("result and snapshot revisions must match")
        if validated_trace.revision != revision:
            raise ValueError("trace and snapshot revisions must match")
        if payload_trace != trace:
            raise ValueError("result payload and committed trace must match")
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT request_hash, result_json FROM command_results WHERE command_key = ?",
                (key,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO command_results(command_key, request_hash, result_json)
                    VALUES (?, ?, ?)
                    """,
                    (key, request_hash, encoded),
                )
            elif str(existing[0]) != request_hash or json.loads(str(existing[1])) != result:
                raise CommandLedgerConflictError(
                    "idempotency key already contains a different request or result"
                )
            self._write_trace(
                connection,
                revision=revision,
                encoded_trace=encoded_trace,
            )
            self._write_snapshot(
                connection,
                revision=revision,
                encoded_snapshot=encoded_snapshot,
                encoded_engine_state=encoded_engine_state,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
        self._remember(key, {"request_hash": request_hash, "result": result})

    def save_snapshot(
        self,
        snapshot: dict[str, Any],
        *,
        engine_state: dict[str, Any],
    ) -> None:
        encoded = self._encode(snapshot)
        encoded_engine_state = self._encode(engine_state)
        revision = int(snapshot["revision"])
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            self._write_snapshot(
                connection,
                revision=revision,
                encoded_snapshot=encoded,
                encoded_engine_state=encoded_engine_state,
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _write_snapshot(
        connection: sqlite3.Connection,
        *,
        revision: int,
        encoded_snapshot: str,
        encoded_engine_state: str,
    ) -> None:
        existing = connection.execute(
            """
            SELECT revision, snapshot_json, engine_state_json
            FROM homeostasis_state
            WHERE singleton = 1
            """
        ).fetchone()
        if existing is not None:
            current_revision = int(existing[0])
            if current_revision > revision:
                raise StaleSnapshotError(
                    f"cannot replace revision {current_revision} with stale revision {revision}"
                )
            if current_revision == revision:
                if (
                    json.loads(str(existing[1])) != json.loads(encoded_snapshot)
                    or json.loads(str(existing[2])) != json.loads(encoded_engine_state)
                ):
                    raise CommandLedgerConflictError(
                        f"revision {revision} already contains a different snapshot"
                    )
                return
            connection.execute(
                """
                UPDATE homeostasis_state
                SET revision = ?, snapshot_json = ?, engine_state_json = ?
                WHERE singleton = 1
                """,
                (revision, encoded_snapshot, encoded_engine_state),
            )
            return
        connection.execute(
            """
            INSERT INTO homeostasis_state(
                singleton, revision, snapshot_json, engine_state_json
            )
            VALUES (1, ?, ?, ?)
            """,
            (revision, encoded_snapshot, encoded_engine_state),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30.0)
        connection.execute("PRAGMA busy_timeout=30000")
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    @staticmethod
    def _encode(payload: dict[str, Any]) -> str:
        return json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

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
                        snapshot_json TEXT NOT NULL,
                        engine_state_json TEXT NOT NULL DEFAULT '{}'
                    )
                    """
                )
                connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS transition_traces (
                        revision INTEGER PRIMARY KEY,
                        trace_json TEXT NOT NULL
                    )
                    """
                )
                columns = {
                    str(row[1])
                    for row in connection.execute(
                        "PRAGMA table_info(homeostasis_state)"
                    ).fetchall()
                }
                if "engine_state_json" not in columns:
                    connection.execute(
                        """
                        ALTER TABLE homeostasis_state
                        ADD COLUMN engine_state_json TEXT NOT NULL DEFAULT '{}'
                        """
                    )
                self._backfill_transition_traces(connection)
            self._migrate_legacy_json(connection)
        finally:
            connection.close()

    def _backfill_transition_traces(self, connection: sqlite3.Connection) -> None:
        completed = connection.execute(
            "SELECT 1 FROM metadata WHERE key = ?",
            (self._TRACE_BACKFILL_KEY,),
        ).fetchone()
        if completed is not None:
            return
        rows = connection.execute(
            "SELECT command_key, result_json FROM command_results ORDER BY rowid"
        )
        for command_key, result_json in rows:
            try:
                result = DomainCommandResult[dict[str, Any]].model_validate_json(
                    str(result_json)
                )
                revision, trace = self._validate_result_payload(result)
            except Exception as exc:
                raise CommandLedgerError(
                    f"stored command result is invalid for key {command_key!r}"
                ) from exc
            if revision is not None and trace is not None:
                self._write_trace(
                    connection,
                    revision=revision,
                    encoded_trace=self._encode(trace),
                )
        connection.execute(
            "INSERT INTO metadata(key, value) VALUES (?, ?)",
            (self._TRACE_BACKFILL_KEY, "true"),
        )

    def _migrate_legacy_json(self, connection: sqlite3.Connection) -> None:
        migrated = connection.execute(
            "SELECT 1 FROM metadata WHERE key = ?",
            (self._MIGRATION_KEY,),
        ).fetchone()
        if migrated is not None:
            return

        entries: list[tuple[str, str, str, int | None, str | None]] = []
        if self.legacy_path.is_file():
            try:
                payload = json.loads(self.legacy_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise LegacyMigrationError(
                    f"cannot read legacy command ledger: {self.legacy_path}"
                ) from exc
            if not isinstance(payload, dict):
                raise LegacyMigrationError("legacy command ledger root must be an object")
            for command_key, raw_entry in payload.items():
                if not isinstance(command_key, str) or not isinstance(raw_entry, dict):
                    raise LegacyMigrationError("legacy command ledger contains an invalid entry")
                request_hash = raw_entry.get("request_hash")
                result = raw_entry.get("result")
                if not isinstance(request_hash, str) or not isinstance(result, dict):
                    raise LegacyMigrationError("legacy command ledger entry is incomplete")
                try:
                    validated_result = DomainCommandResult[dict[str, Any]].model_validate(result)
                except Exception as exc:
                    raise LegacyMigrationError(
                        f"legacy command result is invalid for key {command_key!r}"
                    ) from exc
                if validated_result.request_hash != request_hash:
                    raise LegacyMigrationError(
                        f"legacy request hash mismatch for key {command_key!r}"
                    )
                try:
                    committed_revision, trace = self._validate_result_payload(
                        validated_result
                    )
                except Exception as exc:
                    raise LegacyMigrationError(
                        f"legacy command result payload is invalid for key {command_key!r}"
                    ) from exc
                entries.append(
                    (
                        command_key,
                        request_hash,
                        self._encode(validated_result.model_dump(mode="json")),
                        committed_revision,
                        self._encode(trace) if trace is not None else None,
                    )
                )

        snapshot: dict[str, Any] | None = None
        if self.snapshot_path.is_file():
            try:
                validated_snapshot = HomeostaticSnapshot.model_validate_json(
                    self.snapshot_path.read_text(encoding="utf-8")
                )
            except Exception as exc:
                raise LegacyMigrationError(
                    f"cannot validate legacy snapshot: {self.snapshot_path}"
                ) from exc
            snapshot = validated_snapshot.model_dump(mode="json")

        connection.execute("BEGIN IMMEDIATE")
        try:
            current_state = connection.execute(
                """
                SELECT revision, snapshot_json
                FROM homeostasis_state
                WHERE singleton = 1
                """
            ).fetchone()
            if entries and snapshot is None and current_state is None:
                raise LegacyMigrationError(
                    "legacy command results exist without a recoverable snapshot"
                )
            authoritative_revision = max(
                int(current_state[0]) if current_state is not None else -1,
                int(snapshot["revision"]) if snapshot is not None else -1,
            )
            ahead = [
                revision
                for _, _, _, revision, _ in entries
                if revision is not None and revision > authoritative_revision
            ]
            if ahead:
                raise LegacyMigrationError(
                    "legacy command result revision is ahead of the authoritative snapshot"
                )
            for command_key, request_hash, result_json, revision, trace_json in entries:
                existing = connection.execute(
                    "SELECT request_hash, result_json FROM command_results WHERE command_key = ?",
                    (command_key,),
                ).fetchone()
                if existing is None:
                    connection.execute(
                        """
                        INSERT INTO command_results(command_key, request_hash, result_json)
                        VALUES (?, ?, ?)
                        """,
                        (command_key, request_hash, result_json),
                    )
                elif (
                    str(existing[0]) != request_hash
                    or json.loads(str(existing[1])) != json.loads(result_json)
                ):
                    raise LegacyMigrationError(
                        f"SQLite ledger conflicts with legacy key {command_key!r}"
                    )
                if revision is not None and trace_json is not None:
                    try:
                        self._write_trace(
                            connection,
                            revision=revision,
                            encoded_trace=trace_json,
                        )
                    except CommandLedgerConflictError as exc:
                        raise LegacyMigrationError(
                            f"SQLite trace conflicts with legacy revision {revision}"
                        ) from exc
            if snapshot is not None:
                snapshot_revision = int(snapshot["revision"])
                if current_state is None or int(current_state[0]) < snapshot_revision:
                    self._write_snapshot(
                        connection,
                        revision=snapshot_revision,
                        encoded_snapshot=self._encode(snapshot),
                        encoded_engine_state=self._encode({}),
                    )
                else:
                    current_snapshot = HomeostaticSnapshot.model_validate_json(
                        str(current_state[1])
                    )
                    if (
                        int(current_state[0]) == snapshot_revision
                        and current_snapshot != HomeostaticSnapshot.model_validate(snapshot)
                    ):
                        raise LegacyMigrationError(
                            f"SQLite snapshot conflicts with legacy revision {snapshot_revision}"
                        )
            connection.execute(
                "INSERT INTO metadata(key, value) VALUES (?, ?)",
                (self._MIGRATION_KEY, "true"),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    @staticmethod
    def _validate_result_payload(
        result: DomainCommandResult[dict[str, Any]],
    ) -> tuple[int | None, dict[str, Any] | None]:
        revision = result.committed_revision
        if revision is None:
            return None, None
        payload = result.result
        if not isinstance(payload, dict):
            raise ValueError("committed command result must contain an object payload")
        payload_revision = payload.get("revision")
        if payload_revision is not None and int(payload_revision) != revision:
            raise ValueError("result payload revision does not match committed revision")
        raw_snapshot = payload.get("snapshot")
        if raw_snapshot is not None:
            snapshot = HomeostaticSnapshot.model_validate(raw_snapshot)
            if snapshot.revision != revision:
                raise ValueError("result snapshot revision does not match committed revision")
        raw_trace = payload.get("trace")
        if raw_trace is None:
            return revision, None
        trace = HomeostaticTransitionTrace.model_validate(raw_trace)
        if trace.revision != revision:
            raise ValueError("result trace revision does not match committed revision")
        return revision, trace.model_dump(mode="json")

    @staticmethod
    def _write_trace(
        connection: sqlite3.Connection,
        *,
        revision: int,
        encoded_trace: str,
    ) -> None:
        existing = connection.execute(
            "SELECT trace_json FROM transition_traces WHERE revision = ?",
            (revision,),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO transition_traces(revision, trace_json) VALUES (?, ?)",
                (revision, encoded_trace),
            )
        elif json.loads(str(existing[0])) != json.loads(encoded_trace):
            raise CommandLedgerConflictError(
                f"revision {revision} already contains a different transition trace"
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
