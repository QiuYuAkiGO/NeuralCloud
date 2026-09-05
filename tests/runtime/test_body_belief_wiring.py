"""Objective experience uses actual Runtime decisions and subsequent samples."""

import asyncio
import json
from datetime import UTC, datetime, timedelta

import pytest

from harness.llm.client import ChatResult
from harness.research.manifest import ExperimentManifest
from tests.research.interoception.test_runtime_wiring import body, submit_body
from tests.runtime.test_grounded_runtime_wiring import ResearchProvider, drain, research_manifest, research_runtime


@pytest.mark.parametrize("treatment", ["none", "objective_events", "body_causal_beliefs"])
def test_runtime_experience_and_memory_visibility_with_actual_next_sample(tmp_path, monkeypatch, treatment):
    import harness.runtime.adapters as adapters
    import harness.runtime.supervisor as supervisor

    clock = [datetime.now(UTC)]
    monkeypatch.setattr(supervisor, "utc_now", lambda: clock[0])
    monkeypatch.setattr(adapters, "_utc_now", lambda: clock[0])
    payload = research_manifest(treatment="opaque_history").model_dump(mode="json")
    payload["grounded_agency"]["memory_treatment"] = treatment
    runtime, model, _, homeostasis = research_runtime(tmp_path, [], manifest=ExperimentManifest.model_validate(payload))
    calls = []

    def complete(messages, *, stream=False):
        calls.append(messages)
        return ChatResult(text='{"candidates":[]}' if "body_causal_appraisal" in json.dumps(messages)
                          else '{"kind":"no_action","confidence":0.8}')

    model.complete_chat_result = complete

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, body(1, clock[0]))
            await runtime.submit_user_message("Inspect current observations.")
            assert (await drain(runtime))[0].status == "completed"
            service = runtime._research_memory.service
            assert len(service.store.pending()) == 1
            assert service.store.experiences() == ()
            clock[0] += timedelta(seconds=1)
            await submit_body(runtime, body(2, clock[0], units=40))
            assert await service.drain(5)
            episodes = service.store.experiences()
            assert len(episodes) == 1 and episodes[0].observation_after.sample_sequence == 2
            assert episodes[0].action_outcome_refs and episodes[0].outcome_verified
            await runtime.submit_user_message("Inspect current observations.")
            assert (await drain(runtime))[0].status == "completed"
            decision_messages = [messages for messages in calls if "body_causal_appraisal" not in json.dumps(messages)]
            assert len(decision_messages) == 2
            assert ("body_memory" in json.dumps(decision_messages[-1])) == (treatment != "none")
            for secret in ("available_ratio", "capacity_units", "private://truth", '"seed"', "reading_symbols"):
                assert secret not in json.dumps(calls)
            assert homeostasis.event_calls == homeostasis.outcome_calls == homeostasis.policy_calls == 0
            assert runtime.delivery_backend.deliveries == {}
        finally:
            await runtime.stop()

    asyncio.run(scenario())


def test_safety_deferred_action_is_not_recorded_as_an_executed_causal_experience(tmp_path):
    provider = ResearchProvider()
    provider.supported_intents = ("control.observe.v1",)
    runtime, _, _, _ = research_runtime(
        tmp_path, ['{"kind":"wait","duration_ms":1000,"confidence":0.8}'], provider=provider,
    )

    async def scenario():
        await runtime.boot_and_recover()
        try:
            await submit_body(runtime, body(1, datetime.now(UTC)))
            await runtime.submit_user_message("Inspect the current view.")
            result = (await drain(runtime))[0]
            assert result.decision_kind == "defer"
            assert provider.executed == []
            assert runtime._research_memory.service.store.pending() == ()
            await submit_body(runtime, body(2, datetime.now(UTC), units=40))
            assert runtime._research_memory.service.store.experiences() == ()
        finally:
            await runtime.stop()

    asyncio.run(scenario())
