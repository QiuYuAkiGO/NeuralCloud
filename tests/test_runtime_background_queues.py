"""Foreground progress, thread isolation and lifecycle fences without network calls."""
from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from datetime import UTC, datetime, timedelta
import threading

import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.runtime import PassiveHomeostasisAdapter, RealTimeScheduler
from harness.runtime.background import ThreadLane
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_cognitive_incremental_store import _owner
from tests.test_emotion_service import FakeAux, message, request
from tests.test_runtime_qq_deadline import _accept, _prepared, _request
from tests.test_runtime_supervisor import _SummaryHomeostasis, _manifest, _pipeline_adapter


@pytest.mark.parametrize("phase", ["web", "affect"])
def test_qq_frozen_summary_survives_heartbeat_during_preparation(tmp_path, monkeypatch, phase):
    from harness.runtime.adapters import _validated_homeostatic_interoception

    async def scenario():
        memory, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(expression_mode="responses"), cognitive=adapter,
            homeostasis=_SummaryHomeostasis(), state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        entered, release = asyncio.Event(), asyncio.Event()
        prepared = []

        async def web(*args, **kwargs):
            if phase == "web":
                entered.set()
                await release.wait()
            return None, None

        async def affect(*args, **kwargs):
            if phase == "affect":
                entered.set()
                await release.wait()
            return None

        async def prepare(command):
            # Exercise the real Cognitive contract that rejected the live QQ
            # request when heartbeat advanced during its web preparation.
            summary = _validated_homeostatic_interoception(command)
            assert summary.snapshot_revision == frozen_revision
            assert runtime.version_vector.homeostasis_state > frozen_revision
            prepared.append(command)
            return _prepared(command)

        monkeypatch.setattr("harness.runtime.web_dialogue.prepare_web_context", web)
        monkeypatch.setattr(runtime.affect, "read", affect)
        adapter.prepare_qq_response = prepare
        event = await _accept(runtime, _request(seconds=20))
        frozen_revision = runtime.version_vector.homeostasis_state
        task = asyncio.create_task(runtime.dispatch(event))
        runtime.heartbeat.start()
        try:
            await asyncio.wait_for(entered.wait(), 2)
            await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")

            async def advanced():
                while (runtime.version_vector.homeostasis_state <= frozen_revision
                       or runtime.heartbeat.last_duration_ms is None):
                    await asyncio.sleep(0)

            await asyncio.wait_for(advanced(), 2)
            assert not task.done()
            release.set()
            assert (await asyncio.wait_for(task, 3)).status == "completed"
            assert runtime.lifecycle == RuntimeLifecycle.RUNNING
            command = prepared[0]
            # Replay retains the first durable read binding even after another
            # heartbeat; Homeostasis mutations still use the latest revision.
            await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
            replay = runtime._make_command(event, command_type="PrepareQQResponse",
                idempotency_key=command.idempotency_key, expected_revision=None, payload={})
            assert replay.effective_request_hash() == command.effective_request_hash()
            assert replay.version_vector == command.version_vector
            mutation = runtime._make_command(event, command_type="ApplyHeartbeat",
                idempotency_key="fresh-heartbeat", expected_revision=runtime._homeostasis_snapshot.revision,
                payload={})
            assert mutation.version_vector.homeostasis_state == runtime._homeostasis_snapshot.revision
        finally:
            release.set()
            await runtime.stop(reason="QQ frozen summary regression")
            await asyncio.gather(task, return_exceptions=True)
            memory.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("phase", ["APPRAISAL", "GUIDANCE"])
@pytest.mark.parametrize("incremental", [False, True])
def test_affect_model_does_not_lock_foreground_snapshot_or_checkpoint(tmp_path, phase, incremental):
    entered = threading.Event()
    release = threading.Event()

    class BlockingAux(FakeAux):
        def complete_json(self, *, system, user, **kwargs):
            if f"EMOTION_{phase}" in system:
                entered.set()
                assert release.wait(5), "test must release the model worker"
            return super().complete_json(system=system, user=user, **kwargs)

    async def scenario():
        adapter, _, _ = _owner(tmp_path, enabled=incremental, emotion="on")
        aux = BlockingAux()
        adapter.emotion_service.llm = aux
        now = datetime.now(UTC)
        scope = "qq:group:10001"
        observation = request(scope=scope, as_of=now, messages=[message(scope=scope, timestamp=now)])
        adapter.emotion_input_provider = lambda command: observation
        command = DomainCommandEnvelope(command_type="AppraiseAffect", event_id="affect-event",
            receive_seq=1, correlation_id="affect-trace", runtime_instance_id="runtime-test",
            manifest_revision=1, idempotency_key="affect-test", issued_at=now,
            deadline=now + timedelta(seconds=20), payload={"context_id": observation.context_id,
                "scope": scope, "homeostasis": {}})
        task = asyncio.create_task(adapter.appraise_affect(command))
        try:
            assert await asyncio.to_thread(entered.wait, 2)

            async def foreground():
                # A newer foreground command may commit while the earlier
                # model is still running. Its cursor must not regress later.
                async with adapter._lock:
                    adapter._cursor = 20
                    adapter._event_id = "newer-foreground-event"
                snapshot = await adapter.read_affect_snapshot({"scope": scope,
                    "as_of": datetime.now(UTC).isoformat(), "homeostasis": {}})
                await asyncio.to_thread(adapter.emotion_service.record_delivery,
                    [message("delivered-midflight", scope=scope, timestamp=now, role="character")])
                await asyncio.to_thread(adapter._persist_state)
                return snapshot

            snapshot = await asyncio.wait_for(foreground(), 2)
            assert snapshot["frame"]["scope"] == scope
            assert not task.done()
        finally:
            release.set()
        result = await asyncio.wait_for(task, 3)
        assert result.status == "applied"
        assert adapter._cursor == 20 and adapter._event_id == "newer-foreground-event"
        assert any(item.source_id == "delivered-midflight"
                   for item in adapter.emotion_service._state.windows[scope])
        assert len(aux.calls) == 2
        replay = await adapter.appraise_affect(command)
        assert replay.idempotency_hit and len(aux.calls) == 2
        restored, _, _ = _owner(tmp_path, enabled=incremental, emotion="on")
        assert restored._cursor == 20
        assert restored.emotion_service.guidance(observation.context_id).revision == result.result["emotion_revision"]
        await restored.close()
        await adapter.close()

    asyncio.run(scenario())


def test_heartbeat_queue_does_not_block_qq_and_coalesces_pending_ticks(tmp_path):
    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        entered, release, qq_done = threading.Event(), threading.Event(), asyncio.Event()

        async def slow_maintenance():
            def blocking_scan():
                entered.set()
                assert release.wait(5)

            await runtime.heartbeat.thread.run(blocking_scan)

        async def prepare(command):
            return _prepared(command)

        async def completed(topic, payload):
            if topic == "event.completed" and payload.get("decision_kind") == "qq_prepare":
                assert payload["status"] == "completed"
                qq_done.set()

        runtime._refresh_embodiment = slow_maintenance
        adapter.prepare_qq_response = prepare
        runtime.add_listener(completed)
        running = asyncio.create_task(runtime.run())
        try:
            while not runtime.heartbeat.active:
                await asyncio.sleep(0)
            await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
            assert await asyncio.to_thread(entered.wait, 2)
            for _ in range(3):
                await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                    priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
            assert runtime.heartbeat.queue.qsize() == 1
            assert runtime.heartbeat.queue.statistics().coalesced == 2
            dto = _request(seconds=20)
            await runtime.submit_event(event_type=RuntimeEventType.QQ_TURN,
                priority=RuntimeEventPriority.USER, source="qq_body", payload=dto.model_dump(mode="json"))
            await asyncio.wait_for(qq_done.wait(), 2)
            assert not release.is_set()
            assert not any(e.event_type == RuntimeEventType.HEARTBEAT for e in runtime.queue.snapshot())
            assert runtime.status().background["heartbeat"]["running"]
        finally:
            release.set()
            await runtime.stop(reason="background queue test")
            await asyncio.wait_for(running, 3)
        assert runtime.lifecycle == RuntimeLifecycle.STOPPED
        assert runtime.heartbeat.task.done() and runtime.heartbeat.ticker.done()

    asyncio.run(scenario())


def test_heartbeat_queue_waits_for_resume_without_consuming_pending_tick(tmp_path):
    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        runtime.heartbeat.start()
        await asyncio.sleep(0)
        await runtime.pause(reason="pause queue test")
        await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
        await asyncio.sleep(0.02)
        assert runtime.heartbeat.queue.qsize() == 1
        completed = asyncio.Event()

        async def refreshed():
            completed.set()

        runtime._refresh_embodiment = refreshed
        await runtime.resume(reason="resume queue test")
        await asyncio.wait_for(completed.wait(), 2)
        await runtime.stop(reason="resumed heartbeat completed")

    asyncio.run(scenario())


def test_heartbeat_clock_keeps_ticking_during_foreground_model(tmp_path):
    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime",
            scheduler=RealTimeScheduler(0.02))
        await runtime.boot_and_recover()
        entered, release, heartbeat_done = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def prepare(command):
            entered.set()
            await release.wait()
            return _prepared(command)

        async def completed(topic, payload):
            if topic == "event.completed" and payload.get("decision_kind") == "heartbeat" and entered.is_set():
                heartbeat_done.set()

        adapter.prepare_qq_response = prepare
        runtime.add_listener(completed)
        running = asyncio.create_task(runtime.run())
        try:
            while not runtime.heartbeat.active:
                await asyncio.sleep(0)
            dto = _request(seconds=20)
            await runtime.submit_event(event_type=RuntimeEventType.QQ_TURN,
                priority=RuntimeEventPriority.USER, source="qq_body", payload=dto.model_dump(mode="json"))
            await asyncio.wait_for(entered.wait(), 2)
            await asyncio.wait_for(heartbeat_done.wait(), 2)
            assert not release.is_set() and runtime._foreground_dispatches
        finally:
            release.set()
            # Let the foreground decision finish before shutting down its owner.
            while runtime._foreground_dispatches:
                await asyncio.sleep(0)
            await runtime.stop(reason="independent clock test")
            await asyncio.wait_for(running, 3)

    asyncio.run(scenario())


def test_thread_lanes_are_independent_and_preserve_trace_context():
    async def scenario():
        affect, heartbeat = ThreadLane("test-affect"), ThreadLane("test-heartbeat")
        release, entered = threading.Event(), threading.Event()
        context = ContextVar("background-trace", default="missing")
        context.set("qq-trace:test")

        def blocked():
            entered.set()
            assert release.wait(5)
            return context.get()

        task = asyncio.create_task(affect.run(blocked))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            result = await asyncio.wait_for(heartbeat.run(lambda: (threading.current_thread().name, context.get())), 2)
            assert result[0].startswith("test-heartbeat") and result[1] == "qq-trace:test"
            assert not task.done()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            assert not await affect.drain(0.01)
        finally:
            release.set()
            await affect.close()
            await heartbeat.close()

    asyncio.run(scenario())


def test_homeostasis_mutations_remain_serial_across_background_and_foreground(tmp_path):
    class DelayedHomeostasis(PassiveHomeostasisAdapter):
        def __init__(self):
            super().__init__()
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def apply_heartbeat(self, command):
            self.entered.set()
            await self.release.wait()
            return await super().apply_heartbeat(command)

    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        homeostasis = DelayedHomeostasis()
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=homeostasis, state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        beat = await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
        await runtime.queue.get(timeout=0)
        worker = asyncio.create_task(runtime.dispatch(beat))
        await asyncio.wait_for(homeostasis.entered.wait(), 2)
        front = await runtime.submit_event(event_type=RuntimeEventType.ENVIRONMENT_CHANGE,
            priority=RuntimeEventPriority.USER, source="test")
        await runtime.queue.get(timeout=0)
        task = asyncio.create_task(runtime.dispatch(front))
        try:
            await asyncio.sleep(0)
            assert not task.done()
        finally:
            homeostasis.release.set()
        results = await asyncio.gather(worker, task)
        assert all(r.status == "completed" for r in results)
        assert runtime._homeostasis_snapshot.revision == 2
        assert runtime.lifecycle == RuntimeLifecycle.RUNNING
        await runtime.stop(reason="serialized homeostasis test")

    asyncio.run(scenario())


def test_paused_runtime_fences_qq_result_that_finishes_in_background(tmp_path):
    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(command):
            entered.set()
            await release.wait()
            return _prepared(command)

        adapter.prepare_qq_response = prepare
        event = await _accept(runtime, _request(seconds=20))
        task = asyncio.create_task(runtime.dispatch(event))
        await asyncio.wait_for(entered.wait(), 2)
        runtime.lifecycle = RuntimeLifecycle.PAUSED
        release.set()
        result = await asyncio.wait_for(task, 2)
        assert result.status == "ignored"
        assert result.detail["reason"] == "runtime_result_fenced"
        await runtime.stop(reason="late result fence test")

    asyncio.run(scenario())


def test_heartbeat_shutdown_is_bounded_and_keeps_state_owner_until_worker_finishes(tmp_path):
    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime",
            maintenance_drain_seconds=0.01)
        await runtime.boot_and_recover()
        entered, release = asyncio.Event(), asyncio.Event()

        async def blocked():
            entered.set()
            await release.wait()

        runtime._refresh_embodiment = blocked
        runtime.heartbeat.start()
        await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
        await asyncio.wait_for(entered.wait(), 2)
        try:
            with pytest.raises(RuntimeError, match="heartbeat maintenance did not drain"):
                await asyncio.wait_for(runtime.stop(reason="bounded stop"), 2)
            assert runtime.lifecycle == RuntimeLifecycle.FAULTED
            assert runtime._research_workers_pending()
            assert not runtime.heartbeat.task.done()
        finally:
            release.set()
        await asyncio.wait_for(runtime.heartbeat.task, 2)
        await runtime.stop(reason="worker settled")
        assert runtime.lifecycle == RuntimeLifecycle.STOPPED

    asyncio.run(scenario())


@pytest.mark.parametrize("incremental", [False, True])
def test_background_emotion_delta_does_not_publish_pending_foreground_revision(tmp_path, incremental):
    async def scenario():
        adapter, _, _ = _owner(tmp_path, enabled=incremental, emotion="on")
        adapter._persist_state()
        adapter._revision = 1
        adapter.pipeline.session_store.append("pending-session", "user", "尚未提交")
        adapter.emotion_service.llm = FakeAux()
        now = datetime.now(UTC)
        observation = request(as_of=now, messages=[message(timestamp=now)])
        frame = await asyncio.to_thread(adapter.emotion_service.observe, observation)
        assert frame.status == "model"
        assert (await adapter.get_snapshot()).revision == 0
        persisted = (adapter._state_store.load() if incremental
            else json.loads(adapter.state_path.read_text(encoding="utf-8")))
        assert persisted["revision"] == 0
        assert "pending-session" not in persisted["sessions"]
        adapter._persist_state()
        assert (await adapter.get_snapshot()).revision == 1
        await adapter.close()

    asyncio.run(scenario())


def test_heartbeat_worker_can_restart_after_drained_close(tmp_path):
    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        runtime.heartbeat.start()
        original_queue = runtime.heartbeat.queue
        assert await runtime.heartbeat.close(2)
        runtime.heartbeat.start()
        assert runtime.heartbeat.queue is not original_queue
        completed = asyncio.Event()

        async def refreshed():
            completed.set()

        runtime._refresh_embodiment = refreshed
        await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
        await asyncio.wait_for(completed.wait(), 2)
        await runtime.stop(reason="heartbeat restart test")
        assert runtime.lifecycle == RuntimeLifecycle.STOPPED

    asyncio.run(scenario())


def test_background_policy_result_does_not_schedule_idle_over_active_qq(tmp_path):
    class BlockingPolicies(PassiveHomeostasisAdapter):
        def __init__(self):
            super().__init__(policies=[{"kind": "expression"}])
            self.entered, self.release = asyncio.Event(), asyncio.Event()

        async def propose_policies(self, **kwargs):
            self.entered.set()
            await self.release.wait()
            return await super().propose_policies(**kwargs)

    async def scenario():
        _, _, adapter = _pipeline_adapter(tmp_path)
        homeostasis = BlockingPolicies()
        runtime = RuntimeSupervisor(manifest=_manifest(), cognitive=adapter,
            homeostasis=homeostasis, state_dir=tmp_path / "runtime")
        await runtime.boot_and_recover()
        entered, release = asyncio.Event(), asyncio.Event()

        async def prepare(command):
            entered.set()
            await release.wait()
            return _prepared(command)

        adapter.prepare_qq_response = prepare
        runtime.heartbeat.start()
        await runtime.submit_event(event_type=RuntimeEventType.HEARTBEAT,
            priority=RuntimeEventPriority.HEARTBEAT, source="scheduler")
        await asyncio.wait_for(homeostasis.entered.wait(), 2)
        event = await _accept(runtime, _request(seconds=20))
        foreground = asyncio.create_task(runtime.dispatch(event))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            homeostasis.release.set()
            async with asyncio.timeout(2):
                while runtime.heartbeat.current_event is not None:
                    await asyncio.sleep(0)
            assert not any(e.event_type == RuntimeEventType.IDLE for e in runtime.queue.snapshot())
            assert not foreground.done()
        finally:
            homeostasis.release.set()
            release.set()
            await foreground
            await runtime.stop(reason="background policy fence test")

    asyncio.run(scenario())
