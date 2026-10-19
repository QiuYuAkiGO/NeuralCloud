"""Whole-source historical targets never become truncation or delivery proof."""
from copy import deepcopy
from datetime import timedelta
import json
import sqlite3

import pytest

from harness.qq import decision
from harness.qq.context_window import encode_model_payload
from harness.qq.history_selection import (
    additional_body_messages, history_wire_characters, include_trusted_reply_references, select_bounded_history,
)
from harness.qq.memory_evidence import build_qq_memory_evidence
from harness.qq.memory import QQVisualMemoryStore
from harness.qq.policy import QQLatencyOptions, settings_with_policy
from harness.qq.telemetry import QQTurnTrace
from harness.qq.turn_checkpoints import QQTurnStateError
from tests.test_qq_decision import NOW, add_member, setup as setup
from tests.test_qq_prepared_turn import _binding, _resume


def _history(store, count=69):
    records = []
    for index in range(count):
        _, item = add_member(store, "20002", capture=f"bounded-{index}",
            moment=NOW + timedelta(seconds=index + 1), text=f"HISTORY_{index}:" + "整条历史含引号\"\n" * 40)
        records.append(item)
    return records


def _payload(store):
    context = store.group_context(group_id="10001", window_mode="chronological_append")
    ordered = sorted((item for member in context.members for item in member.observations),
                     key=lambda item: (item.observed_at, item.stored_at, item.record_id))
    closed_count = len(ordered) // 32 * 32
    return context, {"coverage": "partial_verified_observations", "verified_self_member_id": "29999",
        "group_context": context.model_dump(mode="json"), "conversation_window": {"block_size": 32,
            "closed_observation_ids": [item.record_id for item in ordered[:closed_count]],
            "open_observation_ids": [item.record_id for item in ordered[closed_count:]]}}


def _select(payload, required, **updates):
    return select_bounded_history(payload, required_ids=required, gate_target=4_000, body_target=14_000,
        max_characters=updates.get("max_characters", 192_000), assessment_reserve=1000, projection="compact")


def _wire_records(payload):
    return {item["record_id"]: item for block in map(json.loads, encode_model_payload(payload, projection="compact"))
            for item in [*block.get("closed_observations", []),
                         *(item for member in block.get("group_context", {}).get("members", [])
                           for item in member["observations"]) ]}


def test_bounded_first_import_is_optional_but_trigger_supplements_and_reply_are_whole(setup):
    records = _history(setup[0])
    context, payload = _payload(setup[0])
    original = deepcopy(payload)
    mandatory = {records[0].record_id, records[-2].record_id, records[-1].record_id}
    selected = _select(payload, mandatory)
    assert mandatory <= set(selected.gate.observation_ids) <= set(selected.body.observation_ids)
    assert len(selected.gate.observation_ids) < len(records)
    assert payload == original
    for stage in (selected.gate, selected.body):
        rendered = _wire_records(stage.payload)
        for item in records:
            if item.record_id in rendered:
                assert rendered[item.record_id]["text"] == item.text
                assert rendered[item.record_id]["claims"] == item.model_dump(mode="json")["claims"]
        evidence = build_qq_memory_evidence(payload=stage.payload, trusted_context=context,
            current_observation_ids={records[-1].record_id}, receipts=[], partial_receipts=[])
        assert {entry.source_id for entry in evidence} == set(stage.observation_ids)
        assert {entry.source_id for entry in evidence if entry.kind == "current"} == {records[-1].record_id}
        assert {entry.source_id for entry in evidence if entry.kind == "history"} == set(stage.observation_ids) - {records[-1].record_id}
        assert stage.characters == sum(map(len, encode_model_payload(stage.payload, projection="compact")))
        assert set(stage.payload["bounded_history"]["omitted_observation_record_ids"]).isdisjoint(stage.observation_ids)


def test_mandatory_ocr_exceeds_soft_target_whole_but_hard_budget_fails(setup):
    _, payload = _payload(setup[0])
    item = payload["group_context"]["members"][0]["observations"][0]
    item["image_analysis"] = [{"authority": "model_inference", "image_index": 1, "status": "described",
        "evidence_ref": "qq-direct-image:sha256:" + "1" * 64, "analysis_ref": None,
        "description": "图" * 6000, "ocr_text": "文" * 6000, "uncertainties": [], "error": None}]
    selected = _select(payload, {item["record_id"]})
    assert selected.gate.fits
    assert history_wire_characters(selected.gate.payload, projection="compact") > 4_000
    assert _wire_records(selected.gate.payload)[item["record_id"]]["image_analysis"] == item["image_analysis"]
    too_small = _select(payload, {item["record_id"]}, max_characters=10_000)
    assert not too_small.gate.fits and not too_small.body.fits
    assert _wire_records(too_small.gate.payload)[item["record_id"]]["image_analysis"] == item["image_analysis"]


def test_hard_budget_can_trim_more_optional_history_than_the_soft_target(setup):
    records = _history(setup[0])
    _, payload = _payload(setup[0])
    selected = _select(payload, {records[-1].record_id}, max_characters=14_000)
    assert selected.gate.fits and selected.body.fits
    assert records[-1].record_id in selected.gate.observation_ids
    assert selected.body.characters + 1000 <= 14_000
    assert len(selected.body.observation_ids) < len(_select(payload, {records[-1].record_id}).body.observation_ids)


@pytest.mark.parametrize("keep_recent_offer", [False, True])
def test_vision_offer_tracks_presented_records_without_reassigning_aliases(setup, keep_recent_offer):
    from harness.qq.context_budget import select_model_context
    from harness.qq.vision_tool import QQVisionRequest

    records = _history(setup[0])
    _, payload = _payload(setup[0])
    images = [{"image_id": "image_1", "observation_id": records[0].record_id}]
    if keep_recent_offer:
        images.append({"image_id": "image_2", "observation_id": records[-1].record_id})
    schema = QQVisionRequest.model_json_schema()
    schema["properties"]["image_ids"]["items"]["enum"] = [i["image_id"] for i in images]
    payload["vision_tool"] = {"available_images": images, "request_schema": schema}
    original = deepcopy(payload)
    selections = _select(payload, {records[-1].record_id})
    legacy = select_model_context(payload, required_observation_ids={records[-1].record_id},
        handled_observation_ids={i.record_id for i in records[:-1]}, max_characters=14_000,
        assessment_reserve=1000, projection="compact")
    for selection in (selections.gate, selections.body, legacy):
        assert records[0].record_id not in selection.observation_ids
        if keep_recent_offer:
            tool = selection.payload["vision_tool"]
            assert tool["available_images"] == images[1:]
            assert tool["request_schema"]["properties"]["image_ids"]["items"]["enum"] == ["image_2"]
        else:
            assert "vision_tool" not in selection.payload
    assert payload == original


@pytest.mark.parametrize("vision_available", [False, True])
def test_combined_body_budget_counts_extra_history_metadata_and_tool_reserve(tmp_path, vision_available):
    store = QQVisualMemoryStore(tmp_path / "crowded.sqlite3")
    records = []
    for index in range(105):
        _, item = add_member(store, "20002", capture=f"synthetic-{index}",
            moment=NOW + timedelta(seconds=index), text=f"历史消息{index}，今日天气晴朗。")
        records.append(item)
    _, payload = _payload(store)
    payload["request"] = "测" * 20_000
    original = deepcopy(payload)
    reserve = decision._assessment_reserve(include_web_plan=True, include_vision_request=vision_available)
    mandatory = {records[0].record_id, records[-1].record_id}
    selected = select_bounded_history(payload, required_ids=mandatory, gate_target=24_000, body_target=64_000,
        max_characters=120_000, assessment_reserve=reserve, projection="compact")
    extra = additional_body_messages(selected.body, selected.gate, projection="compact")
    assert selected.gate.fits and selected.body.fits
    assert selected.gate.characters + sum(len(item["content"]) for item in extra) + reserve <= 120_000
    assert mandatory <= set(selected.gate.observation_ids) < set(selected.body.observation_ids)
    assert len(selected.body.observation_ids) < len(records)
    assert payload == original
    for stage in (selected.gate, selected.body):
        rendered = _wire_records(stage.payload)
        assert rendered[records[-1].record_id]["text"] == records[-1].text
        omitted = set(stage.payload["model_context_selection"]["omitted_observation_record_ids"])
        assert omitted == {item.record_id for item in records} - set(stage.observation_ids)
    # The extra message must neither reintroduce evicted sources nor lose the
    # original identity/record contents of the history still presented.
    extra_records = {item["record_id"] for block in map(json.loads, [m["content"] for m in extra])
        for item in [*block.get("closed_observations", []),
                     *(item for member in block.get("group_context", {}).get("members", [])
                       for item in member["observations"]) ]}
    assert extra_records == set(selected.body.observation_ids) - set(selected.gate.observation_ids)


def test_crowded_context_prepares_and_resumes_without_budget_fallback(setup, monkeypatch):
    for index in range(103):
        _, latest = add_member(setup[0], "20002", capture=f"crowded-{index}",
            moment=NOW + timedelta(seconds=index + 1), text=f"历史消息{index}，今日天气晴朗。")
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", "52000")
    monkeypatch.setenv("NC_VALIDATOR_ENABLED", "false")
    config = setup[3]["config_path"]
    config.write_text(json.dumps({**json.loads(config.read_text()), "validator_enabled": False}))
    binding, trace = _binding(), QQTurnTrace()
    prepared = decision.prepare_qq_turn(**setup[3], trigger_member_id="20002", turn_binding=binding,
        request="测" * 4000, web_capabilities={"enabled": True}, trace=trace,
        _reply_reference_record_ids=[setup[4][0].record_id])
    assert prepared["kind"] == "prepared"
    counters = trace.snapshot()["counters"]
    assert counters["context_combined_characters"] + counters["context_assessment_reserved_characters"] <= 104_000
    assert "context_budget_exceeded" not in counters
    frozen = decision.get_qq_turn_checkpoint(memory_db=setup[0].path, prepare_id=prepared["prepare_id"])["snapshot"]
    body_ids = set(frozen["presentation"]["body_observation_ids"])
    assert {setup[4][0].record_id, latest.record_id} <= body_ids
    assert len(body_ids) < 105
    result = _resume(setup, binding, prepared)
    assert result.outcome == "ready" and result.model_call_counts == {"gate": 1, "persona": 1}
    assert set(result.source_record_ids) == set(result.seen_observation_record_ids) == body_ids
    actual = sum(len(item["content"]) for item in setup[1].chat_calls[0] if item["role"] != "system")
    assert actual <= 104_000


def test_shadow_keeps_all_actual_history_while_reporting_bounded_candidate(setup, monkeypatch):
    records = _history(setup[0])
    monkeypatch.setenv("NC_QQ_HISTORY_SELECTION", "shadow")
    monkeypatch.setenv("NC_QQ_GATE_HISTORY_SOFT_CHARACTERS", "4000")
    monkeypatch.setenv("NC_QQ_BODY_HISTORY_SOFT_CHARACTERS", "14000")
    setup[1].choice = '{"action":"wait","reason":"只是旁听。"}'
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**setup[3], trigger_member_id="20002", trace=trace)
    assert result.outcome == "natural_wait"
    assert {item.record_id for item in records} <= set(result.source_record_ids)
    counters = trace.snapshot()["counters"]
    assert counters["context_gate_history_candidate_observations"] < len(result.source_record_ids)
    assert counters["context_selected_observations"] == len(result.source_record_ids)


def test_bounded_wait_marks_only_presented_history_seen(setup, monkeypatch):
    records = _history(setup[0])
    monkeypatch.setenv("NC_QQ_HISTORY_SELECTION", "bounded")
    monkeypatch.setenv("NC_QQ_GATE_HISTORY_SOFT_CHARACTERS", "4000")
    monkeypatch.setenv("NC_QQ_BODY_HISTORY_SOFT_CHARACTERS", "14000")
    setup[1].choice = '{"action":"wait","reason":"只是旁听。"}'
    result = decision.decide_qq_reply(**setup[3], trigger_member_id="20002")
    assert result.outcome == "natural_wait"
    assert records[-1].record_id in result.source_record_ids
    assert len(result.source_record_ids) < len(records)
    assert set(result.seen_observation_record_ids) == set(result.source_record_ids)
    assert set(result.new_observation_record_ids) == set(result.source_record_ids)
    assert result.style_diagnostics["body_presented_observations"] == 0


@pytest.mark.parametrize("history_mode", [None, "shadow"])
def test_default_bounds_unhandled_backlog_outside_rollout_before_gate(setup, monkeypatch, history_mode):
    records = _history(setup[0], count=109)
    monkeypatch.delenv("NC_QQ_HISTORY_SELECTION", raising=False)
    if history_mode is not None:
        monkeypatch.setenv("NC_QQ_HISTORY_SELECTION", history_mode)
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", "60000")
    monkeypatch.setenv("NC_QQ_LATENCY_CONVERSATIONS", "[]")
    load = decision._load_configuration

    def excluded_configuration(*args):
        settings, preset = load(*args)
        policy = QQLatencyOptions.from_settings(settings).for_conversation(
            group_id="10001", self_member_id="29999")
        return settings_with_policy(settings, policy), preset

    monkeypatch.setattr(decision, "_load_configuration", excluded_configuration)
    setup[1].choice = '{"action":"wait","reason":"只是旁听。"}'
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**setup[3], trigger_member_id="20002", trace=trace)
    counters = trace.snapshot()["counters"]
    assert counters["context_compact_projection"] == 0
    if history_mode == "shadow":
        assert result.outcome == "error_fallback"
        assert counters["context_selected_observations"] == 111
        assert counters["context_budget_exceeded_final"] == 1
        assert not setup[1].gate_messages
    else:
        assert result.outcome == "natural_wait"
        assert setup[1].gate_messages
        assert "context_budget_exceeded" not in counters
        assert counters["context_selected_observations"] < 111
        assert counters["context_final_characters"] + counters["context_assessment_reserved_characters"] <= 120_000
        assert records[-1].record_id in result.source_record_ids
        assert set(result.seen_observation_record_ids) == set(result.source_record_ids)
        assert set(result.new_observation_record_ids) == set(result.source_record_ids)


def test_bounded_resume_uses_frozen_extra_history_after_exact_gate_prefix(setup, monkeypatch):
    records = _history(setup[0])
    monkeypatch.setenv("NC_QQ_HISTORY_SELECTION", "bounded")
    monkeypatch.setenv("NC_QQ_GATE_HISTORY_SOFT_CHARACTERS", "4000")
    monkeypatch.setenv("NC_QQ_BODY_HISTORY_SOFT_CHARACTERS", "14000")
    binding = _binding()
    prepared = decision.prepare_qq_turn(**setup[3], trigger_member_id="20002", turn_binding=binding)
    row = decision.get_qq_turn_checkpoint(memory_db=setup[0].path, prepare_id=prepared["prepare_id"])
    frozen = row["snapshot"]
    gate_ids = set(frozen["presentation"]["gate_observation_ids"])
    body_ids = set(frozen["presentation"]["body_observation_ids"])
    assert gate_ids < body_ids < {item.record_id for item in records} | {item.record_id for item in setup[4]}
    assert frozen["body_extra_messages"]
    add_member(setup[0], "20003", capture="after-prepare", moment=NOW + timedelta(hours=2), text="MUST_NOT_REQUERY")
    result = _resume(setup, binding, prepared)
    assert result.outcome == "ready"
    body_call = setup[1].chat_calls[0]
    expected = [*frozen["messages"], {"role": "assistant", "content": frozen["raw_gate"]}, *frozen["body_extra_messages"]]
    assert body_call[:len(expected)] == expected
    assert "MUST_NOT_REQUERY" not in str(body_call)
    assert set(result.seen_observation_record_ids) == set(result.source_record_ids) == body_ids
    assert result.style_diagnostics["body_presented_observations"] == len(body_ids)
    assert len(setup[1].gate_messages) == len(setup[1].chat_calls) == 1


def test_extra_body_records_must_still_match_original_source_on_resume(setup, monkeypatch):
    _history(setup[0])
    monkeypatch.setenv("NC_QQ_HISTORY_SELECTION", "bounded")
    monkeypatch.setenv("NC_QQ_GATE_HISTORY_SOFT_CHARACTERS", "4000")
    monkeypatch.setenv("NC_QQ_BODY_HISTORY_SOFT_CHARACTERS", "14000")
    binding = _binding()
    prepared = decision.prepare_qq_turn(**setup[3], trigger_member_id="20002", turn_binding=binding)
    frozen = decision.get_qq_turn_checkpoint(memory_db=setup[0].path, prepare_id=prepared["prepare_id"])["snapshot"]
    extra = set(frozen["presentation"]["body_observation_ids"]) - set(frozen["presentation"]["gate_observation_ids"])
    target = next(iter(extra))
    value = frozen["source_records"]["qq_visual_observations"][target]
    value["text"] += " amended"
    with sqlite3.connect(setup[0].path) as db:
        db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?", (json.dumps(value), target))
    with pytest.raises(QQTurnStateError, match="source_changed"):
        _resume(setup, binding, prepared)
    assert not setup[1].chat_calls


def test_trusted_reply_outside_append_window_is_loaded_exactly_and_pinned(setup, monkeypatch):
    original = setup[4][0]
    _history(setup[0], count=160)
    monkeypatch.setenv("NC_QQ_HISTORY_SELECTION", "bounded")
    monkeypatch.setenv("NC_QQ_GATE_HISTORY_SOFT_CHARACTERS", "4000")
    result = decision.decide_qq_reply(**setup[3], trigger_member_id="20002",
                                    _reply_reference_record_ids=[original.record_id])
    assert result.outcome == "ready"
    assert original.record_id in result.source_record_ids
    assert original.text in str(setup[1].gate_messages)
    assert len(result.source_record_ids) < 100


def test_exact_reference_lookup_rejects_other_scope_and_missing_records(setup):
    context, _ = _payload(setup[0])
    _, foreign = add_member(setup[0], "20003", group="10002", capture="foreign-ref", text="OTHER_SCOPE")
    for record_id in (foreign.record_id, "qq-observation:missing"):
        with pytest.raises(ValueError, match="qq_required_reference_missing"):
            include_trusted_reply_references(setup[0].path, context, [record_id])


def test_remaining_closed_blocks_keep_exact_bytes_when_old_block_is_removed(setup):
    records = _history(setup[0], count=100)
    _, payload = _payload(setup[0])
    selected = select_bounded_history(payload, required_ids={records[0].record_id, records[-1].record_id},
        gate_target=4_000, body_target=60_000, max_characters=192_000, assessment_reserve=1000, projection="compact")
    original = encode_model_payload(payload, projection="compact")[1:-1]
    remaining = encode_model_payload(selected.body.payload, projection="compact")[1:-1]
    assert remaining
    for block in remaining:
        assert block in original
    assert additional_body_messages(selected.body, selected.gate, projection="compact")
