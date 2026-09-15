"""The larger recall budget retains complete evidence without adding hits or rows."""

from copy import deepcopy
from datetime import timedelta
import json

from harness.cognitive.qq_memory import select_recall
from harness.config import Settings
from harness.memory.impressions import ImpressionEvidence, ImpressionProposal, apply_impression
from tests.test_qq_private_recall import NOW, PRIVATE, add_episode, add_impression, memory, qq_scope


def encoded_length(payload):
    return len(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))


def test_default_recall_budget_is_exactly_four_times_larger_without_raising_item_count(monkeypatch):
    monkeypatch.delenv("NC_QQ_MEMORY_RECALL_MAX_CHARACTERS", raising=False)
    monkeypatch.delenv("NC_QQ_MEMORY_RECALL_MAX_ITEMS", raising=False)
    settings = Settings(_env_file=None)
    assert settings.qq_memory_recall_max_characters == 4 * 4000 == 16000
    assert settings.qq_memory_recall_max_items == 8


def test_default_16k_selects_at_most_eight_complete_source_records_without_mutation():
    mem = memory()
    settings = Settings(_env_file=None, qq_memory_recall_max_characters=16000)
    mem.settings = settings
    original = {}
    for index in range(10):
        event = add_episode(mem, f"retained-{index:02}", scope=qq_scope("100"),
            text="待回顾的有来源经历" + "详" * 250 + f"SUMMARY-END-{index}", importance=0.8,
            objective_fact="原始作者：" + "证" * 1000 + f"SOURCE-END-{index}",
            source_refs=[f"original-record-{index}", f"original-image-{index}"])
        original[event.event_id] = event.model_dump()
    before_state = deepcopy(mem.qq_memory_state)
    kwargs = dict(group_id=PRIVATE, allowed_conversation_ids=("100",), include_owner_local=True,
                  now=NOW + timedelta(minutes=1), query="回顾有来源经历")
    expanded = select_recall(mem, **kwargs)
    legacy = select_recall(mem, **kwargs, max_characters=4000)
    assert len(expanded["entries"]) == 8
    assert 4000 < encoded_length(expanded) <= 16000
    assert len(legacy["entries"]) < len(expanded["entries"]) and encoded_length(legacy) <= 4000
    for entry in expanded["entries"]:
        source = original[entry["record_id"]]
        assert entry["summary"] == source["summary"]
        assert entry["objective_fact"] == source["objective_fact"]
        assert entry["source_refs"] == source["source_refs"]
    assert mem.qq_memory_state == before_state
    assert {event.event_id: event.model_dump() for event in mem.stores.episodic.list_all()} == original
    assert all(event.hit_count == 0 for event in mem.stores.episodic.list_all())


def test_omitted_recall_limit_uses_memory_settings_and_explicit_limit_overrides():
    mem = memory()
    for index in range(8):
        add_episode(mem, str(index), scope=qq_scope("100"), text="完整来源内容" * 120)
    mem.settings = Settings(_env_file=None, qq_memory_recall_max_characters=5100)
    kwargs = dict(group_id="100", now=NOW)
    configured = select_recall(mem, **kwargs)
    explicit = select_recall(mem, **kwargs, max_characters=16000)
    assert encoded_length(configured) <= 5100
    assert encoded_length(explicit) <= 16000
    assert len(explicit["entries"]) > len(configured["entries"])


def test_eight_impressions_expose_at_most_forty_whole_frozen_episode_ids():
    mem = memory()
    mem.settings = Settings(_env_file=None, qq_memory_recall_max_characters=16000)
    for index in range(8):
        current = add_impression(mem, "100", member=str(index + 300), topic="人物判断",
                                 statement="人物判断：目前愿意主动交流。")
        scope = current.scope
        for step, stance in enumerate(("support", "support", "contradict", "contradict"), 1):
            moment = NOW + timedelta(hours=step * 2)
            event = add_episode(mem, f"history-{index}-{step}", scope=scope,
                member=str(index + 300), time=moment, text="一段具体且有作者归属的经历", importance=0.1)
            statement = "人物判断：平时愿意交流，但存在尚待解释的反例。"
            evidence = ImpressionEvidence(episode_id=event.event_id, source_ids=event.source_refs,
                time=moment, stance=stance, statement=statement, rationale="同类场景中的实际发言")
            current = apply_impression(current, ImpressionProposal(kind="person", subject_refs=current.subject_refs,
                topic=current.topic, context=current.context, statement=statement, evidence=evidence,
                contrast=stance == "contradict"), scope=scope, now=moment).memory
        mem.qq_memory_state["impressions"][current.impression_id] = current.model_dump(mode="json")
    before = deepcopy(mem.qq_memory_state)
    result = select_recall(mem, group_id=PRIVATE, allowed_conversation_ids=("100",), include_owner_local=True,
                           query="人物判断", now=NOW + timedelta(hours=9))
    assert len(result["entries"]) == 8 and encoded_length(result) <= 16000
    assert all(entry["kind"] == "person" for entry in result["entries"])
    exposed = set()
    for entry in result["entries"]:
        refs = {*entry["supporting_episode_ids"], entry["first_episode_id"],
                *(counter["episode_id"] for counter in entry["counterevidence"])}
        assert len(refs) <= 5
        exposed.update(refs)
    assert len(exposed) == 40
    assert mem.qq_memory_state == before
    assert all(event.hit_count == 0 for event in mem.stores.episodic.list_all())
