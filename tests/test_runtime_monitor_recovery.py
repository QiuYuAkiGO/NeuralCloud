"""Monitoring resumes its own atomic cursor without blocking business boot."""

import asyncio
import sqlite3
import threading

import pytest

from harness.runtime.monitor import MonitorRecorder, MonitorStore
from tests.test_runtime_recovery import open_journal


def recorder_for(root, journal):
    store = MonitorStore(root / "monitor.sqlite3")
    return MonitorRecorder(
        store, journal=journal, status_provider=lambda: {}, trace_dir=root / "traces", sample_seconds=60
    )


def test_monitor_cursor_is_atomic_with_event_projection(tmp_path, monkeypatch):
    journal, cache = open_journal(tmp_path)
    record = journal.append("fixture")
    store = MonitorStore(tmp_path / "monitor.sqlite3")
    cursor = dict(identity=record.checksum, seq=1, checksum=record.checksum)

    def fail(*args, **kwargs):
        raise sqlite3.OperationalError("injected projection failure")

    monkeypatch.setattr(store, "_project_run_locked", fail)
    with pytest.raises(sqlite3.OperationalError):
        store.append_journal(record, journal_cursor=cursor)
    assert store.journal_cursor() is None
    assert store.list_events(limit=10)["items"] == []
    monkeypatch.undo()
    store.append_journal(record, journal_cursor=cursor)
    assert store.journal_cursor() == cursor
    store.close()
    cache.close()


def test_restart_monitor_reads_only_identity_anchor_and_tail(tmp_path, monkeypatch):
    journal, cache = open_journal(tmp_path)
    for _ in range(40):
        journal.append("fixture")

    async def scenario():
        first = recorder_for(tmp_path, journal)
        await first.flush()
        await first.close()
        journal.append("after_restart")
        calls = []
        original = journal.records_after

        def track(sequence, **kwargs):
            calls.append(sequence)
            return original(sequence, **kwargs)

        monkeypatch.setattr(journal, "records_after", track)
        second = recorder_for(tmp_path, journal)
        await second.flush()
        assert calls == [0, 39, 40]
        assert second.recovery_status()["lag"] == 0
        assert len(second.store.list_events(limit=100)["items"]) == 41
        await second.close()

    asyncio.run(scenario())
    cache.close()


def test_start_does_not_wait_for_trace_backfill(tmp_path, monkeypatch):
    journal, cache = open_journal(tmp_path)
    recorder = recorder_for(tmp_path, journal)
    release = threading.Event()
    monkeypatch.setattr(recorder, "_backfill", lambda: release.wait(10))

    async def scenario():
        await asyncio.wait_for(recorder.start(), timeout=0.5)
        assert not release.is_set()
        release.set()
        await recorder.close()

    asyncio.run(scenario())
    cache.close()
