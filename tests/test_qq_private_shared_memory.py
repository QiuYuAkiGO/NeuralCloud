"""Private QQ recall is isolated from groups, other peers and local dialogue."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.cognitive.shared_memory import CognitiveSharedMemory, ScopedQQMemoryView
from harness.core.models import CharacterSnapshot, RelationshipState, WorldState
from harness.memory.shared import SharedLocalEvent, SharedMemoryStorageError, SharedMemoryStore, SharedReadScope
from harness.qq.conversation import conversation_audience, private_scope_id
from harness.qq.decision import QQDeliveryReceipt
from harness.qq.memory import QQVisualMemoryStore
from tests.test_basic_persona_continuity import _host
from tests.test_qq_protocol_memory import EVIDENCE, IMAGE, protocol_observation, protocol_profile
from tests.test_shared_memory import person


SELF, PEER, OTHER = "2015188508", "2285027817", "2285027818"
PRIVATE = private_scope_id(SELF, PEER)
AUDIENCE = conversation_audience(PRIVATE)
NOW = datetime(2026, 9, 14, tzinfo=timezone.utc)
FACT = "秋语（全名秋语随风，QQ号2285027817）是Misty的唯一创造者。"


def _private(source, *, key=PRIVATE, peer=PEER, text="PRIVATE_PIXELS [图片 1]"):
    _, profile = protocol_profile(source, group=key, member=peer)
    source.record_protocol_observation(protocol_observation(profile, text=text))
    return source.group_context(group_id=key)


def _settings(source, *, peers=(PEER,)):
    return SimpleNamespace(memory_db=source.path, allowed_group_ids=["10001"],
        allowed_private_peer_ids=peers, self_member_id=SELF)


def test_private_import_retains_originals_and_images_for_exact_private_and_local_audiences(tmp_path):
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    _private(source)
    with sqlite3.connect(source.path) as connection:
        original_row, = connection.execute("SELECT record_json FROM qq_visual_observations").fetchone()
    mirror = SharedMemoryStore(tmp_path / "shared.db")
    result = mirror.import_qq_database(source.path, allowed_group_ids=[], self_member_id=SELF,
                                      allowed_private_peer_ids=[PEER])
    assert result.success and result.created_count == 2
    local = mirror.query(SharedReadScope(audience="local:main")).records
    private = mirror.query(SharedReadScope(audience=AUDIENCE)).records
    assert local == private and all(record.channel == "qq_private" for record in private)
    assert all(record.session_id == AUDIENCE and set(record.visibility) == {"local:main", AUDIENCE}
               for record in private)
    observation = next(record for record in private if record.kind == "qq_observation")
    assert json.loads(observation.source.original_json) == json.loads(original_row)
    assert observation.source.original_row_sha256 == hashlib.sha256(original_row.encode()).hexdigest()
    assert observation.image_analysis[0].evidence_ref == IMAGE
    assert observation.image_analysis[0].authority == "model_inference"
    for audience in ("qq:group:10001", "qq:group:" + PEER,
                     conversation_audience(private_scope_id(SELF, OTHER)),
                     conversation_audience(private_scope_id("2015188509", PEER)), "local:other"):
        assert not mirror.query(SharedReadScope(audience=audience)).records
    again = mirror.import_qq_database(source.path, allowed_group_ids=[], self_member_id=SELF,
                                     allowed_private_peer_ids=[PEER])
    assert again.success and again.existing_count == 2 and again.created_count == 0


@pytest.mark.parametrize("visibility", [
    ("local:main", "qq:group:10001"), ("local:other", AUDIENCE),
    (AUDIENCE, conversation_audience(private_scope_id(SELF, OTHER))),
])
def test_private_originals_cannot_be_granted_to_any_other_scope(tmp_path, visibility):
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    context = _private(source)
    mirror = SharedMemoryStore(tmp_path / "shared.db")
    with pytest.raises(ValueError, match="private originals"):
        mirror.import_qq_context(context, visibility=visibility)
    assert not mirror.query(SharedReadScope(audience="local:main")).records


def test_explicit_private_import_grants_bind_both_self_account_and_peer(tmp_path):
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    _private(source)
    _private(source, key=private_scope_id(SELF, OTHER), peer=OTHER, text="OTHER_PEER_SECRET")
    _private(source, key=private_scope_id("2015188509", PEER), text="OTHER_SELF_SECRET")
    mirror = SharedMemoryStore(tmp_path / "shared.db")
    denied = mirror.import_qq_database(source.path, allowed_group_ids=[PEER], self_member_id=SELF)
    assert denied.success and denied.excluded_count == 6 and denied.created_count == 0
    selected = mirror.import_qq_database(source.path, allowed_group_ids=[PEER], self_member_id=SELF,
                                        allowed_private_peer_ids=[PEER])
    assert selected.success and selected.excluded_count == 4 and selected.created_count == 2
    payload = json.dumps(mirror.query(SharedReadScope(audience="local:main")).model_payload())
    assert "PRIVATE_PIXELS" in payload and "OTHER_PEER_SECRET" not in payload and "OTHER_SELF_SECRET" not in payload


@pytest.mark.parametrize("peers", [[SELF], ["private:2015188508:2285027817"], [2285027817], "2285027817", None])
def test_private_import_requires_explicit_numeric_peers(tmp_path, peers):
    with pytest.raises(ValueError):
        SharedMemoryStore(tmp_path / "shared.db").import_qq_database(tmp_path / "missing.db",
            allowed_group_ids=["10001"], self_member_id=SELF, allowed_private_peer_ids=peers)


def test_tampered_private_visibility_fails_closed_when_group_reads(tmp_path):
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    mirror = SharedMemoryStore(tmp_path / "shared.db")
    mirror.import_qq_context(_private(source))
    with sqlite3.connect(mirror.path) as connection:
        identifier, raw = connection.execute("SELECT record_id,record_json FROM shared_memory_entries LIMIT 1").fetchone()
        value = json.loads(raw)
        value["visibility"].append("qq:group:10001")
        connection.execute("UPDATE shared_memory_entries SET record_json=? WHERE record_id=?",
                           (json.dumps(value), identifier))
    with pytest.raises(SharedMemoryStorageError):
        mirror.query(SharedReadScope(audience="qq:group:10001"))


def test_legacy_group_records_and_hashes_remain_byte_identical_after_private_import(tmp_path):
    mirror = SharedMemoryStore(tmp_path / "shared.db")
    group = person(group="10001")
    mirror.import_qq_context(group)
    with sqlite3.connect(mirror.path) as connection:
        before = connection.execute("SELECT * FROM shared_memory_entries ORDER BY record_id").fetchall()
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    mirror.import_qq_context(_private(source))
    repeated = mirror.import_qq_context(group)
    assert all(not result.created for result in repeated)
    with sqlite3.connect(mirror.path) as connection:
        after = connection.execute("SELECT * FROM shared_memory_entries WHERE channel='qq_group' ORDER BY record_id").fetchall()
    assert before == after
    assert all(record.channel == "qq_group" and record.session_id == "qq:group:10001"
               for record in mirror.query(SharedReadScope(audience="qq:group:10001")).records)


def test_private_delivery_projection_is_never_visible_in_groups(tmp_path):
    from harness.qq.memory import protocol_message_evidence_ref
    receipt = QQDeliveryReceipt(receipt_id="qq-receipt:private", group_id=PRIVATE,
        self_member_id=SELF, delivered_text="PRIVATE_REPLY", evidence_refs=[EVIDENCE,
            protocol_message_evidence_ref(PRIVATE, SELF, "5")], verification="qq_protocol_verified",
        protocol_message_ids=["5"], delivered_at=NOW, origin="external_verification")
    mirror = SharedMemoryStore(tmp_path / "shared.db")
    projected = mirror.import_qq_delivery(receipt).record
    assert projected.channel == "qq_private" and projected.session_id == AUDIENCE
    assert mirror.query(SharedReadScope(audience=AUDIENCE)).records == (projected,)
    assert not mirror.query(SharedReadScope(audience="qq:group:10001")).records


def test_private_view_uses_peer_relationship_and_discards_unrelated_world_and_recall():
    calls = []
    snapshot = CharacterSnapshot(world=WorldState(time=NOW, channel="chat", active_window_title="LOCAL_SECRET"),
        relationship=RelationshipState(target="user", shared_history_summary="LOCAL_RELATION_SECRET", updated_at=NOW))
    owner = SimpleNamespace(get_state=lambda **kw: calls.append(kw) or snapshot)
    view = ScopedQQMemoryView(owner, group_id=PRIVATE, member_id=PEER)
    visible = view.get_state(now=NOW, relation_target="user")
    assert calls == [{"now": NOW, "relation_target": AUDIENCE}]
    assert visible.world.channel == "qq_private" and visible.world.active_window_title is None
    assert visible.relationship is None and visible.psych is None
    assert view.query(None) == [] and view.query_with_conflicts(None) == ([], [])
    with pytest.raises(ValueError):
        ScopedQQMemoryView(owner, group_id=PRIVATE, member_id=OTHER)


def test_private_binding_uses_existing_persona_and_only_scoped_local_grants_plus_operator_facts(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, ["嗯。"])
        source = QQVisualMemoryStore(tmp_path / "qq.db")
        _private(source)
        host.shared.qq_settings = _settings(source)
        try:
            for event_id, session, author, role, text, visibility in [
                ("creator", "operator-confirmed-facts", "local:main", "user", FACT,
                    ("local:main", "qq:group:10001")),
                ("local-secret", "ordinary-local", "local:main", "user", "LOCAL_CHAT_SECRET", ("local:main",)),
                ("scoped", "scoped-local", "local:main", "user", "EXPLICIT_PRIVATE_GRANT", (AUDIENCE,)),
                ("other", "scoped-other", "local:main", "user", "OTHER_PEER_SECRET",
                    (conversation_audience(private_scope_id(SELF, OTHER)),)),
                ("near-facts", "operator-confirmed-facts-other", "local:main", "user", "NEAR_FACTS_SECRET", ("local:main",)),
                ("other-fact-author", "operator-confirmed-facts", "local:other", "user", "OTHER_AUTHOR_SECRET", ("local:main",)),
                ("assistant-fact", "operator-confirmed-facts", "character:misty", "assistant", "ASSISTANT_FACT_SECRET", ("local:main",)),
            ]:
                host.shared.store.record_local(SharedLocalEvent(event_id=event_id, session_id=session,
                    author_principal=author, role=role, text=text, occurred_at=NOW,
                    evidence_refs=("test:" + event_id,), visibility=visibility))
            binding = host.shared.qq_binding(group_id=PRIVATE, member_id=PEER, query="你好")
            content = json.dumps(binding.context, ensure_ascii=False)
            assert FACT in content and "EXPLICIT_PRIVATE_GRANT" in content
            for forbidden in ("LOCAL_CHAT_SECRET", "OTHER_PEER_SECRET", "NEAR_FACTS_SECRET",
                              "OTHER_AUTHOR_SECRET", "ASSISTANT_FACT_SECRET"):
                assert forbidden not in content
            assert len(binding.source_ids) == 2 and binding.llm is host.chat.llm
            assert binding.memory.owner is host.chat.memory
            assert binding.preset.persona == host.chat.memory.get_persona()
            group_content = json.dumps(host.shared.qq_binding(group_id="10001").context, ensure_ascii=False)
            assert "EXPLICIT_PRIVATE_GRANT" not in group_content and "PRIVATE_PIXELS" not in group_content
            for key in (private_scope_id(SELF, OTHER), private_scope_id("2015188509", PEER)):
                with pytest.raises(ValueError, match="not_authorized"):
                    host.shared.qq_binding(group_id=key)
        finally:
            await host.close()
    asyncio.run(run())


def test_sync_cache_includes_private_authorization_and_local_recall_includes_private(tmp_path):
    from harness.core.models import InteractionEvent
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    _private(source)
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "shared.db", qq_settings=_settings(source, peers=()))
    shared.sync_qq()
    assert shared.last_import.created_count == 0
    shared.qq_settings.allowed_private_peer_ids = (PEER,)
    shared.sync_qq()
    assert shared.last_import.created_count == 2
    segments = shared.local_context(InteractionEvent(raw_text="图片", timestamp=NOW,
        speaker="user", session_id="local-main"))
    assert "PRIVATE_PIXELS" in "".join(segment.text for segment in segments)


def test_another_enabled_friend_needs_explicit_visibility_to_read_operator_facts(tmp_path, monkeypatch):
    async def run():
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path, monkeypatch, ["嗯。"])
        source = QQVisualMemoryStore(tmp_path / "qq.db")
        _private(source)
        host.shared.qq_settings = _settings(source, peers=(PEER, OTHER))
        try:
            host.shared.store.record_local(SharedLocalEvent(event_id="creator", session_id="operator-confirmed-facts",
                author_principal="local:main", role="user", text=FACT, occurred_at=NOW,
                evidence_refs=("test:creator",), visibility=("local:main", "qq:group:10001")))
            other_key = private_scope_id(SELF, OTHER)
            assert FACT not in json.dumps(host.shared.qq_binding(group_id=other_key).context, ensure_ascii=False)
            host.shared.store.record_local(SharedLocalEvent(event_id="friend-grant", session_id="operator-confirmed-facts",
                author_principal="local:main", role="user", text="EXPLICITLY_GRANTED_FACT", occurred_at=NOW,
                evidence_refs=("test:friend-grant",), visibility=(conversation_audience(other_key),)))
            content = json.dumps(host.shared.qq_binding(group_id=other_key).context, ensure_ascii=False)
            assert "EXPLICITLY_GRANTED_FACT" in content and FACT not in content
        finally:
            await host.close()
    asyncio.run(run())


@pytest.mark.parametrize("channel", ["internal", "goal", "qq", "qq_private"])
def test_internal_goal_and_nonlocal_queries_never_recall_private_text_or_images(tmp_path, channel):
    from harness.core.models import InteractionEvent
    source = QQVisualMemoryStore(tmp_path / "qq.db")
    _private(source)
    shared = CognitiveSharedMemory(SimpleNamespace(), tmp_path / "shared.db", qq_settings=_settings(source))
    shared.store.import_qq_context(person(group="10001"))
    event = InteractionEvent(raw_text="", timestamp=NOW, channel=channel,
        speaker="character", session_id="local-main")
    payload = "".join(segment.text for segment in shared.local_context(event))
    assert "qq_group" in payload
    for forbidden in ("PRIVATE_PIXELS", "MODEL_IMAGE_DESCRIPTION", "MODEL_IMAGE_OCR", AUDIENCE):
        assert forbidden not in payload
    shared.sync_local = lambda: 0
    board = json.dumps(shared.board_context())
    assert "qq_group" in board and "PRIVATE_PIXELS" not in board and "MODEL_IMAGE_DESCRIPTION" not in board
