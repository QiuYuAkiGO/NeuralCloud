"""Production Cognitive integration with frozen proposals and no external calls."""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
import threading
from zoneinfo import ZoneInfo

import pytest

from harness.memory.revision.config import DailyRevisionConfig
from harness.memory.revision.models import CleanedExperience, MemoryRevision, RevisionProposal
from harness.memory.shared import SharedLocalEvent
from tests.test_basic_persona_continuity import _host


class Proposer:
    def __init__(self, *, wait=False):
        self.calls = 0
        self.started = threading.Event()
        self.release = threading.Event()
        if not wait:
            self.release.set()

    def propose(self, run_id, sources, context):
        self.calls += 1
        self.started.set()
        assert self.release.wait(10), "test must release provider"
        return RevisionProposal(cleaned=[CleanedExperience(source_ids=[source.source_id],
            disposition="keep", summary=source.text, evidence_kind="speaker_claim") for source in sources],
            memories=[MemoryRevision(kind="episodic", subject=source.author_principal,
                statement=source.text, evidence_ids=[source.source_id]) for source in sources])


def setup_host(tmp_path, monkeypatch, *, wait=False):
    monkeypatch.delenv("NC_DAILY_REVISION_CONFIG", raising=False)
    host, _ = _host(tmp_path, monkeypatch, ["好的。"])
    key = tmp_path / "revision.key"
    key.write_text("synthetic-revision-fixture", encoding="utf-8")
    key.chmod(0o600)
    host.daily_revision.config = DailyRevisionConfig(api_key_file=key)
    host.daily_revision.provider = Proposer(wait=wait)
    moment = datetime.now(UTC) - timedelta(days=2)
    host.shared.store.record_local(SharedLocalEvent(event_id="test-daily-source", session_id="sess_misty",
        author_principal="local:user", role="user", text="我完成了摄影展的布置。", occurred_at=moment,
        evidence_refs=("local-chat:sess_misty:test-daily-source:user",)))
    return host, moment.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()


def test_default_missing_config_is_disabled_without_using_online_model(tmp_path, monkeypatch):
    async def run():
        monkeypatch.delenv("NC_DAILY_REVISION_CONFIG", raising=False)
        host, _ = _host(tmp_path, monkeypatch, ["好的。"])
        try:
            state = await host.control("daily_revision.status")
            assert state["configured"] is False
            assert state["config"]["enabled"] is False
            assert "api_key_file" not in state["config"]
            with pytest.raises(ValueError, match="provider_not_configured"):
                await host.control("daily_revision.run")
        finally:
            await host.close()
    asyncio.run(run())


def test_run_returns_before_model_and_pause_drains_registered_work(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch, wait=True)
        worker = host.daily_revision
        try:
            accepted = await asyncio.wait_for(host.control("daily_revision.run", {"day": day}), timeout=2)
            assert accepted["run_ids"]
            assert await asyncio.to_thread(worker.provider.started.wait, 2)
            # Control remains responsive during a blocked provider request.
            status = await asyncio.wait_for(host.control("daily_revision.status"), timeout=.5)
            assert status["processing"]
            assert worker._work_task in host.research_tasks
            assert not await host.pause_maintenance(timeout=.01)
            assert not worker._receipts()
            assert host.owner_lease._file is not None
            worker.provider.release.set()
            await worker._work_task
            assert not worker._receipts()
            assert worker.queue.get(accepted["run_ids"][0])["status"] == "retry"
            assert await host.pause_maintenance(timeout=1)
        finally:
            worker.provider.release.set()
            await host.close()
    asyncio.run(run())


def test_save_failure_reuses_frozen_proposal_on_retry(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch)
        worker = host.daily_revision
        try:
            run_id = (await worker.enqueue(day=day))["run_ids"][0]
            before = host.chat.memory.stores.episodic.list_all()
            with monkeypatch.context() as patch:
                patch.setattr(host.chat.library, "save", lambda _: (_ for _ in ()).throw(OSError("disk")))
                await worker.tick()
            assert worker.last_error == "OSError"
            assert not worker._receipts()
            assert host.chat.memory.stores.episodic.list_all() == before
            worker.queue.retry(run_id)
            await worker.tick()
            assert worker.last_error is None, worker.last_error
            assert worker.provider.calls == 1
            assert worker._receipts()[run_id]["status"] == "applied"
            assert host.chat.library.load("misty").daily_revision_state["receipts"][run_id]["status"] == "applied"
        finally:
            await host.close()
    asyncio.run(run())


def test_ack_failure_recovers_from_preset_without_reapplying(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch)
        worker = host.daily_revision
        try:
            run_id = (await worker.enqueue(day=day))["run_ids"][0]
            with monkeypatch.context() as patch:
                patch.setattr(worker.queue, "finish", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("ack")))
                await worker.tick()
            assert worker._receipts()[run_id]["status"] == "applied"
            committed = host.chat.library.load("misty").daily_revision_state
            worker.queue.retry(run_id)
            await worker.tick()
            assert worker.provider.calls == 1
            assert host.chat.library.load("misty").daily_revision_state == committed
            assert worker.queue.get(run_id)["status"] == "applied"
        finally:
            await host.close()
    asyncio.run(run())


def test_dry_run_stays_inspectable_without_preset_commit(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch)
        worker = host.daily_revision
        try:
            before = host.chat.library.load("misty").model_dump()
            run_id = (await worker.enqueue(day=day, dry_run=True))["run_ids"][0]
            await worker.tick()
            assert worker.last_error is None, worker.last_error
            assert not worker._receipts()
            assert host.chat.library.load("misty").model_dump() == before
            preview = worker.diff(run_id)
            assert preview["status"] == "dry_run"
            assert preview["proposal"]["memories"] and preview["evidence"]
        finally:
            await host.close()
    asyncio.run(run())


def test_cancelled_close_keeps_owner_lease_until_provider_has_finished(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch, wait=True)
        worker = host.daily_revision
        try:
            await worker.run_now(day=day)
            assert await asyncio.to_thread(worker.provider.started.wait, 2)
            closing = asyncio.create_task(host.close())
            await asyncio.sleep(.01)
            closing.cancel()
            with pytest.raises(asyncio.CancelledError):
                await closing
            assert host.owner_lease._file is not None
            assert not host._close_task.done()
            worker.provider.release.set()
            await host.close()
            assert host.owner_lease._file is None
            assert not worker._receipts()
        finally:
            worker.provider.release.set()
            await host.close()
    asyncio.run(run())


def test_adapter_publication_failure_preserves_commit_and_recovers_once(tmp_path, monkeypatch):
    async def run():
        host, day = setup_host(tmp_path, monkeypatch)
        worker = host.daily_revision
        try:
            run_id = (await worker.enqueue(day=day))["run_ids"][0]
            revision = host.adapter._revision
            with monkeypatch.context() as patch:
                patch.setattr(host.adapter, "_persist_state", lambda: (_ for _ in ()).throw(OSError("snapshot")))
                await worker.tick()
            assert worker._receipts()[run_id]["status"] == "applied"
            assert host.adapter._daily_revision_publication_pending
            assert host.adapter._revision == revision + 1
            await worker.tick()
            assert not host.adapter._daily_revision_publication_pending
            assert host.adapter._revision == revision + 1
            assert worker.provider.calls == 1
            assert worker.queue.get(run_id)["status"] == "applied"
        finally:
            await host.close()
    asyncio.run(run())


def test_rollback_persists_tombstone_and_keeps_new_online_memory(tmp_path, monkeypatch):
    from harness.core.models import CandidateMemory

    async def run():
        host, day = setup_host(tmp_path, monkeypatch)
        worker = host.daily_revision
        try:
            run_id = (await worker.enqueue(day=day))["run_ids"][0]
            await worker.tick()
            assert worker.last_error is None, worker.last_error
            later = CandidateMemory(candidate_id="later-online", claim="后来的有效记录", source="event-new",
                evidence=["event-new"], confidence=.2, created_at=datetime.now(UTC), updated_at=datetime.now(UTC))
            host.chat.memory.stores.candidates.insert(later)
            result = await worker.rollback(run_id)
            assert result["status"] == "rolled_back"
            assert host.chat.memory.stores.candidates.get("later-online") == later
            assert worker.queue.get(run_id)["tombstoned"]
            worker._recover_queue()
            assert worker.queue.claim() is None
            assert host.chat.library.load("misty").daily_revision_state["receipts"][run_id]["status"] == "rolled_back"
        finally:
            await host.close()
    asyncio.run(run())
