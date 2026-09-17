"""Compact wire input retains the full evidence selected by QQ admission."""
from copy import deepcopy
from datetime import timedelta
import json

import pytest

from harness.qq import decision
from harness.qq.context_budget import select_model_context
from harness.qq.context_window import encode_model_payload
from harness.qq.memory_evidence import build_qq_memory_evidence
from tests.test_qq_decision import NOW, add_member, setup as setup


def _selected(store, *, count=33):
    for index in range(count):
        add_member(store, "20001", capture=f"projection-{index}", moment=NOW + timedelta(seconds=index + 1),
                   text=f"第{index}条完整原文\n含引号\"与多字节🙂")
    context = store.group_context(group_id="10001", window_mode="chronological_append")
    observations = sorted((item for member in context.members for item in member.observations),
                          key=lambda item: (item.observed_at, item.stored_at, item.record_id))
    closed_count = len(observations) // 32 * 32
    payload = {"group_context": context.model_dump(mode="json"), "coverage": "partial_verified_observations",
        "verified_self_member_id": "29999", "conversation_window": {
            "block_size": 32, "closed_observation_ids": [item.record_id for item in observations[:closed_count]],
            "open_observation_ids": [item.record_id for item in observations[closed_count:]]}}
    selected = select_model_context(payload, required_observation_ids={item.record_id for item in observations},
        handled_observation_ids=set(), max_characters=192_000, assessment_reserve=decision._assessment_reserve(),
        projection="compact")
    return context, selected


def test_identity_dictionary_deduplicates_without_changing_evidence_or_selected_records(setup):
    store = setup[0]
    context, selected = _selected(store)
    original = deepcopy(selected.payload)
    compact = encode_model_payload(selected.payload, projection="compact")
    legacy = encode_model_payload(selected.payload)
    decoded = decision.combined_user_payload([{"role": "user", "content": item} for item in compact])
    assert len(decoded["group_context"]["identity_snapshots"]) == 2
    selected_records = {item["record_id"]: item for member in selected.payload["group_context"]["members"]
                        for item in member["observations"]}
    wire_records = {item["record_id"]: item for member in decoded["group_context"]["members"]
                    for item in member["observations"]}
    assert wire_records.keys() == selected_records.keys() == set(selected.observation_ids)
    assert selected.payload == original
    for record_id, record in wire_records.items():
        assert record["text"] == selected_records[record_id]["text"]
        assert record["claims"] == selected_records[record_id]["claims"]
        identity = decoded["group_context"]["identity_snapshots"][record["identity_ref"]]
        assert identity["member_id"] == record["member_id"]
        assert "capture_id" not in record and "stored_at" not in record
    entries = build_qq_memory_evidence(payload=selected.payload, trusted_context=context,
        current_observation_ids=set(), receipts=[], partial_receipts=[])
    assert {entry.source_id for entry in entries} == set(selected.observation_ids)
    assert selected.characters == sum(map(len, compact))
    assert sum(map(len, compact)) < sum(map(len, legacy)) * 0.65


def test_projection_keeps_current_ocr_and_original_names_and_never_truncates(setup):
    store = setup[0]
    _, selected = _selected(store, count=1)
    payload = deepcopy(selected.payload)
    member = payload["group_context"]["members"][0]
    item = member["observations"][-1]
    item["image_analysis"] = [{"authority": "model_inference", "image_index": 1, "status": "described",
        "description": "描述" * 2500, "ocr_text": "完整OCR" * 1000, "uncertainties": ["低分辨率"],
        "evidence_ref": "qq-direct-image:sha256:" + "a" * 64, "error": None}]
    member["profiles"][-1]["display_name"] = "新的群名片"
    original = deepcopy(payload)
    wire = decision.combined_user_payload([{"role": "user", "content": value}
                                          for value in encode_model_payload(payload, projection="compact")])
    member_wire = wire["group_context"]["members"][0]
    assert member_wire["observations"][-1]["image_analysis"] == item["image_analysis"]
    assert len(wire["group_context"]["identity_snapshots"]) == 3
    assert payload == original


def test_closed_wire_prefix_remains_identical_after_append_with_new_identity(setup):
    store = setup[0]
    _, first = _selected(store)
    before = encode_model_payload(first.payload, projection="compact")
    add_member(store, "20003", capture="new-identity", moment=NOW + timedelta(seconds=90), text="新成员原文")
    _, second = _selected(store, count=0)
    after = encode_model_payload(second.payload, projection="compact")
    assert before[:-1] == after[:-1]
    assert before[-1] != after[-1]


def test_compact_budget_measures_exact_wire_and_rejects_scope_tampering(setup):
    _, selected = _selected(setup[0], count=1)
    budget = selected.characters + decision._assessment_reserve()
    options = dict(required_observation_ids=set(selected.observation_ids), handled_observation_ids=set(),
                   assessment_reserve=decision._assessment_reserve(), projection="compact")
    assert select_model_context(selected.payload, max_characters=budget, **options).fits
    assert not select_model_context(selected.payload, max_characters=budget - 1, **options).fits
    bad = deepcopy(selected.payload)
    bad["group_context"]["members"][0]["profiles"][0]["group_id"] = "10002"
    with pytest.raises(ValueError, match="scope mismatch"):
        encode_model_payload(bad, projection="compact")
    blocks = [json.loads(item) for item in encode_model_payload(selected.payload, projection="compact")]
    assert blocks[0]["model_projection"] == "qq-compact-evidence-v1"


def test_live_decision_uses_compact_wire_and_direct_body_without_generic_helpers(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    monkeypatch.setenv("NC_QQ_CONTEXT_PROJECTION", "compact")
    monkeypatch.setenv("NC_QQ_BODY_PIPELINE", "direct")
    from harness.pipeline.parser import ContextParser
    from harness.router.gate import ComplexityGate
    from harness.subagents.interpreter import SocialInterpreter

    def forbidden(*args, **kwargs):
        pytest.fail("QQ prepared speech called a redundant generic helper")

    monkeypatch.setattr(ContextParser, "parse", forbidden)
    monkeypatch.setattr(ComplexityGate, "evaluate", forbidden)
    monkeypatch.setattr(SocialInterpreter, "interpret", forbidden)
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.outcome == "ready"
    assert len(client.gate_messages) == 1 and len(client.chat_calls) == 1
    assert client.chat_calls[0][:len(client.gate_messages[0])] == client.gate_messages[0]
