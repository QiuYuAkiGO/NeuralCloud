"""Operator latency bounds, shared refreshes, and authority invalidation."""
import asyncio
from datetime import timedelta, timezone
import json
import sqlite3
from threading import Event

import pytest

from harness.cognitive.ui_snapshot import UISnapshotCache
from harness.memory.shared import SharedMemoryStorageError
from harness.qq.attention import AttentionStore, attention_path
from harness.qq.direct_config import DirectQQSettings
from tests.test_shared_memory import local, person, scope, store  # noqa: F401
from tests.test_basic_persona_continuity import _host


def configure_policy(tmp_path, monkeypatch):
    paths = {name: tmp_path/name for name in ("token_file", "session_config_path", "status_file", "control_file",
        "image_cache_dir", "desktop_status_file", "shared_control_token_file")}
    settings = DirectQQSettings(enabled=True, self_member_id="29999",
        group_roster=[{"group_id": "100", "observe": True, "send": False}],
        state_db=tmp_path/"direct.sqlite3", evidence_dir=tmp_path/"evidence", **paths)
    config = tmp_path/"direct.json"
    config.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(config))
    return AttentionStore(attention_path(settings))


def close_group(policy):
    policy.set({"group_id": "100", "mode": "closed", "reason": "user override"},
        origin="user", action_id="close", expected_revision=None, allowed_groups={"100"})


def test_recent_ui_matches_archive_order_counts_and_validates_only_selection(store, monkeypatch):  # noqa: F811
    for i in range(170):
        # Mixed offsets and submillisecond differences must remain ordered.
        at = (local().occurred_at + timedelta(microseconds=i)).astimezone(timezone(timedelta(hours=i % 5)))
        store.record_local(local(event_id=f"event-{i}", occurred_at=at))
    store.record_local(local(event_id="hidden", visibility=("local:other",)))
    store.import_qq_context(person())
    expected = store.query(scope(), limit=50)
    decoded = []
    original = store._decode

    def decode(row):
        decoded.append(row["record_id"])
        return original(row)

    monkeypatch.setattr(store, "_decode", decode)
    actual = store.operator_recent()
    assert actual == expected
    assert len(decoded) == 50
    assert actual.visible_count == 172


def test_ui_selected_source_and_preset_validation_remain_strict(store):  # noqa: F811
    record = store.record_local(local()).record
    with sqlite3.connect(store.path) as db:
        row = json.loads(db.execute("SELECT record_json FROM shared_memory_entries").fetchone()[0])
        row["text"] = "tampered"
        db.execute("UPDATE shared_memory_entries SET record_json=? WHERE record_id=?", (json.dumps(row), record.record_id))
    with pytest.raises(SharedMemoryStorageError):
        store.operator_recent()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE shared_memory_identity SET preset_id='someone-else'")
    with pytest.raises(SharedMemoryStorageError, match="different preset"):
        store.operator_recent()


def test_closed_group_is_excluded_from_counts_and_decode_and_policy_race_is_rejected(store, tmp_path, monkeypatch):  # noqa: F811
    policy = configure_policy(tmp_path, monkeypatch)
    store.import_qq_context(person())
    original = store._decode
    calls = []

    def close_during_decode(row):
        if not calls:
            close_group(policy)
        calls.append(row["record_id"])
        return original(row)

    monkeypatch.setattr(store, "_decode", close_during_decode)
    with pytest.raises(ValueError, match="attention_policy_changed"):
        store.operator_recent()
    calls.clear()
    assert store.operator_recent().visible_count == 0
    assert not calls


def test_refresh_is_single_flight_and_client_cancellation_does_not_cancel_reader():
    async def run():
        cache = UISnapshotCache(cold_wait=.01)
        release, entered = Event(), Event()
        calls = []

        def slow_read():
            calls.append(1)
            entered.set()
            assert release.wait(2)
            return {"entries": ["result"]}

        try:
            requests = [asyncio.create_task(cache.get("same", slow_read)) for _ in range(20)]
            await asyncio.to_thread(entered.wait, 1)
            requests[0].cancel()
            results = await asyncio.gather(*requests, return_exceptions=True)
            assert len(calls) == 1
            assert all(v[0] == {} and v[1]["status"] == "loading" for v in results[1:])
            release.set()
            await cache._task
            data, meta = await cache.get("same", slow_read)
            assert data["entries"] == ["result"] and meta["status"] == "ready"
            data["entries"].clear()
            assert (await cache.get("same", slow_read))[0]["entries"] == ["result"]
        finally:
            release.set()
            await cache.close()
    asyncio.run(run())


def test_expiry_failure_and_key_change_never_serve_expired_or_other_owner_data():
    async def run():
        now = [10.0]
        cache = UISnapshotCache(clock=lambda: now[0], cold_wait=.01)
        release = Event()
        try:
            await cache.get("A", lambda: {"entries": ["A"]})
            await cache._task
            assert (await cache.get("A", lambda: {"entries": ["A"]}))[0]
            now[0] += 3
            def slow():
                assert release.wait(2)
                return {"entries": ["A-new"]}
            data, meta = await cache.get("A", slow)
            assert data and meta["status"] == "refreshing"
            now[0] += 20
            assert not (await cache.get("A", slow))[0]
            assert not (await cache.get("B", lambda: {"entries": ["B"]}))[0]
            release.set()
            await cache._task
            await cache.get("B", lambda: {"entries": ["B"]})
            await cache._task
            assert (await cache.get("B", lambda: {"entries": ["B"]}))[0]["entries"] == ["B"]
            now[0] += 3
            def fail():
                raise ValueError("private content must not be exposed")
            await cache.get("B", fail)
            await cache._task
            data, meta = await cache.get("B", fail)
            assert not data and meta["last_error_type"] == "ValueError"
            assert "private content" not in json.dumps(meta)
        finally:
            release.set()
            await cache.close()
    asyncio.run(run())


def test_host_warm_cache_obeys_persisted_closure_and_preset_switch(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["ok"])
        policy = configure_policy(tmp_path, monkeypatch)
        host.shared.store.import_qq_context(person())
        try:
            first = await host.ui_snapshot()
            assert first["shared_mind"]["memory_records"] == 2
            count = first["memory_snapshot"]["refresh_count"]
            assert (await host.ui_snapshot())["memory_snapshot"]["refresh_count"] == count
            close_group(AttentionStore(policy.path))  # An independent writer.
            closed = await host.ui_snapshot()
            assert not closed.get("shared_memory", {}).get("entries")
            assert closed["shared_mind"]["memory_records"] in (None, 0)
            host.chat.active_preset_id = "other-preset"
            switched = await host.ui_snapshot()
            assert not switched.get("shared_memory", {}).get("entries")
            assert switched["shared_mind"]["memory_records"] in (None, 0)
        finally:
            host.chat.active_preset_id = "misty"
            await host.close()
    asyncio.run(run())


def test_host_reports_memory_failure_without_losing_other_ui_state(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["ok"])
        def fail():
            raise SharedMemoryStorageError("private database detail")
        monkeypatch.setattr(host.shared, "ui", fail)
        try:
            result = await host.ui_snapshot()
            assert result["memory_snapshot"]["status"] == "unavailable"
            assert result["memory_snapshot"]["last_error_type"] == "SharedMemoryStorageError"
            assert result["shared_mind"]["memory_records"] is None
            assert "session" in result and "qq_memory" in result and "daily_revision" in result
            assert "private database detail" not in json.dumps(result)
        finally:
            await host.close()
    asyncio.run(run())


def test_slow_catalog_does_not_block_snapshots_or_multiply_reads(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["ok"])
        release, entered = Event(), Event()
        calls = []
        original = host.chat.library.list_summaries

        def catalog():
            calls.append(1)
            entered.set()
            assert release.wait(3)
            return original()

        monkeypatch.setattr(host.chat.library, "list_summaries", catalog)
        try:
            task = asyncio.create_task(host.ui_snapshot())
            assert await asyncio.to_thread(entered.wait, 1)
            first = await asyncio.wait_for(task, 1)
            assert first["preset_snapshot"]["status"] == "loading"
            assert "persona" in first and "shared_memory" in first
            responses = await asyncio.gather(*(host.ui_snapshot() for _ in range(8)))
            assert len(calls) == 1
            assert all(r["preset_snapshot"]["status"] == "loading" for r in responses)
            release.set()
            await host._ui_catalog_cache._task
            ready = await host.ui_snapshot()
            assert ready["preset_snapshot"]["status"] == "ready" and ready["presets"]["items"]
        finally:
            release.set()
            await host.close()
    asyncio.run(run())


def test_catalog_cache_detects_external_replace_and_never_caches_open_flags(tmp_path, monkeypatch):
    from gui.presets import PresetLibrary, save_preset_file
    library = PresetLibrary(working_dir=tmp_path/"presets")
    preset = library.ensure_working("misty")
    expected = library.summarize("misty")
    load = library.load
    monkeypatch.setattr(library, "load", lambda *args: pytest.fail("unchanged summary reloaded archive"))
    active = library.summarize("misty", open_ids=["misty"], active_id="misty")
    assert active.active and active.open
    active.name = "mutating caller copy"
    assert library.summarize("misty") == expected
    monkeypatch.setattr(library, "load", load)
    save_preset_file(preset.model_copy(update={"name": "new name"}), library.working_path("misty"))
    assert library.summarize("misty").name == "new name"


def test_qq_aggregate_status_does_not_copy_full_history(tmp_path, monkeypatch):
    async def run():
        host, _ = _host(tmp_path, monkeypatch, ["ok"])
        try:
            before = host.qq_memory.status()
            def no_copy(*args, **kwargs):
                raise AssertionError("aggregate status copied historical bodies")
            with monkeypatch.context() as m:
                m.setattr("harness.cognitive.qq_memory.deepcopy", no_copy)
                assert host.qq_memory.status() == before
        finally:
            await host.close()
    asyncio.run(run())
