"""Source-bound affect events and durable appraisal work in the memory database.

There is no writable global feeling score. Projections derive from still-valid
observations, so a retraction removes its influence even after a restart. Global
mood shares only numbers; causes and relationship prose remain audience scoped.
"""
from __future__ import annotations

from contextlib import closing
from datetime import datetime, timezone
import json
import math
import time

from harness.agentic.affect_models import AffectProposal
from harness.agentic.store import canonical, digest
from harness.memory.production import Experience


def stamp(value):
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


class AffectStore:
    def __init__(self, store, settings, *, clock=time.time):
        self.store, self.memory, self.settings, self.clock = store, store.memory, settings, clock
        with closing(self.memory._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_affect_meta_v1(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_affect_pending_v1(
                    source TEXT PRIMARY KEY,conversation TEXT NOT NULL,observed REAL NOT NULL,
                    ready REAL NOT NULL,job TEXT,status TEXT NOT NULL DEFAULT 'pending');
                CREATE INDEX IF NOT EXISTS agent_affect_pending_ready_v1
                    ON agent_affect_pending_v1(status,ready);
                CREATE TABLE IF NOT EXISTS agent_affect_events_v1(
                    revision INTEGER PRIMARY KEY AUTOINCREMENT,source TEXT NOT NULL UNIQUE,
                    independent_key TEXT NOT NULL UNIQUE,
                    conversation TEXT NOT NULL,subject TEXT NOT NULL,target TEXT NOT NULL,
                    occurred REAL NOT NULL,committed REAL NOT NULL,payload TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS agent_affect_events_time_v1 ON agent_affect_events_v1(occurred);
                CREATE TABLE IF NOT EXISTS agent_affect_sources_v1(
                    event TEXT NOT NULL,source TEXT NOT NULL,digest TEXT NOT NULL,PRIMARY KEY(event,source));
                CREATE TABLE IF NOT EXISTS agent_affect_calls_v1(id TEXT PRIMARY KEY,at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_affect_feedback_v1(
                    source TEXT PRIMARY KEY,payload TEXT NOT NULL,acknowledged INTEGER NOT NULL DEFAULT 0);
            """)
            if settings.mode != "off":
                db.execute("INSERT OR IGNORE INTO agent_affect_meta_v1 VALUES('activated',?)", (str(clock()),))
            if settings.mode == 'shadow':
                db.execute("INSERT OR IGNORE INTO agent_affect_meta_v1 VALUES('shadow_started',?)", (str(clock()),))

    def observe(self, db, conversation, source):
        """Called inside the original-event transaction, including on reply failure."""
        if self.settings.mode == "off" or source.kind != "observation":
            return
        activated = float(db.execute("SELECT value FROM agent_affect_meta_v1 WHERE key='activated'").fetchone()[0])
        # Do not reinterpret years of imported history as new current stimuli.
        if source.occurred_at.timestamp() < activated - 120:
            return
        now = self.clock()
        db.execute("INSERT OR IGNORE INTO agent_affect_pending_v1(source,conversation,observed,ready) VALUES(?,?,?,?)",
            (source.record_id, conversation, now, now + self.settings.appraisal_window_seconds))

    def enqueue_due(self):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            first = db.execute("SELECT * FROM agent_affect_pending_v1 WHERE status='pending' AND ready<=? ORDER BY ready LIMIT 1",
                (self.clock(),)).fetchone()
            if first is None:
                return None
            rows = db.execute("SELECT source FROM agent_affect_pending_v1 WHERE conversation=? AND status='pending' ORDER BY observed LIMIT 4",
                (first['conversation'],)).fetchall()
            sources = [r[0] for r in rows]
            conv = db.execute("SELECT next_seq FROM agent_conversations_v2 WHERE id=?", (first['conversation'],)).fetchone()
            job = self.store._enqueue(db, first['conversation'], sources[0], "affect:" + digest([sources, first['ready']])[:24],
                "affect", {"source_ids": sources}, conv[0] - 1)
            db.executemany("UPDATE agent_affect_pending_v1 SET job=?,status='queued' WHERE source=?",
                [(job['id'], source) for source in sources])
            return job

    def reserve_call(self, identifier):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = self.clock()
            db.execute("DELETE FROM agent_affect_calls_v1 WHERE at<=?", (now - 3600,))
            if db.execute("SELECT 1 FROM agent_affect_calls_v1 WHERE id=?", (identifier,)).fetchone():
                return True
            if db.execute("SELECT count(*) FROM agent_affect_calls_v1").fetchone()[0] >= self.settings.appraisal_calls_per_hour:
                return False
            db.execute("INSERT INTO agent_affect_calls_v1 VALUES(?,?)", (identifier, now))
            return True

    def unassessed(self, conversation, sources):
        with closing(self.memory._connect()) as db, db:
            result = []
            audience = self.store.get(conversation)['audience']
            for source in sources:
                if not self.memory._visible(db, source, audience):
                    db.execute("UPDATE agent_affect_pending_v1 SET status='unavailable' WHERE source=?", (source,))
                elif not db.execute("SELECT 1 FROM agent_affect_events_v1 WHERE source=?", (source,)).fetchone():
                    result.append(source)
            return result

    def submit(self, conversation, proposal: AffectProposal, *, shown):
        """Validate the entire proposal before applying any interpretation."""
        conv = self.store.get(conversation)
        now, checked = self.clock(), []
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            for event in proposal.events:
                refs = list(dict.fromkeys([event.source_id, *event.context_source_ids]))
                if not set(refs) <= set(shown):
                    raise ValueError("affect_requires_read_sources")
                sources = []
                for ref in refs:
                    row = db.execute("SELECT payload,digest FROM pm_sources WHERE id=? AND revoked=0", (ref,)).fetchone()
                    if row is None or not self.memory._visible(db, ref, conv['audience']):
                        raise ValueError("affect_source_unavailable")
                    source = Experience.model_validate_json(row['payload'])
                    if digest(json.loads(row['payload'])) != row['digest']:
                        raise ValueError("affect_source_integrity_failure")
                    if source.kind != "observation":
                        raise ValueError("affect_requires_external_observation")
                    sources.append((source, row['digest']))
                primary = sources[0][0]
                if primary.occurred_at.timestamp() > now + 5:
                    raise ValueError("affect_source_from_future")
                subject = primary.author_id if event.target == "author" else primary.record_id if event.target == "event" else "self"
                checked.append((event, sources, subject))
            receipts = []
            for event, sources, subject in checked:
                primary = sources[0][0]
                prior = db.execute("SELECT revision FROM agent_affect_events_v1 WHERE independent_key=?", (primary.independent_key,)).fetchone()
                if prior is not None:
                    db.execute("UPDATE agent_affect_pending_v1 SET status='appraised' WHERE source=?", (event.source_id,))
                    receipts.append({"source_id": event.source_id, "status": "already_appraised", "revision": prior[0]})
                    continue
                cursor = db.execute("INSERT INTO agent_affect_events_v1(source,independent_key,conversation,subject,target,occurred,committed,payload) VALUES(?,?,?,?,?,?,?,?)",
                    (event.source_id, primary.independent_key, conversation, subject, event.target, primary.occurred_at.timestamp(), now,
                     canonical(event.model_dump(mode='json'))))
                db.executemany("INSERT INTO agent_affect_sources_v1 VALUES(?,?,?)",
                    [(event.source_id, s.record_id, version) for s, version in sources])
                db.execute("UPDATE agent_affect_pending_v1 SET status='appraised' WHERE source=?", (event.source_id,))
                feedback = {"source_id": event.source_id, "revision": cursor.lastrowid,
                    "occurred_at": primary.occurred_at.isoformat(), "source_refs": list(primary.source_refs),
                    "source_versions": {s.record_id: version for s, version in sources},
                    "assessment": {k: getattr(event, k) for k in ('topic', 'unexpectedness', 'meaning', 'confidence', 'impacts')},
                    "engagement": event.engagement,
                    "intent_until": primary.occurred_at.timestamp() + self.settings.intent_seconds}
                db.execute("INSERT INTO agent_affect_feedback_v1 VALUES(?,?,0)", (event.source_id, canonical(feedback)))
                receipts.append({"source_id": event.source_id, "status": "appraised", "revision": cursor.lastrowid})
        return {"status": "recorded", "authority": "revisable_interpretation_not_fact", "events": receipts}

    def requeue_unshown(self, sources):
        with closing(self.memory._connect()) as db, db:
            db.executemany("UPDATE agent_affect_pending_v1 SET status='pending',job=NULL,ready=? WHERE source=? AND status='queued'",
                [(self.clock() + self.settings.appraisal_window_seconds, source) for source in sources])

    @staticmethod
    def valid_clause():
        return """NOT EXISTS(SELECT 1 FROM agent_affect_sources_v1 a LEFT JOIN pm_sources s ON s.id=a.source
            WHERE a.event=e.source AND (s.id IS NULL OR s.revoked<>0 OR s.digest<>a.digest))"""

    def snapshot(self, conversation, *, causes=False, subject_id=None):
        conv, now = self.store.get(conversation), self.clock()
        feeling_tau, mood_tau = self.settings.feeling_half_life_seconds, self.settings.mood_half_life_seconds
        with closing(self.memory._connect()) as db:
            rows = db.execute("SELECT e.* FROM agent_affect_events_v1 e WHERE " + self.valid_clause() +
                " AND (e.occurred>=? OR json_extract(e.payload,'$.relationship') IS NOT NULL) ORDER BY e.occurred,e.revision",
                (now - 8 * mood_tau,)).fetchall()
            # Known identity is scoped; a private cause never introduces a new
            # person's identity into another audience's state projection.
            known_authors = {r[0] for r in db.execute("SELECT DISTINCT s.author FROM pm_sources s JOIN pm_visibility v ON v.id=s.id WHERE s.revoked=0 AND v.audience=?", (conv['audience'],))}
            mood_v = mood_a = 0.0
            mood_at = None
            labels, objects, relationships, visible_causes, cause_candidates = {}, {}, {}, [], []
            intent = None
            revision = db.execute("SELECT coalesce(max(revision),0) FROM agent_affect_events_v1").fetchone()[0]
            for row in rows:
                event = json.loads(row['payload'])
                age = max(0, now - row['occurred'])
                short = event['confidence'] * math.exp2(-age / feeling_tau)
                at = min(now, row['occurred'])
                decay = math.exp2(-(at-mood_at)/mood_tau) if mood_at is not None else 1.0
                alpha = event['confidence']
                mood_v = (1-alpha)*mood_v*decay + alpha*event['valence']
                mood_a = (1-alpha)*mood_a*decay + alpha*event['arousal']
                mood_at = at
                for label, intensity in event['feelings'].items():
                    labels[label] = max(labels.get(label, 0), intensity * short)
                if row['occurred'] + self.settings.intent_seconds > now:
                    intent = {"choice": event['engagement'], "valid_until": row['occurred'] + self.settings.intent_seconds}
                if row['target'] == 'author' and row['subject'] in known_authors:
                    entry = objects.setdefault(row['subject'], {"subject_id": row['subject'], "valence": 0.0, "feelings": {}})
                    entry['valence'] = round(event['valence'] * short, 3)
                    entry['feelings'] = {k: round(v * short, 3) for k,v in event['feelings'].items() if v * short >= .01}
                    if event.get('relationship'):
                        relationships.setdefault(row['subject'], []).append((row,event))
                if causes and (subject_id is None or row['subject'] == subject_id):
                    cause_candidates.append((row, event))
            exposed_objects = list(objects.values())[-3:]
            for entry in exposed_objects:
                for row,event in reversed(relationships.get(entry['subject_id'], [])):
                    if self._cause_visible(db, row, event, conv['audience']):
                        entry['relationship'] = event['relationship']
                        break
            # Only eight causes can be returned. Do not hash and parse every
            # archived original merely to discard all but the newest eight.
            for row,event in reversed(cause_candidates):
                if self._cause_visible(db, row, event, conv['audience']):
                    visible_causes.insert(0, {"source_id": row['source'], "subject_id": row['subject'],
                        "occurred_at": datetime.fromtimestamp(row['occurred'], timezone.utc).isoformat(),
                        "interpretation": event['interpretation'], "confidence": event['confidence']})
                    if len(visible_causes) == 8:
                        break
            # A decaying weighted mean divided by its decaying weights would
            # stay constant after a busy conversation. Decay the actual state.
            tail = math.exp2(-(now-mood_at)/mood_tau) if mood_at is not None else 1.0
            result = {"revision": revision, "as_of": datetime.fromtimestamp(now, timezone.utc).isoformat(),
                "status": "observed" if rows else "unassessed", "authority": "subjective_interpretation",
                "mood": {"valence": round(mood_v * tail, 3),
                    "arousal": round(mood_a * tail, 3)},
                "feelings": dict(sorted(((k,round(v,3)) for k,v in labels.items() if v >= .01), key=lambda item:-item[1])[:3]),
                "engagement": intent or {"choice": "undecided"},
                "objects": exposed_objects}
            if causes:
                result['causes'] = visible_causes[-8:]
            return result

    def _cause_visible(self, db, row, event, audience):
        return self.memory._visible(db, row['source'], audience) and all(
            self.memory._visible(db, ref, audience) for ref in event['context_source_ids'])

    def feedback(self, *, limit=64):
        with closing(self.memory._connect()) as db:
            rows = db.execute("SELECT f.payload FROM agent_affect_feedback_v1 f JOIN agent_affect_events_v1 e ON e.source=f.source WHERE f.acknowledged=0 AND " + self.valid_clause() + " ORDER BY e.revision LIMIT ?", (limit,)).fetchall()
            retracted = db.execute("SELECT f.payload FROM agent_affect_feedback_v1 f JOIN agent_affect_events_v1 e ON e.source=f.source WHERE f.acknowledged=1 AND NOT (" + self.valid_clause() + ") ORDER BY e.revision LIMIT ?", (limit,)).fetchall()
            return ([{**json.loads(r[0]), 'kind': 'retract'} for r in retracted] +
                [{**json.loads(r[0]), 'kind': 'appraise'} for r in rows])[:limit]

    def acknowledge(self, entries):
        with closing(self.memory._connect()) as db, db:
            db.executemany("UPDATE agent_affect_feedback_v1 SET acknowledged=? WHERE source=?",
                [(2 if entry.get('kind') == 'retract' else 1, entry['source_id']) for entry in entries])

    def status(self):
        with closing(self.memory._connect()) as db:
            counts = dict(db.execute("SELECT status,count(*) FROM agent_affect_pending_v1 GROUP BY status"))
            started = db.execute("SELECT value FROM agent_affect_meta_v1 WHERE key='shadow_started'").fetchone()
            started = float(started[0]) if started else None
            elapsed = max(0, self.clock() - started) if started is not None else 0
            valid = db.execute('SELECT count(*) FROM agent_affect_events_v1 e WHERE e.occurred>=? AND ' +
                self.valid_clause(), (started if started is not None else self.clock(),)).fetchone()[0]
            return {"mode": self.settings.mode, "pending": counts,
                'shadow': {'started_at':started, 'elapsed_seconds':round(elapsed), 'valid_natural_events':valid,
                    'observation_gate_met':started is not None and elapsed >= 86400 and valid >= 100},
                "appraised": db.execute("SELECT count(*) FROM agent_affect_events_v1").fetchone()[0],
                "calls_last_hour": db.execute("SELECT count(*) FROM agent_affect_calls_v1 WHERE at>?", (self.clock()-3600,)).fetchone()[0]}
