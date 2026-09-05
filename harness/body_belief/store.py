"""Persistent objective experiences, revision audit, and validated frozen snapshots."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from threading import RLock

from harness.body_belief.models import (
    BodyCausalBelief, BodyCausalBeliefCandidate, BodyExperienceEpisode, FrozenBeliefSnapshot,
    ConsolidationProfile, SensorReliabilityBelief, validate_evidence,
)
from harness.research.grounded_agency.hashes import canonical_json, content_hash


class BodyBeliefStore:
    def __init__(self, path: str | Path, *, profile: ConsolidationProfile | None = None):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        try:
            with self.db:
                self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, data TEXT NOT NULL)")
                stored = self.db.execute("SELECT data FROM metadata WHERE key='profile'").fetchone()
                self.profile = ConsolidationProfile.model_validate_json(stored[0]) if stored else (profile or ConsolidationProfile())
                if profile is not None and profile != self.profile:
                    raise ValueError("consolidation profile drift requires a new store")
                self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('profile',?)", (canonical_json(self.profile),))
                self.db.execute("CREATE TABLE IF NOT EXISTS experiences (id TEXT PRIMARY KEY, decision_id TEXT UNIQUE, data TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS objects (kind TEXT, id TEXT, data TEXT NOT NULL, PRIMARY KEY(kind,id))")
                self.db.execute("CREATE TABLE IF NOT EXISTS audit (digest TEXT PRIMARY KEY, data TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS pending (id TEXT PRIMARY KEY, data TEXT NOT NULL)")
                self.db.execute("CREATE TABLE IF NOT EXISTS appraisal_work (id TEXT PRIMARY KEY)")
            self._validate_snapshot(self.snapshot())
        except BaseException:
            self.db.close()
            raise

    def close(self):
        self.db.close()

    def experiences(self) -> tuple[BodyExperienceEpisode, ...]:
        with self._lock:
            return tuple(BodyExperienceEpisode.model_validate_json(row[0]) for row in
                         self.db.execute("SELECT data FROM experiences ORDER BY id"))

    def _objects(self, kind, model):
        with self._lock:
            return tuple(model.model_validate_json(row[0]) for row in
                         self.db.execute("SELECT data FROM objects WHERE kind=? ORDER BY id", (kind,)))

    def candidates(self):
        return self._objects("candidate", BodyCausalBeliefCandidate)

    def beliefs(self):
        return self._objects("belief", BodyCausalBelief)

    def reliabilities(self):
        return self._objects("reliability", SensorReliabilityBelief)

    def append_experience(self, experience: BodyExperienceEpisode) -> bool:
        experience = BodyExperienceEpisode.model_validate(experience.model_dump(mode="python"))
        serialized = canonical_json(experience)
        with self._lock, self.db:
            old = self.db.execute("SELECT data FROM experiences WHERE id=? OR decision_id=?",
                                  (experience.experience_id, experience.decision_id)).fetchone()
            if old:
                if old[0] != serialized:
                    raise ValueError("same experience/decision has conflicting evidence")
                return False
            self.db.execute("INSERT INTO experiences VALUES (?,?,?)", (experience.experience_id, experience.decision_id, serialized))
            self._audit({"event": "experience", "experience": experience})
        return True

    def _audit(self, data):
        self.db.execute("INSERT OR IGNORE INTO audit VALUES (?,?)", (content_hash(data), canonical_json(data)))

    def audit(self):
        with self._lock:
            return tuple(json.loads(row[0]) for row in self.db.execute("SELECT data FROM audit ORDER BY rowid"))

    def record_audit(self, data):
        with self._lock, self.db:
            self._audit(data)

    def _put(self, kind, identifier, item):
        with self._lock, self.db:
            self.db.execute("INSERT OR REPLACE INTO objects VALUES (?,?,?)", (kind, identifier, canonical_json(item)))
            self._audit({"event": kind, "value": item})

    def put_candidate(self, candidate):
        candidate = BodyCausalBeliefCandidate.model_validate(candidate.model_dump(mode="python"))
        validate_evidence(candidate, {item.experience_id: item for item in self.experiences()})
        self._put("candidate", candidate.candidate_id, candidate)

    def put_belief(self, belief):
        belief = BodyCausalBelief.model_validate(belief.model_dump(mode="python"))
        validate_evidence(belief, {item.experience_id: item for item in self.experiences()})
        self._validate_consolidation(belief, {item.experience_id: item for item in self.experiences()}, self.profile)
        previous = next((item for item in self.beliefs() if item.belief_id == belief.belief_id), None)
        if previous is not None and belief != previous and belief.revision <= previous.revision:
            raise ValueError("belief revisions must increase")
        self._put("belief", belief.belief_id, belief)

    def put_reliability(self, reliability):
        reliability = SensorReliabilityBelief.model_validate(reliability.model_dump(mode="python"))
        episodes = {item.experience_id: item for item in self.experiences()}
        for ref in (*reliability.supporting_refs, *reliability.contradicting_refs):
            item = episodes.get(ref)
            if (item is None or not item.eligible or item.context_signature != reliability.context_signature
                    or item.observation_before.channel_id != reliability.channel_id):
                raise ValueError("reliability requires matching verified experiences")
        previous = next((item for item in self.reliabilities() if item.context_signature == reliability.context_signature
                         and item.channel_id == reliability.channel_id), None)
        if previous is not None and reliability != previous and reliability.revision <= previous.revision:
            raise ValueError("reliability revisions must increase")
        self._put("reliability", content_hash((reliability.context_signature, reliability.channel_id)), reliability)

    def put_pending(self, identifier: str, payload: dict):
        with self._lock, self.db:
            old = self.db.execute("SELECT data FROM pending WHERE id=?", (identifier,)).fetchone()
            if old and old[0] != canonical_json(payload):
                raise ValueError("conflicting pending decision")
            self.db.execute("INSERT OR IGNORE INTO pending VALUES (?,?)", (identifier, canonical_json(payload)))

    def pending(self):
        with self._lock:
            return tuple(json.loads(row[0]) for row in self.db.execute("SELECT data FROM pending ORDER BY id"))

    def finalize_pending(self, experience: BodyExperienceEpisode, *, appraise: bool) -> bool:
        """Commit the next sample, consume its pending decision, and queue work together."""
        experience = BodyExperienceEpisode.model_validate(experience.model_dump(mode="python"))
        if experience.observation_after is None:
            raise ValueError("finalizing an experience requires the next observation")
        serialized = canonical_json(experience)
        with self._lock, self.db:
            old = self.db.execute("SELECT data FROM experiences WHERE id=? OR decision_id=?",
                                  (experience.experience_id, experience.decision_id)).fetchone()
            if old is not None:
                if old[0] != serialized:
                    raise ValueError("same experience/decision has conflicting evidence")
                return False
            pending = self.db.execute("SELECT data FROM pending WHERE id=?", (experience.decision_id,)).fetchone()
            expected = canonical_json(experience.model_copy(update={"observation_after": None}))
            if pending is None or pending[0] != expected:
                raise ValueError("finalized experience does not match its pending terminal decision")
            self.db.execute("INSERT INTO experiences VALUES (?,?,?)",
                            (experience.experience_id, experience.decision_id, serialized))
            self._audit({"event": "experience", "experience": experience})
            self.db.execute("DELETE FROM pending WHERE id=?", (experience.decision_id,))
            if appraise and experience.eligible:
                self.db.execute("INSERT OR IGNORE INTO appraisal_work VALUES (?)", (experience.experience_id,))
        return True

    def remove_pending(self, identifier):
        with self._lock, self.db:
            self.db.execute("DELETE FROM pending WHERE id=?", (identifier,))

    def snapshot(self) -> FrozenBeliefSnapshot:
        with self._lock:
            return FrozenBeliefSnapshot(consolidation_profile=self.profile, experiences=self.experiences(), candidates=self.candidates(),
                                       beliefs=self.beliefs(), reliabilities=self.reliabilities())

    @staticmethod
    def _validate_consolidation(belief, episodes, profile):
        support, contrary = belief.supporting_experience_refs, belief.contradicting_experience_refs
        precision = len(support) / (len(support) + len(contrary))
        if belief.confidence > precision or belief.stability > precision:
            raise ValueError("belief confidence/stability exceeds observed supporting precision")
        windows = {int(episodes[ref].completed_at.timestamp()) // profile.window_seconds for ref in support}
        if len(support) < profile.minimum_support or len(windows) < profile.minimum_windows:
            raise ValueError("consolidated belief lacks independent support across time windows")
        if belief.status in {"consolidated", "revised"} and (
            precision < profile.minimum_precision or 1 - precision > profile.maximum_contradiction_ratio
        ):
            raise ValueError("consolidated belief violates the frozen precision profile")

    @staticmethod
    def _validate_snapshot(snapshot):
        episodes = {item.experience_id: item for item in snapshot.experiences}
        if len(episodes) != len(snapshot.experiences) or len({item.decision_id for item in episodes.values()}) != len(episodes):
            raise ValueError("snapshot contains duplicate experience/decision identities")
        for values, attr in ((snapshot.candidates, "candidate_id"), (snapshot.beliefs, "belief_id")):
            if len({getattr(item, attr) for item in values}) != len(values):
                raise ValueError("snapshot contains duplicate hypothesis identities")
            for item in values:
                validate_evidence(item, episodes)
                if isinstance(item, BodyCausalBelief):
                    BodyBeliefStore._validate_consolidation(item, episodes, snapshot.consolidation_profile)
        reliability_keys = {(item.context_signature, item.channel_id) for item in snapshot.reliabilities}
        if len(reliability_keys) != len(snapshot.reliabilities):
            raise ValueError("snapshot contains duplicate channel reliability identities")
        for item in snapshot.reliabilities:
            for ref in (*item.supporting_refs, *item.contradicting_refs):
                episode = episodes.get(ref)
                if (episode is None or not episode.eligible or episode.context_signature != item.context_signature
                        or episode.observation_before.channel_id != item.channel_id):
                    raise ValueError("snapshot reliability evidence is missing")

    def restore(self, snapshot: FrozenBeliefSnapshot, *, include_beliefs: bool = True) -> str:
        snapshot = FrozenBeliefSnapshot.model_validate(snapshot.model_dump(mode="python"))
        self._validate_snapshot(snapshot)
        if snapshot.consolidation_profile != self.profile:
            raise ValueError("snapshot consolidation profile does not match the frozen store")
        with self._lock, self.db:
            self.db.execute("DELETE FROM experiences")
            self.db.execute("DELETE FROM objects")
            for item in snapshot.experiences:
                self.db.execute("INSERT INTO experiences VALUES (?,?,?)", (item.experience_id, item.decision_id, canonical_json(item)))
            if include_beliefs:
                for kind, values, field in (("candidate", snapshot.candidates, "candidate_id"), ("belief", snapshot.beliefs, "belief_id")):
                    for item in values:
                        self.db.execute("INSERT INTO objects VALUES (?,?,?)", (kind, getattr(item, field), canonical_json(item)))
                for item in snapshot.reliabilities:
                    self.db.execute("INSERT INTO objects VALUES (?,?,?)", ("reliability", content_hash((item.context_signature, item.channel_id)), canonical_json(item)))
            self._audit({"event": "snapshot_restore", "snapshot_hash": snapshot.content_hash(), "include_beliefs": include_beliefs})
        return snapshot.content_hash()
