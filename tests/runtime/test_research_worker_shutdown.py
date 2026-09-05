"""A failed shutdown retains state ownership until research workers finish."""

from __future__ import annotations

import asyncio
import threading
from datetime import UTC, datetime

import pytest

from harness.agency.models import GoalTrigger
from harness.body_belief.appraisal import AppraisalResult
from harness.llm.client import ChatResult
from harness.research.grounded_agency.hashes import content_hash
from harness.research.manifest import ExperimentManifest
from harness.runtime import DomainCommandError, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, RuntimeStateError
from harness.runtime.locking import InstanceLockError, SingleInstanceLock
from harness.social_world.contracts import EntityObservation
from tests.research.interoception.test_runtime_wiring import body, submit_body
from tests.runtime.test_grounded_runtime_wiring import drain, research_manifest, research_runtime


def _manifest(channel):
    payload = research_manifest().model_dump(mode="json")
    if channel == "body":
        payload["grounded_agency"]["memory_treatment"] = "body_causal_beliefs"
    elif channel == "agency":
        payload["grounded_agency"]["agency"] = {"enabled": True, "model_timeout_seconds": .02}
    else:
        payload["grounded_agency"]["open_social"] = {"enabled": True, "entity_memory": "general_beliefs"}
    return ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("channel", ("body", "agency", "social"))
def test_failed_stop_blocks_second_instance_until_real_model_worker_finishes(tmp_path, channel):
    started, release = threading.Event(), threading.Event()
    runtime, _, _, _ = research_runtime(
        tmp_path, ['{"kind":"no_action","confidence":0.8}'], manifest=_manifest(channel),
    )

    class BlockingModel:
        def complete_chat_result(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            return ChatResult(text='{"candidates":[]}' if channel == "social" else
                              '{"arbitration":{"operation":"no_change","confidence":0.8}}')

        def appraise(self, *args, **kwargs):
            started.set()
            assert release.wait(5)
            messages = ({"role": "system", "content": "Observe Q7."},)
            return AppraisalResult(success=True, messages=messages, input_hash=content_hash(messages))

    async def scenario():
        await runtime.boot_and_recover()
        runtime.maintenance_drain_seconds = .01
        worker = None
        competitor = SingleInstanceLock(runtime.instance_lock.path, runtime_instance_id="second-instance")
        try:
            if channel == "body":
                runtime._research_memory.service.appraiser = BlockingModel()
                await submit_body(runtime, body(1, datetime.now(UTC)))
                await runtime.submit_user_message("Inspect the current view.")
                await drain(runtime)
                await submit_body(runtime, body(2, datetime.now(UTC), units=40))
                assert await asyncio.to_thread(started.wait, 1)
                worker = next(iter(runtime._research_memory.service._tasks.values()))
            elif channel == "agency":
                runtime._agency_service.llm = BlockingModel()
                await runtime._agency_service.reconsider(GoalTrigger(
                    kind="environment_change", signature="changed", evidence_refs=("world",), observed_at=datetime.now(UTC),
                ), evidence_refs=("world",), provider_intents=("control.observe.v1",))
                assert started.is_set()
                worker = runtime._agency_service._inflight
            else:
                runtime.cognitive.pipeline.llm = BlockingModel()
                runtime.command_timeout_seconds = .02
                observation = EntityObservation(event_id="event-one", actor_refs=("actor",), event_type="presence_changed",
                    facts={"present": True}, occurred_at=datetime.now(UTC), evidence_refs=("raw-one",))
                event = await runtime.submit_event(event_type=RuntimeEventType.ENVIRONMENT_CHANGE,
                    priority=RuntimeEventPriority.IMPORTANT_ENVIRONMENT, source="fixture",
                    payload={"entity_observations": [observation.model_dump(mode="json")]})
                with pytest.raises(DomainCommandError, match="exceeded its deadline"):
                    await runtime._social_research_context(event)
                assert started.is_set()
                worker = next(iter(runtime._social_appraisal_tasks))

            assert worker is not None and not worker.done()
            with pytest.raises(RuntimeStateError, match="did not drain"):
                await runtime.stop()
            assert runtime.lifecycle == RuntimeLifecycle.FAULTED
            assert runtime.instance_lock.owned and runtime._research_workers_pending()
            with pytest.raises(InstanceLockError, match="already exists"):
                competitor.acquire()
            release.set()
            await worker
            assert not runtime._research_workers_pending()
            await runtime.stop(reason="retry after worker completion")
            assert runtime.lifecycle == RuntimeLifecycle.STOPPED and not runtime.instance_lock.owned
            competitor.acquire()
            assert competitor.owned
        finally:
            release.set()
            if worker is not None:
                await worker
            competitor.release()
            if runtime.lifecycle != RuntimeLifecycle.STOPPED:
                await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("worker_pending", (False, True))
def test_failed_boot_only_retains_lock_for_live_research_worker(tmp_path, monkeypatch, worker_pending):
    runtime, _, _, _ = research_runtime(tmp_path, [])

    async def scenario():
        release = asyncio.Event()
        worker = asyncio.create_task(release.wait()) if worker_pending else None
        if worker is not None:
            runtime._social_appraisal_tasks.add(worker)
        competitor = SingleInstanceLock(runtime.instance_lock.path, runtime_instance_id="second-instance")

        def fail_restore():
            raise RuntimeError("unavailable recovery snapshot")

        monkeypatch.setattr(runtime, "_restore_runtime_indexes", fail_restore)
        try:
            with pytest.raises(RuntimeError, match="unavailable recovery snapshot"):
                await runtime.boot_and_recover()
            assert runtime.instance_lock.owned is worker_pending
            if worker_pending:
                with pytest.raises(InstanceLockError):
                    competitor.acquire()
            else:
                competitor.acquire()
                competitor.release()
            release.set()
            if worker is not None:
                await worker
            await runtime.stop()
            competitor.acquire()
            assert competitor.owned
        finally:
            release.set()
            if worker is not None:
                await worker
            competitor.release()
            if runtime.lifecycle != RuntimeLifecycle.STOPPED:
                await runtime.stop()

    asyncio.run(scenario())
