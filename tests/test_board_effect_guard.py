"""Cancellation after provider health refresh must still prevent the write."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from harness.embodiment.providers.board import BOARD_APPEND
from harness.memory.shared import SharedReadScope
from harness.runtime.models import utc_now
from tests.test_autonomous_capabilities import rig


@pytest.fixture(autouse=True)
def isolated_owner(monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "1")


@pytest.mark.parametrize("change", ["user", "pause", "owner", "disabled", "deadline"])
def test_change_after_provider_guard_prevents_append_reconcile_and_memory(tmp_path, monkeypatch, change):
    async def scenario():
        answers = [{"kind": "act", "offer_id": "board:expression.board.append.v1"},
            {"action": "write", "title": "", "text": "A synthetic candidate that must remain unwritten."}]
        async with rig(tmp_path, monkeypatch, answers) as (sup, host, _, llm, event, requests, _):
            first = await sup.dispatch(event)
            assert first.detail["status"] == "pending"
            board_event = await sup.queue.get(timeout=0)
            provider = sup.board.provider
            original_guard = provider._guard
            post_health = []

            async def changing_guard(request, capability):
                await original_guard(request, capability)
                if capability != BOARD_APPEND or post_health:
                    return
                post_health.append(capability)
                if change == "user":
                    await sup.submit_user_message("Please stop before writing.")
                elif change == "pause":
                    sup.board.turns.pause(True)
                elif change == "deadline":
                    row = sup.capabilities.calls.get(event.payload["opportunity_id"])
                    row["deadline_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
                    sup.capabilities.calls.put(row)
                else:
                    configuration = sup.capabilities.settings.model_dump()
                    if change == "owner":
                        configuration["owners"]["board"] = "off"
                    else:
                        configuration["enabled"] = False
                    sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities": configuration})

            monkeypatch.setattr(provider, "_guard", changing_guard)
            result = await sup.dispatch(board_event)
            assert post_health == [BOARD_APPEND]
            assert len(llm.chat_calls) == 2  # Both decisions finished before cancellation.
            assert result.detail["status"] == "superseded"
            assert requests.count(("POST", "/v1/board/append")) == 0
            assert requests.count(("POST", "/v1/board/reconcile")) == 0
            assert provider._uncertain == {}
            assert host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records == ()
            assert host.chat.memory.stores.episodic.list_all() == []
            assert not any(host.chat.pipeline.session_store.snapshot().values())
            await sup.capabilities.reconcile()
            assert sup.capabilities.calls.get(event.payload["opportunity_id"])["status"] == "superseded"
            while await sup.queue.get(timeout=0) is not None:
                pass
    asyncio.run(scenario())
