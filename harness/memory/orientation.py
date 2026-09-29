"""Private evidence can shape a disposition without becoming public evidence."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class OrientationInterpretation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    direction: Literal["approach", "avoidance", "neutrality", "repair"]
    reason: str = Field(min_length=1, max_length=600)
    source_refs: list[str] = Field(min_length=1, max_length=24)


class OrientationStore:
    """Versioned model interpretation; no score, reward, or credibility effect.

    History contains the private rationale. The only cross-audience projection
    is a closed-set disposition for an authenticated stable principal. It must
    never be used to assert a fact about the person or explain hidden evidence.
    """

    def __init__(self, memory):
        self.memory = memory
        with closing(memory._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pm_orientation(
                    subject TEXT, revision INTEGER, event_id TEXT UNIQUE,
                    audience TEXT, observed_at TEXT, interpretation TEXT, sources TEXT,
                    PRIMARY KEY(subject,revision));
            """)

    def revise(self, interpretation, *, subject_id, audience, event_id, now=None):
        value = OrientationInterpretation.model_validate(interpretation)
        refs = list(dict.fromkeys(value.source_refs))
        if not self.memory.validate_references(refs, audience=audience, now=now):
            raise ValueError("orientation_evidence_unavailable")
        raw = value.model_dump_json()
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            old = db.execute("SELECT subject,audience,interpretation FROM pm_orientation WHERE event_id=?", (event_id,)).fetchone()
            if old:
                if tuple(old) != (subject_id, audience, raw):
                    raise ValueError("orientation_receipt_conflict")
                return False
            # Freeze the revision of beliefs as well as the identity of sources.
            frozen = [{"record_id": ref, "revision": db.execute("SELECT revision FROM pm_records WHERE id=?", (ref,)).fetchone()[0]}
                      for ref in refs]
            revision = db.execute("SELECT COALESCE(MAX(revision),0)+1 FROM pm_orientation WHERE subject=?", (subject_id,)).fetchone()[0]
            db.execute("INSERT INTO pm_orientation VALUES(?,?,?,?,?,?,?)", (subject_id, revision, event_id,
                audience, (now or datetime.now(timezone.utc)).isoformat(), raw, json.dumps(frozen)))
        return True

    def projection(self, subject_id):
        from harness.memory.identity import IdentityStore
        aliases = IdentityStore(self.memory).aliases((subject_id,))
        with closing(self.memory._connect()) as db:
            row = db.execute("SELECT * FROM pm_orientation WHERE subject IN (" + ",".join("?" for _ in aliases)
                + ") ORDER BY observed_at DESC,revision DESC LIMIT 1", aliases).fetchone()
        if row is None or not self.memory.validate_references(json.loads(row["sources"]), audience=row["audience"]):
            return None
        # Do not expose source IDs, timestamps, channel, free text, or reasons.
        # The opaque version binds the internal checkpoint without revealing
        # how often a private exchange revised this interpretation.
        version = hashlib.sha256((row["event_id"] + ":" + str(row["revision"])).encode()).hexdigest()
        return {"subject_id": subject_id, "direction": json.loads(row["interpretation"])["direction"],
                "version": version, "interpretation_only": True}

    def validate(self, projection):
        return projection is None or self.projection(projection.get("subject_id")) == projection
