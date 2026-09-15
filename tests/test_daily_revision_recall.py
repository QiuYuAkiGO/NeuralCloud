"""Daily conclusions reach real local and QQ compilation with scoped evidence."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from gui.presets import hydrate_memory
from harness.cognitive.shared_memory import SharedMemoryCompiler
from harness.config import Settings
from harness.core.inference import SituationState, SituationTrigger, SituationActor
from harness.core.models import InteractionEvent
from harness.memory.revision.ledger import install_revision
from harness.memory.revision.models import MemoryRevision
from harness.memory.revision.recall import select_daily_recall, daily_context_segment, merge_derived_memory
from harness.memory.revision.reducer import build_revision, rollback_revision
from harness.memory.service import build_inmemory_service
from tests.test_basic_persona_continuity import _host
from tests.test_daily_revision_reducer import NOW, preset, source, run, proposal, impression


def memory_from(value):
    memory = build_inmemory_service()
    hydrate_memory(memory, value)
    return memory


def test_long_daily_interpretation_survives_whole_record_compilation():
    statement = "对方分享了摄影的过程，倾向于通过具体例子交流。" * 20
    changed = build_revision(preset(), run(), proposal(impressions=[impression(statement=statement)]), Settings(), now=NOW)
    memory = memory_from(changed)
    try:
        payload = select_daily_recall(memory, audience="local:main", query="摄影")
        segment = daily_context_segment(payload)
        assert segment is not None and len(segment.text) > 1000
        situation = SituationState(trigger=SituationTrigger(ref="current-turn"), utterance="还记得摄影吗？", actor=SituationActor())
        compiled = SharedMemoryCompiler(Settings(context_token_budget=500)).compile(situation, additional_segments=[segment])
        retained = next(row for row in compiled.segments if row.why == "daily_revision_memory")
        assert json.loads(retained.text)["derived_memory"]["entries"][0]["current_impression"] == statement
        entries = compiled.memory_evidence.entries
        assert entries and all(row.kind == "derived_memory" and row.status == "provisional" for row in entries)
        assert entries[0].speaker_id == "character:misty"
        assert "s1" in entries[0].source_refs
    finally:
        memory.close()


def test_local_candidates_are_recalled_without_embedding_and_rollback_removes_them():
    changed = build_revision(preset(), run(), proposal(memories=[MemoryRevision(kind="semantic",
        subject="local:main", predicate="兴趣", value="摄影", statement="对方表示喜欢摄影", evidence_ids=["s1"])]),
        Settings(), now=NOW)
    memory = memory_from(changed)
    restored = memory_from(rollback_revision(changed, "r1", now=NOW))
    try:
        assert not changed.semantic and changed.candidates
        entries = select_daily_recall(memory, audience="local:main", query="摄影")["entries"]
        assert entries and entries[0]["kind"] == "candidate"
        assert select_daily_recall(restored, audience="local:main", query="摄影") == {}
    finally:
        memory.close()
        restored.close()


def test_daily_recall_requires_exact_qq_destination_and_whole_budget():
    records = [source("group-source", author_principal="qq:member:20001", kind="qq_observation",
        scope={"owner_id": "misty", "channel": "qq_group", "conversation_id": "qq:group:10001",
               "visibility": ["local:main", "qq:group:10001"]}, relation_target=None)]
    changed = build_revision(preset(), run(sources=records), proposal(records,
        impressions=[impression(identifier="group-source", subject_refs=["qq:member:20001"])]), Settings(), now=NOW)
    memory = memory_from(changed)
    try:
        assert select_daily_recall(memory, audience="qq:group:10001") == {}
        assert select_daily_recall(memory, audience="qq:group:10002", conversation_id="qq:group:10002") == {}
        assert select_daily_recall(memory, audience="qq:group:10001", conversation_id="qq:group:10001", max_characters=100) == {}
        payload = select_daily_recall(memory, audience="qq:group:10001", conversation_id="qq:group:10001")
        assert payload["entries"] and payload["entries"][0]["source_conversation_id"] == "qq:group:10001"
        merged = merge_derived_memory(payload, payload, max_items=2, max_characters=6000)
        assert len({row["record_id"] for row in merged["entries"]}) == len(merged["entries"])
        assert len(json.dumps(merged, ensure_ascii=False, separators=(",", ":"))) <= 6000
    finally:
        memory.close()


def test_local_and_qq_facades_use_daily_recall_while_online_qq_recall_is_off(tmp_path, monkeypatch):
    async def scenario():
        monkeypatch.delenv("NC_DAILY_REVISION_CONFIG", raising=False)
        host, _ = _host(tmp_path, monkeypatch, ["嗯。"])
        try:
            current = host.chat.library.load("misty")
            local = build_revision(current, run(), proposal(impressions=[impression()]), Settings(), now=NOW)
            host.chat.library.save(local)
            install_revision(host, local)
            event = InteractionEvent(event_id="current", channel="chat", speaker="user", raw_text="摄影", session_id="session1")
            local_segments = host.shared.local_context(event)
            assert any(segment.why == "daily_revision_memory" for segment in local_segments)
            records = [source("group-source", author_principal="qq:member:20001", kind="qq_observation",
                scope={"owner_id": "misty", "channel": "qq_group", "conversation_id": "qq:group:10001",
                       "visibility": ["local:main", "qq:group:10001"]}, relation_target=None)]
            changed = build_revision(local, run("group-run", sources=records), proposal(records,
                impressions=[impression(identifier="group-source", subject_refs=["qq:member:20001"])]), Settings(), now=NOW)
            host.chat.library.save(changed)
            install_revision(host, changed)
            host.shared.qq_settings = SimpleNamespace(memory_db=tmp_path / "not-created.sqlite3",
                allowed_group_ids=["10001"], allowed_private_peer_ids=[], self_member_id="29999")
            assert not host.qq_memory.control.recall_enabled
            binding = host.shared.qq_binding(group_id="10001", member_id="20001", query="摄影")
            assert binding.derived_memory["entries"]
            assert all(row["source_conversation_id"] == "qq:group:10001" for row in binding.derived_memory["entries"])
            assert not host.shared.qq_binding(group_id="10002", query="摄影").derived_memory
        finally:
            await host.close()
    asyncio.run(scenario())
