"""Goal preparation uses the existing Cognitive owner and durable command ledger."""

from __future__ import annotations

import asyncio

import pytest

from harness.contracts.decisions import PreparedResponse
from tests.test_basic_persona_continuity import _host
from tests.test_capability_cognition import FakeLLM, desktop_offer
from tests.test_goal_cognition import goal_command, start_choice


@pytest.fixture(autouse=True)
def isolation(monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "1")
    monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)


def test_goal_preparation_replays_across_restart_without_conversation_mutation(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = llm = FakeLLM(start_choice())
        request = goal_command()
        before = owner.chat.pipeline.session_store.snapshot()
        try:
            first = await owner.prepare_goal_decision(request)
            assert first.status == "applied", first.error
            assert (await owner.prepare_goal_decision(request)).idempotency_hit
            assert len(llm.calls) == 1
            assert owner.chat.pipeline.session_store.snapshot() == before
            assert not owner.adapter._prepared and not owner.chat.memory.stores.episodic.list_all()
        finally:
            owner.chat.llm = original
            await owner.close()
        restored, _ = _host(tmp_path, monkeypatch, [])
        try:
            result = await restored.prepare_goal_decision(request)
            assert result.idempotency_hit and result.result == first.result
        finally:
            await restored.close()
    asyncio.run(scenario())


def test_goal_desktop_prepares_existing_capability_commit_path_once(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        item = desktop_offer()
        output = start_choice()
        output["choice"] = {"kind": "act", "offer_id": item.offer_id, "arguments": {"app_id": "scratchpad"},
            "success_criteria": [{"kind": "window", "condition": "present", "app_id": "scratchpad"}]}
        owner.chat.llm = llm = FakeLLM(output)
        try:
            result = await owner.prepare_goal_decision(goal_command([item]))
            assert result.status == "applied", result.error
            prepared = PreparedResponse.model_validate(result.result["prepared_response"])
            assert owner.adapter._prepared[prepared.prepare_id].capability
            assert len(llm.calls) == 1
            assert result.result["prepared_capability_choice"]["choice"] == result.result["decision"]["choice"]
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())


def test_failed_goal_selection_is_durable_and_does_not_store_private_output(tmp_path, monkeypatch):
    async def scenario():
        owner, _ = _host(tmp_path, monkeypatch, [])
        original = owner.chat.llm
        owner.chat.llm = llm = FakeLLM("PRIVATE_GOAL_FAILURE")
        request = goal_command()
        try:
            result = await owner.prepare_goal_decision(request)
            assert result.status == "failed" and result.error == "goal_invalid_json"
            assert (await owner.prepare_goal_decision(request)).idempotency_hit
            assert len(llm.calls) == 1 and not owner.adapter._prepared
            assert "PRIVATE_GOAL_FAILURE" not in (tmp_path / "cognitive.json").read_text(encoding="utf-8")
        finally:
            owner.chat.llm = original
            await owner.close()
    asyncio.run(scenario())
