from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import httpx
import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory, ScopedQQMemoryView, SharedQQBinding
from harness.core.models import InteractionEvent
from harness.memory.shared import SharedLocalEvent, SharedReadScope
from harness.qq.shared import QQSharedCognitiveClient, SharedQQUnavailable, _import_remote_trace
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace
from tests.test_basic_persona_continuity import _host, _command, _prepare, _commit
from tests import test_qq_decision as qq_fixtures
from harness.qq import decision


NOW = datetime.now(timezone.utc)
setup = qq_fixtures.setup


@pytest.fixture(autouse=True)
def no_model_probe(monkeypatch):
    monkeypatch.setattr('gui.service.probe_llm', lambda *args, **kwargs: True)


def test_qq_borrows_owner_client_persona_and_memory_without_closing(setup):
    _, client, builds, args, _ = setup
    settings, preset = decision._load_configuration(args['config_path'], args['preset_path'])
    memory = decision.build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(preset.persona)
    before = memory.get_state()
    view = ScopedQQMemoryView(memory, group_id='10001', member_id='20001')
    owner = SharedQQBinding(settings, preset, client, view, {}, ())
    try:
        result = decision.decide_qq_reply(**args, trigger_member_id='20001', shared_owner=owner)
        assert result.action == 'speak', result.error_type
        assert not builds and not client.closed
        assert memory.get_state() == before
        assert view.owner is memory and view.query(None) == []
        assert view.get_state().relationship is None
    finally:
        memory.close()


def test_shared_local_context_is_user_data_and_preserves_channel_scope(setup, tmp_path):
    store, _, _, _, _ = setup
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path/'shared.db',
        qq_settings=SimpleNamespace(memory_db=store.path, allowed_group_ids=['10001'], self_member_id='29999'))
    context = shared.local_context(InteractionEvent(raw_text='你还记得群里聊过什么吗？', timestamp=NOW,
                                                   speaker='user', session_id='local-main'))
    assert context and all(segment.kind == 'user' for segment in context)
    payload = ''.join(segment.text for segment in context)
    assert 'UNTRUSTED_A' in payload and 'WRONG_GROUP_SECRET' not in payload
    assert all(segment.source_ids for segment in context)
    qq_question = shared.local_context(InteractionEvent(raw_text='我们在 QQ 群里说过什么？', timestamp=NOW,
                                                   speaker='user', session_id='local-main'))
    selected = json.loads(qq_question[0].text)['shared_memory']['entries']
    assert any(entry['kind'] == 'qq_observation' for entry in selected)
    assert all(entry['kind'] != 'qq_profile' for entry in selected)
    source_before = store.group_context(group_id='10001')
    shared.local_context(InteractionEvent(raw_text='中'*2000, timestamp=NOW, speaker='user', session_id='local-main'))
    assert store.group_context(group_id='10001') == source_before


def test_shared_records_reach_actual_character_messages_intact(setup, tmp_path, monkeypatch):
    store, _, _, _, _ = setup
    async def run():
        host, main = _host(tmp_path, monkeypatch, ['我们讨论过之前的话题。'])
        host.shared.qq_settings = SimpleNamespace(memory_db=store.path, allowed_group_ids=['10001'], self_member_id='29999')
        try:
            await _prepare(host, main, 1, '我们在 QQ 群里说过什么？')
            users = '\n'.join(message['content'] for message in main.chat_calls[-1] if message['role']=='user')
            systems = '\n'.join(message['content'] for message in main.chat_calls[-1] if message['role']=='system')
            assert 'UNTRUSTED_A 我记得之前讨论过这个。' in users
            assert 'UNTRUSTED_B 你怎么看？' in users
            assert 'UNTRUSTED_A' not in systems and 'WRONG_GROUP_SECRET' not in users+systems
            selected=host.shared.local_context(InteractionEvent(raw_text='我们在 QQ 群里说过什么？',
                timestamp=NOW,speaker='user',session_id='local-main'))
            assert selected[0].text in users
        finally:
            await host.close()
    asyncio.run(run())


def test_normal_host_debug_denied_and_commits_share_persistent_memory(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setenv('NC_CONTROL_MODE', 'study_runtime')
        host, main = _host(tmp_path, monkeypatch, ['我觉得雨后的光很好看。'])
        try:
            with pytest.raises(PermissionError):
                await host.control('reset')
            with pytest.raises(PermissionError):
                await host.control('debug.persona', {'name':'Changed'})
            prepared = await _prepare(host, main, 1, '我今天拍了雨后的照片。')
            assert host.shared.ui()['shared_mind']['memory_records'] == 0
            await _commit(host, 1, prepared)
            snapshot = await host.ui_snapshot()
            assert snapshot['shared_mind']['memory_records'] == 2
            assert {item['channel'] for item in snapshot['shared_memory']['entries']} == {'local_chat'}
            assert not host.shared.store.query(SharedReadScope(audience='qq:group:10001')).records
        finally:
            await host.close()
        restored, _ = _host(tmp_path, monkeypatch, ['嗯。'])
        try:
            assert restored.shared.sync_local() == 0
            assert (await restored.ui_snapshot())['shared_mind']['memory_records'] == 2
        finally:
            await restored.close()
    asyncio.run(run())


def test_explicit_shared_local_memory_uses_same_owner_with_private_records_excluded(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ['嗯。'])
        try:
            for name, visibility in [('PRIVATE', ('local:main',)), ('SHARED', ('local:main','qq:group:10001'))]:
                host.shared.store.record_local(SharedLocalEvent(event_id=name, session_id='local-test',
                    author_principal='local:main', role='user', text=name, occurred_at=NOW,
                    visibility=visibility, evidence_refs=('test:'+name,)))
            binding = host.shared.qq_binding(group_id='10001', member_id='20001')
            assert binding.llm is host.chat.llm and binding.memory.owner is host.chat.memory
            assert 'SHARED' in json.dumps(binding.context) and 'PRIVATE' not in json.dumps(binding.context)
            assert not host.shared.qq_binding(group_id='10002').context['entries']
        finally:
            await host.close()
    asyncio.run(run())


def test_qq_command_uses_durable_owner_ledger_without_local_delivery(tmp_path, monkeypatch, setup):
    store, client, builds, args, _ = setup
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ['unused'])
        settings, preset = decision._load_configuration(args['config_path'], args['preset_path'])
        host.shared.qq_settings = SimpleNamespace(memory_db=store.path)
        binding = SharedQQBinding(settings, preset, client,
            ScopedQQMemoryView(host.chat.memory, group_id='10001', member_id='20001'), {}, ())
        monkeypatch.setattr(host.shared, 'qq_binding', lambda **kwargs: binding)
        from harness.runtime import qq_turns
        sources = []
        monkeypatch.setattr(qq_turns, '_source', lambda *args: sources.append(args))
        trace = QQTurnTrace(event_id='qq-watch-event:'+'a'*32)
        payload = {'group_id':'10001','self_member_id':'29999','trigger_member_id':'20001',
            'deadline_at':(datetime.now(timezone.utc)+timedelta(seconds=50)).isoformat(),
            'trace':{'trace_id':trace.trace_id,'event_id':trace.event_id,
                     'first_seen_at':trace.first_seen_at.isoformat(),'first_seen_mono':trace.first_seen_mono}}
        command = _command('PrepareQQResponse', 1, payload)
        before = host.chat.pipeline.session_store.snapshot()
        try:
            result = await host.prepare_qq_response(command)
            assert result.status == 'applied', result.error
            assert result.result['decision']['action'] == 'speak'
            assert sources and not builds and not client.closed
            replay = await host.prepare_qq_response(command)
            assert replay.status == 'idempotent_replay' and replay.result == result.result
            assert len(client.gate_messages) == 1
            assert host.chat.pipeline.session_store.snapshot() == before
            assert host.shared.ui()['shared_mind']['memory_records'] == 0
        finally:
            await host.close()
    asyncio.run(run())


def test_failed_command_remains_failed_on_replay(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ['unused'])
        command = _command('PrepareQQResponse', 1, {})
        try:
            first = await host.prepare_qq_response(command)
            replay = await host.prepare_qq_response(command)
            assert first.status == replay.status == 'failed'
            assert replay.idempotency_hit and replay.result is None
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize('kind,method', [
    ('PrepareResponse', 'prepare_response'),
    ('CommitInteraction', 'commit_interaction'),
    ('PrepareGoalDecision', 'prepare_goal_decision'),
])
def test_explicit_revision_remains_strict_for_other_owner_commands(tmp_path, monkeypatch, kind, method):
    async def run():
        host, main = _host(tmp_path, monkeypatch, ['unused'])
        try:
            host.adapter._revision = 21
            command = _command(kind, 1, {}).model_copy(update={'expected_revision': 14})
            result = await getattr(host, method)(command)
            assert result.status == 'conflict'
            assert result.error == 'expected revision 14, current 21'
            assert not main.chat_calls
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize('url', ['https://127.0.0.1:8770','http://remote.invalid','http://secret@127.0.0.1:8770'])
def test_shared_client_rejects_remote_or_credential_urls(url,tmp_path):
    with pytest.raises(ValueError):
        QQSharedCognitiveClient(url,tmp_path/'missing.token')


def test_shared_client_failure_does_not_retry_or_build_local_model(tmp_path):
    token = tmp_path/'token'
    token.write_text('test-private-token')
    requests=[]
    def fail(request):
        requests.append(request)
        return httpx.Response(503,json={'detail':'unavailable'})
    with httpx.Client(base_url='http://127.0.0.1:8770',transport=httpx.MockTransport(fail)) as http:
        client=QQSharedCognitiveClient('http://127.0.0.1:8770',token,client=http)
        trace=QQTurnTrace(event_id='qq-watch-event:'+'a'*32)
        with pytest.raises(SharedQQUnavailable):
            client(group_id='10001',self_member_id='29999',trace=trace,deadline=QQDeadline.after(30))
        assert len(requests)==1
        with pytest.raises(QQDeadlineExpired):
            client(group_id='10001',self_member_id='29999',trace=trace,deadline=QQDeadline.after(0))
        assert len(requests)==1


def test_remote_trace_keeps_distinct_clock_measurements_and_queue_in_rtt():
    def clock():
        return 120.0
    trace=QQTurnTrace(first_seen_mono=100,clock=clock,event_id='qq-watch-event:'+'a'*32)
    remote={'trace_id':trace.trace_id,'elapsed_ms':3000,
        'stages':[{'name':'gate','start_offset_ms':1000,'end_offset_ms':2000,'duration_ms':1000,'outcome':'ok'}],
        'counters':{'gate_model_calls':1}}
    _import_remote_trace(trace,remote,started=110,ended=120)
    stage=trace.snapshot()['stages'][0]
    assert stage['start_offset_ms']==18000 and stage['remote_start_offset_ms']==1000
    assert stage['rpc_residual_ms']==7000
    assert trace.snapshot()['counters']['gate_model_calls']==1
    bad={**remote,'counters':{'bad':-1}}
    before=trace.snapshot()
    with pytest.raises(SharedQQUnavailable):
        _import_remote_trace(trace,bad,started=110,ended=120)
    after=trace.snapshot()
    # Taking a snapshot advances its wall-clock reporting timestamp, even when
    # invalid remote measurements leave all retained trace data unchanged.
    assert {key:value for key,value in after.items() if key != 'recorded_at'} == {
        key:value for key,value in before.items() if key != 'recorded_at'}


def test_standalone_decision_requires_both_flag_and_development_authority(tmp_path,monkeypatch):
    monkeypatch.setenv('NC_CONTROL_MODE','study_runtime')
    output=tmp_path/'new.json'
    args=['--development-mode','--memory-db',str(tmp_path/'missing.db'),
          '--group-id','10001','--output',str(output)]
    assert decision.main(args)==2 and not output.exists()
