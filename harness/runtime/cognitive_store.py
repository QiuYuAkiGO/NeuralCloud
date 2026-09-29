"""Incremental, synchronous persistence for a single Cognitive owner.

SQLite is authoritative while enabled. JSON remains a periodic checkpoint and
the compatibility backend; switching back exports the latest committed SQLite
state before marking JSON authoritative. No background write acknowledges a
command before its transaction has committed.
"""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any

from harness.files import replace_path
from harness.qq.telemetry import measure_stage

DELETED = object()
_MAPS = ("request_hashes", "ledger", "prepared", "research_traces", "sessions", "production_affect")
_EMOTION_MAPS = ("sources", "windows", "reservations")


class TrackedDict(dict):
    """Track replaced/deleted entries, without inspecting unchanged values.

    Values are owned immutable records. A caller deliberately changing a nested
    mutable value must call ``touch``; normal command paths replace the record.
    Versioned acknowledgement cannot erase a later write to the same key.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._version = 0
        self._dirty: dict[Any, int] = {}
        for key in self:
            self.touch(key)

    def touch(self, key):
        self._version += 1
        self._dirty[key] = self._version

    def __setitem__(self, key, value):
        super().__setitem__(key, value)
        self.touch(key)

    def __delitem__(self, key):
        super().__delitem__(key)
        self.touch(key)

    def update(self, *args, **kwargs):
        for key, value in dict(*args, **kwargs).items():
            self[key] = value

    def setdefault(self, key, default=None):
        if key not in self:
            self[key] = default
        return self[key]

    def pop(self, key, *default):
        if key not in self:
            if default:
                return default[0]
            raise KeyError(key)
        value = self[key]
        del self[key]
        return value

    def popitem(self):
        key, value = super().popitem()
        self.touch(key)
        return key, value

    def clear(self):
        keys = tuple(self)
        super().clear()
        for key in keys:
            self.touch(key)

    def __ior__(self, other):
        self.update(other)
        return self

    def dirty_versions(self):
        return dict(self._dirty)

    def acknowledge(self, versions):
        for key, version in versions.items():
            if self._dirty.get(key) == version:
                del self._dirty[key]


def write_checkpoint(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{os.urandom(6).hex()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            with measure_stage("persistence.serialize"):
                json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
            with measure_stage("persistence.fsync"):
                handle.flush()
                os.fsync(handle.fileno())
        with measure_stage("persistence.replace"):
            replace_path(temporary, path)
        if os.name == "posix":
            # The SQLite -> JSON authority handoff must not commit before the
            # replacement directory entry is durable, including after power loss.
            with measure_stage("persistence.fsync"):
                directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(directory)
                finally:
                    os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def _rows(payload: dict) -> dict[str, dict[str, Any]]:
    rows = {name: dict(payload.get(name) or {}) for name in _MAPS}
    rows["owner_meta"] = {"state": {key: value for key, value in payload.items()
                                    if key not in {*_MAPS, "emotion"}}}
    emotion = payload.get("emotion") or {}
    rows["emotion_meta"] = {"state": {key: value for key, value in emotion.items()
                                      if key not in _EMOTION_MAPS}}
    for name in _EMOTION_MAPS:
        rows[f"emotion_{name}"] = dict(emotion.get(name) or {})
    return rows


class CognitiveStateStore:
    CHECKPOINT_INTERVAL = 128

    def __init__(self, json_path: Path):
        self.json_path = json_path
        self.path = json_path.with_suffix(json_path.suffix + ".sqlite3")
        self.last_commit = {"rows": 0, "bytes": 0}
        self.last_checkpoint_error: str | None = None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("CREATE TABLE IF NOT EXISTS records (category TEXT NOT NULL, "
                               "record_key TEXT NOT NULL, payload TEXT NOT NULL, PRIMARY KEY(category, record_key))")
            connection.execute("CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, value TEXT NOT NULL)")
            version = connection.execute("SELECT value FROM metadata WHERE name='schema_version'").fetchone()
            if version is not None and version[0] != "1":
                raise ValueError("unsupported_cognitive_store_schema")
            connection.execute("INSERT OR IGNORE INTO metadata VALUES ('schema_version', '1')")
        # Keep WAL open between writes. Closing the last connection on every
        # command would force a checkpoint each time instead of the intended
        # periodic checkpoint. The anchor executes no mutable statements.
        self._anchor = self._connect()

    def close(self) -> None:
        self._anchor.close()

    def _connect(self):
        connection = sqlite3.connect(self.path, timeout=30, check_same_thread=False)
        connection.execute("PRAGMA synchronous=FULL")
        return connection

    def authority(self) -> str | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT value FROM metadata WHERE name='authority'").fetchone()
            return row[0] if row else None

    def activate(self, *, enabled: bool) -> dict:
        """Load authoritative state, safely migrating in either direction."""
        authority = self.authority()
        if authority == "sqlite":
            payload = self.load()
            if not enabled:
                # Do not mark JSON authoritative until the replacement is durable.
                write_checkpoint(self.json_path, payload)
                with closing(self._connect()) as connection, connection:
                    connection.execute("UPDATE metadata SET value='json' WHERE name='authority'")
            return payload
        if authority not in {None, "json"}:
            raise ValueError("invalid_cognitive_store_authority")
        if authority == "json" and not self.json_path.exists():
            raise ValueError("cognitive_json_authority_missing")
        payload = json.loads(self.json_path.read_text(encoding="utf-8")) if self.json_path.exists() else {}
        if enabled:
            self.commit(_rows(payload), replace=True, authority="sqlite")
        return payload

    def commit(self, changes: dict[str, dict[str, Any]], *, replace: bool = False,
               authority: str | None = None) -> None:
        # Serialize only dirty rows before opening the transaction. Invalid JSON
        # cannot leave a partially written command/reservation in the database.
        with measure_stage("persistence.serialize"):
            encoded = [(category, key, None if value is DELETED else json.dumps(
                value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
                for category, entries in changes.items() for key, value in entries.items()]
        with measure_stage("persistence.sqlite_commit"), closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            if replace:
                connection.execute("DELETE FROM records")
            for category, key, value in encoded:
                if value is None:
                    connection.execute("DELETE FROM records WHERE category=? AND record_key=?", (category, key))
                else:
                    connection.execute("INSERT INTO records VALUES (?, ?, ?) ON CONFLICT(category, record_key) "
                                       "DO UPDATE SET payload=excluded.payload", (category, key, value))
            connection.execute("INSERT INTO metadata VALUES ('generation', '1') ON CONFLICT(name) "
                               "DO UPDATE SET value=CAST(CAST(value AS INTEGER)+1 AS TEXT)")
            if authority is not None:
                connection.execute("INSERT INTO metadata VALUES ('authority', ?) ON CONFLICT(name) "
                                   "DO UPDATE SET value=excluded.value", (authority,))
        self.last_commit = {"rows": len(encoded), "bytes": sum(len(value.encode("utf-8"))
                            for _, _, value in encoded if value is not None)}

    def load(self) -> dict:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute("SELECT category, record_key, payload FROM records").fetchall()
        categories: dict[str, dict] = {}
        for category, key, value in rows:
            categories.setdefault(category, {})[key] = json.loads(value)
        payload = dict(categories.get("owner_meta", {}).get("state", {}))
        for name in _MAPS:
            payload[name] = categories.get(name, {})
        emotion = dict(categories.get("emotion_meta", {}).get("state", {}))
        for name in _EMOTION_MAPS:
            emotion[name] = categories.get(f"emotion_{name}", {})
        payload["emotion"] = emotion
        return payload

    def checkpoint(self, *, force: bool = False) -> bool:
        try:
            with closing(self._connect()) as connection:
                meta = dict(connection.execute("SELECT name, value FROM metadata"))
            if meta.get("authority") != "sqlite":
                return False
            generation = int(meta.get("generation", 0))
            if not force and generation - int(meta.get("checkpoint_generation", 0)) < self.CHECKPOINT_INTERVAL:
                return False
            write_checkpoint(self.json_path, self.load())
            with closing(self._connect()) as connection, connection:
                connection.execute("INSERT INTO metadata VALUES ('checkpoint_generation', ?) ON CONFLICT(name) "
                                   "DO UPDATE SET value=excluded.value", (str(generation),))
            with closing(self._connect()) as connection:
                connection.execute("PRAGMA wal_checkpoint(PASSIVE)")
        except (OSError, sqlite3.Error, ValueError):
            # The owner transaction is already durable; checkpoint failure must
            # not roll back emotion memory or report that committed work failed.
            self.last_checkpoint_error = "cognitive_checkpoint_failed"
            return False
        self.last_checkpoint_error = None
        return True
