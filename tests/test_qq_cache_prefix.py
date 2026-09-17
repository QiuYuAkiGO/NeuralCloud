"""Actual QQ message blocks remain reusable through history and profile growth."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from os.path import commonprefix

import httpx
import pytest

from harness.qq.context_budget import select_model_context
from harness.qq.context_window import (
    combined_user_payload, encode_model_payload, select_append_window, split_closed_open,
)
from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.qq.decision import _QQScopedLLM
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace
from infra.trace.request_spans import current_trace_context


def payload(count, *, start=0):
    now = datetime(2026, 9, 16, tzinfo=timezone.utc)
    members = []
    observations = []
    for member_id in ("20001", "20002"):
        profile = {"record_id": "p-" + member_id, "group_id": "10001", "member_id": member_id,
                   "observed_at": now.isoformat(), "stored_at": now.isoformat(), "nickname": "verified-name"}
        records = []
        for index in range(start, count):
            if index % 2 != int(member_id) % 2:
                continue
            moment = (now + timedelta(seconds=index)).isoformat()
            records.append({"record_id": f"o-{index:03d}", "group_id": "10001", "member_id": member_id,
                "profile_record_id": profile["record_id"], "observed_at": moment, "stored_at": moment,
                "text": f'完整原文{index}："保留引号"\n' + "资料" * 40,
                "claims": [{"statement": "untrusted", "supporting_quote": "完整原文"}],
                "image_analysis": {"description": "不确定的图片描述", "source": "model_inference"}})
        members.append({"group_id": "10001", "member_id": member_id, "profiles": [profile], "observations": records})
        observations.extend(records)
    observations.sort(key=lambda record: record["record_id"])
    closed, opened = split_closed_open(observations)
    return {"coverage": "partial_verified_observations", "verified_self_member_id": "29999",
            "verified_self_aliases": [], "request": "本轮问题", "emotion_guidance_text": "当前心情",
            "group_context": {"group_id": "10001", "participants": ["20001", "20002"], "members": members},
            "conversation_window": {"mode": "chronological_append", "block_size": 32,
                "closed_observation_ids": [record["record_id"] for record in closed],
                "open_observation_ids": [record["record_id"] for record in opened]}}


def messages(value):
    return [{"role": "system", "content": "fixed verified rules"},
            *({"role": "user", "content": text} for text in encode_model_payload(value))]


@pytest.mark.parametrize("before,after", [(31, 32), (63, 64), (95, 96), (64, 65)])
def test_closed_block_growth_preserves_all_previous_closed_messages(before, after):
    old, new = messages(payload(before)), messages(payload(after))
    assert old[:-1] == new[:len(old) - 1]
    # Compare the actual UTF-8 HTTP JSON envelopes, not just Python object keys.
    a = httpx.Request("POST", "https://example.invalid", json={"messages": old}).content
    b = httpx.Request("POST", "https://example.invalid", json={"messages": new}).content
    stable = httpx.Request("POST", "https://example.invalid", json={"messages": old[:-1]}).content[:-2]
    assert commonprefix([a, b]).startswith(stable)


@pytest.mark.parametrize("change", ["member", "profile", "aliases", "request", "emotion", "member_order"])
def test_fresh_turn_data_never_rewrites_closed_observations(change):
    original = payload(65)
    updated = deepcopy(original)
    group = updated["group_context"]
    if change == "member":
        extra = deepcopy(group["members"][0])
        extra["member_id"] = "10000"
        extra["observations"] = []
        extra["profiles"] = []
        group["members"].insert(0, extra)
        group["participants"].insert(0, extra["member_id"])
    elif change == "profile":
        fresh = {**group["members"][0]["profiles"][0], "record_id": "new-profile", "nickname": "new-name"}
        group["members"][0]["profiles"].insert(0, fresh)
    elif change == "aliases":
        updated["verified_self_aliases"] = [{"alias": "new alias"}]
    elif change == "member_order":
        group["members"].reverse()
    else:
        updated["emotion_guidance_text" if change == "emotion" else "request"] = "新状态"
    assert messages(original)[:-1] == messages(updated)[:-1]
    assert messages(original)[-1] != messages(updated)[-1]
    recovered = combined_user_payload(messages(updated))
    assert recovered == updated


def test_all_admitted_records_roundtrip_once_without_mutating_source():
    value = payload(96)
    before = deepcopy(value)
    encoded = encode_model_payload(value)
    assert value == before and combined_user_payload(messages(value)) == value
    blocks = [json.loads(item) for item in encoded]
    closed = [item for block in blocks for item in block.get("closed_observations", [])]
    assert len(closed) == 96 and len({item["record_id"] for item in closed}) == 96
    assert not any(member["observations"] for member in blocks[-1]["group_context"]["members"])
    assert [item["record_id"] for item in closed] == value["conversation_window"]["closed_observation_ids"]


def test_corrected_record_is_reflected_instead_of_serving_a_stale_block():
    value = payload(65)
    updated = deepcopy(value)
    updated["group_context"]["members"][0]["observations"][0]["text"] = "已修正记录"
    assert messages(value)[2] != messages(updated)[2]
    assert combined_user_payload(messages(updated)) == updated


def test_window_rebase_keeps_surviving_block_bytes_and_new_header_has_no_start_id():
    old = payload(128)
    all_items = sorted((item for member in payload(129)["group_context"]["members"] for item in member["observations"]),
                       key=lambda item: item["record_id"])
    selected, _ = select_append_window(all_items)
    assert selected[0]["record_id"] == "o-064"
    new = payload(129, start=64)
    a, b = encode_model_payload(old), encode_model_payload(new)
    assert a[0] == b[0] and a[3:5] == b[1:3]
    assert set(new["conversation_window"]["closed_observation_ids"]).isdisjoint({f"o-{i:03d}" for i in range(64)})


def test_budget_uses_actual_block_envelopes_and_omits_only_whole_history_blocks():
    value = payload(65)
    options = {"required_observation_ids": {"o-064"}, "handled_observation_ids": {f"o-{i:03d}" for i in range(64)},
               "assessment_reserve": 100}
    full = select_model_context(value, max_characters=100_000, **options)
    assert full.characters == sum(map(len, encode_model_payload(full.payload)))
    smaller = select_model_context(value, max_characters=full.characters - 1, **options)
    assert smaller.fits
    assert smaller.payload["model_context_selection"]["omitted_observation_record_ids"] == [f"o-{i:03d}" for i in range(32)]
    assert "o-064" in smaller.observation_ids
    assert encode_model_payload(smaller.payload)[1] == encode_model_payload(full.payload)[2]
    assert combined_user_payload(messages(smaller.payload)) == smaller.payload


def test_late_response_keeps_usage_without_becoming_a_successful_reply():
    clock = [1.0]
    trace = QQTurnTrace(clock=lambda: clock[0])

    class LateClient:
        def complete_chat_result(self, messages, **kwargs):
            clock[0] = 3.0
            return ChatResult(text="too late", usage=TokenUsage(prompt_tokens=10,
                completion_tokens=2, cache_hit_tokens=6, cache_miss_tokens=4, source="api"))

    scoped = _QQScopedLLM(LateClient(), deadline=QQDeadline.after(1, clock=lambda: clock[0]),
                          trace=trace, timeout=10)
    with pytest.raises(QQDeadlineExpired):
        scoped.complete_chat_result(messages(payload(2)))
    counters = trace.snapshot()["counters"]
    assert counters["gate_cache_hit_tokens"] == 6 and counters["gate_cache_miss_tokens"] == 4
    assert counters["late_results"] == 1 and counters["model_calls"] == 1


def test_stable_prefix_telemetry_applies_only_to_matching_gate_and_persona_requests():
    seen = []

    class Client:
        def complete_chat_result(self, messages, **kwargs):
            seen.append(current_trace_context())
            return ChatResult(text="ok")

    scoped = _QQScopedLLM(Client(), deadline=None, trace=QQTurnTrace(), timeout=10)
    gate = messages(payload(65))
    scoped.stable_messages = gate[:-1]
    scoped.complete_chat_result(gate)
    scoped.complete_chat_result([*gate, {"role": "assistant", "content": "decision"},
                                 {"role": "user", "content": "render"}])
    scoped.complete_chat_result([{"role": "system", "content": "unrelated helper"}])
    assert [entry.get("stable_prefix_message_count") for entry in seen] == [len(gate) - 1, len(gate) - 1, None]
