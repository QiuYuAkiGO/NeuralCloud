"""Durability, authority handoff and hot-write bounds; no real model calls."""
from __future__ import annotations

import asyncio
from contextlib import closing
import json
import os
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

from gui.presets import load_builtin
from harness.config import Settings
from harness.llm.client import MockLLM, RoutingLLMClient
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.pipeline.session import SessionStore
from harness.qq.telemetry import QQTurnTrace, qq_trace_scope
from harness.runtime.adapters import PipelineCognitiveAdapter
from harness.runtime.cognitive_store import CognitiveStateStore, DELETED, TrackedDict
from tests.test_basic_persona_continuity import _command
from tests.test_emotion_service import FakeAux, message, request


def _owner(tmp_path, *, enabled=True, emotion="off"):
    settings = Settings(cognitive_incremental_persistence_enabled=enabled, emotion_mode=emotion,
        parser_llm_enabled=False, planner_llm_enabled=False, tier_llm_enabled=False,
        validator_enabled=False, continuation_enabled=False, call_log_enabled=False,
        aux_llm_model="qwen-test", aux_llm_base_url="http://qwen.invalid/v1")
    main, aux = MockLLM(chat_script=["这是本轮答复。[END]"]), FakeAux()
    llm = RoutingLLMClient(main, aux)
    memory = build_inmemory_service(settings=settings, llm=llm)
    memory.bootstrap_persona(load_builtin("misty").persona)
    pipeline = InferencePipeline(memory, settings=settings, llm=llm)
    return PipelineCognitiveAdapter(pipeline, state_path=tmp_path / "cognition.json"), main, aux


def test_json_migration_and_disable_always_restore_latest_sqlite(tmp_path):
    legacy, _, _ = _owner(tmp_path, enabled=False)
    legacy._revision = 3
    legacy.pipeline.session_store.append("s", "user", "旧检查点")
    legacy._persist_state()
    migrated, _, _ = _owner(tmp_path)
    assert migrated._revision == 3
    migrated._revision = 4
    migrated.pipeline.session_store.append("s", "character", "最新已提交")
    migrated._persist_state()
    assert json.loads(migrated.state_path.read_text(encoding="utf-8"))["revision"] == 3
    disabled, _, _ = _owner(tmp_path, enabled=False)
    assert disabled._revision == 4
    assert disabled.pipeline.session_store.recent("s")[-1].text == "最新已提交"
    assert json.loads(disabled.state_path.read_text(encoding="utf-8"))["revision"] == 4
    disabled._revision = 5
    disabled._persist_state()
    enabled, _, _ = _owner(tmp_path)
    assert enabled._revision == 5  # A stale sidecar cannot override newer JSON.
    assert enabled._state_store.authority() == "sqlite"


def test_disabled_export_failure_preserves_sqlite_authority(tmp_path, monkeypatch):
    owner, _, _ = _owner(tmp_path)
    owner._revision = 9
    owner._persist_state()
    def fail(*args, **kwargs):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr("harness.runtime.cognitive_store.write_checkpoint", fail)
    with pytest.raises(OSError):
        _owner(tmp_path, enabled=False)
    assert owner._state_store.authority() == "sqlite"
    assert _owner(tmp_path)[0]._revision == 9


def _checkpoint_os(monkeypatch, directory_fsync, *, name="posix"):
    """Exercise POSIX checkpoint ordering on Windows without patching global os."""
    from harness.runtime import cognitive_store

    descriptor = object()
    closed = []
    directory_flag = getattr(os, "O_DIRECTORY", 0x10000)

    def open_directory(path, flags):
        assert path.is_dir()
        assert flags == os.O_RDONLY | directory_flag
        return descriptor

    def fsync(fd):
        if fd is descriptor:
            directory_fsync()
        else:
            os.fsync(fd)

    monkeypatch.setattr(cognitive_store, "os", SimpleNamespace(
        name=name, getpid=os.getpid, urandom=os.urandom, O_RDONLY=os.O_RDONLY,
        O_DIRECTORY=directory_flag, open=open_directory, fsync=fsync,
        close=lambda fd: closed.append(fd)))
    return closed, descriptor


def test_disable_fsyncs_replaced_directory_before_json_authority(tmp_path, monkeypatch):
    from harness.runtime import cognitive_store

    store = CognitiveStateStore(tmp_path / "cognition.json")
    try:
        store.activate(enabled=True)
        store.commit({"owner_meta": {"state": {"revision": 9}}})
        events = []
        original_replace = cognitive_store.replace_path
        original_connect = store._connect

        def replace(source, destination):
            original_replace(source, destination)
            assert store.authority() == "sqlite"
            events.append("replace")

        def directory_fsync():
            assert store.authority() == "sqlite"
            assert json.loads(store.json_path.read_text(encoding="utf-8"))["revision"] == 9
            events.append("directory_fsync")

        def connect():
            connection = original_connect()
            connection.set_trace_callback(lambda sql: events.append("authority_json")
                if sql == "UPDATE metadata SET value='json' WHERE name='authority'" else None)
            return connection

        closed, descriptor = _checkpoint_os(monkeypatch, directory_fsync)
        monkeypatch.setattr(cognitive_store, "replace_path", replace)
        monkeypatch.setattr(store, "_connect", connect)
        assert store.activate(enabled=False)["revision"] == 9
        assert store.authority() == "json"
        assert events == ["replace", "directory_fsync", "authority_json"]
        assert closed == [descriptor]
    finally:
        store.close()


def test_directory_fsync_failure_cannot_reactivate_stale_json(tmp_path, monkeypatch):
    path = tmp_path / "cognition.json"
    path.write_text(json.dumps({"revision": 3}), encoding="utf-8")
    store = CognitiveStateStore(path)
    store.activate(enabled=True)
    store.commit({"owner_meta": {"state": {"revision": 9}}})

    def fail():
        raise OSError("synthetic directory fsync failure")

    with monkeypatch.context() as patch:
        closed, descriptor = _checkpoint_os(patch, fail)
        with pytest.raises(OSError, match="directory fsync"):
            store.activate(enabled=False)
        assert store.authority() == "sqlite"
        assert closed == [descriptor]
        store.close()
        # Model an undurable rename disappearing after power loss.
        path.write_text(json.dumps({"revision": 3}), encoding="utf-8")
        restarted = CognitiveStateStore(path)
        try:
            assert restarted.activate(enabled=True)["revision"] == 9
            with pytest.raises(OSError, match="directory fsync"):
                restarted.activate(enabled=False)
            assert restarted.authority() == "sqlite"
        finally:
            restarted.close()

    recovered = CognitiveStateStore(path)
    try:
        assert recovered.activate(enabled=False)["revision"] == 9
        assert recovered.authority() == "json"
        assert json.loads(path.read_text(encoding="utf-8"))["revision"] == 9
    finally:
        recovered.close()


def test_directory_fsync_checkpoint_failure_preserves_durable_commit(tmp_path, monkeypatch):
    store = CognitiveStateStore(tmp_path / "cognition.json")
    try:
        store.activate(enabled=True)
        store.commit({"owner_meta": {"state": {"revision": 11}}})

        def fail():
            raise OSError("synthetic directory fsync failure")

        with monkeypatch.context() as patch:
            closed, descriptor = _checkpoint_os(patch, fail)
            assert not store.checkpoint(force=True)
            assert store.last_checkpoint_error == "cognitive_checkpoint_failed"
            assert store.authority() == "sqlite"
            assert store.load()["revision"] == 11
            assert closed == [descriptor]
        assert store.checkpoint(force=True)
        assert store.last_checkpoint_error is None
        assert json.loads(store.json_path.read_text(encoding="utf-8"))["revision"] == 11
    finally:
        store.close()


def test_windows_checkpoint_does_not_open_directory(tmp_path, monkeypatch):
    from harness.runtime.cognitive_store import write_checkpoint

    def forbidden():
        raise AssertionError("Windows checkpoint must not fsync a directory")

    closed, _ = _checkpoint_os(monkeypatch, forbidden, name="nt")
    path = tmp_path / "cognition.json"
    write_checkpoint(path, {"revision": 7})
    assert json.loads(path.read_text(encoding="utf-8"))["revision"] == 7
    assert not closed


def test_transaction_failure_rolls_back_all_rows_and_retains_dirty_work(tmp_path):
    owner, _, _ = _owner(tmp_path)
    owner._revision = 1
    owner._persist_state()
    store = owner._state_store
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("CREATE TRIGGER reject_session BEFORE INSERT ON records WHEN NEW.category='sessions' "
                           "BEGIN SELECT RAISE(ABORT, 'synthetic failure'); END")
    owner._revision = 2
    owner._request_hashes[("PrepareResponse", "pending")] = "reserved-hash"
    owner.pipeline.session_store.append("s", "user", "待提交")
    with pytest.raises(sqlite3.IntegrityError):
        owner._persist_state()
    durable = CognitiveStateStore(owner.state_path).load()
    assert durable["revision"] == 1
    assert durable["request_hashes"] == {} and durable["sessions"] == {}
    assert owner._request_hashes.dirty_versions()
    assert owner.pipeline.session_store.persistence_changes()[0]
    with closing(sqlite3.connect(store.path)) as connection, connection:
        connection.execute("DROP TRIGGER reject_session")
    owner._persist_state()
    restored, _, _ = _owner(tmp_path)
    assert restored._revision == 2
    assert restored._request_hashes[("PrepareResponse", "pending")] == "reserved-hash"
    assert restored.pipeline.session_store.recent("s")[0].text == "待提交"


def test_new_session_does_not_serialize_or_scan_historical_projections(tmp_path, monkeypatch):
    owner, _, _ = _owner(tmp_path)
    for number in range(80):
        owner.research_traces[str(number)] = {"large": "历史" * 5000}
        owner.pipeline.session_store.append(str(number), "user", "历史" * 1000)
    owner._persist_state()
    def forbidden(*args, **kwargs):
        raise AssertionError("unchanged history must not be projected")
    monkeypatch.setattr(owner.emotion_service, "snapshot", forbidden)
    monkeypatch.setattr(owner.pipeline.session_store, "snapshot", forbidden)
    monkeypatch.setattr(owner, "_legacy_state_payload", forbidden)
    owner.pipeline.session_store.append("new", "user", "新增")
    owner._persist_state()
    assert owner._state_store.last_commit["rows"] == 3  # two tiny metadata rows + one window
    assert owner._state_store.last_commit["bytes"] < 1000
    assert _owner(tmp_path)[0].pipeline.session_store.recent("new")[0].text == "新增"


def test_prepared_and_command_ledger_replay_across_restart(tmp_path):
    async def run():
        owner, _, _ = _owner(tmp_path)
        command = _command("PrepareResponse", 1, {"text": "你好", "speaker": "用户", "session_id": "s"})
        first = await owner.prepare_response(command)
        assert first.status == "applied", first.error
        restarted, main, _ = _owner(tmp_path)
        replayed = await restarted.prepare_response(command)
        assert replayed.idempotency_hit and replayed.status == "idempotent_replay"
        assert replayed.result == first.result
        assert first.result.prepare_id in restarted._prepared
        assert not main.chat_calls
    asyncio.run(run())


def test_emotion_reservation_recovers_then_replays_both_steps(tmp_path, monkeypatch):
    owner, _, aux = _owner(tmp_path, emotion="on")
    # Crash point immediately after the source/reservation transaction, before
    # appraisal. Its original request must remain the authoritative input.
    original = request()
    owner.emotion_service._reserve(original)
    assert not aux.calls
    restarted, _, restored_aux = _owner(tmp_path, emotion="on")
    assert restarted.emotion_service.existing_input(original.context_id) == original
    frame = restarted.emotion_service.observe(original)
    guidance = restarted.emotion_service.guidance(frame)
    assert len(restored_aux.calls) == 2
    def forbidden(*args, **kwargs):
        raise AssertionError("hot persistence cannot snapshot all emotion history")
    monkeypatch.setattr(restarted.emotion_service, "snapshot", forbidden)
    restarted.emotion_service.record_delivery([message("delivered", role="character")])
    again, _, final_aux = _owner(tmp_path, emotion="on")
    assert again.emotion_service.observe(original) == frame
    assert again.emotion_service.guidance(frame.context_id) == guidance
    assert not final_aux.calls
    assert any(item.source_id == "delivered" for item in again.emotion_service._state.sources.values())


def test_emotion_failed_commit_rolls_back_in_memory_and_sqlite(tmp_path, monkeypatch):
    owner, _, _ = _owner(tmp_path, emotion="on")
    before = owner.emotion_service.snapshot()
    original_commit = owner._state_store.commit
    def fail(*args, **kwargs):
        raise OSError("synthetic failure")
    monkeypatch.setattr(owner._state_store, "commit", fail)
    with pytest.raises(OSError):
        owner.emotion_service.observe(request())
    assert owner.emotion_service.snapshot() == before
    assert _owner(tmp_path, emotion="on")[0].emotion_service.existing_input("context-1") is None
    monkeypatch.setattr(owner._state_store, "commit", original_commit)
    assert owner.emotion_service.observe(request()).status == "model"


def test_periodic_checkpoint_failure_does_not_undo_committed_emotion(tmp_path, monkeypatch):
    owner, _, _ = _owner(tmp_path, emotion="on")
    owner._state_store.CHECKPOINT_INTERVAL = 1
    def fail(*args, **kwargs):
        raise OSError("synthetic checkpoint failure")
    monkeypatch.setattr("harness.runtime.cognitive_store.write_checkpoint", fail)
    frame = owner.emotion_service.observe(request())
    assert frame.status == "model"
    assert owner._state_store.last_checkpoint_error == "cognitive_checkpoint_failed"
    restored, _, _ = _owner(tmp_path, emotion="on")
    assert restored.emotion_service.observe(request()) == frame


def test_checkpoint_and_session_deletion_or_replacement(tmp_path):
    owner, _, _ = _owner(tmp_path)
    owner.pipeline.session_store.append("remove", "user", "删除")
    owner.pipeline.session_store.append("keep", "user", "保留")
    owner._persist_state()
    owner.pipeline.session_store.clear("remove")
    owner._persist_state()
    assert set(owner._state_store.load()["sessions"]) == {"keep"}
    replacement = SessionStore()
    replacement.append("replacement", "user", "替换")
    owner.pipeline.session_store = replacement
    owner._persist_state()
    assert owner._state_store.checkpoint(force=True)
    assert set(json.loads(owner.state_path.read_text(encoding="utf-8"))["sessions"]) == {"replacement"}
    assert owner._state_store.last_checkpoint_error is None


def test_dirty_acknowledgement_does_not_erase_newer_mutation():
    tracked = TrackedDict({"x": 1})
    old = tracked.dirty_versions()
    tracked["x"] = 2
    tracked.acknowledge(old)
    assert tracked.dirty_versions()
    tracked.acknowledge(tracked.dirty_versions())
    tracked |= {"y": 3}
    assert tracked.pop("y") == 3
    assert "y" in tracked.dirty_versions()
    session = SessionStore()
    session.append("s", "user", "一")
    versions, _ = session.persistence_changes()
    session.append("s", "character", "二")
    session.acknowledge_persistence(versions)
    assert session.persistence_changes()[0]


@pytest.mark.parametrize("enabled", [False, True])
def test_persistence_stages_are_measured_for_both_backends(tmp_path, enabled):
    owner, _, _ = _owner(tmp_path, enabled=enabled)
    trace = QQTurnTrace()
    with qq_trace_scope(trace):
        owner._persist_state()
    names = {item["name"] for item in trace.snapshot()["stages"]}
    assert {"persistence.project", "persistence.serialize"} <= names
    assert ("persistence.sqlite_commit" if enabled else "persistence.fsync") in names


def test_database_wal_full_and_explicit_record_delete(tmp_path):
    store = CognitiveStateStore(tmp_path / "cognition.json")
    store.activate(enabled=True)
    with closing(store._connect()) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA synchronous").fetchone()[0] == 2
    store.commit({"request_hashes": {"prepare\0k": "hash"}})
    store.commit({"request_hashes": {"prepare\0k": DELETED}})
    assert store.load()["request_hashes"] == {}


def test_process_crash_recovers_committed_wal_and_discards_open_transaction(tmp_path):
    path = tmp_path / "crash.json"
    committed = """
import os, sys
from pathlib import Path
from harness.runtime.cognitive_store import CognitiveStateStore
store = CognitiveStateStore(Path(sys.argv[1]))
store.activate(enabled=True)
store.commit({'owner_meta': {'state': {'revision': 11}},
              'request_hashes': {'PrepareResponse\\0reserved': 'durable'}})
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", committed, str(path)], check=True)
    store = CognitiveStateStore(path)
    assert store.load()["revision"] == 11
    assert store.load()["request_hashes"] == {"PrepareResponse\0reserved": "durable"}
    uncommitted = """
import os, sqlite3, sys
connection = sqlite3.connect(sys.argv[1])
connection.execute('PRAGMA synchronous=FULL')
connection.execute('BEGIN IMMEDIATE')
connection.execute("UPDATE records SET payload='{}' WHERE category='request_hashes'")
connection.execute("UPDATE records SET payload='{}' WHERE category='owner_meta'")
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", uncommitted, str(store.path)], check=True)
    assert store.load()["revision"] == 11
    assert store.load()["request_hashes"] == {"PrepareResponse\0reserved": "durable"}
    store.close()
