"""Only facts used by this turn are checked; append-only progress stays live."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from harness.core.models import EpisodicEvent, MemoryScope, SemanticFact
from harness.memory.shared import SharedLocalEvent, SharedMemoryStore
from harness.memory.revision.recall import select_daily_recall
from harness.qq.conversation import private_scope_id
from harness.qq.prepared_evidence import capture_used_sources, validate_used_sources
from harness.qq.turn_checkpoints import QQTurnStateError


NOW = datetime.now(timezone.utc)
SCOPE = MemoryScope(owner_id="misty", channel="qq_group", conversation_id="qq:group:10001",
                    visibility=["local:main", "qq:group:10001"])


def _host(service, tmp_path):
    coordinator = SimpleNamespace(control=SimpleNamespace(recall_enabled=True),
        _conversation_authorized=lambda group, **kw: group == "10001",
        _owner_private_access=lambda group: False, _authorized_conversations=lambda: ("10001",))
    return SimpleNamespace(shared=SimpleNamespace(store=SharedMemoryStore(tmp_path / "shared.sqlite3"),
        qq_settings=SimpleNamespace(self_member_id="29999", allowed_private_peer_ids=["2285027817"])),
        chat=SimpleNamespace(memory=service, active_preset_id="misty"), qq_memory=coordinator)


def _episode(service, **updates):
    event = EpisodicEvent(**{"event_id": "episode:1", "time": NOW, "participants": ["qq:member:20001"],
        "summary": "讨论过茶", "objective_fact": "成员说喜欢红茶", "importance": 0.5, "emotional_valence": 0,
        "evidence": ["observation:1"], "source_refs": ["shared:1"], "tier": "long", "scope": SCOPE, **updates})
    service.stores.episodic.insert(event)
    return event


def _local(store, **updates):
    return store.record_local(SharedLocalEvent(**{"event_id": "local:1", "session_id": "main",
        "author_principal": "local:main", "role": "user", "text": "共享原始事实", "occurred_at": NOW,
        "evidence_refs": ["local-proof:1"], "visibility": ["local:main", "qq:group:10001"], **updates})).record


def _payload(*, shared=(), derived=(), group="10001"):
    return {"group_context": {"group_id": group}, "shared_memory": {"entries": list(shared)},
            "derived_memory": {"entries": list(derived)}}


def test_shared_exact_lookup_allows_unrelated_append_but_rejects_removal(service, tmp_path, monkeypatch):
    host = _host(service, tmp_path)
    record = _local(host.shared.store)
    payload = _payload(shared=[record.model_projection()])
    monkeypatch.setattr(host.shared.store, "query", lambda *a, **k: pytest.fail("guard performed recall"))
    frozen = capture_used_sources(host, payload)
    _local(host.shared.store, event_id="local:2", text="另一个事实")
    validate_used_sources(host, frozen)
    with host.shared.store._connect(write=True) as db:
        db.execute("DELETE FROM shared_memory_entries WHERE record_id=?", (record.record_id,))
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        validate_used_sources(host, frozen)


def test_operator_local_exception_never_expands_to_another_private_peer(service, tmp_path):
    host = _host(service, tmp_path)
    record = _local(host.shared.store, session_id="operator-confirmed-facts", visibility=["local:main"])
    payload = _payload(group=private_scope_id("29999", "2285027817"))
    payload["shared_memory"]["operator_confirmed_facts"] = {"entries": [record.model_projection()]}
    frozen = capture_used_sources(host, payload)
    validate_used_sources(host, frozen)
    host.shared.qq_settings.allowed_private_peer_ids = []
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        validate_used_sources(host, frozen)


def test_episode_hit_counters_and_unrelated_rows_do_not_invalidate_content(service, tmp_path, monkeypatch):
    host = _host(service, tmp_path)
    event = _episode(service)
    monkeypatch.setattr(service.stores.episodic, "list_all", lambda: pytest.fail("guard scanned all episodes"))
    frozen = capture_used_sources(host, _payload(derived=[{"record_id": event.event_id, "kind": "episode"}]))
    service.stores.episodic.update(event.model_copy(update={"hit_count": 50, "last_hit_at": NOW}))
    _episode(service, event_id="episode:2", objective_fact="不相关的新事实")
    validate_used_sources(host, frozen)
    service.stores.episodic.update(event.model_copy(update={"objective_fact": "原事实已更正"}))
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        validate_used_sources(host, frozen)


@pytest.mark.parametrize("change", ["receipt", "source", "scope", "value", "status"])
def test_daily_used_object_run_sources_and_permission_are_rechecked(service, tmp_path, monkeypatch, change):
    host = _host(service, tmp_path)
    fact = SemanticFact(fact_id="fact:1", subject="qq:member:20001", predicate="likes", value="红茶",
        confidence=0.6, supporting_events=["episode:1"], formed_at=NOW, scope=SCOPE, source_refs=["daily-source:1"])
    service.stores.semantic.insert(fact)
    service.daily_revision_state = {"entries": [{"key": "semantic/fact:1", "run_id": "run:1"}],
        "receipts": {"run:1": {"status": "applied"}}, "sources": {"daily-source:1": {
            "source_id": "daily-source:1", "scope": SCOPE.model_dump(mode="json"), "owner_id": "misty",
            "occurred_at": NOW.isoformat(), "text": "我喜欢红茶", "authority": "speaker_utterance",
            "author_principal": "qq:member:20001"}}}
    recalled = select_daily_recall(service, audience="qq:group:10001", conversation_id="qq:group:10001")
    assert recalled["entries"]
    monkeypatch.setattr(service.stores.semantic, "list_all", lambda: pytest.fail("guard scanned all facts"))
    frozen = capture_used_sources(host, _payload(derived=recalled["entries"]))
    service.daily_revision_state["receipts"]["unrelated"] = {"status": "applied"}
    validate_used_sources(host, frozen)
    if change == "receipt":
        service.daily_revision_state["receipts"]["run:1"]["status"] = "retracted"
    elif change == "source":
        service.daily_revision_state["sources"]["daily-source:1"]["text"] = "实际是绿茶"
    elif change == "scope":
        service.stores.semantic.update(fact.model_copy(update={"scope": SCOPE.model_copy(update={"visibility": ["local:main"]})}))
    elif change == "value":
        service.stores.semantic.update(fact.model_copy(update={"value": "绿茶"}))
    else:
        service.stores.semantic.update(fact.model_copy(update={"revision_metadata": {"status": "retracted"}}))
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        validate_used_sources(host, frozen)
