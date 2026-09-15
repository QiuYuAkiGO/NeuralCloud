import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace

import pytest

from gui.presets import CharacterPreset, atomic_write_json
from harness.cognitive.qq_memory import QQMemoryControl, _hash, memory_state, snapshot_owner
from harness.cognitive.qq_memory_import import import_backfill, validate_bundle
from harness.memory.qq_assimilation import QQAssimilationQueue
from harness.memory.qq_backfill import run_backfill
from harness.memory.shared import SharedMemoryStore
from tests.test_qq_backfill import Proposer, inputs as inputs  # noqa: F401
from tests.test_qq_assimilation import add_message
from tests.test_basic_persona_continuity import _host, _prepare, _commit


def digest(inputs):
    return hashlib.sha256((inputs['output'] / 'preset.json').read_bytes()).hexdigest()


def test_bundle_replays_every_proposal_and_detects_altered_effect(inputs):
    from harness.config import Settings
    run_backfill(**inputs, proposer=Proposer(), progress=lambda _: None)
    validate_bundle(inputs['output'], digest(inputs), Settings())
    path = inputs['output'] / 'preset.json'
    raw = json.loads(path.read_text(encoding='utf-8'))
    raw['episodic'][0]['summary'] = '未由提案产生的结论'
    atomic_write_json(path, raw)
    with pytest.raises(ValueError, match='effects_do_not_match'):
        validate_bundle(inputs['output'], digest(inputs), Settings())


@pytest.mark.parametrize('failure', [False, True])
def test_import_preserves_current_local_turn_and_recovers_failed_save(inputs, tmp_path, monkeypatch, failure):
    async def run():
        host, main = _host(tmp_path / 'owner', monkeypatch, ['记住这次本地讨论。'])
        host.shared.qq_settings = SimpleNamespace(memory_db=None, self_member_id='999', allowed_group_ids=['100'])
        host.qq_memory.control = QQMemoryControl()
        # Use already-verified shared fixture records; no external transport.
        monkeypatch.setattr(host.shared, 'sync_qq', lambda: None)
        host.shared.store.path = inputs['source_db']
        initial = snapshot_owner(host)
        atomic_write_json(inputs['preset'], initial.model_dump(mode='json'))
        run_backfill(**inputs, proposer=Proposer(), progress=lambda _: None)
        original = snapshot_owner(host)
        prepared = await _prepare(host, main, 1, '这是清洗期间新增的本地消息。')
        await _commit(host, 1, prepared)
        local_ids = {event.event_id for event in host.chat.memory.stores.episodic.list_all()}
        try:
            if failure:
                with monkeypatch.context() as patch:
                    patch.setattr(host.chat.library, 'save', lambda _: (_ for _ in ()).throw(OSError('disk')))
                    with pytest.raises(OSError):
                        await import_backfill(host, inputs['output'], expected_preset_sha256=digest(inputs))
                assert memory_state(host.chat.memory.qq_memory_state) == memory_state(original.qq_memory_state)
            result = await import_backfill(host, inputs['output'], expected_preset_sha256=digest(inputs))
            assert result['imported'] and result['observations'] == 2
            saved = host.chat.library.load('misty')
            assert local_ids <= {event.event_id for event in saved.episodic}
            assert any('新增的本地消息' in turn.text for turn in saved.transcript)
            again = await import_backfill(host, inputs['output'], expected_preset_sha256=digest(inputs))
            assert again['replayed'] and not again['imported']
            assert list((inputs['output'] / 'live-backup').glob('*/preset.json'))
        finally:
            await host.close()
    asyncio.run(run())


class ReviewedProposer(Proposer):
    def propose(self, job, *, old_impressions, persona):
        proposal = super().propose(job, old_impressions=old_impressions, persona=persona)
        return proposal.model_copy(update={
            'episodes': tuple(episode.model_copy(update={'summary': '复核后：成员回答了当前问题。'})
                              for episode in proposal.episodes),
        })


async def _import_original_and_review(inputs, host, monkeypatch, *, proposer=None):
    host.shared.qq_settings = SimpleNamespace(
        memory_db=None, self_member_id='999', allowed_group_ids=['100'])
    host.qq_memory.control = QQMemoryControl()
    monkeypatch.setattr(host.shared, 'sync_qq', lambda: None)
    host.shared.store.path = inputs['source_db']
    atomic_write_json(inputs['preset'], snapshot_owner(host).model_dump(mode='json'))
    run_backfill(**inputs, proposer=Proposer(), progress=lambda _: None)
    reviewed = {**inputs, 'output': inputs['output'].with_name('reviewed')}
    run_backfill(**reviewed, proposer=proposer or ReviewedProposer(), progress=lambda _: None)
    await import_backfill(host, inputs['output'], expected_preset_sha256=digest(inputs))
    return reviewed


def _live_queue(inputs):
    return QQAssimilationQueue(inputs['source_db'], owner_id='misty', preset_id='misty',
                               self_id='999', allowed_group_ids=['100'])


def _memory_effects(preset):
    return {
        'episodic': {event.event_id: event for event in preset.episodic},
        'candidates': {candidate.candidate_id: candidate for candidate in preset.candidates},
        'semantic': {fact.fact_id: fact for fact in preset.semantic},
        'qq_memory_state': preset.qq_memory_state,
        'transcript': preset.transcript,
    }


@pytest.mark.parametrize('failure', [False, True])
def test_reviewed_rebase_updates_proposals_receipts_and_preserves_new_local_turn(
        inputs, tmp_path, monkeypatch, failure):
    async def run():
        host, main = _host(tmp_path / 'owner', monkeypatch, ['本地的新讨论会保留。'])
        try:
            reviewed = await _import_original_and_review(inputs, host, monkeypatch)
            original_state = memory_state(host.chat.memory.qq_memory_state)
            basis = _hash(original_state)
            # Local activity after review does not invalidate the QQ-only basis.
            prepared = await _prepare(host, main, 1, '复核期间新增的本地消息。')
            await _commit(host, 1, prepared)
            before = snapshot_owner(host)
            local_events = {event.event_id: event for event in before.episodic
                            if not event.scope or event.scope.channel != 'qq_group'}
            saved_before = host.chat.library.load('misty')
            revision_before = host.adapter._revision
            if failure:
                with monkeypatch.context() as patch:
                    patch.setattr(host.chat.library, 'save', lambda _: (_ for _ in ()).throw(OSError('disk')))
                    with pytest.raises(OSError, match='disk'):
                        await import_backfill(host, reviewed['output'],
                            expected_preset_sha256=digest(reviewed), expected_owner_state_sha256=basis)
                # The queue may stage the reviewed proposal, but the old preset
                # and all live effects remain the authoritative commit point.
                assert _memory_effects(snapshot_owner(host)) == _memory_effects(before)
                assert host.chat.library.load('misty') == saved_before
                assert host.adapter._revision == revision_before
                _live_queue(inputs).recover(set(original_state['receipts']))

            result = await import_backfill(host, reviewed['output'],
                expected_preset_sha256=digest(reviewed), expected_owner_state_sha256=basis)
            assert result['imported'] and result['reviewed_rebase']
            saved = host.chat.library.load('misty')
            artifact = CharacterPreset.model_validate_json((reviewed['output'] / 'preset.json').read_bytes())
            assert memory_state(saved.qq_memory_state) == memory_state(artifact.qq_memory_state)
            assert memory_state(host.chat.memory.qq_memory_state) == memory_state(saved.qq_memory_state)
            assert {event.event_id: event for event in saved.episodic
                    if event.event_id in local_events} == local_events
            assert saved.transcript == before.transcript
            assert any('复核期间新增' in turn.text for turn in saved.transcript)
            qq_events = [event for event in saved.episodic if event.scope and event.scope.channel == 'qq_group']
            assert qq_events and all(event.summary.startswith('复核后：') for event in qq_events)
            assert _memory_effects(snapshot_owner(host)) == _memory_effects(saved)
            for job_id, receipt in saved.qq_memory_state['receipts'].items():
                job = _live_queue(inputs).get_job(job_id)
                assert job.status == 'applied'
                assert receipt['proposal_digest'] == _hash(job.proposal.model_dump(mode='json'))
                assert receipt['proposal_digest'] != original_state['receipts'][job_id]['proposal_digest']
                assert receipt['source_digest'] == original_state['receipts'][job_id]['source_digest']
            again = await import_backfill(host, reviewed['output'],
                expected_preset_sha256=digest(reviewed), expected_owner_state_sha256=basis)
            assert again['replayed'] and not again['imported']
            assert host.chat.library.load('misty') == saved
            backups = list((reviewed['output'] / 'live-backup').glob('*/preset.json'))
            assert backups
            assert all(CharacterPreset.model_validate_json(path.read_bytes()).qq_memory_state
                       == before.qq_memory_state for path in backups)
        finally:
            await host.close()
    asyncio.run(run())


def test_reviewed_rebase_rejects_stale_owner_hash_before_staging(inputs, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path / 'owner', monkeypatch, [])
        try:
            stale_basis = _hash(memory_state(snapshot_owner(host).qq_memory_state))
            reviewed = await _import_original_and_review(inputs, host, monkeypatch)
            before = snapshot_owner(host)
            jobs_before = {ref: _live_queue(inputs).get_job(ref) for ref in before.qq_memory_state['receipts']}
            with pytest.raises(ValueError, match='review_basis_changed'):
                await import_backfill(host, reviewed['output'], expected_preset_sha256=digest(reviewed),
                                      expected_owner_state_sha256=stale_basis)
            assert _memory_effects(snapshot_owner(host)) == _memory_effects(before)
            assert {ref: _live_queue(inputs).get_job(ref) for ref in jobs_before} == jobs_before
            assert not (reviewed['output'] / 'live-backup').exists()
        finally:
            await host.close()
    asyncio.run(run())


def test_reviewed_rebase_cannot_discard_committed_decision_adoption(inputs, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path / 'owner', monkeypatch, [])
        try:
            inputs['now'] = inputs['now'] - timedelta(days=1) + timedelta(seconds=200)
            reviewed = await _import_original_and_review(inputs, host, monkeypatch)
            event = next(event for event in snapshot_owner(host).episodic
                         if event.scope and event.scope.channel == 'qq_group')
            moment = inputs['now'] + timedelta(seconds=1)
            host.qq_memory._recall_pending['decision-used-memory'] = {
                'ids': [event.event_id], 'episode_ids': [event.event_id], 'group_id': '100',
                'at': moment.isoformat(), 'preset_id': 'misty'}
            await host.qq_memory.maintain(now=moment)
            before = snapshot_owner(host)
            assert before.qq_memory_state['recalls']['decision-used-memory'] == [event.event_id]
            assert next(item for item in before.episodic if item.event_id == event.event_id).hit_count == event.hit_count + 1
            with pytest.raises(ValueError, match='review_has_unreplayed_adoptions'):
                await import_backfill(host, reviewed['output'], expected_preset_sha256=digest(reviewed),
                                      expected_owner_state_sha256=_hash(memory_state(before.qq_memory_state)))
            assert _memory_effects(snapshot_owner(host)) == _memory_effects(before)
            assert _memory_effects(host.chat.library.load('misty')) == _memory_effects(before)
            assert not (reviewed['output'] / 'live-backup').exists()
        finally:
            await host.close()
    asyncio.run(run())


class SplitReviewedProposer(Proposer):
    def propose(self, job, *, old_impressions, persona):
        proposal = super().propose(job, old_impressions=old_impressions, persona=persona)
        return proposal.model_copy(update={
            'episodes': tuple(proposal.episodes[0].model_copy(update={
                'source_ids': (source.source_id,), 'summary': '复核后按各条发言分别保留情景。'})
                for source in job.sources),
            'impressions': (),
        })


def test_reviewed_resegmentation_cannot_remove_committed_events_or_candidates(
        inputs, tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path / 'owner', monkeypatch, [])
        try:
            store = SharedMemoryStore(tmp_path / 'one-window.sqlite3')
            for index in range(2):
                add_message(store, str(index), seconds=index * 10, message_id=str(index + 1))
            inputs['source_db'] = store.path
            reviewed = await _import_original_and_review(inputs, host, monkeypatch, proposer=SplitReviewedProposer())
            before = snapshot_owner(host)
            assert len(before.episodic) == 1 and len(before.candidates) == 1
            saved_before = host.chat.library.load('misty')
            jobs_before = {ref: _live_queue(inputs).get_job(ref) for ref in before.qq_memory_state['receipts']}
            with pytest.raises(ValueError, match='review_cannot_remove_committed_record'):
                await import_backfill(host, reviewed['output'], expected_preset_sha256=digest(reviewed),
                                      expected_owner_state_sha256=_hash(memory_state(before.qq_memory_state)))
            assert _memory_effects(snapshot_owner(host)) == _memory_effects(before)
            assert host.chat.library.load('misty') == saved_before
            assert {ref: _live_queue(inputs).get_job(ref) for ref in jobs_before} == jobs_before
            assert not (reviewed['output'] / 'live-backup').exists()
        finally:
            await host.close()
    asyncio.run(run())
