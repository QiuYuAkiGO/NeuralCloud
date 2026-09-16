"""Regression for valid QQ context rejected after cache-envelope splitting."""

from copy import deepcopy
from datetime import timedelta
import json
import shutil
import sqlite3

import pytest

from harness.qq import decision
from harness.qq.context_budget import select_model_context
from harness.qq.context_window import encode_model_payload
from harness.qq.conversation import conversation_audience, private_scope_id
from harness.qq.memory import QQGroupContext, protocol_message_evidence_ref
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_decision import (
    NOW, add_member, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup,
)
from tests.test_qq_protocol_memory import EVIDENCE, protocol_observation, protocol_profile


@pytest.mark.parametrize("private", [False, True], ids=["group", "private"])
def test_reported_176672_character_context_drops_whole_closed_block_and_keeps_current_image(setup, private):
    store = setup[0]
    group = private_scope_id("29999", "20003") if private else "10003"
    _, profile = protocol_profile(store, group=group)
    records = []
    for index in range(33):
        message_id = str(7000 + index)
        item = protocol_observation(profile, text="已核验原文" + "原" * 4200,
            capture_id=f"envelope-budget-{index}", capture_item_id=str(index), message_id=message_id,
            observed_at=NOW + timedelta(seconds=index),
            attribution_evidence=[EVIDENCE, protocol_message_evidence_ref(group, "20003", message_id)])
        records.append(store.record_protocol_observation(item).observation)
    payload = {"coverage": "partial_verified_observations",
        "group_context": store.group_context(group_id=group, window_mode="chronological_append").model_dump(mode="json"),
        "verified_self_member_id": "29999", "verified_self_aliases": [],
        "emotion_guidance_text": decision.render_emotion_guidance(),
        "new_observation_record_ids": [records[-1].record_id],
        "conversation_window": {"mode": "chronological_append", "block_size": 32,
            "closed_observation_ids": [item.record_id for item in records[:-1]],
            "open_observation_ids": [records[-1].record_id]}}
    options = {"required_observation_ids": {records[-1].record_id},
        "handled_observation_ids": {item.record_id for item in records[:-1]},
        "assessment_reserve": decision._assessment_reserve()}
    full = select_model_context(payload, max_characters=1_000_000, **options)
    # Reproduce the incident's actual outgoing context length. This remains a
    # valid complete observation, not truncation or an arbitrary reserve pad.
    padding = 176_672 - full.characters
    assert 0 < padding < 20_000
    payload["group_context"]["members"][0]["observations"][0]["text"] += "补" * padding
    QQGroupContext.model_validate(payload["group_context"])
    before = deepcopy(payload)
    full = select_model_context(payload, max_characters=1_000_000, **options)
    assert full.characters == 176_672
    assert full.characters + 15_489 == 192_161  # Original production measurement.
    old_selection_payload = deepcopy(full.payload)
    old_selection_payload.pop("emotion_guidance_text")
    assert len(decision._canonical(old_selection_payload)) + 15_489 <= 192_000

    actual = select_model_context(payload, max_characters=192_000, **options)
    assert actual.fits and actual.characters + options["assessment_reserve"] <= 192_000
    assert actual.characters == sum(len(message) for message in encode_model_payload(actual.payload))
    assert actual.payload["model_context_selection"]["omitted_observation_record_ids"] == [
        item.record_id for item in records[:-1]]
    retained = actual.payload["group_context"]["members"][0]["observations"]
    assert [item["record_id"] for item in retained] == [records[-1].record_id]
    assert retained[0]["text"] == records[-1].text
    assert retained[0]["image_analysis"] == records[-1].model_dump(mode="json")["image_analysis"]
    assert retained[0]["profile_record_id"] in actual.profile_ids
    base, turn = map(json.loads, encode_model_payload(actual.payload))
    assert "emotion_guidance_text" not in base
    assert turn["emotion_guidance_text"] == payload["emotion_guidance_text"]
    assert payload == before


@pytest.mark.parametrize("emotion_mode", ["off", "on"])
def test_decision_reselects_optional_history_at_exact_encoded_boundary(setup, tmp_path, monkeypatch, emotion_mode):
    monkeypatch.setenv("NC_EMOTION_MODE", emotion_mode)
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", "96000")
    store, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"先前消息已考虑。"}'
    decision.decide_qq_reply(**kwargs)
    _, latest = add_member(store, "20001", text='当前图片说明："完整问题"\n' + "新" * 500,
                           capture="envelope-current", moment=NOW + timedelta(seconds=2))
    reference = tmp_path / "reference.sqlite3"
    shutil.copy2(store.path, reference)
    client.choice = '{"action":"speak","reason":"回应当前新问题。"}'
    guidance = {"scope": conversation_audience("10001"), "text": "平静地回应。" * 20}
    reference_result = decision.decide_qq_reply(**{**kwargs, "memory_db": reference}, emotion_guidance=guidance)
    assert reference_result.outcome == "ready"
    complete_size = sum(len(message["content"]) for message in client.gate_messages[-1] if message["role"] == "user")
    budget = (complete_size + decision._assessment_reserve() - 1) // 2 * 2
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", str(budget // 2))
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, emotion_guidance=guidance, trace=trace)
    assert result.outcome == "ready", result.reason
    gate = decision.combined_user_payload(client.gate_messages[-1])
    assert gate["model_context_selection"]["omitted_prior_decision_ids"]
    assert latest.record_id in result.source_record_ids
    actual_size = sum(len(message["content"]) for message in client.gate_messages[-1] if message["role"] == "user")
    assert actual_size + decision._assessment_reserve() <= budget
    assert trace.snapshot()["counters"]["context_final_characters"] == actual_size
    assert "context_budget_exceeded" not in trace.snapshot()["counters"]
    assert ("emotion_guidance_text" in gate) is (emotion_mode == "on")


def test_emotion_render_error_never_reserves_a_decision_or_calls_models(setup, monkeypatch):
    monkeypatch.setenv("NC_EMOTION_MODE", "on")
    store, client, builds, kwargs, _ = setup

    def failed_render(*args, **options):
        raise ValueError("synthetic critical homeostatic state")

    monkeypatch.setattr(decision, "render_emotion_guidance", failed_render)
    with pytest.raises(ValueError, match="critical homeostatic"):
        decision.decide_qq_reply(**kwargs)
    assert not builds and not client.gate_messages
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT COUNT(*) FROM qq_reply_decisions").fetchone()[0] == 0
