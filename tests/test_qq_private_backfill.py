import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from gui.presets import CharacterPreset, atomic_write_json, load_builtin
from harness.cognitive.qq_memory import (
    QQMemoryControl, build_commit, install_committed, memory_state, snapshot_owner,
)
from harness.cognitive.qq_memory_import import import_backfill, validate_bundle
from harness.config import Settings
from harness.memory import qq_backfill
from harness.memory.qq_assimilation import QQAssimilationQueue
from harness.memory.shared import SharedMemoryStorageError, SharedMemoryStore
from tests.test_basic_persona_continuity import _host, _prepare, _commit
from tests.test_qq_assimilation import NOW, add_message
from tests.test_qq_backfill import Proposer, inputs as inputs  # noqa: F401
from tests.test_qq_memory_import import digest


PRIVATE = 'private:999:200'
AUDIENCE = 'qq:private:999:200'


@pytest.fixture
def private_inputs(inputs):
    store = SharedMemoryStore(inputs['source_db'])
    for index in range(4):
        add_message(store, f'private-{index}', group=PRIVATE, member='200',
                    seconds=index * 86400, message_id=str(100 + index))
    add_message(store, 'different-peer', group='private:999:201', member='201', message_id='300')
    return {**inputs, 'group_ids': [], 'private_peer_ids': ['200'], 'now': NOW + timedelta(days=3, seconds=60)}


def _add_group_memory(preset, data, *, suffix='initial'):
    reader = QQAssimilationQueue(data['source_db'], owner_id='misty', preset_id='misty',
                                self_id='999', allowed_group_ids=['100'])
    queue = QQAssimilationQueue(data['source_db'].with_name(f'group-{suffix}.sqlite3'),
                               owner_id='misty', preset_id='misty', self_id='999', allowed_group_ids=['100'])
    for job in queue.enqueue(now=data['now'], backfill=True, sources=reader.snapshot()):
        proposed = Proposer().propose(job, old_impressions=[], persona=preset.persona)
        preset = build_commit(preset, job, proposed, Settings(), now=data['now'])
    return preset


def _artifact(data):
    return CharacterPreset.model_validate_json((data['output'] / 'preset.json').read_bytes())


def _private_rows(preset, field):
    return [row for row in getattr(preset, field) if row.scope and row.scope.conversation_id == AUDIENCE]


def _effects(preset):
    return preset.model_dump(mode='json', exclude={'updated_at'})


def test_private_only_backfill_keeps_initial_groups_but_reports_selected_scope(private_inputs):
    data = private_inputs
    initial = _add_group_memory(load_builtin('misty'), data)
    atomic_write_json(data['preset'], initial.model_dump(mode='json'))
    original_source = data['source_db'].read_bytes()
    result = qq_backfill.run_backfill(**data, proposer=Proposer(), progress=lambda _: None)
    assert result['completed'] and result['raw_observations'] == 4
    assert result['episodes'] == 4 and result['impressions'] == 1
    assert result['stable_judgments'] == 1 and result['committed_windows'] == 4
    assert len(_artifact(data).episodic) == 6
    assert len(_artifact(data).qq_memory_state['receipts']) == 6
    assert data['source_db'].read_bytes() == original_source
    manifest = json.loads((data['output'] / 'source_manifest.json').read_text())
    assert manifest['group_ids'] == [] and manifest['private_peer_ids'] == ['200']
    validate_bundle(data['output'], digest(data), Settings())
    repeated = qq_backfill.run_backfill(**data, proposer=Proposer(fail=True), progress=lambda _: None)
    assert repeated['processed_this_run'] == 0 and repeated['episodes'] == 4
    with pytest.raises(ValueError, match='scope_changed'):
        qq_backfill.run_backfill(**{**data, 'private_peer_ids': ['201']}, proposer=Proposer())


def test_cli_accepts_private_only_and_multiple_peers(monkeypatch, capsys):
    calls = []
    def run(**kwargs):
        calls.append(kwargs)
        return dict(completed=True, episodes=0, impressions=0, stable_judgments=0,
                    processed_this_run=0, failures_this_run=0, duration_ms=0)
    monkeypatch.setattr(qq_backfill, 'run_backfill', run)
    assert qq_backfill.main(['--source-db', 'source', '--preset', 'preset', '--settings', 'settings',
                            '--output', 'output', '--self-id', '999', '--private-peer-id', '200',
                            '--private-peer-id', '201']) == 0
    assert calls[0]['group_ids'] == [] and calls[0]['private_peer_ids'] == ['200', '201']
    assert json.loads(capsys.readouterr().out)['completed']


async def _host_and_replay(data, tmp_path, monkeypatch):
    host, main = _host(tmp_path / 'owner', monkeypatch, ['保留清洗期间的本地交流。'])
    host.shared.qq_settings = SimpleNamespace(memory_db=None, self_member_id='999',
        allowed_group_ids=['100'], allowed_private_peer_ids=['200', '201'])
    host.qq_memory.control = QQMemoryControl(enabled=True, recall_enabled=True)
    monkeypatch.setattr(host.shared, 'sync_qq', lambda: None)
    host.shared.store.path = data['source_db']
    initial = _add_group_memory(snapshot_owner(host), data)
    host.chat.library.save(initial)
    install_committed(host, initial)
    atomic_write_json(data['preset'], initial.model_dump(mode='json'))
    assert qq_backfill.run_backfill(**data, proposer=Proposer(), progress=lambda _: None)['completed']
    return host, main


@pytest.mark.parametrize('failed_save', [False, True])
def test_private_import_preserves_concurrent_group_and_local_changes_and_is_idempotent(
        private_inputs, tmp_path, monkeypatch, failed_save):
    async def run():
        data = private_inputs
        host, main = await _host_and_replay(data, tmp_path, monkeypatch)
        try:
            prepared = await _prepare(host, main, 1, '清洗期间新增的本地消息。')
            await _commit(host, 1, prepared)
            add_message(SharedMemoryStore(data['source_db']), 'new-group', seconds=20000, message_id='200')
            current = _add_group_memory(snapshot_owner(host), data, suffix='new')
            state = memory_state(current.qq_memory_state)
            group_id = next(row.event_id for row in current.episodic if row.scope and row.scope.channel == 'qq_group')
            state['recalls']['new-group-adoption'] = [group_id]
            state['last_review_at'] = (data['now'] + timedelta(hours=1)).isoformat()
            state['revision'] += 1
            current = current.model_copy(update={'qq_memory_state': state})
            host.chat.library.save(current)
            install_committed(host, current)
            before = snapshot_owner(host)
            group_queue_in_use = object()
            host.qq_memory.queue = group_queue_in_use
            host.qq_memory.processing = True
            if failed_save:
                with monkeypatch.context() as patch:
                    patch.setattr(host.chat.library, 'save', lambda _: (_ for _ in ()).throw(OSError('disk')))
                    with pytest.raises(OSError, match='disk'):
                        await import_backfill(host, data['output'], expected_preset_sha256=digest(data))
                assert _effects(snapshot_owner(host)) == _effects(before)
                assert _effects(host.chat.library.load('misty')) == _effects(before)
            result = await import_backfill(host, data['output'], expected_preset_sha256=digest(data))
            assert result['imported'] and result['imported_scope_counts'] == {
                'episodes': 4, 'impressions': 1, 'stable_judgments': 1, 'windows': 4}
            assert host.qq_memory.queue is group_queue_in_use
            saved = snapshot_owner(host)
            for field, identity in [('episodic', 'event_id'), ('candidates', 'candidate_id'), ('semantic', 'fact_id')]:
                existing = {getattr(row, identity): row for row in getattr(before, field)}
                assert {getattr(row, identity): row for row in getattr(saved, field)
                        if getattr(row, identity) in existing} == existing
                assert _private_rows(saved, field) == _private_rows(_artifact(data), field)
            assert saved.transcript == before.transcript
            assert saved.qq_memory_state['recalls'] == before.qq_memory_state['recalls']
            assert saved.qq_memory_state['last_review_at'] == before.qq_memory_state['last_review_at']
            assert saved.qq_memory_state['revision'] == before.qq_memory_state['revision'] + 1
            for field in ('impressions', 'receipts'):
                assert all(saved.qq_memory_state[field][key] == value
                           for key, value in before.qq_memory_state[field].items())
            again = await import_backfill(host, data['output'], expected_preset_sha256=digest(data))
            assert again['replayed'] and not again['imported']
            assert _effects(snapshot_owner(host)) == _effects(saved)
            queue = QQAssimilationQueue(data['source_db'], owner_id='misty', preset_id='misty',
                self_id='999', allowed_group_ids=[], allowed_private_peer_ids=['200'])
            assert queue.status()['applied'] == 4
        finally:
            host.qq_memory.processing = False
            await host.close()
    asyncio.run(run())


def test_private_import_rejects_changed_target_before_staging(private_inputs, tmp_path, monkeypatch):
    async def run():
        host, _ = await _host_and_replay(private_inputs, tmp_path, monkeypatch)
        try:
            artifact = _artifact(private_inputs)
            event = _private_rows(artifact, 'episodic')[0].model_copy(update={'summary': '目标私聊已发生独立变化。'})
            host.chat.memory.stores.episodic.insert(event)
            before = snapshot_owner(host)
            with pytest.raises(ValueError, match='private_memory_changed'):
                await import_backfill(host, private_inputs['output'], expected_preset_sha256=digest(private_inputs))
            assert _effects(snapshot_owner(host)) == _effects(before)
            assert not (private_inputs['output'] / 'live-backup').exists()
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize('evolution', ['adoption', 'decay'])
def test_committed_private_receipts_survive_later_memory_evolution(
        private_inputs, tmp_path, monkeypatch, evolution):
    async def run():
        data = private_inputs
        host, _ = await _host_and_replay(data, tmp_path, monkeypatch)
        try:
            assert (await import_backfill(host, data['output'], expected_preset_sha256=digest(data)))['imported']
            original = snapshot_owner(host)
            event = max(_private_rows(original, 'episodic'), key=lambda row: row.time)
            if evolution == 'adoption':
                host.qq_memory.control = host.qq_memory.control.model_copy(update={'private_recall_enabled': True})
                used_at = data['now'] + timedelta(seconds=1)
                host.qq_memory.note_recall(SimpleNamespace(
                    decision_id='adoption-after-import', decision_source='model', outcome='ready',
                    completed_at=used_at, group_id=PRIVATE,
                    recalled_memory_ids=[event.event_id], recalled_episode_ids=[event.event_id]))
                await host.qq_memory.maintain(now=used_at)
            else:
                await host.qq_memory.maintain(now=data['now'] + timedelta(days=180))
            evolved = snapshot_owner(host)
            evolved_event = next(row for row in evolved.episodic if row.event_id == event.event_id)
            if evolution == 'adoption':
                assert evolved_event.hit_count == event.hit_count + 1
                assert evolved.qq_memory_state['recalls']['adoption-after-import'] == [event.event_id]
            else:
                assert event.tier != 'forgotten' and evolved_event.tier == 'forgotten'
            assert evolved.qq_memory_state['receipts'] == original.qq_memory_state['receipts']
            with monkeypatch.context() as patch:
                patch.setattr(host.chat.library, 'save', lambda _: (_ for _ in ()).throw(
                    AssertionError('duplicate receipt must not save over evolved memory')))
                result = await import_backfill(host, data['output'], expected_preset_sha256=digest(data))
            assert result['replayed'] and not result['imported']
            assert _effects(snapshot_owner(host)) == _effects(evolved)
            assert _effects(host.chat.library.load('misty')) == _effects(evolved)
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize('corruption', ['receipt', 'missing_receipt', 'source_refs'])
def test_existing_private_commit_must_match_receipts_and_effect_sources(
        private_inputs, tmp_path, monkeypatch, corruption):
    async def run():
        data = private_inputs
        host, _ = await _host_and_replay(data, tmp_path, monkeypatch)
        try:
            await import_backfill(host, data['output'], expected_preset_sha256=digest(data))
            state = memory_state(host.chat.memory.qq_memory_state)
            event = _private_rows(snapshot_owner(host), 'episodic')[0]
            receipt_id = next(key for key, row in state['receipts'].items() if event.event_id in row['episode_ids'])
            if corruption == 'receipt':
                state['receipts'][receipt_id]['proposal_digest'] = '0' * 64
                host.chat.memory.qq_memory_state = state
                error = 'private_receipt_conflict'
            elif corruption == 'missing_receipt':
                state['receipts'].pop(receipt_id)
                host.chat.memory.qq_memory_state = state
                error = 'private_memory_changed'
            else:
                host.chat.memory.stores.episodic.insert(event.model_copy(update={'source_refs': ['unrelated-source']}))
                error = 'private_receipt_effect_mismatch'
            before = snapshot_owner(host)
            with pytest.raises(ValueError, match=error):
                await import_backfill(host, data['output'], expected_preset_sha256=digest(data))
            assert _effects(snapshot_owner(host)) == _effects(before)
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize('tamper', ['peer', 'group', 'scope'])
def test_private_bundle_rejects_forged_scope(private_inputs, tamper):
    data = private_inputs
    qq_backfill.run_backfill(**data, proposer=Proposer(), progress=lambda _: None)
    if tamper == 'scope':
        path = data['output'] / 'preset.json'
        raw = json.loads(path.read_text(encoding='utf-8'))
        raw['episodic'][0]['scope']['conversation_id'] = 'qq:private:999:201'
        raw['episodic'][0]['scope']['visibility'] = ['local:main', 'qq:private:999:201']
    else:
        path = data['output'] / 'source_manifest.json'
        raw = json.loads(path.read_text())
        raw['private_peer_ids'] = ['201'] if tamper == 'peer' else []
        if tamper == 'group':
            raw['group_ids'] = ['100']
    atomic_write_json(path, raw)
    with pytest.raises((ValueError, SharedMemoryStorageError)):
        validate_bundle(data['output'], digest(data), Settings())


@pytest.mark.parametrize('self_id,peers', [('998', ['200']), ('999', ['201'])])
def test_private_import_checks_current_authorization(private_inputs, tmp_path, monkeypatch, self_id, peers):
    async def run():
        host, _ = await _host_and_replay(private_inputs, tmp_path, monkeypatch)
        try:
            host.shared.qq_settings.self_member_id = self_id
            host.shared.qq_settings.allowed_private_peer_ids = peers
            before = deepcopy(host.chat.memory.qq_memory_state)
            with pytest.raises(ValueError, match='authorization_mismatch'):
                await import_backfill(host, private_inputs['output'], expected_preset_sha256=digest(private_inputs))
            assert host.chat.memory.qq_memory_state == before
            assert not (private_inputs['output'] / 'live-backup').exists()
        finally:
            await host.close()
    asyncio.run(run())
