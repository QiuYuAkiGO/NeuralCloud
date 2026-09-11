"""Synthetic group output plans and durable per-bubble receipts; no delivery."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace

import pytest

from harness.core.inference import TokenUsage
from harness.qq import decision, draft
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from tests.test_qq_decision import NOW, add_member, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup


TEXTS = ["嗯，我记得这件事。", "你后来有再试过吗？", "我也有点好奇结果。"]


def plan(setup, *, count=2):
    _, client, _, kwargs, _ = setup
    client.chat_routes = {"": "\n[CONTINUE]\n".join(TEXTS[:count]) + "\n[END]"}
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "ready", result.reason
    assert result.reply_segments == TEXTS[:count]
    return result


def segment(setup, result, index=0, **changes):
    store, _, _, _, records = setup
    options = dict(memory_db=store.path, group_id="10001", self_member_id="29999",
                   decision_id=result.decision_id, segment_index=index,
                   segment_count=len(result.reply_segments), delivered_text=result.reply_segments[index],
                   evidence_refs=[f"test-evidence://segment-{index}"], verification="qq_ui_verified",
                   source_record_ids=[records[index % len(records)].record_id],
                   delivered_at=NOW + timedelta(seconds=index))
    options.update(changes)
    return decision.record_qq_segment_delivery(**options)


def aggregate(setup, result, receipts, **changes):
    options = dict(memory_db=setup[0].path, group_id="10001", self_member_id="29999",
                   decision_id=result.decision_id, delivered_text=result.reply,
                   evidence_refs=sorted({ref for item in receipts for ref in item.evidence_refs}),
                   source_record_ids=sorted({ref for item in receipts for ref in item.source_record_ids}),
                   verification="qq_ui_verified", delivered_at=NOW + timedelta(seconds=5))
    options.update(changes)
    return decision.record_qq_delivery(**options)


@pytest.mark.parametrize("count", [1, 2, 3])
def test_one_persona_output_produces_frozen_protocol_plan_without_continuation(setup, count):
    _, client, builds, kwargs, _ = setup
    loaded, _ = draft._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    result = plan(setup, count=count)
    assert result.reply == "\n\n".join(TEXTS[:count])
    assert decision.effective_reply_segments(result) == TEXTS[:count]
    assert result.reply_style == "conversational"
    assert len(client.gate_messages) == 1 and len(client.chat_calls) == 1
    assert builds[0].max_reply_segments == 3 and not builds[0].continuation_enabled
    assert loaded.max_reply_segments == 1 and not loaded.continuation_enabled
    replay = decision.decide_qq_reply(**kwargs)
    assert replay.replayed and replay.reply_segments == result.reply_segments
    assert len(client.chat_calls) == 1


def test_gate_formal_choice_is_preserved_as_data_under_fixed_style_rules(setup):
    _, client, _, kwargs, _ = setup
    client.choice = json.dumps({"action": "speak", "reason": "明确要求说明。", "reply_style": "formal",
                                "reply_style_reason": "STYLE_REASON_DATA"})
    result = decision.decide_qq_reply(**kwargs, request="FORMAL_REQUEST_DATA 请正式说明。")
    assert result.reply_style == "formal" and result.reply_style_reason == "STYLE_REASON_DATA"
    system = "\n".join(item["content"] for call in client.gate_messages + client.chat_calls for item in call if item["role"] == "system")
    user = "\n".join(item["content"] for call in client.gate_messages + client.chat_calls for item in call if item["role"] == "user")
    assert "默认简短口语" in system and "不主动写长篇分析" in system
    assert "STYLE_REASON_DATA" in user and "STYLE_REASON_DATA" not in system
    assert "FORMAL_REQUEST_DATA" in user and "FORMAL_REQUEST_DATA" not in system


def test_formal_is_not_a_speak_override(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"先等待。","reply_style":"formal","reply_style_reason":"本次正式事项尚不需回应。"}'
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kw: pytest.fail("wait produced text"))
    result = decision.decide_qq_reply(**kwargs)
    assert result.reply_style == "formal" and result.reply is None and result.reply_segments == []


@pytest.mark.parametrize("style,reason", [("unknown", "test"), ("formal", " ")])
def test_invalid_style_never_generates_text(setup, monkeypatch, style, reason):
    _, client, _, kwargs, _ = setup
    client.choice = json.dumps({"action": "speak", "reason": "test", "reply_style": style, "reply_style_reason": reason})
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kw: pytest.fail("invalid gate produced text"))
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "model_invalid" and result.reply_segments == [] and result.reply is None


@pytest.mark.parametrize("segments,reply", [([], "text"), (["text\nline"], "text\nline"),
    (["text\rline"], "text\rline"), (["text\u2028line"], "text\u2028line"),
    (["x" * 4001], "x" * 4001), (["x" * 4000, "y" * 4000], "x" * 4000 + "\n\n" + "y" * 4000),
    (["one", "two", "three", "four"], "one\n\ntwo\n\nthree\n\nfour"),
    (["one", "two"], "one two"), ([" "], " ")])
def test_invalid_new_plan_fails_closed_without_silent_text_rewriting(setup, monkeypatch, segments, reply):
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kw: SimpleNamespace(
        segments=segments, text=reply, usage=TokenUsage(), validation=None))
    result = decision.decide_qq_reply(**setup[3])
    assert result.outcome == "model_invalid" and result.action == "wait"
    assert result.reply is None and result.reply_segments == []


def test_expired_cached_segment_plan_has_no_sendable_remainder(setup):
    result = plan(setup)
    replay = decision.decide_qq_reply(**setup[3], deadline=QQDeadline.after(0))
    assert replay.decision_id == result.decision_id and replay.outcome == "expired"
    assert replay.reply is None and decision.effective_reply_segments(replay) == []


@pytest.mark.parametrize("bodies,expected", [
    (["first\n\nsecond"], ["first", "second"]),
    (["first\r\n\r\nsecond\r\n \r\nthird"], ["first", "second", "third"]),
    (["first", "second\n\nthird"], ["first", "second", "third"]),
    ([" first \n\n second "], [" first ", " second "]),
])
def test_natural_paragraph_fallback_preserves_all_prose_and_uses_no_extra_call(setup, monkeypatch, bodies, expected):
    def prepared(**kwargs):
        return SimpleNamespace(segments=bodies, text="\n\n".join(bodies), usage=TokenUsage(), validation=None)

    monkeypatch.setattr(decision, "_prepare_group_speech", prepared)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**setup[3], trace=trace)
    assert result.outcome == "ready" and result.reply_segments == expected
    assert result.reply == "\n\n".join(expected)
    assert trace.snapshot()["counters"]["paragraph_segmentation"] == 1
    assert len(setup[1].gate_messages) == 1 and setup[1].chat_calls == []


@pytest.mark.parametrize("text", ["first\n\nsecond\n\nthird\n\nfourth", "first\nsecond",
                                 "first\r\nsecond", "first\n\n", "\n\nfirst", " \n\nfirst"])
def test_paragraph_fallback_refuses_extra_or_empty_segments_and_single_line_breaks(setup, monkeypatch, text):
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kw: SimpleNamespace(
        segments=[text], text=text, usage=TokenUsage(), validation=None))
    result = decision.decide_qq_reply(**setup[3])
    assert result.outcome == "model_invalid" and result.reply is None and result.reply_segments == []


def test_mixed_protocol_and_natural_paragraphs_pass_through_the_real_pipeline_once(setup):
    _, client, _, kwargs, _ = setup
    client.chat_routes = {"": TEXTS[0] + "\n[CONTINUE]\n" + TEXTS[1] + "\n\n" + TEXTS[2] + "\n[END]"}
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.reply_segments == TEXTS and result.reply == "\n\n".join(TEXTS)
    assert trace.snapshot()["counters"]["paragraph_segmentation"] == 1
    assert len(client.chat_calls) == 1


def test_legacy_speak_json_keeps_single_body_fallback(setup):
    result = plan(setup, count=1)
    raw = result.model_dump(mode="json")
    for key in ("reply_segments", "reply_style", "reply_style_reason"):
        raw.pop(key)
    raw["prompt_version"] = "qq-group-turn-taking-v5"
    legacy = decision.QQReplyDecision.model_validate(raw)
    assert legacy.reply_segments == []
    assert decision.effective_reply_segments(legacy) == [legacy.reply]


@pytest.mark.parametrize("count", [1, 2, 3])
def test_durable_prefix_survives_restart_and_aggregates_exact_complete_evidence(setup, count):
    result = plan(setup, count=count)
    receipts = []
    for index in range(count):
        receipts.append(segment(setup, result, index))
        fetched = decision.get_qq_segment_deliveries(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id)
        assert fetched == receipts
        if index < count - 1:
            with pytest.raises(ValueError):
                aggregate(setup, result, receipts)
            assert decision.get_qq_delivery(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id) is None
    complete = aggregate(setup, result, receipts)
    assert complete.delivered_segments == result.reply_segments
    assert complete.segment_receipt_ids == [item.receipt_id for item in receipts]
    assert decision.get_qq_delivery(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id) == complete
    assert aggregate(setup, result, receipts, delivered_at=NOW + timedelta(days=1)) == complete
    assert segment(setup, result, 0, delivered_at=NOW + timedelta(days=1)) == receipts[0]


def test_multi_segment_parent_cannot_be_recorded_without_individual_bubbles(setup):
    result = plan(setup)
    with pytest.raises(decision.QQDraftError, match="every segment"):
        aggregate(setup, result, [], evidence_refs=["test-evidence://unsupported-whole-reply"])


@pytest.mark.parametrize("change", [dict(segment_index=1), dict(segment_index=True), dict(segment_count=3),
    dict(segment_count=True), dict(group_id="10002"), dict(self_member_id="28888"),
    dict(delivered_text="different"), dict(source_record_ids=["unknown"]), dict(evidence_refs=[]),
    dict(delivered_at=NOW.replace(tzinfo=None))])
def test_segment_write_rejects_skips_and_mismatched_proof_without_creating_receipt(setup, change):
    result = plan(setup)
    with pytest.raises(ValueError):
        segment(setup, result, **change)
    assert decision.get_qq_segment_deliveries(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id) == []


def test_segment_sources_cannot_escape_considered_group_scope(setup):
    store, _, _, _, _ = setup
    result = plan(setup)
    _, other = add_member(store, "20003", group="10002", text="OTHER_GROUP", capture="other-source")
    with pytest.raises(decision.QQDraftError, match="subset"):
        segment(setup, result, source_record_ids=[other.record_id])


def test_same_slot_concurrent_receipt_is_idempotent_and_not_overwritable(setup):
    result = plan(setup)
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: segment(setup, result), range(2)))
    assert receipts[0].receipt_id == receipts[1].receipt_id
    with pytest.raises(decision.QQDraftError, match="already occupies"):
        segment(setup, result, evidence_refs=["test-evidence://changed"])
    with pytest.raises(decision.QQDraftError, match="timing"):
        segment(setup, result, 1, delivered_at=NOW - timedelta(seconds=1))


@pytest.mark.parametrize("change", [dict(evidence_refs=["test-evidence://extra"]), dict(source_record_ids=[]),
                                   dict(delivered_at=NOW - timedelta(seconds=1))])
def test_aggregate_rejects_inexact_union_and_earlier_timestamp(setup, change):
    result = plan(setup)
    receipts = [segment(setup, result, i) for i in range(2)]
    with pytest.raises(decision.QQDraftError, match="complete delivery"):
        aggregate(setup, result, receipts, **change)


def test_three_independent_bubbles_can_retain_all_96_evidence_references(setup):
    result = plan(setup, count=3)
    receipts = [segment(setup, result, index, evidence_refs=[f"test-evidence://{index}/{i}" for i in range(32)]) for index in range(3)]
    complete = aggregate(setup, result, receipts)
    assert len(complete.evidence_refs) == 96


@pytest.mark.parametrize("mutation", ["hash", "index", "scope", "delete_prefix", "text", "source"])
def test_readonly_segment_lookup_rejects_corrupted_chain(setup, mutation):
    result = plan(setup)
    receipts = [segment(setup, result, i) for i in range(2)]
    with sqlite3.connect(setup[0].path) as connection:
        if mutation == "delete_prefix":
            connection.execute("DELETE FROM qq_reply_segment_deliveries WHERE segment_index=0")
        elif mutation == "hash":
            connection.execute("UPDATE qq_reply_segment_deliveries SET receipt_hash='invalid' WHERE segment_index=0")
        elif mutation == "index":
            connection.execute("UPDATE qq_reply_segment_deliveries SET segment_index=2 WHERE segment_index=1")
        else:
            raw = receipts[0].model_dump(mode="json")
            raw.update({"scope": {"group_id": "10002"}, "text": {"delivered_text": "not frozen"}, "source": {"source_record_ids": ["missing"]}}[mutation])
            connection.execute("UPDATE qq_reply_segment_deliveries SET record_json=?,receipt_hash=? WHERE segment_index=0",
                               (json.dumps(raw), decision._hash(decision._receipt_body(raw))))
    before = setup[0].path.read_bytes()
    with pytest.raises(decision.QQDraftError, match="unavailable or inconsistent"):
        decision.get_qq_segment_deliveries(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id)
    assert setup[0].path.read_bytes() == before


def test_partial_actual_send_is_in_context_without_unverified_remainder_or_duplicate_aggregate(setup):
    _, client, _, kwargs, _ = setup
    result = plan(setup)
    first = segment(setup, result)
    client.choice = '{"action":"wait","reason":"已发出的第一段不必重复。"}'
    partial = decision.decide_qq_reply(**kwargs, request="检查现在实际已经发送的内容。")
    payload = json.loads(client.gate_messages[-1][1]["content"])
    assert payload["delivery_receipts"] == []
    assert [item["delivered_text"] for item in payload["segment_delivery_receipts"]] == [TEXTS[0]]
    assert not payload["prior_decisions"][0]["was_delivered"]
    assert TEXTS[1] not in json.dumps(payload, ensure_ascii=False)
    assert partial.delivery_refs == [first.receipt_id]
    second = segment(setup, result, 1)
    full = aggregate(setup, result, [first, second])
    decision.decide_qq_reply(**kwargs, request="再次检查实际发送的内容。")
    payload = json.loads(client.gate_messages[-1][1]["content"])
    assert payload["segment_delivery_receipts"] == []
    assert [item["receipt_id"] for item in payload["delivery_receipts"]] == [full.receipt_id]


@pytest.mark.parametrize("external", [False, True])
def test_legacy_receipt_hash_uses_original_json_and_replay_keeps_old_record(setup, external):
    result = plan(setup, count=1)
    opts = dict(memory_db=setup[0].path, group_id="10001", self_member_id="29999",
                decision_id=None if external else result.decision_id, delivered_text=result.reply,
                evidence_refs=["test-evidence://old-bubble"], verification="qq_ui_verified", delivered_at=NOW)
    receipt = decision.record_qq_delivery(**opts)
    raw = receipt.model_dump(mode="json")
    raw.pop("delivered_segments")
    raw.pop("segment_receipt_ids")
    with sqlite3.connect(setup[0].path) as connection:
        connection.execute("UPDATE qq_reply_deliveries SET record_json=?,receipt_hash=?",
                           (json.dumps(raw), decision._hash(decision._receipt_body(raw))))
    before = setup[0].path.read_bytes()
    if not external:
        assert decision.get_qq_delivery(memory_db=setup[0].path, group_id="10001", decision_id=result.decision_id) == receipt
        assert setup[0].path.read_bytes() == before
    assert decision.record_qq_delivery(**{**opts, "delivered_at": NOW + timedelta(days=1)}) == receipt
    with sqlite3.connect(setup[0].path) as connection:
        stored = json.loads(connection.execute("SELECT record_json FROM qq_reply_deliveries").fetchone()[0])
    assert "delivered_segments" not in stored and "segment_receipt_ids" not in stored


def test_segment_reader_does_not_create_database_or_schema(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(decision.QQDraftError):
        decision.get_qq_segment_deliveries(memory_db=path, group_id="10001", decision_id="qq-decision:missing")
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE existing(value TEXT)")
    before = path.read_bytes()
    assert decision.get_qq_segment_deliveries(memory_db=path, group_id="10001", decision_id="qq-decision:missing") == []
    assert path.read_bytes() == before
