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


@pytest.mark.parametrize("change", ["source_retraction", "revision", "authorization", "preset", "disabled"])
def test_production_used_sources_freeze_exact_revision_and_scope(service, tmp_path, monkeypatch, change):
    from harness.memory.production import Experience, BeliefRevision, ProductionMemoryStore
    host = _host(service, tmp_path)
    store = ProductionMemoryStore(tmp_path / "production.sqlite3")
    host.shared.production_enabled = True
    host.shared.production_store = store
    host.shared.qq_settings.allowed_group_ids = ["10001"]
    store.ingest([Experience(record_id="source:1", author_id="qq:member:20001", text="我喜欢茶",
        occurred_at=NOW, scope=SCOPE, source_refs=("raw:1",), independent_key="raw:1")])
    belief = BeliefRevision(record_id="belief:1", subject_id="qq:member:20001", topic="饮品",
        statement="对方说喜欢茶", kind="self_report", revision=1, supporting_ids=("source:1",), valid_from=NOW)
    store.revise(belief, event_id="interpret:1")
    payload = _payload()
    payload["derived_memory"] = store.recall(audience="qq:group:10001", query="茶")
    frozen = capture_used_sources(host, payload)
    assert frozen["version"] == "qq-used-sources-v2" and frozen["production"]
    monkeypatch.setattr(store, "recall", lambda **kwargs: pytest.fail("commit guard performed recall"))
    validate_used_sources(host, frozen)
    store.record_use("exposure:1", ["belief:1"], audience="qq:group:10001")
    validate_used_sources(host, frozen)
    if change == "source_retraction":
        store.retract_source("source:1", event_id="retract:1", reason="撤销")
    elif change == "revision":
        store.revise(belief.model_copy(update={"revision": 2, "statement": "改成更具体的饮品偏好"}), event_id="interpret:2")
    elif change == "authorization":
        host.shared.qq_settings.allowed_group_ids = []
    elif change == "preset":
        host.chat.active_preset_id = "other"
    else:
        host.shared.production_enabled = False
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        validate_used_sources(host, frozen)


@pytest.mark.parametrize("change", ["retract", "reinterpret"])
def test_private_orientation_is_frozen_without_exposing_its_evidence(service, tmp_path, change):
    from harness.memory.production import Experience, ProductionMemoryStore
    from harness.memory.orientation import OrientationStore
    from harness.production.metering import production_call_scope
    host = _host(service, tmp_path)
    store = ProductionMemoryStore(tmp_path / "production.sqlite3")
    host.shared.production_enabled = True
    host.shared.production_store = store
    host.shared.qq_settings.allowed_group_ids = ["10001"]
    audience = "qq:private:29999:20001"
    store.ingest([Experience(record_id="private", author_id="qq:member:20001", text="私聊具体原文",
        occurred_at=NOW, scope=MemoryScope(channel="qq_private", conversation_id=audience,
        visibility=["local:main", audience]), source_refs=("private-raw",), independent_key="private")])
    orientations = OrientationStore(store)
    value = {"direction": "repair", "reason": "私密解释", "source_refs": ["private"]}
    orientations.revise(value, subject_id="qq:member:20001", audience=audience, event_id="orientation:1")
    with production_call_scope({"schema_version": "production-context-v1", "configuration": {},
            "interpersonal_orientation": orientations.projection("qq:member:20001")}, command_id="turn", meter=object()):
        frozen = capture_used_sources(host, _payload())
    assert "私聊具体原文" not in str(frozen) and "私密解释" not in str(frozen)
    validate_used_sources(host, frozen)
    if change == "retract":
        store.retract_source("private", event_id="retract-private", reason="撤回")
    else:
        orientations.revise({**value, "direction": "neutrality"}, subject_id="qq:member:20001", audience=audience,
                            event_id="orientation:2")
    with pytest.raises(QQTurnStateError, match="used_source_changed"):
        validate_used_sources(host, frozen)
