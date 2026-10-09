"""Verified QQ window and shared gate/persona emotion guidance, without models."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.emotion.models import EmotionGuidance
from harness.qq import decision
from harness.qq.emotion_window import build_qq_emotion_messages, remove_legacy_emotion_parameters
from harness.qq.memory import (
    QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput, protocol_message_evidence_ref,
)
from harness.memory.service import build_inmemory_service
from harness.homeostasis.models import HomeostaticInteroceptiveSummary
from tests.test_qq_decision import NOW, setup as setup
from tests.test_qq_decision_segments import aggregate, plan, segment


def add_observation(store, *, event="incoming", group="10001", member="20001", at=NOW, text="当前消息"):
    protocol = group.startswith("private:")
    evidence = ["qq-protocol://sha256/" + "a" * 64] if protocol else ["test-evidence://verified"]
    verification = "qq_protocol_verified" if protocol else "qq_ui_verified"
    profile_input = QQVerifiedProfileInput(
        group_id=group, member_id=member, display_name="name", display_name_source="qq_metadata" if protocol else "qq_profile",
        verification=verification, capture_id="profile:" + event, observed_at=at,
        group_evidence=evidence, member_evidence=evidence,
    )
    profile = (store.record_protocol_profile(profile_input) if protocol else store.record_profile(profile_input)).profile
    observation_input = QQVisualObservationInput(
        group_id=group, member_id=member, profile_record_id=profile.record_id, capture_id=event,
        capture_item_id=event, observed_at=at, text=text, evidence=evidence,
        author_attribution=verification,
        attribution_evidence=[*evidence, protocol_message_evidence_ref(group, member, "123")] if protocol else evidence,
        message_id="123" if protocol else None, message_id_source="qq_metadata" if protocol else None,
    )
    return (store.record_protocol_observation(observation_input) if protocol
            else store.record_observation(observation_input)).observation


def test_window_uses_recent_sources_and_exact_trigger_without_search(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    old = add_observation(store, event="old", at=NOW - timedelta(hours=1), text="历史愤怒")
    current = add_observation(store, event="trigger", text="当前刺激")
    add_observation(store, event="future", at=NOW + timedelta(seconds=1))
    add_observation(store, event="elsewhere", group="10002", text="其他群秘密")
    own = add_observation(store, event="own", member="29999", text="自己的旧回复")
    scope, messages, new_ids = build_qq_emotion_messages(
        memory_db=store.path, group_id="10001", self_member_id="29999", as_of=NOW,
        trigger_event_id="trigger", trigger_member_id="20001",
    )
    assert scope == "qq:group:10001"
    assert {item.source_id for item in messages} == {current.record_id, own.record_id}
    assert old.record_id not in new_ids and new_ids == [current.record_id]
    assert next(item for item in messages if item.source_id == own.record_id).role == "character"
    assert all(item.scope == scope for item in messages)


def test_window_caps_at_twenty_but_keeps_the_bound_trigger(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    trigger = add_observation(store, event="trigger", at=NOW - timedelta(minutes=10))
    for index in range(25):
        add_observation(store, event=f"later-{index}", at=NOW - timedelta(seconds=index))
    _, messages, new_ids = build_qq_emotion_messages(
        memory_db=store.path, group_id="10001", self_member_id="29999", as_of=NOW,
        trigger_event_id="trigger", trigger_member_id="20001",
    )
    assert len(messages) == 20 and trigger.record_id in {item.source_id for item in messages}
    assert new_ids == [trigger.record_id]


def test_window_merges_multiple_triggers_and_keeps_all_new_ids(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    first = add_observation(store, event="trigger-a", at=NOW - timedelta(seconds=2), text="第一条")
    second = add_observation(store, event="trigger-b", text="第二条")
    scope, messages, new_ids = build_qq_emotion_messages(
        memory_db=store.path, group_id="10001", self_member_id="29999", as_of=NOW,
        triggers=[("trigger-a", "20001"), ("trigger-b", "20001")],
    )
    assert scope == "qq:group:10001"
    assert new_ids == [first.record_id, second.record_id]
    assert {first.record_id, second.record_id} <= {item.source_id for item in messages}


def test_own_send_receipt_is_processed_context_and_not_a_new_source(setup):
    from harness.emotion import EmotionInput, EmotionService
    from harness.llm.client import MockLLM
    from eval.emotional_expression_diagnostics.emotion import offline_settings

    store = setup[0]
    result = plan(setup, count=1)
    receipt = segment(setup, result, index=0)
    trigger = add_observation(store, event="trigger", text="外部刺激")
    _, messages, new_ids = build_qq_emotion_messages(
        memory_db=store.path, group_id="10001", self_member_id="29999", as_of=NOW + timedelta(seconds=6),
        trigger_event_id="trigger", trigger_member_id="20001",
    )
    assert receipt.receipt_id in {item.source_id for item in messages}
    assert new_ids == [trigger.record_id]
    service = EmotionService(offline_settings("rule-fallback"), MockLLM())
    service.observe(EmotionInput(context_id="receipts", scope="qq:group:10001",
                                 as_of=NOW + timedelta(seconds=6), messages=messages, new_source_ids=new_ids))
    sources = service.snapshot()["sources"]
    own = next(item for item in sources.values() if item["source_id"] == receipt.receipt_id)
    assert own["processed"] is True and own["role"] == "character"
    fresh = next(item for item in sources.values() if item["source_id"] == trigger.record_id)
    assert fresh["processed"] is True and fresh["role"] == "user"


def test_idle_window_does_not_reappraise_history(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    add_observation(store)
    _, messages, new_ids = build_qq_emotion_messages(
        memory_db=store.path, group_id="10001", self_member_id="29999", as_of=NOW,
    )
    assert len(messages) == 1 and new_ids == []


def test_private_window_cannot_read_another_peer(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    current = add_observation(store, group="private:29999:20001", event="trigger")
    add_observation(store, group="private:29999:20002", member="20002", event="secret", text="其他私聊秘密")
    scope, messages, new_ids = build_qq_emotion_messages(
        memory_db=store.path, group_id="private:29999:20001", self_member_id="29999", as_of=NOW,
        trigger_event_id="trigger", trigger_member_id="20001",
    )
    assert scope == "qq:private:29999:20001"
    assert [item.source_id for item in messages] == new_ids == [current.record_id]
    with pytest.raises(ValueError, match="private_scope"):
        build_qq_emotion_messages(memory_db=store.path, group_id="private:29999:20001",
                                 self_member_id="39999", as_of=NOW)


def test_missing_or_tampered_trigger_fails_closed(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    current = add_observation(store, event="trigger")
    options = dict(memory_db=store.path, group_id="10001", self_member_id="29999", as_of=NOW,
                   trigger_event_id="trigger", trigger_member_id="20001")
    with pytest.raises(ValueError, match="trigger_missing"):
        build_qq_emotion_messages(**{**options, "trigger_event_id": "missing"})
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE qq_visual_observations SET member_id='20002' WHERE record_id=?", (current.record_id,))
    with pytest.raises(ValueError, match="trigger_missing"):
        build_qq_emotion_messages(**options)


def test_delivered_segments_keep_stable_identity_after_complete_aggregate(setup):
    from harness.emotion import EmotionInput, EmotionService
    from harness.llm.client import MockLLM
    from eval.emotional_expression_diagnostics.emotion import offline_settings

    store = setup[0]
    result = plan(setup, count=2)
    first = segment(setup, result, index=0)
    options = dict(memory_db=store.path, group_id="10001", self_member_id="29999",
                   as_of=NOW + timedelta(seconds=6))
    _, messages, new_ids = build_qq_emotion_messages(**options)
    own = [item for item in messages if item.role == "character"]
    assert [(item.source_id, item.text) for item in own] == [(first.receipt_id, result.reply_segments[0])]
    assert new_ids == []
    service = EmotionService(offline_settings("rule-fallback"), MockLLM())
    request = EmotionInput(context_id="partial", scope="qq:group:10001", as_of=options["as_of"],
                           messages=messages, new_source_ids=[])
    service.observe(request)
    second = segment(setup, result, index=1)
    full = aggregate(setup, result, [first, second])
    _, messages, _ = build_qq_emotion_messages(**options)
    own = [item for item in messages if item.role == "character"]
    expected = [(first.receipt_id, result.reply_segments[0]), (second.receipt_id, result.reply_segments[1])]
    assert [(item.source_id, item.text) for item in own] == expected
    service.observe(request.model_copy(update={"context_id": "complete", "messages": messages}))
    merged = service.snapshot()["reservations"]["complete"]["window"]
    assert [(item["source_id"], item["text"]) for item in merged if item["role"] == "character"] == expected
    assert full.receipt_id not in {item["source_id"] for item in merged}


def test_protocol_self_echo_waits_for_durable_character_receipt(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "qq.db")
    add_observation(store, group="private:29999:20001", member="29999", event="self-echo")
    _, messages, _ = build_qq_emotion_messages(memory_db=store.path, group_id="private:29999:20001",
                                               self_member_id="29999", as_of=NOW)
    assert messages == []


def test_emotion_parameter_projection_preserves_factual_history_and_source_text():
    value = {"nested": {"psych": {"short": "anger"}, "homeostatic_interoception": {"energy": .9},
                        "relationship": {"trust": .1, "closeness": .2, "shared_history_summary": "一起讨论过"}},
             "text": '对方原话: {"trust": 0.1}', "confidence": .7}
    projected = remove_legacy_emotion_parameters(value)
    assert projected["nested"] == {"relationship": {"shared_history_summary": "一起讨论过"}}
    assert projected["text"] == value["text"] and projected["confidence"] == .7
    assert "psych" in value["nested"]


def test_same_emotion_brief_reaches_gate_and_persona_once_and_is_replayable(setup, monkeypatch):
    _, client, _, options, _ = setup
    settings, preset = decision._load_configuration(options["config_path"], options["preset_path"])
    monkeypatch.setattr(decision, "_load_configuration", lambda *_: (settings.model_copy(update={"emotion_mode": "on"}), preset))
    guidance = EmotionGuidance(text="现在有明显怒意，回应简短而坚定，不迁怒无关对象。", context_id="turn-1",
                               scope="qq:group:10001", revision=1)
    result = decision.decide_qq_reply(**options, emotion_guidance=guidance)
    assert result.outcome == "ready", result.reason
    for call in (client.gate_messages[0], client.chat_calls[0]):
        text = "\n".join(item["content"] for item in call)
        assert text.count(guidance.text) == 1
        assert "可以暂时突破轻柔" in text
        assert "只表达由当前话题引起的感受" not in text
    replay = decision.decide_qq_reply(**options, emotion_guidance=guidance)
    assert replay.replayed and replay.decision_id == result.decision_id
    assert len(client.gate_messages) == len(client.chat_calls) == 1


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_disabled_modes_do_not_inject_a_prepared_guidance(setup, monkeypatch, mode):
    _, client, _, options, _ = setup
    settings, preset = decision._load_configuration(options["config_path"], options["preset_path"])
    monkeypatch.setattr(decision, "_load_configuration", lambda *_: (settings.model_copy(update={"emotion_mode": mode}), preset))
    guidance = EmotionGuidance(text="NEVER_INJECT_SHADOW_BRIEF", context_id="turn-1", scope="qq:group:10001", revision=1)
    result = decision.decide_qq_reply(**options, emotion_guidance=guidance)
    assert result.outcome == "ready"
    assert guidance.text not in json.dumps(client.gate_messages + client.chat_calls, ensure_ascii=False)


def test_guidance_from_a_different_conversation_is_rejected_before_model(setup, monkeypatch):
    _, client, _, options, _ = setup
    settings, preset = decision._load_configuration(options["config_path"], options["preset_path"])
    monkeypatch.setattr(decision, "_load_configuration", lambda *_: (settings.model_copy(update={"emotion_mode": "on"}), preset))
    guidance = EmotionGuidance(text="其他会话提示", context_id="elsewhere", scope="qq:group:10002", revision=1)
    with pytest.raises(ValueError, match="current QQ conversation"):
        decision.decide_qq_reply(**options, emotion_guidance=guidance)
    assert not client.gate_messages and not client.chat_calls


def test_on_mode_removes_legacy_state_from_complete_shared_payload(setup):
    _, client, _, options, _ = setup
    settings, preset = decision._load_configuration(options["config_path"], options["preset_path"])
    settings = settings.model_copy(update={"emotion_mode": "on"})
    memory = build_inmemory_service(settings=settings, llm=client)
    memory.bootstrap_persona(preset.persona)
    shared = SimpleNamespace(settings=settings, preset=preset, llm=client, memory=memory, source_ids=("shared:1",),
        context={"entries": [{"record_id": "shared:1", "text": "一起讨论过任务",
                              "channel": "qq_group", "session_id": "qq:group:10001",
                              "author_principal": "qq:member:20001", "psych": {"short": {"mood": "angry"}},
                              "relationship": {"trust": .1, "recent_tension": .9, "shared_history_summary": "讨论任务"}}]},
        derived_memory={"entries": [], "homeostatic_interoception": {"normalized_deviation": .9}})
    guidance = EmotionGuidance(text="心情有些烦躁，直接回应当前话题。", context_id="turn-1", scope="qq:group:10001", revision=1)
    summary = HomeostaticInteroceptiveSummary(profile_ref="functional-proxy-v1@3", snapshot_revision=7,
                                            salience="nominal", episode_signature="test:nominal")
    try:
        result = decision.decide_qq_reply(**options, shared_owner=shared, emotion_guidance=guidance,
                                         homeostatic_interoception=summary)
    finally:
        memory.close()
    assert result.outcome == "ready", result.reason
    rendered = json.dumps(client.gate_messages + client.chat_calls, ensure_ascii=False)
    for field in ("homeostatic_interoception", "normalized_deviation", "recent_tension", '"psych"'):
        assert field not in rendered
    assert "一起讨论过任务" in rendered and "讨论任务" in rendered


@pytest.mark.parametrize("container", ["shared_memory", "derived_memory"])
def test_emotion_projection_keeps_memory_evidence_exact_and_rejects_source_tampering(setup, container):
    from harness.qq.memory_evidence import build_qq_memory_evidence

    store, _, _, _, observations = setup
    context = store.group_context(group_id="10001")
    shared_record = {"record_id": "shared:1", "text": "一起讨论过任务", "channel": "qq_group",
                     "session_id": "qq:group:10001", "author_principal": "qq:member:20001",
                     "evidence_refs": ["verified:shared"], "psych": {"mood": "angry"}}
    derived_record = {"record_id": "derived:1", "current_impression": "愿意讨论细节", "status": "provisional",
                      "source_channel": "qq_group", "source_conversation_id": "qq:group:10001",
                      "source_refs": ["shared:1"], "trust": .3}
    owner = SimpleNamespace(settings=SimpleNamespace(emotion_mode="on"),
                            context={"entries": [shared_record]}, derived_memory={"entries": [derived_record]})
    payload = {"group_context": decision.project_group_context(context.model_dump(mode="json")),
               "verified_self_member_id": "29999", "shared_memory": deepcopy(owner.context),
               "derived_memory": deepcopy(owner.derived_memory)}
    payload = remove_legacy_emotion_parameters(payload)
    options = dict(trusted_context=context, current_observation_ids={item.record_id for item in observations},
                   receipts=[], partial_receipts=[], shared_owner=owner)
    entries = build_qq_memory_evidence(payload=payload, **options)
    assert {entry.text for entry in entries if entry.kind != "current"} == {"一起讨论过任务", "愿意讨论细节"}
    assert {entry.text for entry in entries if entry.kind == "current"} == {item.text for item in observations}
    assert owner.context["entries"][0]["psych"] == {"mood": "angry"}
    assert owner.derived_memory["entries"][0]["trust"] == .3
    field = "text" if container == "shared_memory" else "current_impression"
    forged = deepcopy(payload)
    forged[container]["entries"][0][field] = "未经核验的替换内容"
    with pytest.raises(ValueError, match="projection mismatch"):
        build_qq_memory_evidence(payload=forged, **options)
    forged = deepcopy(payload)
    source_field = "evidence_refs" if container == "shared_memory" else "source_refs"
    forged[container]["entries"][0][source_field] = ["forged-source"]
    with pytest.raises(ValueError, match="projection mismatch"):
        build_qq_memory_evidence(payload=forged, **options)
    for mode in ("off", "shadow"):
        owner.settings.emotion_mode = mode
        with pytest.raises(ValueError, match="projection mismatch"):
            build_qq_memory_evidence(payload=payload, **options)
