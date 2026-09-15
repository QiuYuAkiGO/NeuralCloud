from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from harness.cognitive.qq_memory import QQMemoryControl, memory_state
from harness.memory.qq_assimilation import QQMemoryProposal, QQEpisodeProposal
from harness.qq.memory import QQVisualMemoryStore
from harness.runtime.adapters import PipelineCognitiveAdapter
from tests.test_basic_persona_continuity import _host, _prepare, _commit
from tests.test_qq_protocol_memory import protocol_profile, protocol_observation, NOW


class Proposer:
    def __init__(self):
        self.calls = 0

    def propose(self, job, **kwargs):
        self.calls += 1
        return QQMemoryProposal(episodes=(QQEpisodeProposal(
            source_ids=tuple(source.source_id for source in job.sources),
            summary="成员谈到摄影", insight="对摄影有些兴趣，但仅为初步理解。",
            importance=.8, valence=.1),))


def setup_host(tmp_path, monkeypatch):
    host, main = _host(tmp_path, monkeypatch, ["照片很好看。"])
    source = QQVisualMemoryStore(tmp_path / "qq.sqlite3")
    _, profile = protocol_profile(source)
    source.record_protocol_observation(protocol_observation(profile,
        text="我今天拍了窗边的光。", observed_at=NOW - timedelta(minutes=3), image_analysis=[]))
    host.shared.qq_settings = SimpleNamespace(memory_db=source.path,
        allowed_group_ids=["10001"], self_member_id="29999")
    host.qq_memory.control = QQMemoryControl(enabled=True)
    host.qq_memory.proposer = Proposer()
    return host, main


def test_worker_commits_then_acknowledges_and_replay_does_not_call_model(tmp_path, monkeypatch):
    async def run():
        host, _ = setup_host(tmp_path, monkeypatch)
        try:
            await host.qq_memory.tick()
            assert host.qq_memory.last_error is None, host.qq_memory.last_error
            state = memory_state(host.chat.memory.qq_memory_state)
            assert len(state["receipts"]) == 1
            assert host.qq_memory.queue.status()["applied"] == 1
            stored = host.chat.library.load("misty")
            assert stored.qq_memory_state == host.chat.memory.qq_memory_state
            await host.qq_memory.tick()
            assert host.qq_memory.proposer.calls == 1
            assert len([e for e in host.chat.memory.stores.episodic.list_all() if e.channel == "qq_group"]) == 1
        finally:
            await host.close()
    asyncio.run(run())


def test_failed_preset_save_never_installs_memory_or_marks_applied(tmp_path, monkeypatch):
    async def run():
        host, _ = setup_host(tmp_path, monkeypatch)
        try:
            before = list(host.chat.memory.stores.episodic.list_all())
            with monkeypatch.context() as patch:
                patch.setattr(host.chat.library, "save", lambda _: (_ for _ in ()).throw(OSError("disk")))
                await host.qq_memory.tick()
            assert host.qq_memory.last_error == "OSError"
            assert host.chat.memory.stores.episodic.list_all() == before
            assert not memory_state(host.chat.memory.qq_memory_state)["receipts"]
            assert host.qq_memory.queue.status()["retry"] == 1
        finally:
            await host.close()
    asyncio.run(run())


def test_memory_revision_is_durable_before_publication_and_survives_restart(tmp_path, monkeypatch):
    async def run():
        host, _ = setup_host(tmp_path, monkeypatch)
        try:
            before = await host.adapter.get_snapshot()
            publish = host.adapter._publish_snapshot
            observed = []

            def verify_durable_then_publish():
                durable = json.loads(host.adapter.state_path.read_text(encoding="utf-8"))
                assert durable["revision"] == host.adapter._revision
                assert durable["component_revisions"] == host.adapter._component_revisions
                publish()
                observed.append(host.adapter._committed_snapshot.revision)

            with monkeypatch.context() as patch:
                patch.setattr(host.adapter, "_publish_snapshot", verify_durable_then_publish)
                await host.qq_memory.tick()
            assert host.qq_memory.last_error is None
            snapshot = await host.adapter.get_snapshot()
            assert observed == [before.revision + 1]
            # Construct before graceful close, which would otherwise persist
            # the missing revision and hide this crash-only regression.
            restored = PipelineCognitiveAdapter(host.chat.pipeline, state_path=host.adapter.state_path)
            assert await restored.get_snapshot() == snapshot
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("reset_worker_queue", [False, True], ids=["worker", "import-style"])
def test_adapter_save_failure_keeps_authoritative_memory_and_recovers_same_revision_next_tick(
        tmp_path, monkeypatch, reset_worker_queue):
    async def run():
        host, _ = setup_host(tmp_path, monkeypatch)
        try:
            host.adapter._persist_state()
            before = await host.adapter.get_snapshot()
            previous_disk = host.adapter.state_path.read_bytes()
            replace = os.replace

            def fail_adapter_replace(source, destination):
                if Path(destination) == host.adapter.state_path:
                    raise OSError("adapter_state_replace_failed")
                return replace(source, destination)

            with monkeypatch.context() as patch:
                patch.setattr("harness.runtime.adapters.os.replace", fail_adapter_replace)
                await host.qq_memory.tick()
            assert host.qq_memory.last_error == "OSError"
            assert host.adapter._qq_memory_publication_pending
            assert host.adapter.state_path.read_bytes() == previous_disk
            assert await host.adapter.get_snapshot() == before
            revision = host.adapter._revision
            assert revision == before.revision + 1
            committed = host.chat.library.load("misty")
            assert len(committed.qq_memory_state["receipts"]) == 1
            assert host.chat.memory.qq_memory_state == committed.qq_memory_state
            events = list(host.chat.memory.stores.episodic.list_all())
            assert events == committed.episodic
            queue = host.qq_memory.queue
            assert queue.status()["retry"] == 1
            # Abrupt restart can only reload the older revision that was
            # actually published; the saved memory/receipt is already durable.
            restored_before_retry = PipelineCognitiveAdapter(host.chat.pipeline,
                state_path=host.adapter.state_path)
            assert await restored_before_retry.get_snapshot() == before
            host._flush_committed_memory()
            assert host.chat.library.load("misty").qq_memory_state == committed.qq_memory_state

            # Recovery is independent of paused features and does not require
            # a worker-owned queue (the importer can fail at this same point).
            host.qq_memory.control = QQMemoryControl(enabled=False, recall_enabled=False)
            host.adapter._maintenance_paused = True
            if reset_worker_queue:
                host.qq_memory.queue = None
            await host.qq_memory.tick()
            assert not host.adapter._qq_memory_publication_pending
            assert host.qq_memory.last_error is None
            assert host.adapter._revision == revision
            snapshot = await host.adapter.get_snapshot()
            assert snapshot.revision == revision
            assert queue.status()["applied"] == 1
            assert queue.status()["retry"] == 0
            assert host.qq_memory.proposer.calls == 1
            assert host.chat.memory.stores.episodic.list_all() == events
            assert host.chat.memory.qq_memory_state == committed.qq_memory_state
            restored_after_retry = PipelineCognitiveAdapter(host.chat.pipeline,
                state_path=host.adapter.state_path)
            assert await restored_after_retry.get_snapshot() == snapshot
            await host.qq_memory.tick()
            assert await host.adapter.get_snapshot() == snapshot
            assert host.qq_memory.proposer.calls == 1
        finally:
            await host.close()
    asyncio.run(run())


def test_acknowledgement_crash_recovers_from_committed_preset(tmp_path, monkeypatch):
    async def run():
        host, _ = setup_host(tmp_path, monkeypatch)
        try:
            from harness.memory.qq_assimilation import QQAssimilationQueue
            with monkeypatch.context() as patch:
                patch.setattr(QQAssimilationQueue, "acknowledge", lambda *a, **k: (_ for _ in ()).throw(OSError("ack")))
                await host.qq_memory.tick()
            state = memory_state(host.chat.library.load("misty").qq_memory_state)
            assert len(state["receipts"]) == 1
            host.qq_memory.queue.recover(set(state["receipts"]))
            assert host.qq_memory.queue.status()["applied"] == 1
            assert host.qq_memory.proposer.calls == 1
        finally:
            await host.close()
    asyncio.run(run())


def test_model_work_does_not_hold_foreground_lock_and_local_commit_survives(tmp_path, monkeypatch):
    async def run():
        host, main = setup_host(tmp_path, monkeypatch)
        started, release = threading.Event(), threading.Event()
        delegate = host.qq_memory.proposer
        def blocked(job, **kwargs):
            started.set()
            assert release.wait(10)
            return delegate.propose(job, **kwargs)
        host.qq_memory.proposer = SimpleNamespace(propose=blocked)
        worker = asyncio.create_task(host.qq_memory.tick())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            await asyncio.wait_for(host.adapter._lock.acquire(), timeout=.2)
            host.adapter._lock.release()
            prepared = await _prepare(host, main, 1, "我今天拍了照片。")
            await _commit(host, 1, prepared)
            local_ids = {e.event_id for e in host.chat.memory.stores.episodic.list_all()}
            release.set()
            await worker
            assert host.qq_memory.last_error is None, host.qq_memory.last_error
            assert local_ids <= {e.event_id for e in host.chat.library.load("misty").episodic}
        finally:
            release.set()
            await worker
            await host.close()
    asyncio.run(run())


def test_control_switches_persist_without_enabling_send(tmp_path, monkeypatch):
    async def run():
        host, _ = setup_host(tmp_path, monkeypatch)
        try:
            result = await host.control("qq_memory.configure", {"enabled": False, "recall_enabled": True})
            assert not result["enabled"] and result["recall_enabled"]
            assert "send" not in result
            with pytest.raises(ValueError):
                await host.control("qq_memory.configure", {"group_id": "other"})
            assert host.qq_memory.control_path.exists()
        finally:
            await host.close()
    asyncio.run(run())
