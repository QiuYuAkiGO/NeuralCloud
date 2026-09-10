"""Slow production Host commands retain ownership through cancellation and flush."""

from __future__ import annotations

import asyncio
import json
import threading
from datetime import UTC, datetime

import pytest

from harness.contracts.decisions import DeliveryEvidence
from tests.test_basic_persona_continuity import _command, _host, _prepare


def _commit_command(prepared):
    evidence = DeliveryEvidence(
        prepare_id=prepared.prepare_id, delivered_at=datetime.now(UTC),
        channel="chat", success=True, delivered_segments=prepared.decision.segments,
        external_ref="test-receipt:slow-command",
    )
    return _command("CommitInteraction", 1, {
        "prepare_id": prepared.prepare_id, "evidence": evidence.model_dump(mode="json"),
    })


def test_cancelled_commit_keeps_lock_and_replays_without_duplicate_write(tmp_path, monkeypatch):
    host, model = _host(tmp_path, monkeypatch, ["我是小雾。", "我记得。"])
    started, release = threading.Event(), threading.Event()

    async def scenario():
        prepared = await _prepare(host, model, 1, "记住《雨后微光》。")
        before = await host.get_snapshot()
        record = host.chat.memory.record_event
        calls = []

        def slow_record(event, **kwargs):
            calls.append(event.event_id)
            if len(calls) == 1:
                started.set()
                assert release.wait(5)
            return record(event, **kwargs)

        monkeypatch.setattr(host.chat.memory, "record_event", slow_record)
        commit = _commit_command(prepared)
        caller = asyncio.create_task(host.commit_interaction(commit))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(caller, .02)
            assert caller.cancelled() and host.adapter._lock.locked()
            assert len(host.adapter.command_tasks) == 1
            assert (await asyncio.wait_for(host.health(), .1)).revision == 0
            assert await asyncio.wait_for(host.get_snapshot(), .1) == before
            assert not await host.pause_maintenance(timeout=.01)
            replay = asyncio.create_task(host.commit_interaction(commit))
            next_prepare = asyncio.create_task(host.prepare_response(_command(
                "PrepareResponse", 2, {"text": "名字是什么？"},
            )))
            await asyncio.sleep(.02)
            assert not replay.done() and not next_prepare.done()
            assert len(model.chat_calls) == 1
            release.set()
            result = await replay
            assert result.status == "idempotent_replay" and result.committed_revision == 1
            assert (await next_prepare).status == "applied"
            assert calls.count("evt_chat_1") == 1
            assert len(calls) == len(set(calls))
            assert (await host.get_snapshot()).revision == 1
            ledger = json.loads((tmp_path / "cognitive.json").read_text(encoding="utf-8"))
            assert ledger["revision"] == 1
            assert ledger["ledger"]["CommitInteraction\0CommitInteraction:1"]["status"] == "applied"
        finally:
            release.set()
            await host.close()

    asyncio.run(scenario())


def test_cancelled_prepare_keeps_worker_serialized_and_persists_replay(tmp_path, monkeypatch):
    host, _ = _host(tmp_path, monkeypatch, ["我是小雾。"])
    started, release = threading.Event(), threading.Event()
    prepare = host.chat.pipeline.prepare
    calls = []

    def slow_prepare(*args, **kwargs):
        calls.append(1)
        started.set()
        assert release.wait(5)
        return prepare(*args, **kwargs)

    monkeypatch.setattr(host.chat.pipeline, "prepare", slow_prepare)

    async def scenario():
        command = _command("PrepareResponse", 1, {"text": "你好。"})
        caller = asyncio.create_task(host.prepare_response(command))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(caller, .02)
            assert host.adapter._lock.locked()
            snapshot = await asyncio.wait_for(host.get_snapshot(), .1)
            assert snapshot.acknowledged_receive_seq == 0
            assert snapshot.payload["prepared_count"] == 0
            replay = asyncio.create_task(host.prepare_response(command))
            await asyncio.sleep(.02)
            assert not replay.done() and len(calls) == 1
            release.set()
            result = await replay
            assert result.status == "idempotent_replay" and len(calls) == 1
            assert not any(host.chat.pipeline.session_store.snapshot().values())
            assert (await host.get_snapshot()).payload["prepared_count"] == 1
            await host.close()
            restored, _ = _host(tmp_path, monkeypatch, [])
            try:
                assert (await restored.prepare_response(command)).result == result.result
            finally:
                await restored.close()
        finally:
            release.set()
            await host.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("slow_phase", ["preset", "ledger"])
def test_health_publishes_only_durable_revision_and_close_waits(tmp_path, monkeypatch, slow_phase):
    host, model = _host(tmp_path, monkeypatch, ["我记住雨后微光了。"])
    started, release = threading.Event(), threading.Event()

    async def scenario():
        prepared = await _prepare(host, model, 1, "照片叫《雨后微光》。")
        original = host.chat.flush_active if slow_phase == "preset" else host.adapter._persist_state
        blocked = False

        def slow_flush():
            nonlocal blocked
            if not blocked:
                blocked = True
                started.set()
                assert release.wait(5)
            return original()

        if slow_phase == "preset":
            monkeypatch.setattr(host.chat, "flush_active", slow_flush)
        else:
            monkeypatch.setattr(host.adapter, "_persist_state", slow_flush)
        caller = asyncio.create_task(host.commit_interaction(_commit_command(prepared)))
        try:
            assert await asyncio.to_thread(started.wait, 1)
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(caller, .02)
            assert (await asyncio.wait_for(host.health(), .1)).revision == 0
            assert (await asyncio.wait_for(host.get_snapshot(), .1)).revision == 0
            assert json.loads((tmp_path / "cognitive.json").read_text(encoding="utf-8"))["revision"] == 0
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(host.close(), .02)
            assert host.adapter._close_task is not None and not host.adapter._close_task.done()
            assert host.adapter.command_tasks and host.adapter._lock.locked()
            with pytest.raises(RuntimeError, match="closing"):
                await host.prepare_response(_command("PrepareResponse", 2, {"text": "再见。"}))
            release.set()
            await host.close()
            assert not host.adapter.command_tasks
            assert (await host.get_snapshot()).revision == 1
            restored, _ = _host(tmp_path, monkeypatch, [])
            try:
                assert (await restored.get_snapshot()).revision == 1
                turns = restored.chat.pipeline.session_store.recent(restored.chat.active_session_id)
                assert [turn.text for turn in turns] == ["照片叫《雨后微光》。", prepared.decision.text]
                assert restored.chat.memory.stores.episodic.get("evt_chat_1") is not None
                assert (await restored.commit_interaction(_commit_command(prepared))).status == "conflict"
                # Reconstructing evidence changes its ID/time, so only the exact
                # original request (covered above) qualifies as an idempotent replay.
            finally:
                await restored.close()
        finally:
            release.set()
            await host.close()

    asyncio.run(scenario())
