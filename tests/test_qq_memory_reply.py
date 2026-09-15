"""The same bounded retained memories inform turn-taking and its actual draft."""
import asyncio
import time
import threading
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

from harness.cognitive.shared_memory import SharedQQBinding, ScopedQQMemoryView
from harness.qq import decision
from harness.qq.telemetry import QQDeadline
from tests.test_qq_decision import setup as setup  # noqa: F401
from tests.test_qq_decision_context_budget import _gate_payload, _persona_payload
from tests.test_qq_memory_worker import setup_host
from tests.test_basic_persona_continuity import _command


def _binding(setup):
    _, client, _, args, _ = setup
    settings, preset = decision._load_configuration(args['config_path'], args['preset_path'])
    memory = decision.build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(preset.persona)
    derived = {'schema_version': 'qq-memory-v1', 'entries': [{
        'record_id': 'impression_example', 'kind': 'person', 'subject_refs': ['qq:member:20001'],
        'first_impression': '最早觉得他很爱开玩笑', 'first_episode_id': 'qqepisode_first',
        'current_impression': '平时爱开玩笑，但认真求助时也会耐心解释', 'status': 'disputed',
        'confidence': .44, 'supporting_episode_ids': ['qqepisode_visible'],
        'counterevidence': [{'episode_id': 'qqepisode_counter', 'statement': '认真回答过求助'}]}]}
    return SharedQQBinding(settings, preset, client,
        ScopedQQMemoryView(memory, group_id='10001', member_id='20001'), {}, (), derived), memory


def test_gate_and_draft_share_exact_memories_and_freeze_only_visible_evidence(setup):
    _, client, _, args, _ = setup
    binding, memory = _binding(setup)
    try:
        result = decision.decide_qq_reply(**args, trigger_member_id='20001', shared_owner=binding)
        assert result.outcome == 'ready', result.error_type
        assert _gate_payload(client)['derived_memory'] == _persona_payload(client)['derived_memory']
        assert result.recalled_memory_ids == ['impression_example']
        assert set(result.recalled_episode_ids) == {'qqepisode_first', 'qqepisode_visible', 'qqepisode_counter'}
        assert not memory.stores.episodic.list_all()
    finally:
        memory.close()


def test_trimmed_memory_cannot_be_recorded_as_adopted(setup, monkeypatch):
    _, client, _, args, _ = setup
    binding, memory = _binding(setup)
    binding.derived_memory['entries'][0]['current_impression'] = '太长' * decision.MAX_GROUP_CONTEXT_CHARACTERS
    try:
        result = decision.decide_qq_reply(**args, trigger_member_id='20001', shared_owner=binding)
        assert result.outcome == 'ready', result.error_type
        assert _gate_payload(client)['derived_memory']['entries'] == []
        assert result.recalled_memory_ids == result.recalled_episode_ids == []
    finally:
        memory.close()


def test_qq_owner_prepares_from_memory_committed_after_runtime_snapshot(tmp_path, monkeypatch):
    from harness.cognitive.qq_memory import QQMemoryControl
    from harness.qq.decision import QQReplyDecision
    from harness.qq.telemetry import QQTurnTrace

    async def run():
        monkeypatch.setattr('gui.service.probe_llm', lambda *a, **kw: True)
        monkeypatch.setattr('tests.test_qq_memory_worker.NOW', datetime.now(timezone.utc))
        host, _ = setup_host(tmp_path, monkeypatch)
        source_checks, prepared_memory = [], []
        monkeypatch.setattr('harness.runtime.qq_turns._source', lambda *a: source_checks.append(a))
        trace = QQTurnTrace(event_id='qq-watch-event:' + 'b' * 32)
        command = _command('PrepareQQResponse', 1, {
            'group_id': '10001', 'self_member_id': '29999', 'trigger_member_id': '20003',
            'query': '摄影', 'deadline_at': (datetime.now(timezone.utc) + timedelta(seconds=50)).isoformat(),
            'trace': {'trace_id': trace.trace_id, 'event_id': trace.event_id,
                      'first_seen_at': trace.first_seen_at.isoformat(), 'first_seen_mono': trace.first_seen_mono}})

        def prepare(**values):
            prepared_memory.append(values['shared_owner'].derived_memory)
            return QQReplyDecision(decision_id='qq-decision:' + 'c' * 32, input_hash='a' * 64,
                created_at=datetime.now(timezone.utc), group_id='10001', self_member_id='29999',
                trigger_member_id='20003', action='wait', reason='Synthetic wait.',
                decision_source='model', outcome='natural_wait', participants=[], source_record_ids=[],
                new_observation_record_ids=[], evidence_refs=[], delivery_refs=[], model='synthetic',
                persona_version='synthetic', preset_id='misty', trace_id=trace.trace_id)

        monkeypatch.setattr(decision, 'decide_qq_reply', prepare)
        try:
            revision = (await host.get_snapshot()).revision
            await host.qq_memory.tick()
            assert host.qq_memory.last_error is None
            assert (await host.get_snapshot()).revision > revision
            host.qq_memory.control = QQMemoryControl(enabled=True, recall_enabled=True)
            result = await host.prepare_qq_response(command)
            assert result.status == 'applied', result.error
            assert source_checks and len(prepared_memory) == 1
            assert prepared_memory[0]['entries']
            assert '成员谈到摄影' in str(prepared_memory[0])
            replay = await host.prepare_qq_response(command)
            assert replay.status == 'idempotent_replay'
            assert replay.result == result.result and len(prepared_memory) == 1
        finally:
            await host.close()

    asyncio.run(run())


def test_fixed_latency_reply_does_not_wait_for_background_cleaner(setup, tmp_path, monkeypatch):
    async def run():
        _, client, _, args, _ = setup
        monkeypatch.setattr('gui.service.probe_llm', lambda *a, **kw: True)
        host, _ = setup_host(tmp_path / 'owner', monkeypatch)
        binding, memory = _binding(setup)
        original_chat = client.complete_chat_result
        def fixed_chat(*a, **kw):
            time.sleep(.025)
            return original_chat(*a, **kw)
        monkeypatch.setattr(client, 'complete_chat_result', fixed_chat)
        started = threading.Event()
        original_proposer = host.qq_memory.proposer
        def fixed_background(job, **kw):
            started.set()
            time.sleep(1.2)
            return original_proposer.propose(job, **kw)
        host.qq_memory.proposer = SimpleNamespace(propose=fixed_background)
        try:
            start = time.perf_counter()
            baseline = await asyncio.to_thread(decision.decide_qq_reply, **args,
                shared_owner=binding, request='baseline', deadline=QQDeadline.after(1))
            baseline_seconds = time.perf_counter() - start
            assert baseline.outcome == 'ready', baseline.error_type
            background = asyncio.create_task(host.qq_memory.tick())
            assert await asyncio.to_thread(started.wait, 5)
            start = time.perf_counter()
            async with host.adapter._lock:
                concurrent = await asyncio.to_thread(decision.decide_qq_reply, **args,
                    shared_owner=binding, request='during cleaner', deadline=QQDeadline.after(1))
            concurrent_seconds = time.perf_counter() - start
            assert concurrent.outcome == 'ready', concurrent.error_type
            assert not background.done()
            assert concurrent_seconds < baseline_seconds + .35
            await background
            assert host.qq_memory.last_error is None
        finally:
            await host.close()
            memory.close()
    asyncio.run(run())
