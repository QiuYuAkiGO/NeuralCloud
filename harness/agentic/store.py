"""Additive v2 conversation/outbox tables in the production memory authority.

Original events and their ingestion jobs share one SQLite transaction. No model
draft is archived as spoken. All public reads require a bound conversation.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import json
import time

from harness.memory.production import Experience


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


class AgentStore:
    def __init__(self, memory):
        self.memory = memory
        self.affect = None
        with closing(memory._connect()) as db, db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS agent_conversations_v2(
                    id TEXT PRIMARY KEY,audience TEXT NOT NULL,entrance TEXT NOT NULL,
                    persona TEXT NOT NULL,version INTEGER NOT NULL DEFAULT 0,
                    state TEXT NOT NULL DEFAULT '{}',next_seq INTEGER NOT NULL DEFAULT 1,
                    UNIQUE(audience,entrance));
                CREATE TABLE IF NOT EXISTS agent_events_v2(
                    conversation TEXT NOT NULL,seq INTEGER NOT NULL,source TEXT NOT NULL,
                    role TEXT NOT NULL,time TEXT NOT NULL,tokens INTEGER NOT NULL,
                    PRIMARY KEY(conversation,seq),UNIQUE(conversation,source));
                CREATE INDEX IF NOT EXISTS agent_events_source_v2 ON agent_events_v2(source);
                CREATE TABLE IF NOT EXISTS agent_compactions_v2(
                    conversation TEXT NOT NULL,first_seq INTEGER NOT NULL,last_seq INTEGER NOT NULL,
                    source_hash TEXT NOT NULL,persona TEXT NOT NULL,summary TEXT NOT NULL,
                    PRIMARY KEY(conversation,first_seq,last_seq));
                CREATE TABLE IF NOT EXISTS agent_jobs_v2(
                    id TEXT PRIMARY KEY,conversation TEXT NOT NULL,source TEXT NOT NULL,
                    operation TEXT NOT NULL,kind TEXT NOT NULL,payload TEXT NOT NULL,
                    cutoff INTEGER NOT NULL,status TEXT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
                    lease TEXT,lease_until REAL,available_at REAL NOT NULL,created REAL NOT NULL,
                    result TEXT,UNIQUE(conversation,source,operation));
                CREATE INDEX IF NOT EXISTS agent_jobs_pending_v2 ON agent_jobs_v2(status,available_at,created);
                CREATE TABLE IF NOT EXISTS agent_notices_v2(
                    id TEXT PRIMARY KEY,conversation TEXT NOT NULL,job TEXT NOT NULL UNIQUE,
                    payload TEXT NOT NULL,delivery_receipt TEXT);
                CREATE TABLE IF NOT EXISTS agent_artifacts_v2(
                    job TEXT PRIMARY KEY,kind TEXT NOT NULL,payload TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS agent_job_transitions_v1(
                    seq INTEGER PRIMARY KEY AUTOINCREMENT,job TEXT NOT NULL,at REAL NOT NULL,
                    status TEXT NOT NULL,detail TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS agent_job_transitions_time_v1 ON agent_job_transitions_v1(at);
                CREATE INDEX IF NOT EXISTS agent_job_transitions_job_v1 ON agent_job_transitions_v1(job,at);
                CREATE TABLE IF NOT EXISTS agent_compaction_receipts_v1(
                    conversation TEXT NOT NULL,first_seq INTEGER NOT NULL,last_seq INTEGER NOT NULL,
                    committed REAL NOT NULL,PRIMARY KEY(conversation,first_seq,last_seq));
                CREATE INDEX IF NOT EXISTS agent_compaction_receipts_time_v1 ON agent_compaction_receipts_v1(committed);
                CREATE TABLE IF NOT EXISTS agent_report_meta_v1(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO agent_report_meta_v1 VALUES('started',?)", (str(time.time()),))

    def conversation(self, *, audience, entrance, persona):
        identifier = "conversation:" + digest([self.memory.owner_id, self.memory.preset_id, audience, entrance])[:32]
        with closing(self.memory._connect()) as db, db:
            db.execute("INSERT OR IGNORE INTO agent_conversations_v2(id,audience,entrance,persona) VALUES(?,?,?,?)",
                (identifier, audience, entrance, persona))
            row = db.execute("SELECT * FROM agent_conversations_v2 WHERE id=?", (identifier,)).fetchone()
            if row["persona"] != persona:
                raise ValueError("agent_persona_changed_requires_explicit_migration")
            return dict(row)

    def get(self, conversation):
        with closing(self.memory._connect()) as db:
            row = db.execute("SELECT * FROM agent_conversations_v2 WHERE id=?", (conversation,)).fetchone()
            if row is None:
                raise ValueError("unknown_conversation")
            return {**dict(row), "state": json.loads(row["state"])}

    def append(self, conversation, experience: Experience, *, role, tokens, assimilate=True):
        if role not in {"user", "assistant"}:
            raise ValueError("archive_role_invalid")
        if experience.kind == "legacy_episode":
            raise ValueError("archive_requires_original_event")
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            conv = db.execute("SELECT * FROM agent_conversations_v2 WHERE id=?", (conversation,)).fetchone()
            if conv is None or conv["audience"] not in experience.scope.visibility:
                raise ValueError("archive_scope_mismatch")
            self.memory._ingest(db, experience)
            prior = db.execute("SELECT seq FROM agent_events_v2 WHERE conversation=? AND source=?",
                (conversation, experience.record_id)).fetchone()
            if prior:
                return prior[0]
            seq = conv["next_seq"]
            db.execute("INSERT INTO agent_events_v2 VALUES(?,?,?,?,?,?)", (conversation, seq,
                experience.record_id, role, experience.occurred_at.isoformat(), tokens))
            db.execute("UPDATE agent_conversations_v2 SET next_seq=next_seq+1 WHERE id=?", (conversation,))
            if self.affect is not None and role == "user":
                self.affect.observe(db, conversation, experience)
            if assimilate and role == "user":
                self._enqueue(db, conversation, experience.record_id, "auto-memory", "memory",
                    {"source_ids": [experience.record_id], "proposal": "Extract only durable, source-supported facts; otherwise return no updates."}, seq)
            return seq

    @staticmethod
    def _enqueue(db, conversation, source, operation, kind, payload, cutoff):
        identifier = "job:" + digest([conversation, source, operation])[:32]
        prior = db.execute("SELECT * FROM agent_jobs_v2 WHERE id=?", (identifier,)).fetchone()
        if prior:
            if prior["kind"] != kind or prior["payload"] != canonical(payload):
                raise ValueError("operation_id_reused_with_different_arguments")
            return dict(prior)
        now = time.time()
        db.execute("""INSERT INTO agent_jobs_v2(id,conversation,source,operation,kind,payload,cutoff,
            status,available_at,created) VALUES(?,?,?,?,?,?,?,'queued',?,?)""",
            (identifier, conversation, source, operation, kind, canonical(payload), cutoff, now, now))
        return dict(db.execute("SELECT * FROM agent_jobs_v2 WHERE id=?", (identifier,)).fetchone())

    def enqueue(self, conversation, *, source, operation, kind, payload):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            conv = db.execute("SELECT * FROM agent_conversations_v2 WHERE id=?", (conversation,)).fetchone()
            if conv is None:
                raise ValueError("unknown_conversation")
            return self._enqueue(db, conversation, source, operation, kind, payload, conv["next_seq"] - 1)

    def events(self, conversation, *, after=0, through=None, limit=256, newest=False):
        conv = self.get(conversation)
        with closing(self.memory._connect()) as db:
            rows = db.execute("""SELECT e.*,s.payload,s.digest FROM agent_events_v2 e
                JOIN pm_sources s ON s.id=e.source WHERE e.conversation=? AND e.seq>? AND e.seq<=? AND s.revoked=0
                ORDER BY e.seq """ + ("DESC" if newest else "ASC") + " LIMIT ?",
                (conversation, after, through if through is not None else conv["next_seq"] - 1, limit)).fetchall()
            result = [{**dict(row), "original": json.loads(row["payload"])} for row in rows
                if self.memory._visible(db, row["source"], conv["audience"])]
        return sorted(result, key=lambda item: item["seq"])

    def validate_sources(self, conversation, identifiers):
        return self.memory.validate_references(identifiers, audience=self.get(conversation)["audience"])

    def state_update(self, conversation, *, version, persona, state):
        with closing(self.memory._connect()) as db, db:
            result = db.execute("UPDATE agent_conversations_v2 SET state=?,version=version+1 WHERE id=? AND version=? AND persona=?",
                (canonical(state), conversation, version, persona))
            if result.rowcount != 1:
                raise ValueError("conversation_state_superseded")

    def checkpoints(self, conversation, *, limit=4):
        conv = self.get(conversation)
        with closing(self.memory._connect()) as db:
            rows = db.execute("SELECT * FROM agent_compactions_v2 WHERE conversation=? ORDER BY last_seq DESC LIMIT ?",
                (conversation, limit)).fetchall()
            result = []
            for row in reversed(rows):
                sources = self._source_version(db, conversation, row["first_seq"], row["last_seq"], conv["audience"])
                if sources is not None and digest(sources) == row["source_hash"] and row["persona"] == conv["persona"]:
                    result.append({**dict(row), "source_ids": [source[1] for source in sources]})
            return result

    def _source_version(self, db, conversation, first, last, audience):
        rows = db.execute("""SELECT e.seq,e.source,s.digest FROM agent_events_v2 e
            JOIN pm_sources s ON s.id=e.source WHERE e.conversation=? AND e.seq BETWEEN ? AND ? ORDER BY e.seq""",
            (conversation, first, last)).fetchall()
        if not rows or any(not self.memory._visible(db, row["source"], audience) for row in rows):
            return None
        return [list(row) for row in rows]

    def checkpoint(self, conversation, *, first, last, source_hash, persona, summary):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            conv = db.execute("SELECT * FROM agent_conversations_v2 WHERE id=?", (conversation,)).fetchone()
            sources = self._source_version(db, conversation, first, last, conv["audience"])
            if conv["persona"] != persona or sources is None or digest(sources) != source_hash:
                raise ValueError("compaction_sources_superseded")
            # Does not overwrite working state or any newer tail.
            inserted = db.execute("INSERT INTO agent_compactions_v2 VALUES(?,?,?,?,?,?) ON CONFLICT DO NOTHING",
                (conversation, first, last, source_hash, persona, summary))
            if inserted.rowcount:
                db.execute("INSERT INTO agent_compaction_receipts_v1 VALUES(?,?,?,?)",
                    (conversation, first, last, time.time()))

    def jobs(self, conversation, identifier=None, *, kind=None):
        with closing(self.memory._connect()) as db:
            filters, values = [], [conversation]
            if identifier:
                filters.append("AND id=? ")
                values.append(identifier)
            if kind:
                filters.append("AND kind=? ")
                values.append(kind)
            rows = db.execute("SELECT * FROM agent_jobs_v2 WHERE conversation=? " +
                "".join(filters) + "ORDER BY CASE WHEN kind='diary' AND status IN ('queued','running') THEN 0 ELSE 1 END,created DESC LIMIT 20",
                values).fetchall()
        return [{**dict(row), "payload": json.loads(row["payload"]),
            "result": json.loads(row["result"]) if row["result"] else None} for row in rows]

    def claim(self, *, worker, lease_seconds=90, preferred_kind=None):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            now = time.time()
            # All effects have stable operation receipts; expired work may replay.
            db.execute("UPDATE agent_jobs_v2 SET status='queued',lease=NULL WHERE status='running' AND lease_until<?", (now,))
            row = db.execute("""SELECT * FROM agent_jobs_v2 WHERE status='queued' AND available_at<=?
                ORDER BY CASE WHEN kind='control' THEN 0 ELSE 1 END, CASE WHEN kind=? THEN 0 ELSE 1 END,
                CASE WHEN kind='memory' AND operation<>'auto-memory' THEN 0 ELSE 1 END,
                CASE WHEN kind='memory' THEN -created ELSE created END LIMIT 1""",
                (now, preferred_kind)).fetchone()
            if row is None:
                return None
            db.execute("UPDATE agent_jobs_v2 SET status='running',lease=?,lease_until=?,attempts=attempts+1 WHERE id=?",
                (worker, now + lease_seconds, row["id"]))
            return {**dict(row), "lease": worker, "status": "running", "payload": json.loads(row["payload"])}

    def release(self, identifier, worker, *, delay=1, failure=None):
        with closing(self.memory._connect()) as db, db:
            updated = db.execute("UPDATE agent_jobs_v2 SET status='queued',lease=NULL,available_at=? WHERE id=? AND status='running' AND lease=?",
                (time.time() + delay, identifier, worker))
            if updated.rowcount and failure:
                db.execute("INSERT INTO agent_job_transitions_v1(job,at,status,detail) VALUES(?,?,'retrying',?)",
                    (identifier, time.time(), canonical(failure)))

    def bounded_failure_count(self, identifier):
        """Count up to three retry failures, excluding explicit transient outages.

        Claims also occur during maintenance and foreground preemption; they are
        not failed executions. Old retry receipts without a policy remain counted
        conservatively. Existing terminal jobs are never reopened.
        """
        with closing(self.memory._connect()) as db:
            return db.execute("""SELECT count(*) FROM (
                SELECT 1 FROM agent_job_transitions_v1 WHERE job=? AND status='retrying'
                AND json_extract(detail,'$.retry_policy') IS NOT 'transient' LIMIT 3
                )""", (identifier,)).fetchone()[0]

    def finish(self, job, *, status, result, notify=False):
        if status not in {"applied", "rejected", "failed", "cancelled"}:
            raise ValueError("invalid_job_terminal_status")
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            updated = db.execute("UPDATE agent_jobs_v2 SET status=?,result=?,lease=NULL WHERE id=? AND status='running' AND lease=?",
                (status, canonical(result), job["id"], job["lease"]))
            if updated.rowcount != 1:
                raise ValueError("job_lease_lost_or_cancelled")
            # Timing belongs to the committed transition, not the enqueue time
            # or lease-attempt counter. Never backdate legacy terminal jobs.
            db.execute("INSERT INTO agent_job_transitions_v1(job,at,status,detail) VALUES(?,?,?,?)",
                (job["id"], time.time(), status, canonical({
                    key: result[key] for key in ("error", "reason", "validation") if key in result
                })))
            if notify:
                db.execute("INSERT OR IGNORE INTO agent_notices_v2 VALUES(?,?,?,?,NULL)",
                    ("notice:" + job["id"], job["conversation"], job["id"], canonical(result)))

    def cancel(self, conversation, identifier):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            # Committing artifacts and cancelling use the same transaction lock.
            if db.execute("SELECT 1 FROM agent_artifacts_v2 WHERE job=?", (identifier,)).fetchone():
                return False
            return db.execute("UPDATE agent_jobs_v2 SET status='cancelled',lease=NULL WHERE id=? AND conversation=? AND status IN ('queued','running')",
                (identifier, conversation)).rowcount == 1

    def artifact(self, identifier):
        with closing(self.memory._connect()) as db:
            row = db.execute("SELECT payload FROM agent_artifacts_v2 WHERE job=?", (identifier,)).fetchone()
            return json.loads(row[0]) if row else None

    def save_artifact(self, job, payload):
        with closing(self.memory._connect()) as db, db:
            db.execute("BEGIN IMMEDIATE")
            current = db.execute("SELECT status,lease FROM agent_jobs_v2 WHERE id=?", (job["id"],)).fetchone()
            if tuple(current or ()) != ("running", job["lease"]):
                raise ValueError("job_lease_lost_or_cancelled")
            db.execute("INSERT INTO agent_artifacts_v2 VALUES(?,?,?) ON CONFLICT DO NOTHING",
                (job["id"], job["kind"], canonical(payload)))
        return self.artifact(job["id"])

    def notices(self, conversation):
        with closing(self.memory._connect()) as db:
            return [{"id": row["id"], "job_id": row["job"], "result": json.loads(row["payload"])}
                for row in db.execute("SELECT * FROM agent_notices_v2 WHERE conversation=? AND delivery_receipt IS NULL ORDER BY rowid LIMIT 20", (conversation,))]

    def acknowledge_notices(self, conversation, identifiers, receipt):
        if not receipt:
            raise ValueError("delivery_receipt_required")
        with closing(self.memory._connect()) as db, db:
            db.executemany("UPDATE agent_notices_v2 SET delivery_receipt=? WHERE id=? AND conversation=? AND delivery_receipt IS NULL",
                [(receipt, identifier, conversation) for identifier in identifiers])
