"""Daily inputs preserve actual source identity across windows and online QQ jobs."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory
from harness.contracts.decisions import ActionOutcomeEvidence
from harness.memory.qq_assimilation import QQAssimilationQueue, QQSource
from harness.memory.revision.sources import collect_source_window, partition_sources
from harness.memory.shared import (
    SharedActionOutcome, SharedLocalEvent, SharedMemoryStorageError, SharedMemoryStore,
    SharedReadScope,
)
from harness.pipeline.session import SessionStore
from tests.test_shared_memory import person
from tests.test_qq_assimilation import add_message, NOW as QQ_NOW


NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)


def event(identifier, *, at=NOW, role="user", session="local-one"):
    return SharedLocalEvent(event_id=identifier, session_id=session,
        author_principal="local:main" if role == "user" else "character:misty", role=role,
        text="原始消息 " + identifier, occurred_at=at, evidence_refs=("evidence:" + identifier,))


def collect(store, **kwargs):
    return collect_source_window(store, owner_id=store.owner_id, preset_id=store.preset_id,
        start=NOW, end=NOW + timedelta(days=1), **kwargs)


def test_window_pages_all_records_and_excludes_late_arrivals_with_frozen_watermark(tmp_path):
    store = SharedMemoryStore(tmp_path / "sources.sqlite3")
    before = store.record_local(event("before", at=NOW - timedelta(microseconds=1))).record
    expected = [store.record_local(event(f"entry-{i}", at=NOW + timedelta(seconds=i))).record.record_id for i in range(245)]
    end = store.record_local(event("end", at=NOW + timedelta(days=1))).record
    window = store.freeze_window(NOW, NOW + timedelta(days=1))
    page = store.read_window(window, limit=37)
    seen = [record.record_id for record in page.records]
    late = store.record_local(event("late-import", at=NOW + timedelta(hours=1))).record
    while not page.exhausted:
        page = store.read_window(window, cursor=page.next_cursor, limit=37)
        seen.extend(record.record_id for record in page.records)
    assert seen == expected
    assert not {before.record_id, end.record_id, late.record_id}.intersection(seen)
    assert len(collect(store, watermark=window.watermark, page_size=31).records) == 245
    assert len(collect(store, page_size=31).records) == 246


def test_owner_preset_isolation_and_unlabelled_database_cannot_be_rebound(tmp_path):
    store = SharedMemoryStore(tmp_path / "misty.sqlite3")
    store.record_local(event("one"))
    with pytest.raises(ValueError, match="matching owner and preset"):
        collect_source_window(store, owner_id="misty", preset_id="another", start=NOW, end=NOW + timedelta(days=1))
    with pytest.raises(SharedMemoryStorageError, match="different preset"):
        SharedMemoryStore(store.path, preset_id="another").freeze_window(NOW, NOW + timedelta(days=1))
    other = SharedMemoryStore(store.path, owner_id="another")
    assert collect(other).records == ()
    copy = SharedMemoryStore(tmp_path / "copy.sqlite3", preset_id="copy")
    copy.record_local(event("one"))
    assert collect(copy).records[0].preset_id == "copy"
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE shared_memory_identity")
    with pytest.raises(SharedMemoryStorageError, match="different preset"):
        SharedMemoryStore(store.path, preset_id="another").freeze_window(NOW, NOW + timedelta(days=1))


def test_legacy_store_gets_index_without_rewriting_original_json(tmp_path):
    store = SharedMemoryStore(tmp_path / "legacy.sqlite3")
    record = store.record_local(event("old")).record
    with sqlite3.connect(store.path) as db:
        original = db.execute("SELECT record_json FROM shared_memory_entries").fetchone()[0]
        db.execute("DROP TABLE shared_memory_source_index")
        db.execute("DROP TABLE shared_memory_identity")
    result = collect(store)
    assert result.records[0].source_id == record.record_id
    assert "local_chat_history_before_continuous_archive" in result.coverage_gaps
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT record_json FROM shared_memory_entries").fetchone()[0] == original


def test_archive_callback_preserves_turns_beyond_session_window_and_separates_presets(tmp_path):
    sessions = SessionStore(window=2)
    chat = SimpleNamespace(active_preset_id="misty", active_session_id="local-one", pipeline=SimpleNamespace(session_store=sessions))
    shared = CognitiveSharedMemory(SimpleNamespace(chat=chat), tmp_path / "shared.sqlite3")
    for i in range(7):
        shared.archive_local(event_id=f"turn-{i}", session_id="local-one", role="user",
                             text=f"消息{i}", occurred_at=NOW)
        sessions.append("local-one", "user", f"消息{i}", event_id=f"turn-{i}", timestamp=NOW)
    assert len(sessions.recent("local-one")) == 2
    assert len(collect(shared.store).records) == 7
    assert shared.sync_local() == 0
    old_path = shared.store.path
    chat.active_preset_id = "another"
    chat.active_session_id = "another-session"
    assert shared.store.path != old_path
    assert shared.sync_local() == 0
    assert collect(shared.store).records == ()
    with pytest.raises(ValueError, match="active_preset"):
        shared.archive_local(event_id="wrong", session_id="local-one", role="user", text="旧人格消息", occurred_at=NOW)


def test_real_action_receipt_retained_and_self_expression_is_not_independent(tmp_path):
    store = SharedMemoryStore(tmp_path / "actions.sqlite3")
    evidence = ActionOutcomeEvidence(outcome_id="outcome-one", prepare_id="prepare-one", goal_id="goal-one",
        observed_at=NOW, success=True, summary="MODEL_PROSE_MUST_NOT_BECOME_FACT", evidence_refs=["verifier:one"])
    action = SharedActionOutcome(event_id="prepare-one:action_outcome", session_id="local-one", intent_id="move", evidence=evidence)
    record = store.record_action(action).record
    store.record_local(event("self-summary", role="assistant"))
    sources = collect(store).records
    actual = next(source for source in sources if source.kind == "action_outcome")
    assert actual.authority == "verified_action_outcome"
    assert "MODEL_PROSE" not in actual.text
    assert json.loads(record.source.original_json)["evidence"]["outcome_id"] == "outcome-one"
    assert all(source.self_generated and not source.independent for source in sources)
    assert actual.scope.visibility == ["local:main"]
    assert store.query(SharedReadScope(audience="qq:group:100")).records == ()
    with pytest.raises(ValueError, match="verifier evidence"):
        store.record_action(action.model_copy(update={"evidence": evidence.model_copy(update={"evidence_refs": []})}))


def test_qq_uses_same_evidence_identity_as_existing_online_assignment(tmp_path):
    store = SharedMemoryStore(tmp_path / "shared.sqlite3")
    first_context = person(suffix="old")
    first_context = first_context.model_copy(update={"observations": [first_context.observations[0].model_copy(update={"observed_at": NOW - timedelta(days=1)})]})
    first = next(item.record for item in store.import_qq_context(first_context) if item.record.kind == "qq_observation")
    old = QQSource.from_record(first, self_id="999")
    queue = QQAssimilationQueue(store.path, owner_id="misty", preset_id="misty", self_id="999", allowed_group_ids=("100",))
    queue.enqueue(sources=(old,), now=NOW, backfill=True)
    new_context = person(suffix="new", message_id="123", message_id_source="qq_metadata")
    new_context = new_context.model_copy(update={"observations": [new_context.observations[0].model_copy(update={"observed_at": NOW})]})
    newer = next(item.record for item in store.import_qq_context(new_context) if item.record.kind == "qq_observation")
    result = collect(store, self_member_id="999")
    source = next(item for item in result.records if item.source_id == newer.record_id)
    assert source.evidence_keys == (old.independent_key,)
    assert not source.independent
    assert source.relation_target == "qq:100:200"
    assert source.scope.conversation_id == "qq:group:100"


def test_scope_partitions_keep_private_peer_and_group_separate(tmp_path):
    store = SharedMemoryStore(tmp_path / "scopes.sqlite3")
    for group in ("100", "private:999:200"):
        add_message(store, group=group, seconds=int((NOW - QQ_NOW).total_seconds()))
    store.record_local(event("local"))
    result = collect(store, self_member_id="999")
    partitions = partition_sources(result.records)
    assert len(partitions) == 3
    assert {group[0].scope.channel for group in partitions} == {"local_chat", "qq_group", "qq_private"}
    private = next(group[0] for group in partitions if group[0].scope.channel == "qq_private")
    assert private.relation_target == "qq:private:999:200"
    assert private.scope.visibility == ["local:main", "qq:private:999:200"]
    missing_account = collect(store)
    assert "qq_observations_excluded_without_authenticated_self_account" in missing_account.coverage_gaps


def test_coverage_boundary_reports_partial_history_and_clear_complete_future_window(tmp_path):
    store = SharedMemoryStore(tmp_path / "coverage.sqlite3")
    for kind in ("local_chat", "action_outcome"):
        store.mark_archive_started(kind, at=NOW)
    assert collect(store).coverage_gaps == ()
    assert store.freeze_window(NOW - timedelta(days=1), NOW).coverage_gaps
    with pytest.raises(ValueError):
        store.freeze_window(NOW.replace(tzinfo=None), NOW + timedelta(days=1))
    with pytest.raises(ValueError):
        store.read_window(store.freeze_window(NOW, NOW + timedelta(days=1)), limit=0)
