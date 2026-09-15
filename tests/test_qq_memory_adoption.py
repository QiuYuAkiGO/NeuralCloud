import asyncio
from datetime import timedelta
from types import SimpleNamespace

from harness.cognitive.qq_memory import install_committed, snapshot_owner, QQMemoryControl
from tests.test_basic_persona_continuity import _host
from tests.test_qq_memory_commit import SourceFixture, _four_day_commit, NOW


def test_adoption_freezes_shown_evidence_and_is_not_repeated(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path / 'owner', monkeypatch, [])
        host.shared.qq_settings = SimpleNamespace(self_member_id="999", allowed_group_ids=("100", "101"),
                                                   allowed_private_peer_ids=())
        preset = _four_day_commit(SourceFixture(tmp_path / 'source'))
        current = snapshot_owner(host).model_copy(update={'episodic': preset.episodic,
            'semantic': preset.semantic, 'candidates': preset.candidates, 'qq_memory_state': preset.qq_memory_state})
        install_committed(host, current)
        host.qq_memory.control = QQMemoryControl(enabled=True, recall_enabled=True)
        impression = next(iter(preset.qq_memory_state['impressions'].values()))
        shown = impression['evidence'][-1]['episode_id']
        moment = NOW + timedelta(days=4)
        adoption = {'ids': [impression['impression_id']], 'episode_ids': [shown], 'group_id': '100',
                    'at': moment.isoformat(), 'preset_id': 'misty'}
        before = {event.event_id: event.hit_count for event in host.chat.memory.stores.episodic.list_all()}
        host.qq_memory._recall_pending['decision-frozen'] = adoption
        try:
            await host.qq_memory.maintain(now=moment)
            first = {event.event_id: event.hit_count for event in host.chat.memory.stores.episodic.list_all()}
            assert first[shown] == before[shown] + 1
            assert all(first[key] == before[key] for key in before if key != shown)
            host.qq_memory._recall_pending['decision-frozen'] = adoption
            await host.qq_memory.maintain(now=moment + timedelta(seconds=1))
            assert {event.event_id: event.hit_count for event in host.chat.memory.stores.episodic.list_all()} == first
        finally:
            await host.close()
    asyncio.run(run())


def test_dormant_impression_cannot_leave_a_semantic_fact_retrievable(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path / 'owner', monkeypatch, [])
        preset = _four_day_commit(SourceFixture(tmp_path / 'source'))
        expired = [event.model_copy(update={'tier': 'forgotten'}) for event in preset.episodic]
        current = snapshot_owner(host).model_copy(update={'episodic': expired, 'semantic': preset.semantic,
            'candidates': preset.candidates, 'qq_memory_state': preset.qq_memory_state})
        install_committed(host, current)
        try:
            assert any(f.fact_id.startswith('qqsemantic_') for f in host.chat.memory.stores.semantic.list_all())
            await host.qq_memory.maintain(now=NOW + timedelta(days=5))
            assert not any(f.fact_id.startswith('qqsemantic_') for f in host.chat.memory.stores.semantic.list_all())
            assert all(c.status == 'dropped' for c in host.chat.memory.stores.candidates.list_all())
            assert all(i['status'] == 'dormant' for i in host.chat.memory.qq_memory_state['impressions'].values())
        finally:
            await host.close()
    asyncio.run(run())
