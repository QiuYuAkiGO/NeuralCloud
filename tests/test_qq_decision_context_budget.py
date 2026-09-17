"""Receipt proof stays durable without consuming the model's dialogue budget."""

from datetime import timedelta
import json
import sqlite3

import httpx
import pytest

from harness.qq import decision
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_decision import NOW, add_member, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup
from tests.test_qq_decision_segments import CASUAL_TEXTS, aggregate, plan, segment


@pytest.fixture(autouse=True)
def legacy_input_budget(monkeypatch):
    # These tests retain their original 48k-character boundary; the default QQ
    # budget is larger, but trimming/proof/receipt guarantees are unchanged.
    monkeypatch.setenv("NC_QQ_INPUT_TOKEN_BUDGET", "24000")


def _external_receipts(setup, *, count=9, body_length=80, large_proof=True):
    store, _, _, _, records = setup
    return [decision.record_qq_delivery(
        memory_db=store.path, group_id="10001", self_member_id="29999", delivered_text=f"实际已发{i}：" + "短" * body_length,
        evidence_refs=[f"test-evidence://SENT_PROOF_{i}_{j}/" + "a" * 180 for j in range(32)] if large_proof else [f"test-evidence://sent-{i}"],
        verification="qq_ui_verified", source_record_ids=[records[0].record_id],
        delivered_at=NOW + timedelta(seconds=i),
    ) for i in range(count)]


def _gate_payload(client):
    return decision.combined_user_payload(client.gate_messages[-1])


def _persona_payload(client):
    return decision.combined_user_payload(client.chat_calls[-1])


def _stored_receipts(path):
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT receipt_id,receipt_hash,record_json FROM qq_reply_deliveries ORDER BY receipt_id").fetchall()


def test_large_verified_proof_attachments_do_not_block_small_actual_dialogue(setup):
    store, client, builds, kwargs, _ = setup
    receipts = _external_receipts(setup)
    durable_before = _stored_receipts(store.path)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.outcome == "ready" and builds and result.reply
    gate, persona = _gate_payload(client), _persona_payload(client)
    assert gate["delivery_receipts"] == persona["delivery_receipts"]
    assert len(gate["delivery_receipts"]) == 9
    full_payload = dict(gate, delivery_receipts=[item.model_dump(mode="json") for item in receipts])
    assert len(decision._canonical(full_payload)) > 48_000
    assert len(decision._canonical(gate)) < 48_000
    assert len(decision._canonical(persona)) < 48_000
    assert "SENT_PROOF" not in decision._canonical(gate) + decision._canonical(persona)
    assert _stored_receipts(store.path) == durable_before
    for projected, receipt in zip(gate["delivery_receipts"], receipts, strict=True):
        raw = receipt.model_dump(mode="json")
        for field in ("receipt_id", "decision_id", "group_id", "self_member_id", "delivered_at", "delivered_text", "source_record_ids", "verification", "origin"):
            assert projected[field] == raw[field]
        assert projected["segment_count"] == 1 and "evidence_refs" not in projected
    assert result.delivery_refs == [item.receipt_id for item in receipts]
    counters = trace.snapshot()["counters"]
    encoded = [item["content"] for item in client.gate_messages[-1] if item["role"] == "user"]
    assert counters["context_final_characters"] == sum(len(item) for item in encoded)
    assert counters["context_persona_characters"] == len(decision._canonical({"assessment": persona["assessment"]}))
    assert 0 < counters["context_base_characters"] < counters["context_final_characters"]
    assert "context_budget_exceeded" not in counters


def test_optional_old_deliveries_are_removed_whole_but_latest_actual_send_stays(setup):
    store, client, _, kwargs, _ = setup
    receipts = _external_receipts(setup, count=9, body_length=6000, large_proof=False)
    durable_before = _stored_receipts(store.path)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    gate = _gate_payload(client)
    assert result.outcome == "ready"
    selected = gate["delivery_receipts"]
    assert selected == [decision._model_delivery_receipt(item) for item in receipts[-len(selected):]]
    assert selected[-1]["receipt_id"] == receipts[-1].receipt_id
    assert gate["model_context_selection"]["omitted_delivery_receipt_ids"]
    assert _stored_receipts(store.path) == durable_before
    assert len(decision._canonical(gate)) + decision._assessment_reserve() <= 48_000
    assert _persona_payload(client)["delivery_receipts"] == selected


def test_required_observations_over_budget_persist_safe_failure_not_false_considered(setup):
    store, client, builds, kwargs, _ = setup
    for index in range(3, 9):
        add_member(store, f"2000{index}", text="OBSERVATION_PRIVATE_MARKER" + "原" * 6000)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.outcome == "error_fallback" and result.error_type == "model_invalid"
    assert result.reason == decision._CONTEXT_BUDGET_REASON
    assert result.reply == decision.ERROR_FALLBACK_REPLY and result.reply_segments == [decision.ERROR_FALLBACK_REPLY]
    assert result.seen_observation_record_ids == result.new_observation_record_ids
    assert result.source_record_ids
    assert result.usage.calls == 0
    assert "OBSERVATION_PRIVATE_MARKER" not in result.reason + json.dumps(trace.snapshot())
    assert not builds and not client.gate_messages
    counters = trace.snapshot()["counters"]
    assert counters["context_base_characters"] > 48_000
    assert counters["context_budget_exceeded_final"] == 1
    replay = decision.decide_qq_reply(**kwargs)
    assert replay.replayed and replay.decision_id == result.decision_id and not builds


@pytest.mark.parametrize("failure", [httpx.ConnectError("SYNTHETIC_PRIVATE_TRANSPORT"), "not-json"])
def test_http_and_invalid_json_remain_distinct_from_context_budget(setup, failure):
    _, client, _, kwargs, _ = setup
    client.choice = failure
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.outcome == "error_fallback"
    assert result.decision_source in {"model_error", "model_invalid"}
    assert "context_budget_exceeded" not in result.reason
    assert "context_budget_exceeded" not in trace.snapshot()["counters"]
    assert result.error_type == ("model_http" if isinstance(failure, Exception) else "model_invalid")
    assert result.reply == decision.ERROR_FALLBACK_REPLY


def test_compact_partial_prefix_never_exposes_unverified_suffix_to_either_model(setup):
    _, client, _, kwargs, _ = setup
    frozen = plan(setup)
    first = segment(setup, frozen, evidence_refs=[f"test-evidence://PARTIAL_PROOF_{i}/" + "b" * 180 for i in range(32)])
    result = decision.decide_qq_reply(**kwargs, request="核对已发送内容。")
    gate, persona = _gate_payload(client), _persona_payload(client)
    assert result.outcome == "ready"
    assert gate["segment_delivery_receipts"] == persona["segment_delivery_receipts"]
    assert gate["delivery_receipts"] == []
    projected = gate["segment_delivery_receipts"][0]
    assert projected == {
        key: value for key, value in first.model_dump(mode="json").items() if key != "evidence_refs"
    }
    assert projected["segment_index"] == 0 and projected["segment_count"] == 2
    for payload in (gate, persona):
        encoded = decision._canonical(payload)
        assert CASUAL_TEXTS[0] in encoded and CASUAL_TEXTS[1] not in encoded and "PARTIAL_PROOF" not in encoded


def test_completed_aggregate_projects_one_actual_body_with_exact_segment_links(setup):
    _, client, _, kwargs, _ = setup
    frozen = plan(setup, count=3)
    children = [segment(setup, frozen, index) for index in range(3)]
    complete = aggregate(setup, frozen, children)
    decision.decide_qq_reply(**kwargs, request="核对完整发送。")
    gate, persona = _gate_payload(client), _persona_payload(client)
    assert gate["delivery_receipts"] == persona["delivery_receipts"]
    projected = gate["delivery_receipts"][0]
    assert projected["receipt_id"] == complete.receipt_id and projected["delivered_text"] == frozen.reply
    assert projected["segment_receipt_ids"] == [child.receipt_id for child in children]
    assert projected["source_record_ids"] == complete.source_record_ids and projected["segment_count"] == 3
    assert "delivered_segments" not in projected and "evidence_refs" not in projected
    assert gate["segment_delivery_receipts"] == persona["segment_delivery_receipts"] == []


@pytest.mark.parametrize("partial", [False, True])
def test_fingerprint_still_binds_full_proof_when_model_projection_is_unchanged(setup, partial):
    store, client, _, kwargs, _ = setup
    if partial:
        frozen = plan(setup)
        receipt = segment(setup, frozen)
        table = "qq_reply_segment_deliveries"
    else:
        receipt = _external_receipts(setup, count=1, large_proof=False)[0]
        table = "qq_reply_deliveries"
    options = dict(kwargs, request="同一明确检查请求。")
    before = decision.decide_qq_reply(**options)
    first_projection = _gate_payload(client)["segment_delivery_receipts" if partial else "delivery_receipts"]
    assert decision.decide_qq_reply(**options).replayed
    raw = receipt.model_dump(mode="json")
    raw["evidence_refs"].append("test-evidence://new-complete-proof")
    with sqlite3.connect(store.path) as connection:
        connection.execute(f"UPDATE {table} SET record_json=?,receipt_hash=? WHERE receipt_id=?",
                           (json.dumps(raw), decision._hash(decision._receipt_body(raw)), receipt.receipt_id))
    after = decision.decide_qq_reply(**options)
    assert not after.replayed and after.decision_id != before.decision_id and after.input_hash != before.input_hash
    assert _gate_payload(client)["segment_delivery_receipts" if partial else "delivery_receipts"] == first_projection
    assert receipt.receipt_id in after.delivery_refs


def test_unverified_receipt_mutation_is_rejected_before_projection(setup):
    store, _, builds, kwargs, _ = setup
    receipt = _external_receipts(setup, count=1, large_proof=False)[0]
    raw = receipt.model_dump(mode="json")
    raw["evidence_refs"].append("test-evidence://unverified-change")
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE qq_reply_deliveries SET record_json=? WHERE receipt_id=?", (json.dumps(raw), receipt.receipt_id))
    with pytest.raises(decision.QQDraftError, match="inconsistent"):
        decision.decide_qq_reply(**kwargs)
    assert not builds
