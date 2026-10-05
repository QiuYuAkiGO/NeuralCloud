"""Append optimization preserves the original validation and atomicity contract."""
from contextlib import closing
import json
import sqlite3

import pytest

from harness.memory.shared import SharedMemoryStore
from harness.qq.memory import QQVisualMemoryStore
from tests.test_shared_memory import person, source_database, scope


def append(source, suffix="new", *, reuse_profile=False):
    context = person(suffix=suffix)
    with closing(QQVisualMemoryStore(source)._connect()) as db, db:
        profile, observation = context.profiles[0], context.observations[0]
        if reuse_profile:
            existing = db.execute("SELECT record_id FROM qq_visual_profiles WHERE group_id='100' LIMIT 1").fetchone()[0]
            observation = observation.model_copy(update={"profile_record_id": existing})
        else:
            db.execute("INSERT INTO qq_visual_profiles VALUES (?,?,?,?,?,?)", (
                profile.record_id, profile.group_id, profile.member_id, profile.capture_id,
                profile.observed_at.isoformat(), profile.model_dump_json()))
        db.execute("INSERT INTO qq_visual_observations VALUES (?,?,?,?,?,?,?,?)", (
            observation.record_id, observation.group_id, observation.member_id, observation.capture_id,
            observation.capture_item_id, observation.profile_record_id, observation.observed_at.isoformat(),
            observation.model_dump_json()))


def sync(store, source, **kwargs):
    return store.import_qq_database(source, allowed_group_ids=["100"], self_member_id="999",
                                    track_changes=True, **kwargs)


def checkpoint(store):
    with sqlite3.connect(store.path) as db:
        return db.execute("SELECT state_json FROM shared_qq_checkpoints").fetchone()[0]


def test_delta_survives_restart_and_matches_full_import(tmp_path):
    source = source_database(tmp_path, segmented=True)
    store = SharedMemoryStore(tmp_path / "delta.db")
    assert sync(store, source).created_count == 27
    original = source.read_bytes()
    assert sync(store, source).selected_count == 0
    assert source.read_bytes() == original
    append(source, reuse_profile=True)
    store = SharedMemoryStore(store.path)  # durable checkpoint, no process cache
    delta = sync(store, source)
    assert delta.success and delta.created_count == delta.selected_count == 1
    full = SharedMemoryStore(tmp_path / "full.db")
    assert full.import_qq_database(source, allowed_group_ids=["100"], self_member_id="999").success
    def projected(s):
        return {r.record_id: r.model_dump(exclude={"stored_at"}) for r in s.query(scope(), limit=100).records}
    assert projected(store) == projected(full)


@pytest.mark.parametrize("table,sql", [
    ("profile", "UPDATE qq_visual_profiles SET member_id='201' WHERE group_id='100'"),
    ("observation", "UPDATE qq_visual_observations SET profile_record_id='missing' WHERE group_id='100'"),
    ("receipt", "UPDATE qq_reply_deliveries SET receipt_hash='bad'"),
    ("decision", "UPDATE qq_reply_decisions SET status='pending'"),
    ("segment", "UPDATE qq_reply_segment_deliveries SET segment_index=2 WHERE segment_index=1"),
])
def test_old_source_change_invalidates_prefix_without_partial_commit(tmp_path, table, sql):
    source = source_database(tmp_path, segmented=True)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).success
    old = checkpoint(store)
    append(source)
    with sqlite3.connect(source) as db:
        db.execute(sql)
    report = sync(store, source)
    assert not report.success and checkpoint(store) == old
    assert len(store.query(scope(), limit=100).records) == 27


def test_target_failure_rolls_back_records_and_checkpoint(tmp_path):
    source = source_database(tmp_path)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).success
    old = checkpoint(store)
    append(source)
    with sqlite3.connect(store.path) as db:
        db.execute("CREATE TRIGGER fail_checkpoint BEFORE UPDATE ON shared_qq_checkpoints "
                   "BEGIN SELECT RAISE(ABORT,'simulated disk failure'); END")
    assert not sync(store, source).success
    assert checkpoint(store) == old
    assert len(store.query(scope(), limit=100).records) == 25
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER fail_checkpoint")
    assert sync(store, source).created_count == 2


def test_scope_expansion_has_its_own_full_baseline(tmp_path):
    source = source_database(tmp_path)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).created_count == 25
    result = store.import_qq_database(source, allowed_group_ids=["100", "101"],
                                     self_member_id="999", track_changes=True)
    assert result.success and result.created_count == 2 and result.existing_count == 25


def test_legacy_database_stays_read_only_and_uses_full_validation(tmp_path):
    source = source_database(tmp_path)
    with sqlite3.connect(source) as db:
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'nc_append_%'").fetchall():
            db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE qq_source_append_state")
    before = source.read_bytes()
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).created_count == 25
    assert sync(store, source).existing_count == 25
    assert source.read_bytes() == before


def test_missing_guard_rejected_then_repaired_with_invalidation(tmp_path):
    source = source_database(tmp_path)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).success
    with sqlite3.connect(source) as db:
        db.execute("DROP TRIGGER nc_append_qq_visual_profiles_update")
    assert not sync(store, source).success
    with closing(QQVisualMemoryStore(source)._connect()):
        pass
    assert sync(store, source).existing_count == 25


def test_source_replacement_forces_full_audit(tmp_path):
    source = source_database(tmp_path)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).success
    replacement = tmp_path / "replacement.db"
    with closing(sqlite3.connect(source)) as src, closing(sqlite3.connect(replacement)) as dst:
        src.backup(dst)
    replacement.replace(source)
    assert sync(store, source).existing_count == 25


def test_deletion_and_rowid_reuse_invalidate_prefix(tmp_path):
    source = source_database(tmp_path)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).success
    old = json.loads(checkpoint(store))
    with sqlite3.connect(source) as db:
        row = db.execute("SELECT rowid,* FROM qq_visual_profiles WHERE group_id='101'").fetchone()
        db.execute("DELETE FROM qq_visual_profiles WHERE rowid=?", (row[0],))
        db.execute("INSERT INTO qq_visual_profiles(rowid,record_id,group_id,member_id,capture_id,observed_at,record_json) VALUES (?,?,?,?,?,?,?)", row)
    result = sync(store, source)
    assert result.success and result.existing_count == 25
    assert json.loads(checkpoint(store))["revision"] > old["revision"]


@pytest.mark.parametrize("table", ["qq_reply_decisions", "qq_visual_profiles"])
def test_replace_without_recursive_delete_triggers_invalidates_prefix(tmp_path, table):
    source = source_database(tmp_path)
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).success
    with sqlite3.connect(source) as db:
        db.execute("PRAGMA recursive_triggers=OFF")
        if table == "qq_reply_decisions":
            row = list(db.execute("SELECT * FROM qq_reply_decisions LIMIT 1").fetchone())
            row[3] = "pending"
            db.execute("INSERT OR REPLACE INTO qq_reply_decisions VALUES (?,?,?,?,?)", row)
        else:
            row = list(db.execute("SELECT * FROM qq_visual_profiles WHERE group_id='100' LIMIT 1").fetchone())
            # A capture collision replaces a referenced profile with another ID.
            row[0] += ":replacement"
            raw = json.loads(row[-1])
            raw['record_id'] = row[0]
            row[-1] = json.dumps(raw)
            db.execute("INSERT OR REPLACE INTO qq_visual_profiles VALUES (?,?,?,?,?,?)", row)
    old = checkpoint(store)
    assert not sync(store, source).success
    assert checkpoint(store) == old


def test_explicit_nonpositive_rowid_is_never_skipped(tmp_path):
    source = source_database(tmp_path)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE qq_visual_observations SET rowid=0 WHERE rowid=1")
    store = SharedMemoryStore(tmp_path / "target.db")
    assert sync(store, source).created_count == 25
    append(source)
    with sqlite3.connect(source) as db:
        db.execute("UPDATE qq_visual_observations SET rowid=-1 WHERE record_id LIKE '%:new'")
    result = sync(store, source)
    assert result.success and result.created_count == 2 and result.existing_count == 25


def test_facade_detects_replacement_even_with_same_mtime_and_size(tmp_path):
    from types import SimpleNamespace
    import shutil
    from harness.cognitive.shared_memory import CognitiveSharedMemory
    source = source_database(tmp_path)
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "target.db",
        qq_settings=SimpleNamespace(memory_db=source, allowed_group_ids=["100"], self_member_id="999"))
    shared.sync_qq()
    old = shared.last_import
    replacement = tmp_path / "replacement.db"
    shutil.copy2(source, replacement)
    replacement.replace(source)
    shared.sync_qq()
    assert shared.last_import is not old
    assert shared.last_import.success and shared.last_import.existing_count == 25
