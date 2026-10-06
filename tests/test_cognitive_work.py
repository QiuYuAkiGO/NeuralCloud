from datetime import UTC, datetime, timedelta
import asyncio

from harness.homeostasis.work import WorkLedger
from harness.runtime.models import RuntimeEventType, RuntimeEventPriority, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeSupervisor
from harness.homeostasis.engine import HomeostasisEngine
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter
from tests.test_cognitive_dynamics import active_profile


def test_work_counts_generated_and_images_excludes_automatic_history_but_allows_explicit_reread(tmp_path):
    ledger = WorkLedger(tmp_path/"work.sqlite3")
    messages = [{"role":"system", "content":"fixed prompt"*1000},
                {"role":"user", "content":[{"type":"text", "text":"abcd"}, {"type":"image_url", "image_url":{"url":"data:fake"}}]}]
    args = dict(context="qq:1", messages=messages, prefix_count=1, usage={"completion_tokens":3})
    first = ledger.record(span_id="first", **args)
    second = ledger.record(span_id="second", **args)
    reread = ledger.record(span_id="third", reread=True, **args)
    assert first.input_tokens == reread.input_tokens == 1
    assert first.images == reread.images == 1
    assert second.input_tokens == second.images == 0
    assert first.output_tokens == second.output_tokens == 3
    ledger.acknowledge("first")
    assert [r.exposure_id for r in WorkLedger(ledger.path).pending()] == ["second", "third"]
    assert "fixed prompt" not in ledger.path.read_bytes().decode(errors="ignore")


def test_optional_bad_assessment_does_not_drop_processing_receipt(tmp_path):
    receipt = WorkLedger(tmp_path/"work.sqlite3").record(span_id="bad-assessment",context="tool",
        messages=[{"role":"user", "content":"text"}],prefix_count=0,usage={"completion_tokens":5},
        assessment={"impacts":{"energy_reserve":1}})
    assert receipt.assessment is None and receipt.output_tokens == 5


def test_identical_new_message_costs_work_but_same_event_prefix_does_not(tmp_path):
    ledger = WorkLedger(tmp_path/"work.sqlite3")
    args = dict(context="qq:1", messages=[{"role":"user", "content":"重复的新消息"}], prefix_count=0, usage={})
    first = ledger.record(span_id="first", evidence_ref="event-1", **args)
    carried = ledger.record(span_id="carried", evidence_ref="event-1", **args)
    new = ledger.record(span_id="new", evidence_ref="event-2", **args)
    assert first.input_tokens == new.input_tokens > 0
    assert carried.input_tokens == 0
    assert new.input_count_basis == "text_units_estimate"
    assert ledger.record(span_id="new", evidence_ref="event-2", **args) == new


def test_independent_worker_advances_during_manual_pause_without_cognitive_invocation(tmp_path, monkeypatch):
    async def run():
        clock = [datetime.now(UTC)]
        profile = active_profile()
        engine = HomeostasisEngine(profile, state_dir=tmp_path/"homeostasis", clock=lambda: clock[0])
        memory, _, adapter = _pipeline_adapter(tmp_path)
        manifest = _manifest(expression_mode="responses").model_copy(update={"homeostatic_profile":profile.profile_ref,
            "persona_version":profile.persona_version, "coupling_version":profile.coupling_matrix_version})
        runtime = RuntimeSupervisor(manifest=manifest, cognitive=adapter, homeostasis=engine, state_dir=tmp_path/"runtime")
        await runtime.boot_and_recover()
        await runtime.pause(reason="user manual pause")
        monkeypatch.setattr(adapter, "prepare_response", lambda _: (_ for _ in ()).throw(AssertionError("no cognition when paused")))
        prior = engine.snapshot.dimensions["cognitive_load"].value
        engine._dynamics.updated_at -= timedelta(minutes=30)
        runtime.passive_state.start()
        event = await runtime.submit_event(event_type=RuntimeEventType.HOMEOSTASIS_TICK,
            priority=RuntimeEventPriority.CONTROL, source_time=clock[0], source="test.passive")
        async def completed():
            while engine.snapshot.acknowledged_receive_seq < event.receive_seq:
                await asyncio.sleep(.01)
        try:
            await asyncio.wait_for(completed(), 5)
            assert runtime.lifecycle == RuntimeLifecycle.PAUSED
            assert engine.snapshot.dimensions["cognitive_load"].value < prior
            assert runtime.passive_state.last_error is None
        finally:
            await runtime.stop(reason="test complete")
            memory.close()
    asyncio.run(run())
