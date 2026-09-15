"""The original command deadline also bounds new Board memory transactions."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import time

import pytest

from harness.embodiment.providers.board import BoardClient
from harness.memory.shared import SharedReadScope
from harness.runtime.models import utc_now
from tests.test_autonomous_capabilities import rig


@pytest.fixture(autouse=True)
def isolated_owner(monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    monkeypatch.setenv("NC_ISOLATED_DEVELOPMENT", "1")


@pytest.mark.parametrize("delay", ["read_timeout", "late_read", "close", "worker"])
def test_expired_commit_cannot_start_memory_but_receipt_recovery_can(tmp_path, monkeypatch, delay):
    async def scenario():
        answers = [{"kind": "act", "offer_id": "board:expression.board.append.v1"},
            {"action": "write", "title": "", "text": "A verified synthetic entry with delayed memory readback."}]
        async with rig(tmp_path, monkeypatch, answers) as (sup, host, _, llm, event, requests, _):
            await sup.dispatch(event)
            board_event = await sup.queue.get(timeout=0)
            original_evidence = BoardClient.evidence
            original_close = BoardClient.close
            original_to_thread = asyncio.to_thread
            original_command = sup.board._command
            reads = []
            delayed = []

            async def slow_independent_read(client, request):
                entry = await original_evidence(client, request)
                reads.append(request)
                if len(reads) == 2 and delay in {"read_timeout", "late_read"}:
                    # First read is the provider's own append verification.
                    delayed.append(delay)
                    try:
                        await asyncio.sleep(0.2)
                    except asyncio.CancelledError:
                        if delay != "late_read":
                            raise
                return entry

            async def slow_close(client):
                if delay == "close" and not delayed:
                    delayed.append(delay)
                    await asyncio.sleep(0.2)
                await original_close(client)

            async def queued_worker(func, /, *args, **kwargs):
                if delay == "worker" and func.__name__ == "_commit_before_deadline" and not delayed:
                    delayed.append(delay)
                    def delayed_start():
                        time.sleep(0.2)
                        return func(*args, **kwargs)
                    return await original_to_thread(delayed_start)
                return await original_to_thread(func, *args, **kwargs)

            async def short_commit(event, command_type, payload, deadline=None):
                if command_type == "CommitBoardEntry":
                    deadline = min(deadline, utc_now() + timedelta(seconds=0.1))
                return await original_command(event, command_type, payload, deadline)

            monkeypatch.setattr(BoardClient, "evidence", slow_independent_read)
            monkeypatch.setattr(BoardClient, "close", slow_close)
            monkeypatch.setattr(asyncio, "to_thread", queued_worker)
            monkeypatch.setattr(sup.board, "_command", short_commit)
            result = await sup.dispatch(board_event)
            assert result.detail["status"] == "written_pending_memory"
            await asyncio.sleep(0.25)  # The shielded command has finished, including a late transport response.
            assert not host.adapter.command_tasks
            assert delayed == [delay]
            assert not host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records
            assert requests.count(("POST", "/v1/board/append")) == 1
            assert len(llm.chat_calls) == 2

            monkeypatch.setattr(sup.board, "_command", original_command)
            row = sup.board.turns.get(result.detail["entry_id"])
            sup.board._recovery_next[row["entry_id"]] = 0
            await sup.board.heartbeat()
            recovery = await sup.queue.get(timeout=0)
            recovered = await sup.dispatch(recovery)
            assert recovered.detail["status"] == "committed"
            assert len(host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",))).records) == 1
            assert requests.count(("POST", "/v1/board/append")) == 1
            assert len(llm.chat_calls) == 2
    asyncio.run(scenario())
