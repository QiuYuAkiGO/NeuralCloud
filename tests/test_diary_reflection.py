"""Diary coverage, restart caching, evidence fences and semantic decisions."""
import asyncio
from datetime import datetime, timedelta, timezone
import hashlib
from types import SimpleNamespace
import time

import pytest

from harness.cognitive.diary_reflection import DiaryReflectionWorker, Expression, Review, Summary, prepare
from harness.diary.reflection import ReflectionStore, batches, read_sources, source_boundary, source_events
from harness.diary.store import DiaryStore
from harness.diary.models import DiaryOriginal
from harness.memory.shared import SharedLocalEvent, SharedMemoryStore
from tests.test_diary_controller import rig  # noqa: F401


def incoming(store, identifier, text="新经历", at=None):
    return store.record_local(SharedLocalEvent(event_id=identifier, session_id="local:test",
        author_principal="local:main", role="user", text=text,
        occurred_at=at or datetime.now(timezone.utc), evidence_refs=[identifier])).record


def host_for(store):
    shared = SimpleNamespace(store=store, production_enabled=False, sync_qq=lambda: None, sync_local=lambda: None)
    return SimpleNamespace(shared=shared, chat=SimpleNamespace(llm=object(), memory=SimpleNamespace(
        get_persona=lambda: SimpleNamespace(model_dump=lambda **kw: {}))),
        adapter=SimpleNamespace(_maintenance_paused=False, command_tasks=()))


def test_watermark_late_records_and_no_consumption_on_wait(tmp_path):
    source = SharedMemoryStore(tmp_path / "shared.sqlite3")
    incoming(source, "old", at=datetime.now(timezone.utc)-timedelta(days=4))
    cache = ReflectionStore(tmp_path / "reflection.sqlite3")
    basis = {"written_at": datetime.now(timezone.utc).isoformat()}
    boundary = source_boundary(source, cache, basis)
    assert read_sources(source, boundary)[0] == []
    late = incoming(source, "late", at=datetime.now(timezone.utc)-timedelta(days=5))
    extended = source_boundary(source, cache, basis)
    assert extended["start"] == boundary["start"]
    assert [r.record_id for r in read_sources(source, extended)[0]] == [late.record_id]
    assert read_sources(source, boundary)[0] == []  # Frozen upper boundary.
    assert source_boundary(source, cache, {"coverage": extended})["start"] == extended["end"]


def test_no_history_covers_whole_archive_and_long_inputs(tmp_path):
    source = SharedMemoryStore(tmp_path / "shared.sqlite3")
    for i in range(70):
        incoming(source, f"e{i}", "字" * (15000 if i == 0 else 10),
                 datetime(2026, 10, 1, 15, 59, tzinfo=timezone.utc)+timedelta(minutes=i))
    cache = ReflectionStore(tmp_path / "reflection.sqlite3")
    records, _ = read_sources(source, source_boundary(source, cache, {}))
    events = source_events(records)
    groups = list(batches(events))
    assert len(records) == 70
    assert all(len(g) <= 64 and sum(len(e["text"]) for e in g) <= 12000 for g in groups)
    assert sum(len(e["text"]) for g in groups for e in g) == 15000+69*10
    assert all(len({e["day"] for e in g}) == 1 for g in groups)
    assert len(groups) >= 3


def test_cache_restart_and_permissions_invalidate_ready_job(tmp_path):
    async def run():
        source = SharedMemoryStore(tmp_path / "shared.sqlite3")
        incoming(source, "fresh")
        host = host_for(source)
        first = DiaryReflectionWorker(host)
        calls = []
        async def ask(*args, **kw):
            calls.append(1)
            return Summary(text="用户提供新经历。", source_ids=["r0"])
        first.ask = ask
        job = first.enqueue({})
        await first.process(first.store, job)
        assert job["status"] == "ready"
        second = DiaryReflectionWorker(host)
        second.ask = ask
        resumed = second.enqueue({})
        await second.process(second.store, resumed)
        assert len(calls) == 1
        assert second.valid(resumed)
        second.allowed = lambda ids: []
        assert not second.valid(resumed)
    asyncio.run(run())


@pytest.mark.parametrize("reason,grounded,changed,expected", [
    ("changed_reflection", True, True, "write"),
    ("new_experience", True, True, "wait"),
    ("repetition", True, False, "wait"),
    ("unsupported", False, True, "wait"),
    ("uncertain", False, False, "wait"),
])
def test_semantic_review_allows_change_rejects_rewording_and_invented_actions(tmp_path, reason, grounded, changed, expected):
    async def run():
        source = SharedMemoryStore(tmp_path / "shared.sqlite3")
        host = host_for(source)
        w = host.diary_reflection = DiaryReflectionWorker(host)
        basis = {"recent": [{"entry_id": "diary:old", "text": "想听一首新歌。", "written_at": "2026-10-01T00:00:00Z"}]}
        job = w.enqueue(basis)
        await w.process(w.store, job)  # No sources still permits grounded internal reflection.
        async def start():
            pass
        w.start = start
        async def ask(phase, *args, **kw):
            if phase == "expression":
                return Expression(action="write", text="这段时间我对那件旧事有了不同看法。", source_refs=[],
                    revisited_diary_ids=["diary:old"], reflection_delta="现在理解了当时的犹豫。")
            return Review(unsupported_claims=[], grounded=grounded, meaningful_change=changed, reason=reason)
        w.ask = ask
        now = datetime.now(timezone.utc)
        req = SimpleNamespace(reflection_basis=basis, entry_id="diary:test", source_event_id="event:test", day="2026-10-08")
        result = await prepare(host, SimpleNamespace(command_id="cmd:test"), req, now+timedelta(seconds=30), "有了新的想法", started=time.monotonic())
        assert result["decision"]["action"] == expected
        if expected == "wait":
            assert result["decision"]["text"] == ""
            assert w.store.get("reflection_jobs", job["id"])["status"] == "wait"
    asyncio.run(run())


def test_unknown_evidence_never_reaches_reviewer(tmp_path):
    async def run():
        source = SharedMemoryStore(tmp_path / "shared.sqlite3")
        host = host_for(source)
        w = host.diary_reflection = DiaryReflectionWorker(host)
        job = w.enqueue({})
        await w.process(w.store, job)
        async def start(): pass
        w.start = start
        async def ask(phase, *args, **kw):
            assert phase == "expression"
            return Expression(action="write", text="我听完了歌", source_refs=["invented"], revisited_diary_ids=[], reflection_delta="新经历")
        w.ask = ask
        req = SimpleNamespace(reflection_basis={}, entry_id="diary:test", source_event_id="event:test", day="2026-10-08")
        result = await prepare(host, SimpleNamespace(command_id="cmd:test"), req,
            datetime.now(timezone.utc)+timedelta(seconds=30), "", started=time.monotonic())
        assert result["decision"]["reflection"]["reason"] == "unknown_evidence"
        assert result["decision"]["action"] == "wait"
    asyncio.run(run())


def test_atomic_coverage_and_original_body_survive_reopen(tmp_path):
    path = tmp_path / "diary.sqlite3"
    store = DiaryStore(path)
    original = DiaryOriginal(entry_id="diary:test", decision_id="decision:test", text="真实记录",
        body_hash=hashlib.sha256("真实记录".encode()).hexdigest(), written_at=datetime.now(timezone.utc), source_event_id="event:test")
    evidence = {"coverage": {"start": 2, "end": 8}}
    store.put(original, day="2026-10-08", revision=1, reflection=evidence)
    store.close()
    restored = DiaryStore(path)
    assert restored.reflection_basis()["coverage"] == evidence["coverage"]
    assert restored.get(original.entry_id)["body"] == original.text
    assert restored.contains_body(original.text)
    restored.close()


def test_runtime_recovers_existing_body_without_generation_or_second_write(rig):  # noqa: F811
    async def run():
        diary, supervisor = rig
        diary.settings = diary.settings.model_copy(update={"reflection_enabled": True})
        original_commit = supervisor.cognitive.commit_diary_entry
        async def fail(command): raise RuntimeError("transport")
        supervisor.cognitive.commit_diary_entry = fail
        await diary.request({"request_id": "recovery"})
        event = supervisor.events[-1]
        await diary.dispatch(event, 1)
        assert diary.store.count(diary._day()) == 1
        supervisor.cognitive.commit_diary_entry = original_commit
        await diary.dispatch(event, 2)
        assert len(supervisor.cognitive.prepare_calls) == 1
        assert diary.store.count(diary._day()) == 1
        assert diary.turns.all()[0]["status"] == "committed"
    asyncio.run(run())


def test_protocol_dedup_preserves_distinct_messages_and_aggregate_lineage():
    import json
    now = datetime.now(timezone.utc)
    def record(identifier, kind, text, raw):
        return SimpleNamespace(record_id=identifier, kind=kind, text=text, session_id="qq:group:1",
            author_principal="character:misty", occurred_at=now, source=SimpleNamespace(original_json=json.dumps(raw)))
    rows = [record("segment", "qq_segment_delivery", "hello", {"verification": "qq_protocol_verified", "protocol_message_id": "m1"}),
        record("aggregate", "qq_delivery", "hello\nworld", {"verification": "qq_protocol_verified",
            "protocol_message_ids": ["m1", "m2"], "delivered_segments": ["hello", "world"]}),
        record("observation", "qq_observation", "hello", {"author_attribution": "qq_protocol_verified", "message_id": "m1"}),
        record("other", "qq_observation", "hello", {"author_attribution": "qq_protocol_verified", "message_id": "m3"})]
    events = source_events(rows)
    assert len(events) == 3
    assert sum(len(e["source_ids"]) for e in events) == 5
    assert len([e for e in events if e["text"] == "hello"]) == 2


def test_completed_batches_survive_later_batch_failure(tmp_path):
    async def run():
        source = SharedMemoryStore(tmp_path / "shared.sqlite3")
        incoming(source, "a", "a"*15000)
        w = DiaryReflectionWorker(host_for(source))
        job = w.enqueue({})
        calls = []
        async def ask(*args, **kw):
            calls.append(1)
            if len(calls) == 2:
                raise TimeoutError()
            return Summary(text="a supplied experience", source_ids=["r0"])
        w.ask = ask
        with pytest.raises(TimeoutError):
            await w.process(w.store, job)
        await w.process(w.store, job)
        assert len(calls) == 3  # First leaf came from cache.
        assert job["status"] == "ready"
    asyncio.run(run())


def test_background_does_not_hold_owner_lock(tmp_path):
    async def run():
        source = SharedMemoryStore(tmp_path / "shared.sqlite3")
        incoming(source, "a")
        host = host_for(source)
        host.adapter._lock = asyncio.Lock()
        w = DiaryReflectionWorker(host)
        started, release = asyncio.Event(), asyncio.Event()
        async def ask(*args, **kw):
            assert not host.adapter._lock.locked()
            started.set()
            await release.wait()
            return Summary(text="new event", source_ids=["r0"])
        w.ask = ask
        task = asyncio.create_task(w.process(w.store, w.enqueue({})))
        await started.wait()
        async with host.adapter._lock:
            assert not task.done()
        release.set()
        await task
    asyncio.run(run())
