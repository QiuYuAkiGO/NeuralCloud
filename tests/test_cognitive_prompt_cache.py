"""Stable prompt opportunities must not freeze facts or promote source data."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from harness.cognitive import board, capabilities, diary, goals, web
from harness.cognitive.prompts import (
    build_messages, canonical_json, chronological_memory, persona_prefix,
    project_context_segments, split_offer_catalog,
)
from harness.core.inference import ContextSegment, MemoryEvidenceContext, MemoryEvidenceEntry
from tests.test_capability_cognition import FakeLLM, command, host, moment, offer
from tests.test_goal_cognition import goal_command


def test_shared_persona_prefix_and_schema_precede_dynamic_state_without_role_changes():
    persona = {"identity": {"name": "同一人格", "style": ["简短", "自然"]}, "version": "v1"}
    reordered = {"version": "v1", "identity": {"style": ["简短", "自然"], "name": "同一人格"}}
    assert persona_prefix(persona) == persona_prefix(reordered)
    for rules in (board._RULES, capabilities._RULES, goals._RULES, diary._RULES, web._STEP_RULES):
        first = build_messages(persona=persona, rules=rules,
            fixed_payload={"choice_schema": {"type": "object"}},
            payload={"observation": "UNTRUSTED_SOURCE", "opportunity_id": "random-1"}, emotion_text="当前情绪A")
        second = build_messages(persona=reordered, rules=rules,
            fixed_payload={"choice_schema": {"type": "object"}},
            payload={"observation": "UNTRUSTED_SOURCE", "opportunity_id": "random-1"}, emotion_text="当前情绪B")
        assert first[:2] == second[:2]
        assert first[0]["content"].startswith(persona_prefix(persona))
        assert first[1]["content"].startswith('{"choice_schema":')
        assert [item["role"] for item in first] == ["system", "user", "system"]
        assert all("UNTRUSTED_SOURCE" not in item["content"] for item in first if item["role"] == "system")


def test_catalog_definition_survives_opportunity_changes_but_current_authority_does_not():
    original = offer(offer_id="random-offer-1", observation={"current": "first"}).model_dump(mode="json")
    changed = {**deepcopy(original), "offer_id": "random-offer-2", "scope": {"board_id": "other-scope"},
               "activation_revision": "new-binding", "observation_refs": ["new-observation"],
               "valid_until": (moment() + timedelta(seconds=5)).isoformat(), "observation": {"current": "latest"}}
    before = deepcopy(original), deepcopy(changed)
    definitions, current = split_offer_catalog([original])
    later_definitions, later_current = split_offer_catalog([changed])
    assert definitions == later_definitions
    assert current != later_current
    for source, projected in ((original, current[0]), (changed, later_current[0])):
        definition = next(item for item in definitions if item["definition_id"] == projected["definition_id"])
        restored = {key: value for key, value in projected.items() if key != "definition_id"}
        restored["input_schema"] = definition["input_schema"]
        assert restored == source
    assert (original, changed) == before
    assert not {"offer_id", "scope", "valid_until", "observation", "activation_revision"} & definitions[0].keys()
    another_schema = {**changed, "input_schema": {"type": "object", "properties": {"new": {"type": "string"}}}}
    assert split_offer_catalog([another_schema])[0] != definitions
    assert split_offer_catalog([changed, original]) == split_offer_catalog([original, changed])
    assert len(split_offer_catalog([changed, original])[0]) == 1


def memory_payload():
    return {"schema_version": "shared-memory-context-v1", "authority": "untrusted_source_attributed_memory",
        "entries": [
            {"record_id": "new", "occurred_at": "2026-09-16T10:00:00+08:00", "text": "NEW_SOURCE_ONCE",
             "author_principal": "local:main", "authority": "speaker_utterance", "channel": "local_chat"},
            {"record_id": "old", "occurred_at": "2026-09-16T01:00:00Z", "text": "OLD_SOURCE_ONCE",
             "author_principal": "qq:member:1", "authority": "verified_delivery", "channel": "qq_group"},
        ], "selected_count": 2, "matched_count": 4, "omitted_count": 2}


def test_projection_keeps_each_attributed_text_once_and_leaves_internal_binding_untouched():
    payload = memory_payload()
    segment = ContextSegment(kind="user", text=json.dumps({"shared_memory": payload}, ensure_ascii=False),
                             source_ids=["new", "old"], why="verified_shared_channel_memory")
    evidence = [MemoryEvidenceEntry(source_id=item["record_id"], text=item["text"], audience="local:main",
        source_channel=item["channel"], speaker_id=item["author_principal"], status="retained_fact")
        for item in payload["entries"]]
    segment.memory_evidence = MemoryEvidenceContext.bind(audience="local:main", entries=evidence, segment=segment)
    before = segment.model_dump(mode="json")
    projected = project_context_segments([segment])
    encoded = canonical_json(projected)
    assert encoded.count("NEW_SOURCE_ONCE") == encoded.count("OLD_SOURCE_ONCE") == 1
    assert "memory_evidence" not in encoded and "segment_sha256" not in encoded
    assert projected[0]["source_ids"] == ["new", "old"]
    shown = json.loads(projected[0]["text"])["shared_memory"]
    assert [item["record_id"] for item in shown["entries"]] == ["old", "new"]
    assert sorted(shown["entries"], key=lambda item: item["record_id"]) == sorted(payload["entries"], key=lambda item: item["record_id"])
    assert segment.model_dump(mode="json") == before
    assert segment.memory_evidence.admitted([segment], trigger_ref="unrelated").has_history


def test_history_reorders_selected_set_only_and_never_hides_changed_facts():
    payload = memory_payload()
    original = deepcopy(payload)
    projected = chronological_memory(payload)
    assert projected["selected_count"] == 2 and projected["omitted_count"] == 2
    assert payload == original
    payload["entries"][0]["text"] = "revised latest source"
    assert chronological_memory(payload)["entries"][-1]["text"] == "revised latest source"
    projected["entries"][0]["text"] = "caller modification"
    assert payload["entries"][1]["text"] == "OLD_SOURCE_ONCE"
    unknown = {"entries": [{"text": "without an attributed timestamp"}]}
    assert chronological_memory(unknown) == unknown


@pytest.mark.parametrize("module,make_command,output,schema_field", [
    (capabilities, command, {"kind": "wait"}, "choice_schema"),
    (goals, goal_command, {"episode_operation": "wait", "choice": {"kind": "wait"}}, "decision_schema"),
])
def test_actual_selection_prompts_keep_schema_prefix_and_live_dynamic_inputs(
        monkeypatch, module, make_command, output, schema_field):
    owner = host(FakeLLM(output))
    owner.chat.settings = SimpleNamespace(emotion_mode="on", llm_model="mock")
    emotion = {"text": "emotion-before"}

    async def guidance(*args):
        return dict(emotion)

    monkeypatch.setattr(module, "resolve_command_emotion", guidance)
    for number in (1, 2):
        emotion["text"] = f"emotion-{number}"
        request = make_command([offer(offer_id=f"per-call-{number}", observation={"current": number})])
        asyncio.run(module.prepare(owner, request))
    first, second = (item[0] for item in owner.chat.llm.calls)
    assert first[0] == second[0]
    assert first[1]["content"].split(',"shared_memory":', 1)[0] == second[1]["content"].split(',"shared_memory":', 1)[0]
    assert first[1]["content"].startswith('{"' + schema_field + '":')
    shown = json.loads(second[1]["content"])
    assert shown["available_offers"][0]["offer_id"] == "per-call-2"
    assert shown["available_offers"][0]["observation"] == {"current": 2}
    assert "homeostatic_summary" not in shown
    assert "emotion-2" in second[-1]["content"] and second[-1]["role"] == "system"
    assert all("emotion-2" not in message["content"] for message in second[:-1])


def test_diary_reads_fresh_selected_memory_and_places_emotion_after_shared_sources(monkeypatch):
    owner = host(FakeLLM({"action": "wait", "text": ""}))
    owner.chat.settings = SimpleNamespace(emotion_mode="on", llm_model="mock")
    owner.adapter = SimpleNamespace(_ledger={})
    memory = memory_payload()
    owner.shared.diary_context = lambda **kwargs: memory
    monkeypatch.setattr(diary.DiarySettings, "from_environment", lambda: diary.DiarySettings())

    async def guidance(*args):
        return {"text": "current diary emotion"}

    monkeypatch.setattr(diary, "resolve_command_emotion", guidance)
    request = command()
    request = request.model_copy(update={"payload": {
        "entry_id": "diary:1", "source_event_id": request.event_id, "deadline_at": request.deadline.isoformat(),
        "trigger": "user_request", "request_text": "留下一点想法", "day": "2026-09-16"}})
    assert asyncio.run(diary.prepare(owner, request))["decision"]["action"] == "wait"
    original_order = [item["record_id"] for item in memory["entries"]]
    memory["entries"][0]["text"] = "UPDATED_DIARY_SOURCE"
    assert asyncio.run(diary.prepare(owner, request))["decision"]["action"] == "wait"
    first, second = (item[0] for item in owner.chat.llm.calls)
    assert first[0] == second[0]
    shown = json.loads(second[1]["content"])
    assert [item["record_id"] for item in shown["shared_memory"]["entries"]] == ["old", "new"]
    assert shown["shared_memory"]["entries"][-1]["text"] == "UPDATED_DIARY_SOURCE"
    assert [item["record_id"] for item in memory["entries"]] == original_order
    assert second[-1]["role"] == "system" and "current diary emotion" in second[-1]["content"]
    assert "UPDATED_DIARY_SOURCE" not in second[0]["content"] + second[-1]["content"]
