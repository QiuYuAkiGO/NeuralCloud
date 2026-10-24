"""Explicit maintenance migration for a tool-policy-only system prompt change.

Never called automatically on an identity mismatch. The operator must stop the
Cognitive writer and provide the verified old and new system strings. Original
events, working state and completed job receipts retain their original bytes.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json

from harness.agentic.store import canonical, digest


def _core(system):
    prefix = '你是 Misty（小雾）。完整、固定的 PersonaCore：\n'
    core, separator, _ = system.partition('\n\n')
    if not separator or not core.startswith(prefix):
        raise ValueError('policy_migration_requires_canonical_system')
    return json.loads(core.removeprefix(prefix))


def migrate_policy(db, *, old_system, new_system):
    """Commit only same-Persona policy bindings, with one durable audit record."""
    if _core(old_system) != _core(new_system):
        raise ValueError('policy_migration_cannot_change_persona_core')
    old, new = digest(old_system), digest(new_system)
    if old == new:
        raise ValueError('policy_migration_requires_changed_policy')
    identity = 'policy-migration:' + digest([old, new])[:32]
    with db:
        db.execute('BEGIN IMMEDIATE')
        db.execute('''CREATE TABLE IF NOT EXISTS agent_policy_migrations_v1(
            id TEXT PRIMARY KEY,receipt TEXT NOT NULL)''')
        prior = db.execute('SELECT receipt FROM agent_policy_migrations_v1 WHERE id=?', (identity,)).fetchone()
        if prior:
            return {**json.loads(prior[0]), 'already_applied':True}
        conversations = [row[0] for row in db.execute('SELECT id FROM agent_conversations_v2 WHERE persona=?', (old,))]
        checkpoints = []
        jobs = []
        for conversation in conversations:
            checkpoints.extend([[conversation,*row] for row in db.execute(
                'SELECT first_seq,last_seq FROM agent_compactions_v2 WHERE conversation=? AND persona=?',
                (conversation,old))])
            for row in db.execute("SELECT id,payload,status FROM agent_jobs_v2 WHERE conversation=? AND kind='compact' AND status IN ('queued','running')", (conversation,)):
                payload = json.loads(row[1])
                if payload.get('persona') != old:
                    raise ValueError('policy_migration_pending_job_binding_mismatch')
                if row[2] == 'running':
                    raise ValueError('policy_migration_requires_drained_writer')
                jobs.append((row[0], row[1], canonical({**payload,'persona':new})))
        db.executemany('UPDATE agent_conversations_v2 SET persona=? WHERE id=? AND persona=?',
            [(new,conversation,old) for conversation in conversations])
        for conversation in conversations:
            db.execute('UPDATE agent_compactions_v2 SET persona=? WHERE conversation=? AND persona=?',
                (new,conversation,old))
        db.executemany('UPDATE agent_jobs_v2 SET payload=? WHERE id=? AND payload=?',
            [(updated,identifier,original) for identifier,original,updated in jobs])
        receipt = {'id':identity, 'at':datetime.now(timezone.utc).isoformat(),
            'kind':'explicit_same_persona_tool_policy_migration', 'old_system_sha256':old,'new_system_sha256':new,
            'persona_core_sha256':digest(_core(new_system)), 'conversations':conversations,
            'checkpoints':checkpoints, 'pending_compaction_jobs':[{'id':identifier,'previous_payload':original}
                for identifier,original,_ in jobs], 'already_applied':False}
        db.execute('INSERT INTO agent_policy_migrations_v1 VALUES(?,?)', (identity,canonical(receipt)))
    return receipt
