"""General entity prediction memory with evidence-bound revisions and tombstones."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from threading import RLock

from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.social_world.contracts import EntityBelief, EntityObservation


def belief_key(belief: EntityBelief) -> str:
    return content_hash((belief.entity_ref, belief.belief_type, belief.prediction.event_type,
                         belief.prediction.field, belief.prediction.scope_ref))


class EntityMemoryStore:
    """The same event/prediction store accepts people, tools and services."""

    def __init__(self, path: str | Path) -> None:
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._lock = RLock()
        with self._db:
            self._db.execute("CREATE TABLE IF NOT EXISTS entity_events (event_id TEXT PRIMARY KEY, payload TEXT NOT NULL)")
            self._db.execute("CREATE TABLE IF NOT EXISTS entity_predictions (belief_key TEXT, revision INTEGER, payload TEXT NOT NULL, deleted INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(belief_key, revision))")

    def close(self) -> None:
        self._db.close()

    def add_event(self, event: EntityObservation) -> EntityObservation:
        event = EntityObservation.model_validate(event.model_dump(mode="python"))
        payload = canonical_json(event)
        with self._lock, self._db:
            existing = self._db.execute("SELECT payload FROM entity_events WHERE event_id = ?", (event.event_id,)).fetchone()
            if existing and existing[0] != payload:
                raise ValueError("event identity already binds different actors, facts or evidence")
            for row in self._db.execute("SELECT payload FROM entity_events"):
                previous = EntityObservation.model_validate_json(row[0])
                if set(previous.evidence_refs).intersection(event.evidence_refs) and set(previous.actor_refs) != set(event.actor_refs):
                    raise ValueError("evidence reference already binds a different entity set")
            self._db.execute("INSERT OR IGNORE INTO entity_events VALUES (?, ?)", (event.event_id, payload))
        return event

    def event(self, event_id: str) -> EntityObservation:
        row = self._db.execute("SELECT payload FROM entity_events WHERE event_id = ?", (event_id,)).fetchone()
        if row is None:
            raise ValueError(f"unknown objective event: {event_id}")
        return EntityObservation.model_validate_json(row[0])

    def events(self, actor_refs: tuple[str, ...] = ()) -> tuple[EntityObservation, ...]:
        rows = self._db.execute("SELECT payload FROM entity_events").fetchall()
        values = [EntityObservation.model_validate_json(row[0]) for row in rows]
        return tuple(sorted((event for event in values if not actor_refs or set(actor_refs).intersection(event.actor_refs)),
                            key=lambda event: (event.occurred_at, event.event_id)))

    def beliefs(self, actor_refs: tuple[str, ...] = ()) -> tuple[EntityBelief, ...]:
        rows = self._db.execute("""SELECT p.payload, p.deleted FROM entity_predictions p JOIN
            (SELECT belief_key, MAX(revision) AS latest FROM entity_predictions GROUP BY belief_key) q
            ON p.belief_key=q.belief_key AND p.revision=q.latest ORDER BY p.belief_key""").fetchall()
        beliefs = [EntityBelief.model_validate_json(payload) for payload, deleted in rows if not deleted]
        return tuple(belief for belief in beliefs if not actor_refs or belief.entity_ref in actor_refs)

    def upsert_belief(self, candidate: EntityBelief) -> EntityBelief:
        candidate = EntityBelief.model_validate(candidate.model_dump(mode="python"))
        key = belief_key(candidate)
        support, counter = set(candidate.supporting_event_refs), set(candidate.contradicting_event_refs)
        for event_id in sorted(support | counter):
            event = self.event(event_id)
            if candidate.entity_ref not in event.actor_refs:
                raise ValueError("prediction evidence belongs to a different entity")
            if event.event_type != candidate.prediction.event_type:
                raise ValueError("prediction evidence has an incomparable event type")
            scope = candidate.prediction.scope_ref
            if scope is not None and scope not in {
                getattr(event.facts, field, None) for field in ("scope_ref", "task_ref", "request_ref", "commitment_ref", "location_ref")
            }:
                raise ValueError("prediction evidence does not match its declared scope")
            agrees = getattr(event.facts, candidate.prediction.field) == candidate.prediction.expected
            if agrees != (event_id in support):
                raise ValueError("support/counterevidence contradicts the objective fact")
        # A general empirical prediction estimate, not an entity/relationship score.
        count = len(support) + len(counter)
        candidate = candidate.model_copy(update={
            "confidence": (len(support) + 1) / (count + 2),
            "stability": abs(len(support) - len(counter)) / (count + 2),
        })
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            previous = self._db.execute("SELECT revision, payload, deleted FROM entity_predictions WHERE belief_key=? ORDER BY revision DESC LIMIT 1", (key,)).fetchone()
            if previous and previous[0] == candidate.revision and not previous[2] and previous[1] == canonical_json(candidate):
                return candidate
            expected_revision = previous[0] + 1 if previous else 1
            if candidate.revision != expected_revision:
                raise ValueError("entity prediction revision must advance exactly once")
            if previous and not previous[2]:
                old = EntityBelief.model_validate_json(previous[1])
                if not set(old.supporting_event_refs + old.contradicting_event_refs).issubset(support | counter):
                    raise ValueError("a revision cannot silently discard prior evidence")
            self._db.execute("INSERT INTO entity_predictions VALUES (?, ?, ?, 0)", (key, candidate.revision, canonical_json(candidate)))
        return candidate

    def delete_belief(self, key: str, *, expected_revision: int) -> None:
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            previous = self._db.execute("SELECT revision, payload, deleted FROM entity_predictions WHERE belief_key=? ORDER BY revision DESC LIMIT 1", (key,)).fetchone()
            if previous is None or previous[0] != expected_revision or previous[2]:
                raise ValueError("deletion must target the current live prediction revision")
            tombstone = EntityBelief.model_validate_json(previous[1]).model_copy(update={"revision": expected_revision+1})
            self._db.execute("INSERT INTO entity_predictions VALUES (?, ?, ?, 1)", (key, expected_revision+1, canonical_json(tombstone)))
