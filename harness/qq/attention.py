"""Durable local attention policy, independent of QQ send authority.

The protocol archive stays intact. Policy revisions fence readers, media work,
and senders, including across process restarts and dynamics rollbacks.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from harness.qq.official_identity import GROUP


ATTENTION_SET = "communication.qq.attention.set.v1"
ATTENTION_DENIALS = frozenset({"attention_closed", "attention_policy_changed", "attention_backlog", "attention_deferred"})


class AttentionArguments(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)
    group_id: str = Field(pattern=rf"^(?:[1-9][0-9]{{0,19}}|{GROUP})$")
    mode: Literal["open", "closed"]
    reason: str = Field(min_length=1, max_length=512, pattern=r"\S")
    review_after_seconds: int | None = Field(default=None, ge=900, le=14400)


def attention_path(settings):
    return settings.attention_db or settings.state_db.with_name("attention.sqlite3")


def _time(value):
    value = datetime.fromisoformat(value) if isinstance(value, str) else value
    if value.tzinfo is None:
        raise ValueError("attention_aware_time_required")
    return value


def text_units(text):
    """Deterministic fallback when an exact tokenizer count is unavailable.

    CJK characters count individually; Latin words/punctuation use four UTF-8
    bytes per unit. Automatic prompt prefixes are excluded by the caller.
    """
    cjk = re.findall(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]", text)
    other = re.sub(r"[\u2e80-\u9fff\uac00-\ud7af\uf900-\ufaff]", "", text)
    return len(cjk)+(len(other.encode("utf-8"))+3)//4


def is_group(value):
    return isinstance(value, str) and re.fullmatch(rf"(?:[1-9][0-9]{{0,19}}|{GROUP})", value) is not None


def require_attention(group_id, *, revision=None, received_at=None):
    """Shared fence for Cognitive-side readers with no Direct actor instance."""
    import os
    path = os.environ.get("NC_QQ_DIRECT_CONFIG")
    if path is None or not is_group(group_id):
        return None
    stat = Path(path).stat()
    policy = _configured_store(path, stat.st_mtime_ns, stat.st_size).get(group_id)
    if policy["mode"] == "closed":
        raise ValueError("attention_closed")
    if revision is not None and policy["revision"] != revision:
        raise ValueError("attention_policy_changed")
    if received_at is not None and policy["opened_at"] and _time(received_at) < _time(policy["opened_at"]):
        raise ValueError("attention_backlog")
    return policy["revision"]


@lru_cache(maxsize=8)
def _configured_store(path, modified, size):
    from harness.qq.direct_config import DirectQQSettings
    return AttentionStore(attention_path(DirectQQSettings.read(Path(path))))


def guarded_group_read(method):
    from functools import wraps
    @wraps(method)
    def read(self, *, group_id, **kwargs):
        revision = require_attention(group_id)
        result = method(self, group_id=group_id, **kwargs)
        require_attention(group_id, revision=revision)
        return result
    return read


def attention_visible(conversation_id):
    """Filter historical group material without revoking owner/private control."""
    value = str(conversation_id or "")
    if value.startswith("qq:group:"):
        value = value.removeprefix("qq:group:")
    if not is_group(value):
        return True
    try:
        require_attention(value)
        return True
    except ValueError as exc:
        if str(exc) in ATTENTION_DENIALS:
            return False
        raise


def attention_policy_token():
    """Fence cached operator projections with persisted policy revisions.

    Read on every use so an override in another process takes effect without
    waiting for cache expiry. Reasons and message data never enter the token.
    """
    import os
    path = os.environ.get("NC_QQ_DIRECT_CONFIG")
    if path is None:
        return None
    stat = Path(path).stat()
    policies = _configured_store(path, stat.st_mtime_ns, stat.st_size).policies()
    return (path, stat.st_mtime_ns, stat.st_size,
            tuple((p["group_id"], p["revision"], p["mode"]) for p in policies))


def processing_evidence(payload):
    """Bind model interpretation to the same immutable causes as QQ feedback."""
    try:
        return _processing_evidence(payload)
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError):
        # Optional appraisal attribution cannot make an otherwise valid reply fail.
        # The actual source/attention authority is still checked independently.
        return []


def _processing_evidence(payload):
    import os
    path = os.environ.get("NC_QQ_DIRECT_CONFIG")
    trace = payload.get("trace") or {}
    if not path or not trace.get("event_id") or not payload.get("group_id"):
        return []
    from harness.qq.direct_config import DirectQQSettings
    settings = DirectQQSettings.read(Path(path))
    with closing(sqlite3.connect(settings.state_db.as_uri()+"?mode=ro", uri=True)) as db:
        row = db.execute("SELECT record_json FROM qq_direct_events WHERE event_id=? AND group_id=? AND self_id=?",
            (trace["event_id"], payload["group_id"], settings.self_member_id)).fetchone()
    if row is None:
        return []
    value = json.loads(row[0])
    # Full source validation remains in the normal preparation boundary.
    return list(dict.fromkeys([value["event_evidence_ref"],
        *value.get("bound_observation", {}).get("attribution_evidence", [])]))


class AttentionStore:
    def __init__(self, path, *, clock=None):
        self.path = Path(path)
        if self.path.is_symlink():
            raise ValueError("attention_store_path_invalid")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        with closing(self.connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS attention_policy(group_id TEXT PRIMARY KEY,record_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_actions(action_id TEXT PRIMARY KEY,request_hash TEXT NOT NULL,receipt_json TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS attention_admissions(event_id TEXT PRIMARY KEY,group_id TEXT NOT NULL,
                    at REAL NOT NULL,units INTEGER NOT NULL,disposition TEXT NOT NULL,revision INTEGER NOT NULL,addressed INTEGER NOT NULL);
                CREATE INDEX IF NOT EXISTS attention_admissions_at ON attention_admissions(at);
                CREATE TABLE IF NOT EXISTS attention_control(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)

    def connect(self):
        db = sqlite3.connect(self.path, timeout=3)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA synchronous=FULL")
        return db

    @staticmethod
    def _get(db, group_id):
        row = db.execute("SELECT record_json FROM attention_policy WHERE group_id=?", (group_id,)).fetchone()
        return json.loads(row[0]) if row else dict(group_id=group_id, mode="open", revision=0,
            reason="", origin="default", changed_at=None, closed_at=None, opened_at=None,
            next_review_at=None, review_count=0)

    def get(self, group_id):
        with closing(self.connect()) as db:
            return self._get(db, group_id)

    def action(self, action_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT receipt_json FROM attention_actions WHERE action_id=?", (action_id,)).fetchone()
        return json.loads(row[0]) if row else None

    def policies(self):
        with closing(self.connect()) as db:
            return [json.loads(row[0]) for row in db.execute("SELECT record_json FROM attention_policy ORDER BY group_id")]

    def due(self, *, now=None):
        now = now or self.clock()
        return [p for p in self.policies() if p["mode"] == "closed" and p["next_review_at"] and _time(p["next_review_at"]) <= now]

    def set(self, arguments, *, origin, action_id, expected_revision, allowed_groups):
        args = AttentionArguments.model_validate(arguments)
        if args.group_id not in allowed_groups or origin not in {"model", "user"}:
            raise ValueError("attention_scope_not_authorized")
        request = {"arguments": args.model_dump(), "origin": origin, "expected_revision": expected_revision}
        request_hash = hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()
        now = self.clock()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            existing = db.execute("SELECT * FROM attention_actions WHERE action_id=?", (action_id,)).fetchone()
            if existing:
                if existing["request_hash"] != request_hash:
                    raise ValueError("attention_action_identity_conflict")
                return json.loads(existing["receipt_json"])
            old = self._get(db, args.group_id)
            if expected_revision is not None and old["revision"] != expected_revision:
                raise ValueError("attention_policy_changed")
            if origin == "model" and expected_revision is None:
                raise ValueError("attention_model_revision_required")
            closed = args.mode == "closed"
            count = old["review_count"]+1 if closed and old["mode"] == "closed" else 0
            interval = args.review_after_seconds or min(14400, 900*(2**min(count, 4)))
            current = {"group_id": args.group_id, "mode": args.mode, "revision": old["revision"]+1,
                       "reason": args.reason, "origin": origin, "changed_at": now.isoformat(),
                       "closed_at": (old["closed_at"] if old["mode"] == "closed" else now.isoformat()) if closed else None,
                       "opened_at": old["opened_at"] if closed else now.isoformat(),
                       "next_review_at": (now+timedelta(seconds=interval)).isoformat() if closed else None,
                       "review_count": count}
            receipt = {"kind": "qq_attention_changed", "action_id": action_id, "previous": old,
                       "current": current, "evidence_ref": "qq-attention:"+hashlib.sha256(action_id.encode()).hexdigest()}
            db.execute("INSERT INTO attention_policy VALUES (?,?) ON CONFLICT(group_id) DO UPDATE SET record_json=excluded.record_json",
                       (args.group_id, json.dumps(current, ensure_ascii=False)))
            db.execute("INSERT INTO attention_actions VALUES (?,?,?)", (action_id, request_hash, json.dumps(receipt, ensure_ascii=False)))
            return receipt

    def check(self, group_id, *, revision=None, received_at=None):
        if not is_group(group_id):
            return None  # This authority controls groups, never owner/private access.
        policy = self.get(group_id)
        if policy["mode"] == "closed":
            return "attention_closed"
        if revision is not None and revision != policy["revision"]:
            return "attention_policy_changed"
        if received_at is not None and policy["opened_at"] and _time(received_at) < _time(policy["opened_at"]):
            return "attention_backlog"
        return None

    def admit(self, event_id, group_id, *, received_at, units, addressed=False, budget_enabled=False, group_count=1, historical_read=False):
        if not is_group(group_id):
            return {"disposition": "admitted", "revision": 0}
        now = self.clock()
        with closing(self.connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT disposition,revision FROM attention_admissions WHERE event_id=?", (event_id,)).fetchone()
            policy = self._get(db, group_id)
            if old:
                if policy["mode"] == "closed":
                    return {"disposition": "attention_closed", "revision": policy["revision"]}
                if old["revision"] != policy["revision"]:
                    return {"disposition": "attention_policy_changed", "revision": policy["revision"]}
                return dict(old)
            reason = "admitted"
            if policy["mode"] == "closed":
                reason = "attention_closed"
            elif not historical_read and policy["opened_at"] and _time(received_at) < _time(policy["opened_at"]):
                reason = "attention_backlog"
            elif budget_enabled:
                rows = db.execute("SELECT group_id,units,addressed FROM attention_admissions WHERE at>? AND disposition='admitted'",
                                  (now.timestamp()-60,)).fetchall()
                total = sum(r["units"] for r in rows)
                own = sum(r["units"] for r in rows if r["group_id"] == group_id)
                passive = sum(r["units"] for r in rows if not r["addressed"])
                # A direct address has a reserved global quarter. Ambient input
                # is also bounded per group so a busy read-only group cannot monopolize it.
                share = max(1, 4096 // max(1, group_count))
                if (total+units > 4096 or own+units > share
                        or (not addressed and passive+units > 3072)):
                    reason = "attention_deferred"
            db.execute("INSERT INTO attention_admissions VALUES (?,?,?,?,?,?,?)",
                       (event_id, group_id, now.timestamp(), units if reason == "admitted" else 0, reason, policy["revision"], int(addressed)))
            return {"disposition": reason, "revision": policy["revision"]}

    def admission(self, event_id):
        with closing(self.connect()) as db:
            row = db.execute("SELECT disposition,revision FROM attention_admissions WHERE event_id=?", (event_id,)).fetchone()
        return dict(row) if row else None

    def counts(self):
        with closing(self.connect()) as db:
            return {row[0]: row[1] for row in db.execute("SELECT disposition,COUNT(*) FROM attention_admissions WHERE at>? GROUP BY disposition",
                                                       (self.clock().timestamp()-3600,))}
