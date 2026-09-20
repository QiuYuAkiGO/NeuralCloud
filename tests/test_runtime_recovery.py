"""Fast recovery is equivalent; audit results have no Runtime control authority."""

from __future__ import annotations

import asyncio
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from harness.runtime.journal import JournalCorruptionError, RuntimeJournal
from harness.runtime.journal_audit import JournalAudit
from harness.runtime.recovery import RecoveryStore
from tests.test_runtime_command_index import _event, _envelope

IDENTITY = {"character": "misty", "manifest": "fixture"}


def open_journal(root, mode="strict", **kwargs):
    store = RecoveryStore(root / "recovery.sqlite3", identity=IDENTITY)
    journal = RuntimeJournal(
        root / "journal",
        runtime_instance_id="fixture",
        character_id="misty",
        manifest_revision=1,
        fsync=False,
        recovery_store=store,
        recovery_mode=mode,
        **kwargs,
    )
    return journal, store


def append_event(journal, n):
    event = _event(n)
    journal.append(
        "event_accepted", event_id=event.event_id, receive_seq=n, data={"event": event.model_dump(mode="json")}
    )
    return event


def mutate_old_record(root, sequence):
    for path in sorted((root / "journal").glob("*.jsonl")):
        lines = path.read_bytes().splitlines(keepends=True)
        for i, line in enumerate(lines):
            record = json.loads(line)
            if record["record_seq"] == sequence:
                # Preserve byte count, line boundaries and sequence metadata.
                assert b"noise" in line
                lines[i] = line.replace(b"noise", b"noisX", 1)
                path.write_bytes(b"".join(lines))
                return
    raise AssertionError("fixture record not found")


def test_fast_restart_only_reads_tail_and_supports_historical_pages(tmp_path, monkeypatch):
    journal, store = open_journal(tmp_path, max_segment_bytes=8192)
    for i in range(1100):
        journal.append("noise", data={"n": i})
    assert store.commit()
    saved_hash = journal.last_hash
    for i in range(7):
        journal.append("noise", data={"tail": i})
    store.close()  # rollback uncommitted projection; journal remains durable
    decoded = []
    decode = RuntimeJournal._decode_record

    def counted(*args):
        record = decode(*args)
        decoded.append(record.record_seq)
        return record

    monkeypatch.setattr(RuntimeJournal, "_decode_record", staticmethod(counted))
    resumed, restored = open_journal(tmp_path, "fast", max_segment_bytes=8192)
    assert restored.mode == "fast"
    assert resumed.recovered_records == 7
    assert resumed.last_record_seq == 1107
    assert decoded == [1, 1100, *range(1101, 1108)]
    assert resumed.records_after(1099, limit=1)[0].checksum == saved_hash
    assert [r.record_seq for r in resumed.records_after(4, limit=3)] == [5, 6, 7]
    assert not resumed._index  # historical seek metadata is not hydrated
    assert resumed.retained_records <= 512
    restored.close()


@pytest.mark.parametrize("cut", [1, 3, 7, 12])
def test_projection_full_and_incremental_are_equal(tmp_path, cut):
    journal, store = open_journal(tmp_path)
    for n in range(1, 7):
        event = append_event(journal, n)
        if journal.last_record_seq == cut:
            store.commit()
        journal.append("processing_completed", event_id=event.event_id, data={"status": "completed"})
        if journal.last_record_seq == cut:
            store.commit()
    journal.append("agency_terminal")
    store.close()
    fast, cache = open_journal(tmp_path, "fast")
    fast_state = dict(cache.projector.state)
    fast_tables = {
        table: list(cache.rows(f"SELECT * FROM {table} ORDER BY 1"))
        for table in ("kv", "events", "commands", "actions", "ranges")
    }
    assert not list(cache.pending_events())
    cache.close()
    full, rebuilt = open_journal(tmp_path)
    assert full.last_hash == fast.last_hash
    assert rebuilt.projector.state == fast_state
    for table, rows in fast_tables.items():
        assert list(rebuilt.rows(f"SELECT * FROM {table} ORDER BY 1")) == rows
    rebuilt.close()


def test_pending_range_completion_and_intent_cross_checkpoint(tmp_path):
    journal, store = open_journal(tmp_path)
    events = [append_event(journal, n) for n in range(1, 6)]
    envelope = _envelope(events[0])
    journal.append(
        "domain_command_intent",
        command_id=envelope.command_id,
        idempotency_key=envelope.idempotency_key,
        data={"service": "cognitive", "envelope": envelope.model_dump(mode="json")},
    )
    journal.append(
        "processing_started",
        event_id="merged",
        receive_seq=1,
        data={"event_type": "user_message", "source": "user", "first_receive_seq": 1, "last_receive_seq": 4},
    )
    store.commit()
    journal.append("processing_completed", event_id="merged", data={"status": "completed"})
    journal.append("domain_command_result", command_id=envelope.command_id, data={"service": "cognitive"})
    store.close()
    _, cache = open_journal(tmp_path, "fast")
    assert [r.event_id for r in cache.pending_events()] == [events[4].event_id]
    assert not list(cache.pending_commands())
    assert list(cache.rows("SELECT * FROM ranges")) == [("user_message", "user", 1, 4)]
    cache.close()


def test_corrupt_cache_and_changed_anchor_fall_back(tmp_path):
    journal, store = open_journal(tmp_path)
    for i in range(3):
        journal.append("noise")
    store.commit()
    store.close()
    with sqlite3.connect(tmp_path / "recovery.sqlite3") as db:
        data = json.loads(db.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()[0])
        data["anchor"]["checksum"] = "f" * 64
        db.execute("UPDATE meta SET value=? WHERE key='checkpoint'", (json.dumps(data),))
    resumed, cache = open_journal(tmp_path, "fast")
    assert cache.mode == "strict" and "checkpoint" in cache.fallback_reason
    assert resumed.recovered_records == 3
    cache.commit()
    cache.close()


def test_fast_tail_corruption_is_not_silently_ignored(tmp_path):
    journal, store = open_journal(tmp_path)
    journal.append("noise")
    store.commit()
    store.close()
    with journal.path.open("ab") as handle:
        handle.write(b"invalid complete line\n")
    with pytest.raises(JournalCorruptionError):
        open_journal(tmp_path, "fast")


def test_fast_partial_tail_repairs_only_unfinished_final_line(tmp_path):
    journal, store = open_journal(tmp_path)
    journal.append("noise")
    store.commit()
    store.close()
    with journal.path.open("ab") as handle:
        handle.write(b"{broken")
    resumed, cache = open_journal(tmp_path, "fast")
    assert resumed.truncated_tail_bytes == 7
    assert resumed.append("after").record_seq == 2
    cache.close()


def test_emergency_buffer_cannot_be_published(tmp_path, monkeypatch):
    journal, store = open_journal(tmp_path)
    journal.append("noise")
    store.commit()
    from harness.runtime.journal import JournalUnavailableError

    def fail(*args):
        raise OSError("disk unavailable")

    monkeypatch.setattr(journal, "_write_record", fail)
    with pytest.raises(JournalUnavailableError):
        journal.append("agency_terminal")
    store.sync()
    assert store.projector.cursor == 2
    assert store.commit() is False
    with sqlite3.connect(tmp_path / "recovery.sqlite3") as db:
        saved = json.loads(db.execute("SELECT value FROM meta WHERE key='checkpoint'").fetchone()[0])
    assert saved["cursor"] == 1
    store.close()


def test_audit_corruption_is_diagnostic_only_and_does_not_disable_fast_restart(tmp_path):
    journal, store = open_journal(tmp_path)
    for _ in range(600):
        journal.append("noise")
    store.commit()
    store.close()
    mutate_old_record(tmp_path, 20)
    resumed, cache = open_journal(tmp_path, "fast")
    anchor = dict(cache.anchor)
    audit = JournalAudit(resumed, tmp_path / "audit.json", duty_cycle=1)
    result = asyncio.run(audit.run_once())
    assert result["state"] == "corrupt" and result["record_seq"] == 20
    assert cache.mode == "fast" and cache.anchor == anchor
    assert not resumed.degraded
    assert resumed.append("still_operational").record_seq == 601
    cache.close()
    again, cache = open_journal(tmp_path, "fast")
    assert cache.mode == "fast" and again.last_record_seq == 601
    cache.close()


def test_audit_io_and_diagnostic_write_errors_do_not_modify_journal(tmp_path, monkeypatch, caplog):
    journal, store = open_journal(tmp_path)
    journal.append("noise")
    store.commit()
    before = journal.path.read_bytes()
    audit = JournalAudit(journal, tmp_path / "audit.json", duty_cycle=1)
    original = Path.open

    def denied(path, *args, **kwargs):
        if path == journal.path or path.name.startswith(".audit.json"):
            raise PermissionError("injected permission failure")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    result = asyncio.run(audit.run_once())
    assert result["state"] == "audit_error"
    assert "diagnostic write failed" in caplog.text
    monkeypatch.setattr(Path, "open", original)
    assert journal.path.read_bytes() == before
    assert not journal.degraded and store.committed_seq == 1
    store.close()


@pytest.mark.parametrize("cut", ["before_commit", "after_commit", "after_rotation"])
def test_real_process_exit_preserves_only_committed_projection(tmp_path, cut):
    script = """
import os, sys
from pathlib import Path
from tests.test_runtime_recovery import open_journal, append_event
journal,store = open_journal(Path(sys.argv[1]),max_segment_bytes=1024)
event = append_event(journal,1)
if sys.argv[2] != 'before_commit':
    store.commit()
journal.append('processing_completed',event_id=event.event_id,data={'status':'completed'})
if sys.argv[2] == 'after_rotation':
    for _ in range(20):
        journal.append('noise')
os._exit(0)
"""
    subprocess.run([sys.executable, "-c", script, str(tmp_path), cut], check=True)
    journal, store = open_journal(tmp_path, "fast", max_segment_bytes=1024)
    assert journal.last_record_seq == (22 if cut == "after_rotation" else 2)
    assert not list(store.pending_events())
    assert store.projector.cursor == journal.last_record_seq
    assert journal.append("after_restart").record_seq == (23 if cut == "after_rotation" else 3)
    store.close()


def native_runtime(tmp_path):
    from harness.runtime.supervisor import RuntimeSupervisor
    from tests.test_runtime_supervisor import _manifest

    return RuntimeSupervisor(
        manifest=_manifest(), cognitive=object(), homeostasis=object(), state_dir=tmp_path, character_id="misty"
    )


def test_native_fast_state_and_old_checkpoint_do_not_override_new_agency(tmp_path, monkeypatch):
    from harness.runtime.models import AgencyState

    first = native_runtime(tmp_path)
    first.journal.append("body_state_observed", data={"episode_id": "e", "body_revision": 1})
    first._restore_runtime_indexes()
    first.write_checkpoint()
    first.journal.append("agency_terminal")
    first.recovery.close()
    first.instance_lock.release()
    monkeypatch.setenv("NC_RUNTIME_RECOVERY_MODE", "fast")
    resumed = native_runtime(tmp_path)
    resumed._restore_runtime_indexes()
    assert resumed._agency_state == AgencyState.TERMINAL
    assert resumed._terminal_body_revision == 1
    assert resumed.recovery.mode == "fast"
    resumed.recovery.close()
    resumed.instance_lock.release()


def test_audit_cannot_change_runtime_control_or_health(tmp_path, monkeypatch):
    from harness.runtime.models import RuntimeLifecycle, RuntimeEventType

    runtime = native_runtime(tmp_path)
    for _ in range(600):
        runtime.journal.append("noise")
    runtime.recovery.commit()
    runtime.lifecycle = RuntimeLifecycle.RUNNING
    before_health = runtime.status().health
    guard = runtime.action_coordinator.execution_guard
    anchor = dict(runtime.recovery.anchor)
    mutate_old_record(tmp_path, 20)
    audit = JournalAudit(runtime.journal, tmp_path / "audit.json", duty_cycle=1)
    runtime._audit = audit
    assert asyncio.run(audit.run_once())["state"] == "corrupt"
    assert runtime.status().health == before_health
    assert runtime.lifecycle == RuntimeLifecycle.RUNNING
    assert runtime.accepts_event(RuntimeEventType.USER_MESSAGE)
    assert runtime.action_coordinator.execution_guard is guard
    assert runtime.recovery.anchor == anchor
    assert runtime.status().recovery["audit"]["state"] == "corrupt"
    runtime.recovery.close()
    runtime.instance_lock.release()


def test_unknown_action_still_requires_reconciliation(tmp_path):
    from datetime import UTC, datetime, timedelta
    from harness.runtime.models import ActionExecution, ActionExecutionState

    journal, store = open_journal(tmp_path)
    action = ActionExecution(
        goal_id="g",
        goal="fixture",
        idempotency_key="a",
        reversible=True,
        verification_method="fixture",
        state=ActionExecutionState.UNKNOWN,
        deadline=datetime.now(UTC) + timedelta(days=1),
    )
    journal.append("action_reconciled", data={"action": action.model_dump(mode="json")})
    store.commit()
    store.close()
    _, cache = open_journal(tmp_path, "fast")
    assert [row[0] for row in cache.action_rows()] == [action.action_id]
    cache.close()


def test_corrupt_database_is_preserved_and_rebuilt(tmp_path):
    journal, store = open_journal(tmp_path)
    journal.append("noise")
    store.commit()
    store.close()
    (tmp_path / "recovery.sqlite3").write_bytes(b"not a sqlite database")
    resumed, cache = open_journal(tmp_path, "fast")
    assert resumed.last_record_seq == 1 and cache.mode == "strict"
    assert list(tmp_path.glob("recovery.sqlite3.corrupt-*"))
    assert cache.commit()
    cache.close()
    _, cache = open_journal(tmp_path, "fast")
    assert cache.mode == "fast"
    cache.close()


def test_derived_write_failure_rebuilds_memory_without_rejecting_durable_append(tmp_path):
    journal, store = open_journal(tmp_path)
    journal.append("noise")
    store.commit()
    # SQLite query_only reproduces a derived write failure independently of
    # journal durability, including failures that occur after a flush.
    store.db.execute("PRAGMA query_only=ON")
    event = append_event(journal, 2)
    assert journal.last_record_seq == 2 and not journal.degraded
    assert not store.persistent
    assert [r.event_id for r in store.pending_events()] == [event.event_id]
    store.close()
    _, cache = open_journal(tmp_path, "fast")
    assert cache.mode == "fast"
    assert [r.event_id for r in cache.pending_events()] == [event.event_id]
    cache.close()


def test_action_revision_does_not_regress_or_advance_just_for_hydration(tmp_path, monkeypatch):
    from datetime import UTC, datetime, timedelta
    from harness.runtime.models import ActionExecution, ActionExecutionState

    runtime = native_runtime(tmp_path)
    action = ActionExecution(
        goal_id="g",
        goal="fixture",
        idempotency_key="a",
        reversible=True,
        verification_method="fixture",
        state=ActionExecutionState.SUCCEEDED,
        deadline=datetime.now(UTC) + timedelta(days=1),
    )
    runtime.action_coordinator.set_revision_floor(19)
    runtime._append_journal("action_completed", data={"action": action.model_dump(mode="json")})
    runtime.recovery.commit()
    runtime.recovery.close()
    runtime.instance_lock.release()
    monkeypatch.setenv("NC_RUNTIME_RECOVERY_MODE", "fast")
    resumed = native_runtime(tmp_path)
    resumed._restore_runtime_indexes()
    assert resumed.action_coordinator.revision == 19
    assert asyncio.run(resumed._restore_and_reconcile_actions()) is False
    assert resumed.action_coordinator.revision == 19
    assert resumed.action_coordinator.get(action.action_id) == action
    query = list(resumed.recovery.rows("EXPLAIN QUERY PLAN SELECT id FROM actions WHERE work=1 ORDER BY seq"))
    assert any("actions_pending" in str(row) for row in query)
    resumed.recovery.close()
    resumed.instance_lock.release()


def test_offline_verifier_never_repairs_its_input(tmp_path):
    from tools.verify_runtime_recovery import verify

    journal, store = open_journal(tmp_path)
    for _ in range(6):
        journal.append("noise")
    store.close()
    with journal.path.open("ab") as handle:
        handle.write(b"{partial")
    before = journal.path.read_bytes()
    result = verify(tmp_path / "journal", manifest_hash="fixture", tail=3)
    assert result["equal"] and result["mode"] == "fast" and result["replayed_records"] == 3
    assert journal.path.read_bytes() == before
