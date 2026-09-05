"""Runtime lifecycle owns body-memory workers and isolates their failures."""

import asyncio
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path

from harness.body_belief.appraisal import AppraisalResult
from harness.research.grounded_agency.hashes import content_hash
from harness.research.manifest import ExperimentManifest
from harness.runtime.models import AgencyState, RuntimeEvent, RuntimeLifecycle
from tests.research.interoception.test_runtime_wiring import body, crash_without_checkpoint, submit_body
from tests.runtime.test_grounded_runtime_wiring import drain, research_manifest, research_runtime


def memory_manifest(treatment="body_causal_beliefs"):
    payload = research_manifest().model_dump(mode="json")
    payload["grounded_agency"]["memory_treatment"] = treatment
    return ExperimentManifest.model_validate(payload)


def test_failed_body_memory_cannot_block_terminal_body_authority(tmp_path, monkeypatch):
    runtime, model, _, _ = research_runtime(tmp_path, [], manifest=memory_manifest("objective_events"))

    async def broken(*args, **kwargs):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(runtime._research_memory, "observe", broken)

    async def scenario():
        await runtime.boot_and_recover()
        try:
            snapshot = body(1, datetime.now(UTC), units=0)
            snapshot = snapshot.model_copy(update={"energy": snapshot.energy.model_copy(update={"terminal": True})})
            await submit_body(runtime, snapshot)
            assert runtime._agency_state == AgencyState.TERMINAL
            assert runtime._grounded_body == snapshot
            await runtime.submit_user_message("Inspect the current view.")
            assert (await drain(runtime))[0].decision_kind == "no_action"
            assert not model.messages
            failure = next(record for record in runtime.journal.records() if record.record_type == "body_memory_observation_failed")
            assert failure.data["analysis_eligible"] is False
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_failed_body_memory_context_is_audited_without_changing_model_choice(tmp_path, monkeypatch):
    output = '{"kind":"no_action","confidence":0.8}'
    runtime, model, provider, _ = research_runtime(tmp_path, [output], manifest=memory_manifest("objective_events"))

    def broken(*args, **kwargs):
        raise RuntimeError("retrieval unavailable")

    monkeypatch.setattr(runtime._research_memory, "context", broken)

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, body(1, datetime.now(UTC)))
            await runtime.submit_user_message("Inspect the current view.")
            assert (await drain(runtime))[0].decision_kind == "no_action"
            assert len(model.messages) == 1 and not provider.executed
            assert any(record.record_type == "body_memory_context_failed" for record in runtime.journal.records())
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_runtime_pause_drains_appraiser_and_restart_keeps_objective_refs(tmp_path):
    started, release = threading.Event(), threading.Event()

    class BlockingAppraiser:
        def appraise(self, experiences, **kwargs):
            started.set()
            assert release.wait(2)
            messages = ({"role": "system", "content": "Observe Q7."},)
            return AppraisalResult(success=True, messages=messages, input_hash=content_hash(messages))

    manifest = memory_manifest()
    runtime, _, _, _ = research_runtime(tmp_path, ['{"kind":"no_action","confidence":0.8}'], manifest=manifest)
    runtime._research_memory.service.appraiser = BlockingAppraiser()

    async def scenario():
        await runtime.boot_and_recover()
        await submit_body(runtime, body(1, datetime.now(UTC)))
        await runtime.submit_user_message("Inspect the current view.")
        await drain(runtime)
        await submit_body(runtime, body(2, datetime.now(UTC), units=40))
        assert await asyncio.to_thread(started.wait, 1)
        pause = asyncio.create_task(runtime.pause(reason="test domain drain"))
        await asyncio.sleep(.02)
        assert not pause.done()
        release.set()
        await pause
        assert runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert not runtime._research_memory.service._tasks
        before = runtime._research_memory.service.store.experiences()
        assert len(before) == 1
        await runtime.stop()
        second, model, _, _ = research_runtime(tmp_path, [], manifest=manifest)
        await second.boot_and_recover()
        try:
            assert second._research_memory.service.store.experiences() == before
            assert not model.messages
        finally:
            await second.stop()

    asyncio.run(scenario())


def test_runtime_appraisal_exception_preserves_action_and_audits_failure(tmp_path):
    class BrokenAppraiser:
        def appraise(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

    runtime, model, provider, _ = research_runtime(
        tmp_path, ['{"kind":"wait","duration_ms":0,"confidence":0.8}'], manifest=memory_manifest(),
    )
    runtime._research_memory.service.appraiser = BrokenAppraiser()

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, body(1, datetime.now(UTC)))
            await runtime.submit_user_message("Inspect the current view.")
            await drain(runtime)
            await drain(runtime)
            await submit_body(runtime, body(2, datetime.now(UTC), units=40))
            assert await runtime._research_memory.service.drain(1)
            assert len(provider.executed) == 1 and len(model.messages) == 1
            assert len(runtime._research_memory.service.store.experiences()) == 1
            assert any(entry["event"] == "appraisal_failed" for entry in runtime._research_memory.service.store.audit())
            assert runtime.delivery_backend.deliveries == {}
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_crash_retry_reuses_durable_terminal_time_and_evidence_identity(tmp_path):
    manifest = memory_manifest("objective_events")
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    runtime, _, _, _ = research_runtime(tmp_path, ['{"kind":"no_action","confidence":0.8}'], manifest=manifest)
    runtime._research_clock = lambda: clock[0]

    async def scenario():
        await runtime.boot_and_recover()
        await submit_body(runtime, body(1, clock[0]))
        await runtime.submit_user_message("Inspect the current view.")
        await drain(runtime)
        saved = next(record for record in runtime.journal.records() if record.record_type == "body_experience_outcome")
        original = next(RuntimeEvent.model_validate(record.data["event"]) for record in runtime.journal.records()
                        if record.record_type == "event_accepted" and record.event_id == saved.event_id)
        pending = runtime._research_memory.service.store.pending()
        assert len(pending) == 1
        await crash_without_checkpoint(runtime)
        clock[0] += timedelta(seconds=100)
        second, _, _, _ = research_runtime(tmp_path, [], manifest=manifest)
        second._research_clock = lambda: clock[0]
        await second.boot_and_recover()
        try:
            second._record_grounded_experience(original, evidence={"new_nondeterministic_ref": "retry"},
                                              success=True, verified=True)
            assert second._research_memory.service.store.pending() == pending
            outcomes = [record for record in second.journal.records() if record.record_type == "body_experience_outcome"]
            assert len(outcomes) == 1 and outcomes[0].data == saved.data
            assert not any(record.record_type == "body_experience_recording_failed" for record in second.journal.records())
            # A sample captured before restart still follows the original terminal instant.
            await submit_body(second, body(2, datetime(2026, 1, 1, tzinfo=UTC) + timedelta(seconds=1), units=40))
            episodes = second._research_memory.service.store.experiences()
            assert len(episodes) == 1
            assert episodes[0].completed_at == datetime.fromisoformat(saved.data["completed_at"])
        finally:
            await second.stop()

    asyncio.run(scenario())


def test_lost_body_memory_database_replays_exact_original_next_sample(tmp_path):
    manifest = memory_manifest("objective_events")
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    runtime, _, _, _ = research_runtime(tmp_path, ['{"kind":"no_action","confidence":0.8}'], manifest=manifest)
    runtime._research_clock = lambda: clock[0]

    async def scenario():
        await runtime.boot_and_recover()
        await submit_body(runtime, body(1, clock[0]))
        await runtime.submit_user_message("Inspect the current view.")
        await drain(runtime)
        clock[0] += timedelta(seconds=1)
        await submit_body(runtime, body(2, clock[0], units=40))
        expected = runtime._research_memory.service.snapshot()
        assert len(expected.experiences) == 1
        db_path = Path(runtime._research_memory.service.store.db.execute("PRAGMA database_list").fetchone()[2])
        assert db_path.resolve().is_relative_to(tmp_path.resolve())
        await crash_without_checkpoint(runtime)
        db_path.unlink()
        clock[0] += timedelta(seconds=100)
        second, model, _, _ = research_runtime(tmp_path, [], manifest=manifest)
        second._research_clock = lambda: clock[0]
        await second.boot_and_recover()
        try:
            assert second._research_memory.service.snapshot() == expected
            assert not second._research_memory.service.store.pending()
            assert not model.messages
        finally:
            await second.stop()

    asyncio.run(scenario())
