"""Explicit owner-authorized identity links; names and model guesses never bind."""
from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json


class IdentityStore:
    def __init__(self, memory):
        self.memory = memory
        with closing(memory._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pm_identity_events(event_id TEXT PRIMARY KEY,digest TEXT,payload TEXT);
                CREATE TABLE IF NOT EXISTS pm_identity_links(left_id TEXT,right_id TEXT,active INTEGER,evidence TEXT,
                    PRIMARY KEY(left_id,right_id));
                CREATE INDEX IF NOT EXISTS pm_identity_right ON pm_identity_links(right_id,active);
            """)

    def bind(self, left, right, *, event_id, evidence_ids, authorized_by, active=True):
        if authorized_by != "local:main":
            raise ValueError("identity_binding_requires_explicit_owner_authorization")
        if left == right or any(not isinstance(value, str) or ":" not in value or len(value) > 160 for value in (left, right)):
            raise ValueError("identity_binding_requires_distinct_stable_principals")
        refs = sorted(set(evidence_ids))
        if not refs or not self.memory.validate_references(refs, audience="local:main"):
            raise ValueError("identity_binding_requires_retained_authorization_evidence")
        left, right = sorted((left, right))
        value = {"left": left, "right": right, "active": bool(active), "evidence": refs, "authorized_by": authorized_by}
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT digest FROM pm_identity_events WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if old[0] != digest:
                    raise ValueError("identity_binding_receipt_conflict")
                return False
            history = {**value, "observed_at": datetime.now(timezone.utc).isoformat()}
            db.execute("INSERT INTO pm_identity_events VALUES(?,?,?)", (event_id, digest, json.dumps(history)))
            db.execute("INSERT INTO pm_identity_links VALUES(?,?,?,?) ON CONFLICT(left_id,right_id) DO UPDATE SET active=excluded.active,evidence=excluded.evidence",
                (left, right, int(active), json.dumps(refs)))
        return True

    def aliases(self, principals):
        found = set(principals)
        pending = list(found)
        if len(found) > 32:
            raise ValueError("identity_component_exceeds_budget")
        with closing(self.memory._connect()) as db:
            while pending:
                principal = pending.pop()
                rows = db.execute("SELECT left_id,right_id,evidence FROM pm_identity_links WHERE active=1 AND (left_id=? OR right_id=?) LIMIT 33",
                                  (principal, principal)).fetchall()
                for row in rows:
                    if not self.memory.validate_references(json.loads(row["evidence"]), audience="local:main"):
                        continue
                    other = row["right_id"] if row["left_id"] == principal else row["left_id"]
                    if other not in found:
                        found.add(other)
                        if len(found) > 32:
                            raise ValueError("identity_component_exceeds_budget")
                        pending.append(other)
        return tuple(sorted(found))

