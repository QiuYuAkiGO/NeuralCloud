"""Import a reviewed offline replay through the existing Cognitive owner."""
from __future__ import annotations

import asyncio
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from gui.presets import CharacterPreset, atomic_write_json
from harness.cognitive.qq_memory import _hash, build_commit, install_committed, memory_state, snapshot_owner
from harness.memory.qq_assimilation import QQAssimilationQueue, QQSource
from harness.memory.qq_backfill import _backup_readonly, _maintain, _manifest_audiences


def validate_bundle(directory, expected_sha256, settings):
    root = Path(directory).resolve(strict=True)
    raw = (root / 'preset.json').read_bytes()
    if hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise ValueError('qq_backfill_reviewed_artifact_changed')
    result = CharacterPreset.model_validate_json(raw)
    initial_bytes = (root / 'input-preset.json').read_bytes()
    initial = CharacterPreset.model_validate_json(initial_bytes)
    manifest = json.loads((root / 'source_manifest.json').read_text(encoding='utf-8'))
    report = json.loads((root / 'report.json').read_text(encoding='utf-8'))
    if (not report.get('completed') or manifest.get('owner_id') != 'misty'
            or result.preset_id != initial.preset_id or result.preset_id != 'misty'
            or hashlib.sha256(initial_bytes).hexdigest() != manifest['input_preset_sha256']):
        raise ValueError('qq_backfill_incomplete_or_owner_mismatch')
    queue = QQAssimilationQueue(root / 'queue.sqlite3', owner_id='misty', preset_id='misty',
        self_id=manifest['self_id'], allowed_group_ids=manifest['group_ids'],
        allowed_private_peer_ids=manifest.get('private_peer_ids', []))
    with closing(queue._connect(write=False)) as connection:
        rows = [dict(row) for row in connection.execute('SELECT * FROM qq_memory_jobs ORDER BY rowid')]
        assignments = [dict(row) for row in connection.execute('SELECT * FROM qq_memory_job_sources ORDER BY rowid')]
    jobs = [queue._decode_job(row) for row in rows]
    sources = {source.source_id: source for job in jobs for source in job.sources}
    if ({item['source_id']: item['source_sha256'] for item in manifest['sources']}
            != {ref: source.source_sha256 for ref, source in sources.items()}
            or len(sources) != len(assignments)):
        raise ValueError('qq_backfill_source_coverage_mismatch')
    source_jobs = {source.source_id: job.job_id for job in jobs for source in job.sources}
    for assignment in assignments:
        source = QQSource.model_validate_json(assignment['frozen_json'])
        if (tuple(assignment[key] for key in ('owner_id', 'preset_id', 'processing_version', 'self_id')) != queue._scope
                or source != sources.get(assignment['source_id'])
                or assignment['source_sha256'] != source.source_sha256
                or assignment['job_id'] != source_jobs[source.source_id]):
            raise ValueError('qq_backfill_assignment_source_mismatch')
    # Recompute the full derivation from retained model proposals. Reviewing a
    # preset hash cannot by itself establish that its effects match its receipts.
    replay = initial
    state = memory_state(result.qq_memory_state)
    policy = settings.model_copy(update=manifest['policy'])
    expected_receipts = set(memory_state(initial.qq_memory_state)['receipts']) | {job.job_id for job in jobs}
    if set(state['receipts']) != expected_receipts:
        raise ValueError('qq_backfill_receipt_coverage_mismatch')
    for job in jobs:
        if job.status != 'applied' or job.proposal is None:
            raise ValueError('qq_backfill_uncommitted_window')
        receipt = state['receipts'][job.job_id]
        replay = build_commit(replay, job, job.proposal, policy,
                              now=datetime.fromisoformat(receipt['committed_at']))
    replay = _maintain(replay, policy, datetime.fromisoformat(state['last_review_at']))
    if replay.model_dump(mode='json') != result.model_dump(mode='json'):
        raise ValueError('qq_backfill_effects_do_not_match_proposals')
    return root, initial, result, manifest, queue, rows, assignments, sources


def _stage_queue(live, shadow_rows, shadow_assignments, *, reviewed_rebase=False):
    with closing(live._connect(write=True)) as connection, connection:
        connection.execute('BEGIN IMMEDIATE')
        job_columns = {row[1] for row in connection.execute('PRAGMA table_info(qq_memory_jobs)')}
        source_columns = {row[1] for row in connection.execute('PRAGMA table_info(qq_memory_job_sources)')}
        if any(set(row) != job_columns for row in shadow_rows) or any(set(row) != source_columns for row in shadow_assignments):
            raise ValueError('qq_backfill_queue_schema_mismatch')
        for row in shadow_rows:
            old = connection.execute('SELECT * FROM qq_memory_jobs WHERE job_id=?', (row['job_id'],)).fetchone()
            if old is not None:
                if any(old[key] != row[key] for key in ('source_digest', 'frozen_json')):
                    raise ValueError('qq_backfill_existing_job_conflict')
                if any(old[key] != row[key] for key in ('proposal_json', 'proposal_sha256')):
                    if not reviewed_rebase:
                        raise ValueError('qq_backfill_existing_job_conflict')
                    # Sources stay immutable. The previous complete queue is in
                    # the backup; the revised preset is still the commit point.
                    connection.execute('UPDATE qq_memory_jobs SET proposal_json=?,proposal_sha256=?, '
                        'proposal_basis_digest=? WHERE job_id=?',
                        (row['proposal_json'], row['proposal_sha256'], row['proposal_basis_digest'], row['job_id']))
                continue
            staged = {**row, 'status': 'pending', 'next_attempt_at': None}
            columns = list(staged)
            connection.execute(f"INSERT INTO qq_memory_jobs({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                               [staged[key] for key in columns])
        for row in shadow_assignments:
            old = connection.execute('SELECT * FROM qq_memory_job_sources WHERE owner_id=? AND preset_id=? '
                'AND processing_version=? AND self_id=? AND source_id=?',
                tuple(row[key] for key in ('owner_id', 'preset_id', 'processing_version', 'self_id', 'source_id'))).fetchone()
            if old is not None:
                if dict(old) != row:
                    raise ValueError('qq_backfill_existing_assignment_conflict')
                continue
            columns = list(row)
            connection.execute(f"INSERT INTO qq_memory_job_sources({','.join(columns)}) VALUES({','.join('?' for _ in columns)})",
                               [row[key] for key in columns])


def _private_projection(preset, audiences):
    """Only the selected conversations can invalidate a private replay basis."""
    def selected(scope):
        if isinstance(scope, dict):
            return (scope.get('owner_id') == 'misty' and scope.get('channel') == 'qq_private'
                    and scope.get('conversation_id') in audiences)
        return bool(scope and scope.owner_id == 'misty' and scope.channel == 'qq_private'
                    and scope.conversation_id in audiences)

    projection = {}
    for field, identity in (('episodic', 'event_id'), ('candidates', 'candidate_id'), ('semantic', 'fact_id')):
        projection[field] = {getattr(row, identity): row.model_dump(mode='json')
                             for row in getattr(preset, field) if selected(row.scope)}
    state = memory_state(preset.qq_memory_state)
    projection['impressions'] = {key: raw for key, raw in state['impressions'].items()
                                  if selected(raw.get('scope'))}
    episode_ids = set(projection['episodic'])
    projection['receipts'] = {}
    for key, receipt in state['receipts'].items():
        refs = set(receipt['episode_ids'])
        if refs & episode_ids:
            if not refs <= episode_ids:
                raise ValueError('qq_backfill_private_receipt_crosses_scope')
            projection['receipts'][key] = receipt
    memory_ids = episode_ids | set(projection['impressions'])
    projection['recalls'] = {key: [ref for ref in refs if ref in memory_ids]
                            for key, refs in state['recalls'].items() if memory_ids.intersection(refs)}
    return projection


def _private_counts(projection):
    statuses = [row['status'] for row in projection['impressions'].values()]
    return {'episodes': len(projection['episodic']), 'impressions': len(statuses),
            'stable_judgments': statuses.count('stable'), 'windows': len(projection['receipts'])}


def _merge_private(current, initial, result, audiences):
    baseline = _private_projection(initial, audiences)
    target = _private_projection(result, audiences)
    current_projection = _private_projection(current, audiences)
    added_receipts = target['receipts'].keys() - baseline['receipts'].keys()
    current_receipts = memory_state(current.qq_memory_state)['receipts']
    for key in added_receipts & current_receipts.keys():
        if current_receipts[key] != target['receipts'][key]:
            raise ValueError('qq_backfill_private_receipt_conflict')
    if added_receipts and added_receipts <= current_receipts.keys():
        # A successful import can lose its ACK while the owner continues to
        # adopt, age or revise these memories. Matching durable receipts prove
        # the artifact was applied; do not require its old strength/confidence
        # projection or overwrite subsequent evolution to confirm that commit.
        for key in added_receipts:
            for ref in target['receipts'][key]['episode_ids']:
                present = current_projection['episodic'].get(ref)
                expected = target['episodic'].get(ref)
                if (present is None or expected is None or present['scope'] != expected['scope']
                        or present['source_refs'] != expected['source_refs']):
                    raise ValueError('qq_backfill_private_receipt_effect_mismatch')
        return None, _private_counts(target)
    if current_projection == target:
        return None, _private_counts(target)
    if current_projection != baseline:
        raise ValueError('qq_backfill_private_memory_changed')
    merged = {}
    for field, identity in (('episodic', 'event_id'), ('candidates', 'candidate_id'), ('semantic', 'fact_id')):
        # install_committed upserts episodes/candidates; deletion is unsupported.
        if field != 'semantic' and baseline[field].keys() - target[field].keys():
            raise ValueError('qq_backfill_private_cannot_remove_committed_record')
        present = {getattr(row, identity): row for row in getattr(current, field)}
        for key in baseline[field]:
            present.pop(key)
        for row in getattr(result, field):
            key = getattr(row, identity)
            if key not in target[field]:
                continue
            if key in present:
                raise ValueError('qq_backfill_private_record_identity_conflict')
            present[key] = row
        merged[field] = list(present.values())
    state = deepcopy(memory_state(current.qq_memory_state))
    for field in ('impressions', 'receipts'):
        for key in baseline[field]:
            state[field].pop(key)
        for key, value in target[field].items():
            if key in state[field]:
                raise ValueError('qq_backfill_private_state_identity_conflict')
            state[field][key] = deepcopy(value)
    # Global lifecycle/adoption state belongs to the current owner. In
    # particular, a shadow's final aging must not rewind live group activity.
    state['revision'] += 1
    merged['qq_memory_state'] = state
    return current.model_copy(update=merged), _private_counts(target)


async def _import_private(host, bundle):
    root, initial, result, manifest, _, rows, assignments, sources = bundle
    if manifest['group_ids']:
        raise ValueError('qq_backfill_mixed_private_import_not_supported')
    qq = host.shared.qq_settings
    peers = manifest['private_peer_ids']
    if (qq is None or manifest['self_id'] != qq.self_member_id
            or not set(peers) <= set(getattr(qq, 'allowed_private_peer_ids', ()))):
        raise ValueError('qq_backfill_current_authorization_mismatch')
    live = QQAssimilationQueue(host.shared.store.path, owner_id='misty', preset_id='misty',
        self_id=qq.self_member_id, allowed_group_ids=[], allowed_private_peer_ids=peers)
    await asyncio.to_thread(host.shared.sync_qq)
    live_sources = {source.source_id: source for source in await asyncio.to_thread(live.snapshot)}
    if any(ref not in live_sources or source.source_sha256 != live_sources[ref].source_sha256
           for ref, source in sources.items()):
        raise ValueError('qq_backfill_live_source_changed')
    async with host.adapter._lock:
        current = snapshot_owner(host)
        if current.preset_id != result.preset_id:
            raise ValueError('qq_backfill_current_preset_changed')
        committed, counts = _merge_private(current, initial, result, _manifest_audiences(manifest))
        if committed is None:
            await asyncio.to_thread(live.recover, set(memory_state(current.qq_memory_state)['receipts']))
            return {'imported': False, 'replayed': True, 'observations': len(sources),
                    'imported_scope_counts': counts}
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = root / 'live-backup' / stamp
        backup.mkdir(parents=True, exist_ok=False)
        atomic_write_json(backup / 'preset.json', current.model_dump(mode='json'))
        await asyncio.to_thread(_backup_readonly, live.path, backup / 'shared.sqlite3')
        await asyncio.to_thread(_stage_queue, live, rows, assignments)
        await asyncio.to_thread(host.chat.library.save, committed)
        install_committed(host, committed)
        await asyncio.to_thread(live.recover, set(memory_state(committed.qq_memory_state)['receipts']))
        # The live worker may hold a group proposal outside the commit lock.
        # Keep its queue object valid; the next poll sees the staged receipts,
        # and the revision check rebases its proposal on this committed owner.
        return {'imported': True, 'replayed': False, 'observations': len(sources), 'windows': len(rows),
                'backup': str(backup), 'imported_scope_counts': counts, **host.qq_memory.status()}


async def import_backfill(host, output_dir, *, expected_preset_sha256, expected_owner_state_sha256=None):
    """The caller holds host._control_lock; validation runs off its reply lock."""
    worker = host.qq_memory
    bundle = await asyncio.to_thread(validate_bundle, output_dir, expected_preset_sha256, host.chat.settings)
    root, initial, result, manifest, _, rows, assignments, sources = bundle
    if manifest.get('private_peer_ids'):
        return await _import_private(host, bundle)
    if worker.control.enabled or worker.control.recall_enabled or worker.processing:
        raise ValueError('qq_backfill_requires_paused_memory_switches')
    qq = host.shared.qq_settings
    if (qq is None or manifest['self_id'] != qq.self_member_id
            or set(manifest['group_ids']) != set(qq.allowed_group_ids)):
        raise ValueError('qq_backfill_current_authorization_mismatch')
    live = QQAssimilationQueue(host.shared.store.path, owner_id='misty', preset_id='misty',
        self_id=qq.self_member_id, allowed_group_ids=qq.allowed_group_ids)
    await asyncio.to_thread(host.shared.sync_qq)
    live_sources = {source.source_id: source for source in await asyncio.to_thread(live.snapshot)}
    if any(ref not in live_sources or source.source_sha256 != live_sources[ref].source_sha256
           for ref, source in sources.items()):
        raise ValueError('qq_backfill_live_source_changed')
    async with host.adapter._lock:
        current = snapshot_owner(host)
        if current.preset_id != result.preset_id:
            raise ValueError('qq_backfill_current_preset_changed')
        current_state = memory_state(current.qq_memory_state)
        if current_state == memory_state(result.qq_memory_state):
            await asyncio.to_thread(live.recover, set(current_state['receipts']))
            return {'imported': False, 'replayed': True, 'observations': len(sources)}
        reviewed_rebase = expected_owner_state_sha256 is not None
        if reviewed_rebase:
            if _hash(current_state) != expected_owner_state_sha256:
                raise ValueError('qq_backfill_review_basis_changed')
            result_state = memory_state(result.qq_memory_state)
            if any(ref not in result_state['receipts'] or receipt['source_digest'] !=
                   result_state['receipts'][ref]['source_digest'] for ref, receipt in current_state['receipts'].items()):
                raise ValueError('qq_backfill_review_drops_committed_sources')
            # This rollout repair replays evidence only. It must not silently
            # discard real decision adoptions made since the frozen baseline.
            if current_state['recalls'] != memory_state(initial.qq_memory_state)['recalls']:
                raise ValueError('qq_backfill_review_has_unreplayed_adoptions')
            for field, identity in (('episodic', 'event_id'), ('candidates', 'candidate_id')):
                revised_ids = {getattr(row, identity) for row in getattr(result, field)}
                if any(row.scope and row.scope.channel == 'qq_group'
                       and getattr(row, identity) not in revised_ids for row in getattr(current, field)):
                    raise ValueError('qq_backfill_review_cannot_remove_committed_record')
        elif current_state != memory_state(initial.qq_memory_state):
            raise ValueError('qq_backfill_owner_memory_changed')
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
        backup = root / 'live-backup' / stamp
        backup.mkdir(parents=True, exist_ok=False)
        atomic_write_json(backup / 'preset.json', current.model_dump(mode='json'))
        await asyncio.to_thread(_backup_readonly, live.path, backup / 'shared.sqlite3')
        merged = {}
        for field, identity in (('episodic', 'event_id'), ('candidates', 'candidate_id'), ('semantic', 'fact_id')):
            initial_rows = {getattr(row, identity): row for row in getattr(initial, field)}
            result_rows = {getattr(row, identity): row for row in getattr(result, field)}
            delta = [row for key, row in result_rows.items() if row != initial_rows.get(key)
                     or (reviewed_rebase and row.scope and row.scope.channel == 'qq_group')]
            present = {getattr(row, identity): row for row in getattr(current, field)}
            if reviewed_rebase:
                covered = {ref for receipt in current_state['receipts'].values() for ref in receipt['source_ids']}
                for key, item in list(present.items()):
                    if item.scope and item.scope.channel == 'qq_group':
                        if (item.scope.owner_id != 'misty' or not item.source_refs
                                or not set(item.source_refs) <= covered):
                            raise ValueError('qq_backfill_review_contains_uncovered_memory')
                        present.pop(key)
            for item in delta:
                key = getattr(item, identity)
                if not item.scope or item.scope.channel != 'qq_group':
                    raise ValueError('qq_backfill_cannot_change_local_memory')
                if key in present and present[key] not in (item, initial_rows.get(key)):
                    raise ValueError('qq_backfill_existing_memory_conflict')
                present[key] = item
            for key in initial_rows.keys() - result_rows.keys():
                if reviewed_rebase and key not in present:
                    continue
                if present.get(key) != initial_rows[key] or not initial_rows[key].scope or initial_rows[key].scope.channel != 'qq_group':
                    raise ValueError('qq_backfill_existing_memory_conflict')
                present.pop(key, None)
            merged[field] = list(present.values())
        merged['qq_memory_state'] = result.qq_memory_state
        committed = current.model_copy(update=merged)
        await asyncio.to_thread(_stage_queue, live, rows, assignments, reviewed_rebase=reviewed_rebase)
        await asyncio.to_thread(host.chat.library.save, committed)
        install_committed(host, committed)
        await asyncio.to_thread(live.recover, set(memory_state(committed.qq_memory_state)['receipts']))
        worker.queue = None
        return {'imported': True, 'replayed': False, 'reviewed_rebase': reviewed_rebase, 'observations': len(sources),
                'windows': len(rows), 'backup': str(backup), **worker.status()}
