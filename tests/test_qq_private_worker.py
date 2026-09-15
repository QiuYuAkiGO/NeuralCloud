"""Synthetic private ingress and owner recall through the real Cognitive owner."""

from __future__ import annotations

import asyncio
from datetime import timedelta
import json
import os
import threading
from types import SimpleNamespace

import pytest

from harness.cognitive.qq_memory import QQMemoryControl, install_committed, memory_state, snapshot_owner
from harness.core.models import EpisodicEvent, MemoryScope, SemanticFact
from harness.memory.qq_assimilation import QQMemoryProposal
from harness.qq.conversation import conversation_audience, private_scope_id
from tests.test_basic_persona_continuity import _host
from tests.test_qq_assimilation import NOW, add_message, impression, proposal


SELF, OWNER, FRIEND = "999", "2285027817", "201"
PRIVATE = private_scope_id(SELF, OWNER)
FRIEND_PRIVATE = private_scope_id(SELF, FRIEND)
MOMENT = NOW + timedelta(hours=3)


class Proposer:
    def __init__(self):
        self.calls = []

    def propose(self, job, **kwargs):
        self.calls.append(job)
        proposed = proposal(job, impressions=[impression(job,
            subject_refs=[job.sources[0].member_id], statement="暂时觉得对方愿意认真解释。")])
        raw = proposed.model_dump()
        raw["episodes"][0].update(summary="对方认真解释了问题的步骤。", importance=0.8)
        return QQMemoryProposal.model_validate(raw)


def setup_host(tmp_path, monkeypatch, *, groups=("100",), peers=(OWNER, FRIEND)):
    # No inherited production paths, controls, model credentials, or endpoints.
    for key in tuple(os.environ):
        if key.startswith("NC_"):
            monkeypatch.delenv(key)
    host, _ = _host(tmp_path, monkeypatch, [])
    host.shared.qq_settings = SimpleNamespace(memory_db=tmp_path / "unused-qq.sqlite3",
        allowed_group_ids=groups, allowed_private_peer_ids=peers, self_member_id=SELF)
    host.shared.sync_qq = lambda: None
    host.qq_memory.control = QQMemoryControl(enabled=True, recall_enabled=True,
        private_enabled=True, private_recall_enabled=True)
    host.qq_memory.proposer = Proposer()
    monkeypatch.setattr("harness.cognitive.qq_memory.now_utc", lambda: MOMENT)
    monkeypatch.setattr("harness.memory.qq_assimilation._now", lambda value=None: value or MOMENT)
    return host


def seed_source(host, suffix="private", *, group=PRIVATE, member=OWNER, seconds=0):
    return add_message(host.shared.store, suffix, group=group, member=member,
        seconds=seconds, message_id=str(seconds + 1), text="我会认真解释这个问题的步骤。")


def retained(identifier, *, conversation=None, channel="chat"):
    scope = None
    if conversation:
        audience = conversation_audience(conversation)
        channel = "qq_private" if conversation.startswith("private:") else "qq_group"
        scope = MemoryScope(channel=channel, conversation_id=audience, visibility=["local:main", audience])
    return EpisodicEvent(event_id=identifier, time=NOW, participants=[f"qq:member:{OWNER}"],
        summary=identifier, objective_fact=f"synthetic evidence {identifier}", importance=0.8,
        emotional_valence=0.0, evidence=[identifier], source_refs=[identifier],
        channel=channel, session_id=scope.conversation_id if scope else "local-test",
        scope=scope, tier="mid", strength_updated_at=NOW)


def install_records(host, *events, semantic=()):
    current = snapshot_owner(host)
    current = current.model_copy(update={"episodic": [*current.episodic, *events],
                                         "semantic": [*current.semantic, *semantic]})
    host.chat.library.save(current)
    install_committed(host, current)


def recalled_ids(payload):
    return {entry["record_id"] for entry in payload.get("entries", [])}


def note_adoption(host, decision_id, *, conversation=PRIVATE, shown="group-shown"):
    host.qq_memory.note_recall(SimpleNamespace(decision_id=decision_id, group_id=conversation,
        decision_source="model", outcome="ready", completed_at=MOMENT,
        recalled_memory_ids=[shown], recalled_episode_ids=[shown]))


def test_private_new_experiences_commit_to_same_scope_and_recall_without_duplicate_effects(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        seed_source(host)
        seed_source(host, "later", seconds=7200)
        try:
            for _ in range(2):
                await host.qq_memory.tick()
                assert host.qq_memory.last_error is None
            state = memory_state(host.chat.memory.qq_memory_state)
            assert len(state["receipts"]) == 2 and len(state["impressions"]) == 1
            current = next(iter(state["impressions"].values()))
            assert len(current["evidence"]) == 2 and current["confidence"] > 0.15
            events = host.chat.memory.stores.episodic.list_all()
            assert len(events) == 2
            for event in events:
                assert event.channel == event.scope.channel == "qq_private"
                assert event.scope.conversation_id == event.session_id == conversation_audience(PRIVATE)
                assert set(event.scope.visibility) == {"local:main", conversation_audience(PRIVATE)}
            recall = host.qq_memory.recall(group_id=PRIVATE, member_id=OWNER, query="认真解释")
            assert current["impression_id"] in recalled_ids(recall)
            assert recalled_ids(recall) <= {current["impression_id"], *(event.event_id for event in events)}
            before = host.chat.memory.qq_memory_state.copy()
            await host.qq_memory.tick()
            assert len(host.qq_memory.proposer.calls) == 2
            assert host.chat.memory.qq_memory_state == before
            assert host.chat.library.load("misty").qq_memory_state == before
        finally:
            await host.close()
    asyncio.run(run())


def test_owner_full_recall_requires_reserved_peer_and_explicit_control(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        local_fact = SemanticFact(fact_id="local-fact", subject="synthetic", predicate="likes", value="photography",
            confidence=0.8, supporting_events=["local-event"], formed_at=NOW)
        install_records(host, retained("own-private", conversation=PRIVATE),
            retained("friend-private", conversation=FRIEND_PRIVATE), retained("group", conversation="100"),
            retained("unauthorized-group", conversation="101"), retained("local-event"), semantic=[local_fact])
        try:
            assert recalled_ids(host.qq_memory.recall(group_id=PRIVATE)) == {"own-private"}
            host.qq_memory.configure({"owner_private_full_recall_enabled": True})
            expanded = host.qq_memory.recall(group_id=PRIVATE)
            ids = recalled_ids(expanded)
            assert {"own-private", "group", "local-event", "local-fact"} <= ids
            assert "unauthorized-group" not in ids
            assert expanded["coverage"] == "selected_owner_authorized_cross_conversation_memories"
            assert all("source_channel" in entry and "source_conversation_id" in entry for entry in expanded["entries"])
            assert len(expanded["entries"]) <= 8
            assert len(json.dumps(expanded, ensure_ascii=False, separators=(",", ":"))) <= 16000
            assert recalled_ids(host.qq_memory.recall(group_id=FRIEND_PRIVATE, member_id=FRIEND)) == {"friend-private"}
            assert recalled_ids(host.qq_memory.recall(group_id="100")) == {"group"}
            assert host.qq_memory.recall(group_id=private_scope_id("998", OWNER)) == {}
            assert host.qq_memory.recall(group_id=private_scope_id(SELF, "202")) == {}
            assert host.qq_memory.recall(group_id=PRIVATE, member_id=FRIEND) == {}
            host.qq_memory.configure({"owner_private_full_recall_enabled": False})
            assert recalled_ids(host.qq_memory.recall(group_id=PRIVATE)) == {"own-private"}
            host.qq_memory.configure({"private_recall_enabled": False})
            assert host.qq_memory.recall(group_id=PRIVATE) == {}
            assert recalled_ids(host.qq_memory.recall(group_id="100")) == {"group"}
        finally:
            await host.close()
    asyncio.run(run())


def test_private_disabled_does_not_enqueue_private_and_preserves_group_processing_and_status(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        seed_source(host)
        seed_source(host, "group", group="100", member="200")
        host.qq_memory.configure({"private_enabled": False})
        try:
            await host.qq_memory.tick()
            assert [job.group_id for job in host.qq_memory.proposer.calls] == ["100"]
            assert {job.group_id for job in host.qq_memory.queue.snapshot()} == {"100"}
            status = host.qq_memory.status()
            assert status["channels"]["qq_group"]["episodes"] == 1
            assert status["channels"]["qq_private"]["episodes"] == 0
            host.qq_memory.configure({"private_enabled": True})
            await host.qq_memory.tick()
            status = host.qq_memory.status()
            assert status["channels"]["qq_private"]["episodes"] == 1
            assert status["channels"]["qq_private"]["impressions"] == 1
            assert status["channels"]["qq_group"]["episodes"] == 1
            assert status["committed_windows"] == status["processed_observations"] == 2
            assert "认真解释" not in json.dumps(status, ensure_ascii=False)
            assert recalled_ids(host.qq_memory.recall(group_id="100")).isdisjoint(
                {row.event_id for row in host.chat.memory.stores.episodic.list_all() if row.channel == "qq_private"})
        finally:
            await host.close()
    asyncio.run(run())


def test_owner_private_adopts_only_shown_group_episode_once_and_friend_cannot_forge_adoption(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        host.qq_memory.configure({"owner_private_full_recall_enabled": True})
        install_records(host, retained("group-shown", conversation="100"),
                        retained("group-unshown", conversation="100"))
        try:
            assert "group-shown" in recalled_ids(host.qq_memory.recall(group_id=PRIVATE, query="group-shown"))
            note_adoption(host, "owner-adoption")
            await host.qq_memory.maintain(now=MOMENT)
            rows = {row.event_id: row for row in host.chat.memory.stores.episodic.list_all()}
            assert rows["group-shown"].hit_count == 1 and rows["group-unshown"].hit_count == 0
            note_adoption(host, "owner-adoption")
            note_adoption(host, "friend-forgery", conversation=FRIEND_PRIVATE)
            await host.qq_memory.maintain(now=MOMENT + timedelta(seconds=1))
            assert host.chat.memory.stores.episodic.get("group-shown").hit_count == 1
            assert host.chat.memory.qq_memory_state["recalls"]["owner-adoption"] == ["group-shown"]
            assert host.chat.memory.qq_memory_state["recalls"]["friend-forgery"] == []
        finally:
            await host.close()
    asyncio.run(run())


def test_owner_private_adoption_updates_local_hits_and_only_frozen_semantic_supports(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        host.qq_memory.configure({"owner_private_full_recall_enabled": True})
        host.chat.settings.qq_memory_recall_max_items = 2
        local = retained("local-focus").model_copy(update={
            "tier": "short", "hit_count": host.chat.settings.tier_promotion_hits_mid - 1,
            "expire_at": MOMENT + timedelta(hours=1)})
        support, unshown = retained("semantic-support"), retained("unshown-local")
        fact = SemanticFact(fact_id="local-fact", subject="local-focus", predicate="recall", value="local-focus",
            confidence=0.8, supporting_events=[support.event_id], formed_at=NOW)
        install_records(host, local, support, unshown, semantic=[fact])
        try:
            before = {row.event_id: row.model_dump() for row in host.chat.memory.stores.episodic.list_all()}
            payload = host.qq_memory.recall(group_id=PRIVATE, query="local-focus")
            assert recalled_ids(payload) == {local.event_id, fact.fact_id}
            assert {row.event_id: row.model_dump() for row in host.chat.memory.stores.episodic.list_all()} == before
            shown_fact = next(entry for entry in payload["entries"] if entry["record_id"] == fact.fact_id)
            assert shown_fact["supporting_episode_ids"] == [support.event_id]
            # A later supporting episode must not be pulled into an earlier
            # decision when its retained semantic judgment has changed.
            host.chat.memory.stores.semantic.update(fact.model_copy(update={
                "supporting_events": [support.event_id, unshown.event_id]}))
            adoption = SimpleNamespace(decision_id="local-semantic-adoption", group_id=PRIVATE,
                decision_source="model", outcome="ready", completed_at=MOMENT,
                recalled_memory_ids=[local.event_id, fact.fact_id],
                recalled_episode_ids=[local.event_id, *shown_fact["supporting_episode_ids"]])
            host.qq_memory.note_recall(adoption)
            await host.qq_memory.maintain(now=MOMENT)
            stored = host.chat.library.load("misty")
            after = {row.event_id: row for row in stored.episodic}
            assert after[local.event_id].tier == "mid"
            assert after[local.event_id].hit_count == local.hit_count + 1
            assert after[local.event_id].last_hit_at == MOMENT
            assert after[support.event_id].hit_count == 1 and after[unshown.event_id].hit_count == 0
            assert stored.qq_memory_state["recalls"][adoption.decision_id] == sorted([local.event_id, support.event_id])
            host.qq_memory.note_recall(adoption)
            await host.qq_memory.maintain(now=MOMENT + timedelta(seconds=1))
            assert {row.event_id: row.hit_count for row in host.chat.memory.stores.episodic.list_all()} == {
                key: row.hit_count for key, row in after.items()}
            assert host.chat.memory.stores.semantic.get(fact.fact_id).confidence == fact.confidence
        finally:
            await host.close()
    asyncio.run(run())


def test_owner_local_adoption_save_failure_leaves_live_memory_untouched_and_retry_counts_once(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        host.qq_memory.configure({"owner_private_full_recall_enabled": True})
        local = retained("local-shown").model_copy(update={
            "tier": "short", "hit_count": host.chat.settings.tier_promotion_hits_mid - 1,
            "expire_at": MOMENT + timedelta(hours=1)})
        install_records(host, local)
        note_adoption(host, "local-save-failure", shown=local.event_id)
        try:
            before = host.chat.memory.stores.episodic.get(local.event_id).model_dump()
            disk_before = host.chat.library.load("misty")
            with monkeypatch.context() as patch:
                patch.setattr(host.chat.library, "save", lambda _: (_ for _ in ()).throw(OSError("synthetic save failure")))
                with pytest.raises(OSError, match="synthetic save failure"):
                    await host.qq_memory.maintain(now=MOMENT)
            assert host.chat.memory.stores.episodic.get(local.event_id).model_dump() == before
            assert host.chat.library.load("misty") == disk_before
            assert "local-save-failure" not in memory_state(host.chat.memory.qq_memory_state)["recalls"]
            await host.qq_memory.maintain(now=MOMENT)
            assert host.chat.memory.stores.episodic.get(local.event_id).hit_count == local.hit_count + 1
            assert host.chat.memory.stores.episodic.get(local.event_id).tier == "mid"
            note_adoption(host, "local-save-failure", shown=local.event_id)
            await host.qq_memory.maintain(now=MOMENT + timedelta(seconds=1))
            assert host.chat.memory.stores.episodic.get(local.event_id).hit_count == local.hit_count + 1
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["private_off", "peer_revoked", "self_changed", "preset_changed"])
def test_private_proposal_cannot_commit_after_control_identity_or_preset_changes(tmp_path, monkeypatch, change):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        seed_source(host)
        started, release = threading.Event(), threading.Event()
        delegate = host.qq_memory.proposer

        def blocked(job, **kwargs):
            started.set()
            assert release.wait(10)
            return delegate.propose(job, **kwargs)

        host.qq_memory.proposer = SimpleNamespace(propose=blocked)
        worker = asyncio.create_task(host.qq_memory.tick())
        try:
            assert await asyncio.to_thread(started.wait, 5)
            if change == "private_off":
                host.qq_memory.configure({"private_enabled": False})
            elif change == "peer_revoked":
                host.shared.qq_settings.allowed_private_peer_ids = (FRIEND,)
            elif change == "self_changed":
                host.shared.qq_settings.self_member_id = "998"
            else:
                host.chat.active_preset_id = "other-preset"
            release.set()
            await worker
            assert not host.chat.memory.stores.episodic.list_all()
            assert not memory_state(host.chat.memory.qq_memory_state)["receipts"]
            assert host.qq_memory.last_error is not None
        finally:
            release.set()
            await worker
            host.chat.active_preset_id = "misty"
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("change", ["private_recall_off", "peer_revoked", "self_changed"])
def test_private_pending_adoption_cannot_survive_revoked_authorization(tmp_path, monkeypatch, change):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        install_records(host, retained("private-shown", conversation=PRIVATE))
        note_adoption(host, "revoked-adoption", shown="private-shown")
        try:
            if change == "private_recall_off":
                host.qq_memory.configure({"private_recall_enabled": False})
            elif change == "peer_revoked":
                host.shared.qq_settings.allowed_private_peer_ids = (FRIEND,)
            else:
                host.shared.qq_settings.self_member_id = "998"
            await host.qq_memory.maintain(now=MOMENT)
            assert host.chat.memory.stores.episodic.get("private-shown").hit_count == 0
        finally:
            await host.close()
    asyncio.run(run())


def test_changed_preset_never_receives_pending_private_adoption(tmp_path, monkeypatch):
    async def run():
        host = setup_host(tmp_path, monkeypatch)
        install_records(host, retained("private-shown", conversation=PRIVATE))
        note_adoption(host, "previous-preset", shown="private-shown")
        try:
            host.chat.active_preset_id = "other-preset"
            assert host.qq_memory.recall(group_id=PRIVATE) == {}
            await host.qq_memory.tick()
            assert host.chat.memory.stores.episodic.get("private-shown").hit_count == 0
            assert not memory_state(host.chat.memory.qq_memory_state)["recalls"]
        finally:
            host.chat.active_preset_id = "misty"
            await host.close()
    asyncio.run(run())
