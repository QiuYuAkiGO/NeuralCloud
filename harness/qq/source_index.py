"""Transactional append watermarks for the immutable QQ source archive.

Writers install the guards; readers never migrate a source database. Updates,
deletes, explicit rowid reuse and changes to a delivered decision invalidate the
append proof. A reader then uses the original full validation path. No source
payload or delivery authorization is cached in this index.
"""
from __future__ import annotations

import json
import sqlite3
from uuid import uuid4
from collections.abc import Iterator


SOURCE_TABLES = ("qq_visual_profiles", "qq_visual_observations",
                 "qq_reply_deliveries", "qq_reply_segment_deliveries")
META_TABLE = "qq_source_append_state"
_UNIQUE_KEYS = {
    "qq_visual_profiles": (("record_id",), ("group_id", "member_id", "capture_id")),
    "qq_visual_observations": (("record_id",), ("group_id", "capture_id", "capture_item_id")),
    "qq_reply_deliveries": (("receipt_id",), ("receipt_hash",), ("decision_id",)),
    "qq_reply_segment_deliveries": (("receipt_id",), ("receipt_hash",), ("decision_id", "segment_index")),
}


def _guards(tables: set[str]) -> Iterator[tuple[str, str]]:
    for table in SOURCE_TABLES:
        if table not in tables:
            continue
        for operation in ("UPDATE", "DELETE", "INSERT"):
            name = f"nc_append_{table}_{operation.lower()}"
            condition = (f" WHEN NEW.rowid <= 0 OR NEW.rowid <= (SELECT MAX(rowid) FROM {table} WHERE rowid != NEW.rowid)"
                         if operation == "INSERT" else "")
            yield name, (f"CREATE TRIGGER {name} AFTER {operation} ON {table}{condition} BEGIN "
                         f"UPDATE {META_TABLE} SET revision=revision+1 WHERE singleton=1; END")
        # SQLite REPLACE can delete a conflicting row without firing DELETE
        # triggers (recursive_triggers defaults to OFF). Detect every declared
        # uniqueness collision before the old row disappears.
        collisions = ' OR '.join('(' + ' AND '.join(f'{key}=NEW.{key}' for key in keys) + ')'
                                 for keys in _UNIQUE_KEYS[table])
        name = f"nc_append_{table}_replace"
        yield name, (f"CREATE TRIGGER {name} BEFORE INSERT ON {table} "
                     f"WHEN EXISTS(SELECT 1 FROM {table} WHERE {collisions}) BEGIN "
                     f"UPDATE {META_TABLE} SET revision=revision+1 WHERE singleton=1; END")
    if "qq_reply_decisions" in tables:
        references = [f"EXISTS(SELECT 1 FROM {table} WHERE decision_id=OLD.decision_id)"
                      for table in SOURCE_TABLES[2:] if table in tables]
        if references:
            for operation in ("UPDATE", "DELETE"):
                name = f"nc_append_qq_reply_decisions_{operation.lower()}"
                condition = ' OR '.join(references)
                if operation == "UPDATE":
                    condition += ' OR ' + condition.replace('OLD.', 'NEW.')
                yield name, (f"CREATE TRIGGER {name} AFTER {operation} ON qq_reply_decisions "
                             f"WHEN {condition} BEGIN "
                             f"UPDATE {META_TABLE} SET revision=revision+1 WHERE singleton=1; END")
            name = "nc_append_qq_reply_decisions_replace"
            linked = ' OR '.join(references).replace('OLD.', 'prior.')
            yield name, (f"CREATE TRIGGER {name} BEFORE INSERT ON qq_reply_decisions "
                         f"WHEN EXISTS(SELECT 1 FROM qq_reply_decisions prior WHERE "
                         f"(prior.decision_id=NEW.decision_id OR prior.input_hash=NEW.input_hash) "
                         f"AND ({linked})) BEGIN "
                         f"UPDATE {META_TABLE} SET revision=revision+1 WHERE singleton=1; END")


def ensure_source_index(connection: sqlite3.Connection) -> None:
    """Install alongside source schemas, without committing the caller's work."""
    tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    guards = dict(_guards(tables))
    existing = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'nc_append_%'"))
    if META_TABLE in tables and all(existing.get(name) == sql for name, sql in guards.items()):
        return
    connection.execute(f"""CREATE TABLE IF NOT EXISTS {META_TABLE} (
        singleton INTEGER PRIMARY KEY CHECK(singleton=1), source_id TEXT NOT NULL,
        revision INTEGER NOT NULL CHECK(revision>=0))""")
    connection.execute(f"INSERT OR IGNORE INTO {META_TABLE} VALUES (1,?,0)", (uuid4().hex,))
    for name, sql in guards.items():
        if existing.get(name) != sql:
            # Repaired/missing guards cannot certify the old append prefix.
            connection.execute(f"UPDATE {META_TABLE} SET revision=revision+1 WHERE singleton=1")
            connection.execute(f"DROP TRIGGER IF EXISTS {name}")
            connection.execute(sql)


def read_source_index(connection: sqlite3.Connection, tables: set[str]) -> dict | None:
    """Read within the importer's source snapshot; reject damaged tracking."""
    if META_TABLE not in tables:
        return None
    expected = dict(_guards(tables))
    actual = dict(connection.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND name LIKE 'nc_append_%'"))
    if any(actual.get(name) != sql for name, sql in expected.items()):
        raise ValueError("QQ append guards are incomplete")
    row = connection.execute(f"SELECT source_id,revision FROM {META_TABLE} WHERE singleton=1").fetchone()
    if (row is None or not isinstance(row[0], str) or len(row[0]) != 32
            or not all(c in '0123456789abcdef' for c in row[0]) or type(row[1]) is not int or row[1] < 0):
        raise ValueError("QQ append identity is invalid")
    return {"source_id": row[0], "revision": row[1], "watermarks": {
        table: max(0, connection.execute(f"SELECT COALESCE(MAX(rowid),0) FROM {table}").fetchone()[0])
        for table in SOURCE_TABLES if table in tables}}


def append_cursors(previous: str | None, current: dict) -> dict[str, int]:
    """Reuse a prefix only while the same guarded source remains append-only."""
    old = json.loads(previous) if previous else None
    if old is not None and not isinstance(old, dict):
        raise ValueError("QQ append checkpoint is invalid")
    if old is None or any(old.get(key) != current[key] for key in ("source_id", "revision", "file_identity")):
        return {}
    cursors = old.get("watermarks", {})
    if not isinstance(cursors, dict):
        raise ValueError("QQ append watermarks are invalid")
    if any(type(value) is not int or value < 0 or value > current['watermarks'].get(table, -1)
           for table, value in cursors.items()):
        return {}
    return cursors
