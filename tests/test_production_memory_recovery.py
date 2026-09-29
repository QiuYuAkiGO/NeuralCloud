"""Process restart and owner switching recover saved learning without a model."""
import asyncio
from datetime import timedelta

import pytest

from harness.cognitive.qq_memory import snapshot_owner
from harness.cognitive.service import ProductionMemoryRecoveryError
from harness.memory.revision.ledger import install_revision
from harness.memory.revision.models import MemoryRevision
from harness.memory.revision.reducer import build_revision
from harness.memory.revision.sources import collect_source_window
from tests.test_basic_persona_continuity import _host
from tests.test_daily_revision_reducer import NOW, proposal, run
from tests.test_shared_memory import local


def saved_learning(host):
    record = host.shared.store.record_local(local(event_id="saved-learning-source",
        session_id=host.chat.active_session_id, occurred_at=NOW - timedelta(hours=12), text="我最近喜欢摄影")).record
    source = collect_source_window(host.shared.store, owner_id="misty", preset_id="misty",
        start=NOW - timedelta(days=1), end=NOW).records[0]
    revised = build_revision(snapshot_owner(host), run(sources=[source]), proposal([source], memories=[MemoryRevision(
        kind="episodic", subject="local:main", statement="记下近期摄影兴趣", evidence_ids=[source.source_id])]),
        host.chat.settings, now=NOW)
    host.chat.library.save(revised)
    return record, revised


def test_new_host_recovers_saved_preset_before_either_worker_without_relearning(tmp_path, monkeypatch):
    async def scenario():
        first, model = _host(tmp_path, monkeypatch, [])
        first.chat.settings.production_memory_enabled = True
        record, saved = saved_learning(first)
        episode_id = next(event.event_id for event in saved.episodic if event.event_type == "daily_revision")
        def unavailable(preset=None):
            raise OSError("synthetic SQL publication interruption")
        monkeypatch.setattr(first.shared, "sync_production", unavailable)
        with pytest.raises(OSError):
            install_revision(first, saved)
        assert first.adapter._daily_revision_publication_pending
        assert first.chat.library.load("misty").daily_revision_state["receipts"]["r1"]["status"] == "applied"
        assert not first.shared.production_store.validate_references([episode_id], audience="local:main", now=NOW)
        await first.close()

        restarted, restarted_model = _host(tmp_path, monkeypatch, [])
        restarted.chat.settings.production_memory_enabled = True
        restarted.adapter.production_meter = object()
        calls, started = [], []
        publish = restarted.shared.sync_production
        def observe_publication(preset=None):
            if preset is not None:
                calls.append(preset.preset_id)
            return publish(preset)
        monkeypatch.setattr(restarted.shared, "sync_production", observe_publication)
        async def worker_start(name):
            assert restarted.shared.production_store.validate_references([record.record_id, episode_id], audience="local:main", now=NOW)
            assert restarted.production_memory_recovery_status()["status"] == "ready"
            started.append(name)
        monkeypatch.setattr(restarted.qq_memory, "start", lambda: worker_start("qq"))
        monkeypatch.setattr(restarted.daily_revision, "start", lambda: worker_start("daily"))
        try:
            await restarted.start()
            assert calls == ["misty"] and started == ["qq", "daily"]
            assert not await restarted.recover_production_memory()
            await restarted.start()
            assert calls == ["misty"]
            assert not model.chat_calls and not restarted_model.chat_calls
        finally:
            await restarted.close()
    asyncio.run(scenario())


def test_startup_failure_keeps_pending_blocks_workers_and_can_retry(tmp_path, monkeypatch):
    async def scenario():
        host, model = _host(tmp_path, monkeypatch, [])
        host.chat.settings.production_memory_enabled = True
        host.adapter.production_meter = object()
        publish = host.shared.sync_production
        async def forbidden_start():
            pytest.fail("worker started before durable memory recovery")
        monkeypatch.setattr(host.qq_memory, "start", forbidden_start)
        monkeypatch.setattr(host.daily_revision, "start", forbidden_start)
        def unavailable(preset=None):
            raise OSError("synthetic disk failure")
        monkeypatch.setattr(host.shared, "sync_production", unavailable)
        try:
            with pytest.raises(ProductionMemoryRecoveryError, match="recovery_failed:OSError"):
                await host.start()
            assert host.adapter._qq_memory_publication_pending and host.adapter._daily_revision_publication_pending
            assert host.production_memory_recovery_status()["status"] == "failed"
            assert (await host.health()).detail["production_memory_recovery"]["error_type"] == "OSError"
            monkeypatch.setattr(host.shared, "sync_production", publish)
            # Recovery can be retried independently before restarting workers.
            assert await host.recover_production_memory()
            assert not host.adapter._qq_memory_publication_pending and not host.adapter._daily_revision_publication_pending
            assert host.production_memory_recovery_status()["status"] == "ready"
            assert not await host.recover_production_memory()
            assert not model.chat_calls
        finally:
            await host.close()
    asyncio.run(scenario())


def test_preset_switch_recovers_own_projection_once_per_loaded_owner(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
        host, _ = _host(tmp_path, monkeypatch, [])
        host.chat.settings.production_memory_enabled = True
        host.adapter.production_meter = object()
        publish = host.shared.sync_production
        calls = []
        def observe_publication(preset=None):
            if preset is not None:
                calls.append((preset.preset_id, host.shared.production_store.path))
            return publish(preset)
        monkeypatch.setattr(host.shared, "sync_production", observe_publication)
        try:
            assert await host.recover_production_memory()
            await host.control("presets.new", {"from_id": "misty"})
            created = host.chat.active_preset_id
            assert created != "misty" and host.chat.settings.production_memory_enabled
            assert [item[0] for item in calls] == ["misty", created]
            assert calls[0][1] != calls[1][1]
            assert host.shared.production_store.preset_id == created
            await host.control("presets.open", {"preset_id": created})
            assert len(calls) == 2
            await host.control("presets.apply", {"preset_id": "misty"})
            assert [item[0] for item in calls] == ["misty", created, "misty"]
            assert calls[0][1] == calls[2][1]
            assert not await host.recover_production_memory()
        finally:
            await host.close()
    asyncio.run(scenario())


def test_unactivated_legacy_host_does_not_publish_production_projection(tmp_path, monkeypatch):
    async def scenario():
        host, _ = _host(tmp_path, monkeypatch, [])
        monkeypatch.setattr(host.shared, "sync_production", lambda *args: pytest.fail("legacy production recovery"))
        try:
            assert not await host.recover_production_memory()
            assert not host.production_memory_recovery_status()["enabled"]
        finally:
            await host.close()
    asyncio.run(scenario())


def test_explicit_research_manifest_cannot_inherit_production_memory_mode(tmp_path, monkeypatch):
    from pathlib import Path
    from harness.cognitive.service import CognitiveServiceHost

    async def scenario():
        monkeypatch.setenv("NC_PRODUCTION_MEMORY_ENABLED", "true")
        previous, _ = _host(tmp_path, monkeypatch, [])
        try:
            assert previous.chat.settings.production_memory_enabled
        finally:
            await previous.close()
        monkeypatch.setenv("NC_COGNITIVE_MANIFEST_PATH", str(Path("presets/runtime/shared-misty-v1.json").resolve()))
        research = CognitiveServiceHost(config_path=tmp_path / "gui.json", presets_dir=tmp_path / "presets",
                                        state_path=tmp_path / "cognitive.json")
        try:
            assert not research.chat.settings.production_memory_enabled
            assert not await research.recover_production_memory()
        finally:
            await research.close()
    asyncio.run(scenario())
