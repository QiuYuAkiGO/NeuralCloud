"""Opaque sensor samples cross the production Runtime/Cognitive boundary safely."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness.embodiment.contracts import BodyStateSnapshot, EnergyState, EvidenceRef
from harness.embodiment.capabilities import EmbodimentState
from harness.runtime import RuntimeEventPriority, RuntimeEventType
from harness.runtime.models import AgencyState
from tests.runtime.test_grounded_runtime_wiring import drain, research_manifest, research_runtime


def body(revision, captured_at, *, units=50.0):
    return BodyStateSnapshot(
        provider_id="simulator", body_instance_id="sim-body-01", revision=revision, captured_at=captured_at,
        energy=EnergyState(
            capacity_units=100, available_units=units, available_ratio=units / 100,
            delta_units_per_minute=-1, terminal=False,
        ),
        evidence_refs=[EvidenceRef(provider_id="simulator", ref=f"private://truth/{revision}")],
    )


async def submit_body(runtime, snapshot):
    event = await runtime.submit_event(
        event_type=RuntimeEventType.BODY_STATE, priority=RuntimeEventPriority.PERCEPTION,
        source="verified-provider", source_time=snapshot.captured_at,
        payload={"body_state": snapshot.model_dump(mode="json")},
    )
    result = (await drain(runtime))[0]
    assert result.status == "completed", result.detail
    assert result.decision_kind == "body_observation"
    return event


@pytest.mark.parametrize("legacy", [False, True])
def test_body_tick_is_observation_only_and_opaque_reaches_actual_main_prompt(tmp_path, legacy):
    manifest = research_manifest(legacy=legacy)
    output = "Observed." if legacy else '{"kind":"no_action","confidence":0.8}'
    runtime, model, provider, homeostasis = research_runtime(tmp_path, [output], manifest=manifest)

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, body(1, datetime.now(UTC)))
            assert model.messages == []
            assert not any(item.event_type == RuntimeEventType.GOAL_RECONSIDERATION for item in runtime.queue.snapshot())
            assert homeostasis.event_calls == homeostasis.policy_calls == 0
            assert runtime._interoception is None
            event = await runtime.submit_user_message("Inspect the current view.")
            result = (await drain(runtime))[0]
            assert result.status == "completed", result.detail
            assert result.decision_kind == ("reply" if legacy else "no_action")
            assert len(model.messages) == 1
            visible = json.dumps(model.messages)
            assert "channel=q7" in visible and "sample=1" in visible
            for hidden in ["available_ratio", "capacity_units", "private://truth", "source_revision", "energy_reserve", "transform_ref"]:
                assert hidden not in visible
            if legacy:
                assert "grounded_agency_decision" not in visible
                assert "decision_schema" not in visible
                assert len(runtime.delivery_backend.deliveries) == 1
            else:
                assert runtime.delivery_backend.deliveries == {}
                assert runtime.cognitive.research_traces[event.event_id]["observation_hashes"]
            assert not provider.executed
        finally:
            await runtime.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize(("treatment", "samples"), [("opaque_current", (2,)), ("opaque_history", (1, 2))])
def test_runtime_samples_current_or_history_from_sensor_clock_not_user_time(tmp_path, monkeypatch, treatment, samples):
    import harness.runtime.adapters as adapters_module
    import harness.runtime.supervisor as supervisor_module

    moment = datetime.now(UTC)
    clock = [moment]
    monkeypatch.setattr(supervisor_module, "utc_now", lambda: clock[0])
    monkeypatch.setattr(adapters_module, "_utc_now", lambda: clock[0])
    output = '{"kind":"no_action","confidence":0.8}'
    runtime, model, _, _ = research_runtime(
        tmp_path, [output, output], manifest=research_manifest(treatment=treatment, delay="fixed:10"),
    )

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, body(1, moment))
            clock[0] = moment + timedelta(seconds=1)
            await submit_body(runtime, body(2, clock[0], units=40))
            clock[0] = moment + timedelta(seconds=5)
            await runtime.submit_event(
                event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
                source="untrusted", source_time=moment + timedelta(days=1),
                payload={"text": "Inspect the current view."},
            )
            assert (await drain(runtime))[0].status == "completed"
            assert "Internal channel sample" not in json.dumps(model.messages[0])
            clock[0] = moment + timedelta(seconds=12)
            event = await runtime.submit_user_message("Inspect the current view.")
            assert (await drain(runtime))[0].status == "completed"
            trace = runtime.cognitive.research_traces[event.event_id]
            assert len(trace["observation_hashes"]) == len(samples)
            visible = json.dumps(model.messages[1])
            if treatment == "opaque_current":
                assert "sample=2" in visible and "sample=1" not in visible
            else:
                assert "@1" in visible and "@2" in visible
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_same_body_revision_restarts_with_identical_sample_and_deduplicates(tmp_path):
    manifest = research_manifest()
    snapshot = body(7, datetime.now(UTC))

    async def scenario():
        first, _, _, _ = research_runtime(tmp_path, [], manifest=manifest)
        await first.boot_and_recover()
        await submit_body(first, snapshot)
        before = first._opaque_store.envelopes()
        await first.stop()

        second, model, _, _ = research_runtime(
            tmp_path, ['{"kind":"no_action","confidence":0.8}'], manifest=manifest,
        )
        await second.boot_and_recover()
        try:
            assert second._opaque_store.envelopes() == before
            await submit_body(second, snapshot)
            assert second._opaque_store.envelopes() == before
            await second.submit_user_message("Inspect the current view.")
            assert (await drain(second))[0].status == "completed"
            assert "sample=1" in json.dumps(model.messages)
            assert len(second._opaque_store.envelopes()) == 1
        finally:
            await second.stop()

    asyncio.run(scenario())


async def crash_without_checkpoint(runtime):
    """Release test resources while preserving only the already durable journal."""
    await runtime.cognitive.close()
    await runtime.homeostasis.close()
    if runtime._opaque_store is not None:
        runtime._opaque_store.close()
    if runtime._research_memory is not None:
        await runtime._research_memory.service.close()
    await runtime.queue.close()
    runtime.instance_lock.release()


@pytest.mark.parametrize("treatment", ["opaque_current", "none"])
@pytest.mark.parametrize("checkpoint_before_terminal", [False, True])
def test_terminal_body_survives_restart_without_checkpoint(tmp_path, treatment, checkpoint_before_terminal):
    manifest = research_manifest(treatment=treatment)
    terminal = body(3, datetime.now(UTC), units=0)
    terminal = terminal.model_copy(update={"energy": terminal.energy.model_copy(update={"terminal": True})})

    async def scenario():
        first, _, _, _ = research_runtime(tmp_path, [], manifest=manifest)
        await first.boot_and_recover()
        if checkpoint_before_terminal:
            await submit_body(first, body(1, terminal.captured_at - timedelta(milliseconds=1)))
            first.write_checkpoint()
        await submit_body(first, terminal)
        assert first._agency_state == AgencyState.TERMINAL
        await crash_without_checkpoint(first)
        second, model, provider, _ = research_runtime(tmp_path, [], manifest=manifest)
        await second.boot_and_recover()
        try:
            assert second._agency_state == AgencyState.TERMINAL
            assert second.embodiment.status().state == EmbodimentState.TERMINAL
            assert second._grounded_body == terminal
            assert second._body_revision == 3
            await second.submit_user_message("Inspect the current view.")
            assert (await drain(second))[0].decision_kind == "no_action"
            assert not model.messages and not provider.executed
        finally:
            await second.stop()

    asyncio.run(scenario())


@pytest.mark.parametrize("treatment", ["opaque_current", "none"])
@pytest.mark.parametrize("invalid", ["older_revision", "conflicting_revision", "older_time"])
def test_rejected_body_revision_cannot_change_runtime_state(tmp_path, treatment, invalid):
    moment = datetime.now(UTC)
    original = body(5, moment)
    bad = {
        "older_revision": body(4, moment, units=40),
        "conflicting_revision": body(5, moment, units=40),
        "older_time": body(6, moment - timedelta(seconds=1), units=40),
    }[invalid]
    runtime, _, _, _ = research_runtime(tmp_path, [], manifest=research_manifest(treatment=treatment))

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, original)
            await runtime.submit_event(
                event_type=RuntimeEventType.BODY_STATE, priority=RuntimeEventPriority.PERCEPTION,
                source="verified-provider", source_time=bad.captured_at,
                payload={"body_state": bad.model_dump(mode="json")},
            )
            queued = await runtime.queue.get(timeout=0)
            result = await runtime.dispatch(queued)
            assert result.status in {"failed", "deferred"}
            assert runtime._grounded_body == original
            assert runtime._body_revision == 5 and runtime._body_captured_at == moment
            if runtime._opaque_store is not None:
                assert len(runtime._opaque_store.envelopes()) == 1
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_missing_sensor_store_rebuilds_identical_samples_from_runtime_journal(tmp_path):
    manifest = research_manifest(treatment="opaque_history")
    moment = datetime.now(UTC)

    async def scenario():
        first, _, _, _ = research_runtime(tmp_path, [], manifest=manifest)
        await first.boot_and_recover()
        await submit_body(first, body(1, moment))
        await submit_body(first, body(2, moment + timedelta(milliseconds=1), units=40))
        before = first._opaque_store.envelopes()
        records = first._opaque_store.records()
        await first.stop()
        (tmp_path / "runtime" / "opaque-interoception.sqlite3").unlink()
        second, model, _, _ = research_runtime(
            tmp_path, ['{"kind":"no_action","confidence":0.8}'], manifest=manifest,
        )
        await second.boot_and_recover()
        try:
            assert second._opaque_store.envelopes() == before
            assert second._opaque_store.records() == records
            assert second._body_revision == 2
            await second.submit_user_message("Inspect the current view.")
            assert (await drain(second))[0].status == "completed"
            assert "@1" in json.dumps(model.messages) and "@2" in json.dumps(model.messages)
        finally:
            await second.stop()

    asyncio.run(scenario())
