"""Durable platform revocations, separate from Misty's attention policy."""

from contextlib import closing
import sqlite3


def require_platform_scope(settings, group_id):
    if settings.transport != "qqbot":
        return
    try:
        with closing(sqlite3.connect(settings.state_db.as_uri() + "?mode=ro", uri=True)) as db:
            row = db.execute(
                "SELECT 1 FROM qqbot_revocations WHERE app_id=? AND group_id=?", (settings.qqbot_app_id, group_id)
            ).fetchone()
        if row is not None:
            raise ValueError("qqbot_platform_scope_revoked")
    except sqlite3.Error:
        raise ValueError("qqbot_platform_scope_unavailable") from None


def initialize(store):
    with closing(store.connect()) as db, db:
        db.execute(
            "CREATE TABLE IF NOT EXISTS qqbot_revocations (app_id TEXT NOT NULL, group_id TEXT NOT NULL, "
            "event_type TEXT NOT NULL, evidence_ref TEXT NOT NULL, occurred_at TEXT NOT NULL, "
            "PRIMARY KEY(app_id,group_id))"
        )


def revoke(store, *, app_id, group_id, event_type, evidence_ref, occurred_at):
    with closing(store.connect()) as db, db:
        db.execute(
            "INSERT INTO qqbot_revocations VALUES(?,?,?,?,?) ON CONFLICT(app_id,group_id) DO UPDATE SET "
            "event_type=excluded.event_type,evidence_ref=excluded.evidence_ref,occurred_at=excluded.occurred_at",
            (app_id, group_id, event_type, evidence_ref, occurred_at),
        )
