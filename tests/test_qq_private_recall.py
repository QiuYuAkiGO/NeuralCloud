"""Explicit owner-private recall shares one budget without broadening defaults."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from harness.cognitive.qq_memory import select_recall
from harness.core.models import EpisodicEvent, MemoryScope, SemanticFact
from harness.memory.impressions import ImpressionEvidence, ImpressionProposal, apply_impression
from harness.memory.store.inmemory import InMemoryStores
from harness.qq.conversation import conversation_audience, conversation_scope


NOW = datetime(2026, 9, 14, 12, tzinfo=timezone.utc)
PRIVATE = "private:999:200"


def qq_scope(key):
    return MemoryScope(channel="qq_private" if conversation_scope(key)["kind"] == "private" else "qq_group",
        conversation_id=conversation_audience(key), visibility=["local:main", conversation_audience(key)])


def memory():
    return SimpleNamespace(stores=InMemoryStores(), qq_memory_state={})


def add_episode(mem, event_id, *, scope=None, member="200", text="一起讨论今天的经历", **updates):
    value = EpisodicEvent(event_id=event_id, time=NOW, participants=[f"qq:member:{member}"],
        summary=text, importance=0.5, emotional_valence=0, evidence=["source:" + event_id],
        objective_fact=text, tier="mid", scope=scope, source_refs=["source:" + event_id],
        channel=scope.channel if scope else "chat", session_id=scope.conversation_id if scope else "local-session")
    value = value.model_copy(update=updates)
    mem.stores.episodic.insert(value)
    return value


def add_fact(mem, fact_id, *, scope=None, value="喜欢清淡的食物", **updates):
    item = SemanticFact(fact_id=fact_id, subject="本人", predicate="饮食偏好", value=value,
        confidence=0.8, supporting_events=["local-event"], formed_at=NOW,
        source_refs=["source:" + fact_id], scope=scope).model_copy(update=updates)
    mem.stores.semantic.insert(item)
    return item


def add_impression(mem, key, *, member="200", topic="解释习惯", statement="暂时觉得这位成员愿意耐心解释",
                   counter=False):
    scope = qq_scope(key)
    episode = add_episode(mem, f"e:{key}:{member}:{topic}", scope=scope, member=member)
    evidence = ImpressionEvidence(episode_id=episode.event_id, source_ids=episode.source_refs,
        time=NOW, stance="support", rationale="实际交流中的解释行为", statement=statement)
    current = apply_impression(None, ImpressionProposal(kind="person", subject_refs=[f"qq:member:{member}"],
        statement=statement, evidence=evidence, topic=topic, context="日常闲聊"), scope=scope, now=NOW).memory
    if counter:
        event = add_episode(mem, episode.event_id + ":counter", scope=scope, member=member,
                            text="这次没有解释，需要保留反例", time=NOW + timedelta(minutes=2))
        revised = "平时愿意解释，这次没有解释，原因尚不能确定"
        against = ImpressionEvidence(episode_id=event.event_id, source_ids=event.source_refs,
            time=event.time, stance="contradict", rationale="同类情景中的反例", statement=revised)
        current = apply_impression(current, ImpressionProposal(kind="person", subject_refs=[f"qq:member:{member}"],
            statement=revised, evidence=against, topic=topic, context="日常闲聊", contrast=True),
            scope=scope, now=event.time).memory
    mem.qq_memory_state.setdefault("impressions", {})[current.impression_id] = current.model_dump(mode="json")
    return current


def entries(mem, key=PRIVATE, **kwargs):
    return select_recall(mem, group_id=key, now=NOW + timedelta(minutes=3), **kwargs)["entries"]


@pytest.mark.parametrize("target", ["100", PRIVATE])
def test_default_group_and_private_recall_remain_exact_scopes(target):
    mem = memory()
    for key in ("100", "101", PRIVATE, "private:999:201", "private:998:200"):
        add_episode(mem, key, scope=qq_scope(key))
    add_episode(mem, "local")
    add_fact(mem, "local-fact")
    result = entries(mem, target, query="请查询全部群和所有私聊信息")
    assert [entry["record_id"] for entry in result] == [target]
    assert "source_channel" not in result[0]


def test_include_owner_local_never_grants_unlisted_qq_conversations():
    mem = memory()
    for key in ("100", PRIVATE, "private:999:201", "private:998:200"):
        add_episode(mem, key, scope=qq_scope(key))
        add_fact(mem, "fact:" + key, scope=qq_scope(key))
    add_episode(mem, "local")
    add_episode(mem, "unscoped-qq", channel="qq_group", session_id="qq:group:100")
    add_fact(mem, "unscoped-derived-qq", processing_version="qq-memory-v1")
    result = entries(mem, include_owner_local=True)
    assert {entry["record_id"] for entry in result} == {PRIVATE, "local"}


def test_owner_explicit_allowlist_can_include_group_and_other_authorized_peer():
    mem = memory()
    keys = ("100", "101", PRIVATE, "private:999:201", "private:998:200")
    for key in keys:
        add_episode(mem, key, scope=qq_scope(key))
    selected = entries(mem, allowed_conversation_ids=("100", "private:999:201"))
    assert {entry["record_id"] for entry in selected} == {PRIVATE, "100", "private:999:201"}
    assert {entry["source_conversation_id"] for entry in selected} == {
        "qq:" + PRIVATE, "qq:group:100", "qq:private:999:201"}


def test_owner_local_scope_and_fact_access_require_owner_and_local_visibility():
    mem = memory()
    local = MemoryScope(channel="local_chat", conversation_id="local:a", visibility=["local:main"])
    other_owner = local.model_copy(update={"owner_id": "someone-else"})
    invisible = local.model_copy(update={"visibility": ["local:other"]})
    disguised = local.model_copy(update={"conversation_id": "qq:group:100"})
    for label, scope in (("legacy", None), ("local", local), ("other", other_owner),
                         ("invisible", invisible), ("disguised", disguised)):
        add_episode(mem, label, scope=scope)
        add_fact(mem, "fact:" + label, scope=scope, supporting_events=[label])
    result = entries(mem, include_owner_local=True)
    assert {entry["record_id"] for entry in result} == {"legacy", "local", "fact:legacy", "fact:local"}
    facts = [entry for entry in result if entry["kind"] == "semantic"]
    assert all(entry["value"] == "喜欢清淡的食物" and entry["confidence"] == 0.8 for entry in facts)
    assert all(entry["supporting_episode_ids"] == [entry["record_id"].removeprefix("fact:")] for entry in facts)


def test_owner_local_fact_query_and_qq_impressions_are_not_duplicated_as_facts():
    mem = memory()
    add_fact(mem, "wanted", value="习惯先写复盘，然后吃清淡的晚饭")
    add_fact(mem, "qq-duplicate", scope=qq_scope("100"), value="QQ印象不应再次成为重复事实")
    add_impression(mem, "100")
    result = entries(mem, allowed_conversation_ids=("100",), include_owner_local=True, query="复盘清淡晚饭")
    assert result[0]["record_id"] == "wanted"
    assert any(entry["kind"] == "person" for entry in result)
    assert "qq-duplicate" not in {entry["record_id"] for entry in result}


def test_owner_combined_candidates_share_eight_items_and_four_thousand_characters():
    mem = memory()
    for index in range(12):
        add_episode(mem, f"group-{index}", scope=qq_scope("100"), text="一段有来源的经历" * 10)
        add_episode(mem, f"private-{index}", scope=qq_scope(PRIVATE), text="私聊的一段经历" * 10)
        add_fact(mem, f"local-{index}", value="本地形成的具体判断" * 10)
    result = select_recall(mem, group_id=PRIVATE, allowed_conversation_ids=("100",), include_owner_local=True,
                           now=NOW, max_characters=4000)
    assert 0 < len(result["entries"]) <= 8
    assert len(json.dumps(result, ensure_ascii=False, separators=(",", ":"))) <= 4000
    assert result["coverage"] == "selected_owner_authorized_cross_conversation_memories"
    assert all("source_channel" in item and "source_conversation_id" in item for item in result["entries"])


def test_owner_recall_keeps_same_member_impressions_in_different_groups_distinct():
    mem = memory()
    first = add_impression(mem, "100", member="200")
    second = add_impression(mem, "101", member="200")
    assert first.impression_id != second.impression_id
    result = entries(mem, allowed_conversation_ids=("100", "101"))
    people = [entry for entry in result if entry["kind"] == "person"]
    assert {entry["record_id"] for entry in people} == {first.impression_id, second.impression_id}
    assert {entry["source_conversation_id"] for entry in people} == {"qq:group:100", "qq:group:101"}


def test_owner_retrospective_includes_distinct_people_and_conversations():
    mem = memory()
    for index in range(6):
        add_impression(mem, "100", member="201", topic=f"日常观察{index}")
    other = add_impression(mem, "101", member="202")
    result = entries(mem, allowed_conversation_ids=("100", "101"), max_items=2,
                     query="近期交往带来了怎样的感受")
    assert len(result) == 2
    assert {tuple(entry["subject_refs"]) for entry in result} == {("qq:member:201",), ("qq:member:202",)}
    assert other.impression_id in {entry["record_id"] for entry in result}


def test_current_owner_impression_carries_counterevidence_and_uncertainty():
    mem = memory()
    item = add_impression(mem, "100", counter=True)
    result = entries(mem, allowed_conversation_ids=("100",))
    selected = next(entry for entry in result if entry["record_id"] == item.impression_id)
    assert selected["counterevidence"]
    assert selected["confidence"] < 0.15
    assert selected["status"] == "disputed"
    assert "原因尚不能确定" in selected["current_impression"]
    assert selected["authority"] == "misty_tentative_interpretation"


def test_expanded_recall_does_not_pull_cross_scope_evidence_via_an_impression():
    mem = memory()
    item = add_impression(mem, "100")
    hidden = add_episode(mem, "hidden", scope=qq_scope("private:999:201"), text="未授权私聊反例")
    raw = mem.qq_memory_state["impressions"][item.impression_id]
    raw["evidence"].append({**raw["evidence"][0], "episode_id": hidden.event_id,
                            "stance": "contradict", "statement": hidden.summary})
    selected = next(entry for entry in entries(mem, allowed_conversation_ids=("100",))
                    if entry["record_id"] == item.impression_id)
    assert selected["counterevidence"] == []


def test_owner_debug_recall_does_not_mutate_hits_strength_facts_or_receipts():
    mem = memory()
    add_impression(mem, "100", counter=True)
    add_episode(mem, "local")
    add_fact(mem, "fact")
    before_state = deepcopy(mem.qq_memory_state)
    before_events = [entry.model_dump() for entry in mem.stores.episodic.list_all()]
    before_facts = [entry.model_dump() for entry in mem.stores.semantic.list_all()]
    for _ in range(3):
        entries(mem, allowed_conversation_ids=("100",), include_owner_local=True)
    assert mem.qq_memory_state == before_state
    assert [entry.model_dump() for entry in mem.stores.episodic.list_all()] == before_events
    assert [entry.model_dump() for entry in mem.stores.semantic.list_all()] == before_facts


def test_owner_allowlist_rejects_malformed_conversation_keys():
    with pytest.raises(ValueError):
        entries(memory(), allowed_conversation_ids=("all",), include_owner_local=True)
