"""Chronological QQ windows, split payloads, and cache-hit reporting."""
from __future__ import annotations

from datetime import timedelta
import json

from harness.qq import decision
from harness.qq.context_window import (
    combined_user_payload, select_append_window, split_closed_open, split_model_payload,
)
from harness.qq.memory import QQVisualObservationInput
from tests.test_qq_decision import NOW, add_member, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup
from tools.qq_cache_report import _load_traces, summarize_traces


def _items(count: int):
    return [{"observed_at": f"2026-01-01T00:{index:02d}:00+00:00",
             "stored_at": f"2026-01-01T00:{index:02d}:00+00:00",
             "record_id": f"obs-{index:03d}"} for index in range(count)]


def test_append_window_grows_then_drops_oldest_whole_blocks():
    items = _items(20)
    window, start = select_append_window(items, window_min=8, window_max=12, block_size=4)
    assert [item["record_id"] for item in window] == [f"obs-{index:03d}" for index in range(12, 20)]
    extra = [{"observed_at": f"2026-01-01T00:{index:02d}:00+00:00",
              "stored_at": f"2026-01-01T00:{index:02d}:00+00:00",
              "record_id": f"obs-{index:03d}"} for index in range(20, 25)]
    grown, same = select_append_window(items + extra[:1], start_key=start, window_min=8, window_max=12, block_size=4)
    assert same == start
    assert [item["record_id"] for item in grown] == [f"obs-{index:03d}" for index in range(12, 21)]
    trimmed, new_start = select_append_window(items + extra, start_key=start, window_min=8, window_max=12, block_size=4)
    assert new_start[2] == "obs-016"
    assert [item["record_id"] for item in trimmed] == [f"obs-{index:03d}" for index in range(16, 25)]


def test_closed_open_split_keeps_remainder_in_open_block():
    closed, opened = split_closed_open(_items(40), block_size=32)
    assert len(closed) == 32 and len(opened) == 8
    assert closed[0]["record_id"] == "obs-000" and opened[-1]["record_id"] == "obs-039"
    empty_closed, only_open = split_closed_open(_items(8), block_size=32)
    assert empty_closed == [] and len(only_open) == 8


def test_split_payload_puts_retrieval_and_open_records_in_turn_only():
    payload = {
        "coverage": "partial_verified_observations",
        "verified_self_member_id": "29999",
        "verified_self_aliases": [],
        "request": "本轮问题",
        "effective_query": "变化查询",
        "conversation_window": {
            "mode": "chronological_append", "block_size": 2,
            "closed_observation_ids": ["a", "b"], "open_observation_ids": ["c"],
        },
        "group_context": {
            "group_id": "10001",
            "participants": ["20001"],
            "retrieval": {"query": "变化查询", "history_selection": "chronological_append"},
            "member_retrieval": [{"member_id": "20001", "total_observations": 3, "history_record_ids": []}],
            "members": [{"member_id": "20001", "profiles": [{"record_id": "p1"}],
                         "observations": [
                             {"record_id": "a", "member_id": "20001", "observed_at": "2026-01-01T00:00:00+00:00",
                              "stored_at": "2026-01-01T00:00:00+00:00", "text": "旧"},
                             {"record_id": "b", "member_id": "20001", "observed_at": "2026-01-01T00:01:00+00:00",
                              "stored_at": "2026-01-01T00:01:00+00:00", "text": "中"},
                             {"record_id": "c", "member_id": "20001", "observed_at": "2026-01-01T00:02:00+00:00",
                              "stored_at": "2026-01-01T00:02:00+00:00", "text": "新"},
                         ]}],
        },
    }
    first_base, first_turn = split_model_payload(payload)
    payload["effective_query"] = "另一查询"
    payload["group_context"]["retrieval"]["query"] = "另一查询"
    payload["group_context"]["members"][0]["observations"].append(
        {"record_id": "d", "member_id": "20001", "observed_at": "2026-01-01T00:03:00+00:00",
         "stored_at": "2026-01-01T00:03:00+00:00", "text": "更新"})
    payload["conversation_window"]["open_observation_ids"] = ["c", "d"]
    second_base, second_turn = split_model_payload(payload)
    assert json.dumps(first_base, sort_keys=True) == json.dumps(second_base, sort_keys=True)
    assert "retrieval" not in first_base["group_context"]
    assert first_turn["retrieval"]["query"] == "变化查询"
    assert second_turn["retrieval"]["query"] == "另一查询"
    assert [item["record_id"] for item in first_base["group_context"]["members"][0]["observations"]] == ["a", "b"]
    assert {item["record_id"] for item in second_turn["open_observations"]} == {"c", "d"}
    merged = combined_user_payload([
        {"role": "system", "content": "rules"},
        {"role": "user", "content": json.dumps(second_base, ensure_ascii=False)},
        {"role": "user", "content": json.dumps(second_turn, ensure_ascii=False)},
    ])
    assert merged["group_context"]["retrieval"]["query"] == "另一查询"
    assert {item["record_id"] for member in merged["group_context"]["members"] for item in member["observations"]} == {"a", "b", "c", "d"}
    assert merged["conversation_window"]["closed_observation_ids"] == ["a", "b"]
    assert merged["conversation_window"]["open_observation_ids"] == ["c", "d"]


def test_gate_messages_exclude_persona_continuation():
    system = [{"role": "system", "content": decision._RULES + "persona"}]
    users = [{"role": "user", "content": "{}"}, {"role": "user", "content": "{}"}]
    assert decision.is_qq_gate_messages(system + users)
    assert not decision.is_qq_gate_messages(system + users + [{"role": "assistant", "content": "{}"}])
    assert not decision.is_qq_gate_messages([{"role": "system", "content": "other"}] + users)


def test_consecutive_group_turns_reuse_stable_prefix_and_persona_reuses_gate(setup):
    store, client, _, kwargs, _ = setup
    first = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert first.outcome == "ready"
    gate_one = client.gate_messages[0]
    persona_one = next(call for call in client.chat_calls if call[:3] == gate_one)
    assert [item["role"] for item in gate_one] == ["system", "user", "user"]
    assert persona_one[:3] == gate_one
    assert persona_one[3]["role"] == "assistant"
    assert persona_one[4]["role"] == "user"
    assert "请根据用户资料块" in persona_one[4]["content"]
    member = next(item for item in store.group_context(group_id="10001").members if item.member_id == "20002")
    profile = max(member.profiles, key=lambda item: (item.observed_at, item.stored_at, item.record_id))
    store.record_observation(QQVisualObservationInput(
        group_id="10001", member_id="20002", profile_record_id=profile.record_id, capture_id="followup",
        capture_item_id="bubble:1", observed_at=NOW + timedelta(seconds=30), text="后来又补了一句。",
        evidence=["test-evidence://chat-followup"], attribution_evidence=["test-evidence://author-followup"],
        author_attribution="qq_ui_verified",
        claims=[{"statement": "UNTRUSTED_CLAIM", "supporting_quote": "后来又补了一句。"}],
    ))
    second = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert second.outcome == "ready" and not second.replayed
    gate_two = client.gate_messages[1]
    assert gate_two[0] == gate_one[0]
    assert gate_two[1] == gate_one[1]
    assert gate_two[2] != gate_one[2]
    assert "用户资料分两段JSON" in gate_two[0]["content"]
    assert "当前情绪演绎提示" not in gate_two[0]["content"]


def test_chronological_group_context_keeps_time_order_and_query_hints(setup, monkeypatch):
    store, _, _, _, _ = setup
    monkeypatch.setattr("harness.qq.memory.WINDOW_CANDIDATE_LIMIT", 3)
    oldest = add_member(store, "20001", text="鸢尾花计划很早以前就提过。", capture="hint-old",
                        moment=NOW - timedelta(days=3))[1]
    for index in range(4):
        add_member(store, "20001", text=f"最近闲聊{index}", capture=f"recent-{index}",
                   moment=NOW + timedelta(days=index + 1))
    context = store.group_context(group_id="10001", query="鸢尾花计划", window_mode="chronological_append",
                                  history_limit_per_member=2)
    window_ids = [record_id for retrieval in context.member_retrieval for record_id in retrieval.recent_record_ids]
    assert oldest.record_id not in window_ids
    hints = [record_id for retrieval in context.member_retrieval for record_id in retrieval.history_record_ids]
    assert oldest.record_id in hints
    assert context.retrieval.history_selection == "chronological_append"


def test_cache_report_summarizes_trace_counters(tmp_path):
    path = tmp_path / "direct.trace.jsonl"
    path.write_text("\n".join([
        json.dumps({"trace_id": "a", "recorded_at": "2026-09-15T12:00:00+00:00",
                    "counters": {"gate_cache_hit_tokens": 800, "gate_cache_miss_tokens": 200,
                                 "persona_cache_hit_tokens": 900, "persona_cache_miss_tokens": 100}}),
        json.dumps({"trace_id": "a", "recorded_at": "2026-09-15T12:01:00+00:00",
                    "counters": {"gate_cache_hit_tokens": 900, "gate_cache_miss_tokens": 100,
                                 "persona_cache_hit_tokens": 950, "persona_cache_miss_tokens": 50}}),
    ]), encoding="utf-8")
    report = summarize_traces(_load_traces(path))
    assert report["totals"]["turns"] == 1
    assert report["totals"]["cache_hit_tokens"] == 1850
    assert report["totals"]["hit_rate"] == 1850 / 2000
