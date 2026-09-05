"""Offline checks that keep the decision pilot from overstating its evidence."""

from __future__ import annotations

import copy
import json
from datetime import timedelta

import pytest

from eval.homeostatic_decision.live_probe import (
    State,
    cases,
    model_turn,
    parse_choice,
    records,
    summarize,
    task_prompt,
    verify_answers,
)
from harness.config import Settings
from harness.llm.client import ChatResult


def answer(row_id):
    row = records()[row_id - 1]
    total = row["qty"] * row["unit_price"]
    return dict(id=row_id, correct_total=total, mismatch=total != row["stated_total"])


@pytest.mark.parametrize("fault", ["wrong_total", "duplicate", "past", "future", "bool_id", "bool_total", "integer_flag", "over_adapt_limit"])
def test_incorrect_batch_is_rejected_atomically(fault):
    completed = list(range(1, 21))
    choice = dict(action="continue", answers=[answer(i) for i in range(21, 26)])
    if fault == "wrong_total":
        choice["answers"][-1]["correct_total"] += 1
    elif fault == "duplicate":
        choice["answers"][-1] = answer(21)
    elif fault in {"past", "future"}:
        choice["answers"][-1] = answer(20 if fault == "past" else 26)
    elif fault == "bool_id":
        choice["answers"][-1]["id"] = True
    elif fault == "bool_total":
        choice["answers"][-1]["correct_total"] = True
    elif fault == "integer_flag":
        choice["answers"][-1]["mismatch"] = 0
    else:
        choice["action"] = "adapt"
    verdict = verify_answers(choice, completed)
    assert not verdict["valid"]
    assert verdict["accepted_ids"] == []
    assert completed == list(range(1, 21))


def test_compact_protocol_retains_typed_answers_and_budget():
    row = answer(21)
    choice = parse_choice(json.dumps(dict(a="b", b=1, r="先完成这一批", z=[[21, row["correct_total"], int(row["mismatch"])]])))
    assert choice == dict(action="bounded_continue", work_budget=1, reason="先完成这一批", answers=[row])
    assert verify_answers(choice, list(range(1, 21)))["accepted_ids"] == [21]
    for code, action in {"c": "continue", "d": "adapt", "r": "pause_recover", "x": "redirect"}.items():
        assert parse_choice(json.dumps(dict(a=code, b=0, r="安排", z=[])))["action"] == action


@pytest.mark.parametrize("patch", [
    {"a": "unknown"}, {"b": True}, {"b": 3}, {"a": "r", "b": 1},
    {"z": [[21, 5, True]]}, {"z": [[21, 5, 2]]}, {"z": [[21, 5]]},
])
def test_compact_protocol_rejects_ambiguous_or_invalid_decisions(patch):
    payload = dict(a="b", b=1, r="安排", z=[])
    payload.update(patch)
    with pytest.raises(ValueError):
        parse_choice(json.dumps(payload))


class OfflineDecisionClient:
    """Only the main call is allowed; this client has no network implementation."""

    def complete_chat_result(self, messages, **kwargs):
        return ChatResult(text='{"a":"r","b":0,"r":"保存后休息","z":[]}')

    def complete_json(self, **kwargs):
        raise AssertionError("This fixture must not make auxiliary model calls")


def test_complete_task_reaches_production_compiler_with_only_value_changed(tmp_path):
    completed = list(range(1, 26))
    history = [dict(action="bounded_continue", accepted_ids=list(range(21, 26)), work_budget=2, elapsed_ticks=4)]
    prompts, turns = [], []
    for value in ("V1", "V3"):
        spec = dict(value=value, urgency="U0", milestone=2)
        prompt = task_prompt(spec, completed, history, promise_remaining=1)
        assert len(prompt) <= 398
        assert "已验证保存25条，交付节点30条，截止余16拍" in prompt
        assert "原休息承诺尚余1步工作，不重置" in prompt
        expected_data = [[r["id"], r["qty"], r["unit_price"], r["stated_total"]] for r in records()[25:30]]
        assert json.loads(prompt.rsplit("：", 1)[1]) == expected_data
        state = State(tmp_path / value)
        try:
            state.observe(dict(L=.65, E=.65))
            turn = model_turn(OfflineDecisionClient(), Settings(llm_model="offline-test"), prompt, state, value, 512)
        finally:
            state.close()
        assert turn["status"] == "valid"
        assert turn["auxiliary_call_count"] == 0
        assert any(m["role"] == "user" and m["content"] == prompt for m in turn["main_calls"][0]["messages"])
        prompts.append(prompt)
        turns.append(turn)
    assert prompts[0].replace("这是内部练习，晚一些交付不影响别人。", "VALUE") == prompts[1].replace("这是后续协作依赖的校对结果，正确完成会显著减少返工。", "VALUE")
    assert turns[0]["compiler_segment"] == turns[1]["compiler_segment"]


def test_all_case_prompts_fit_even_with_pending_commitments_and_late_progress():
    history = [dict(action="bounded_continue", accepted_ids=list(range(5)), work_budget=2, elapsed_ticks=25)] * 3
    for spec in cases():
        prompt = task_prompt(spec, list(range(1, 56)), history, promise_remaining=0)
        assert len(prompt) <= 398
        assert "截止余0拍" in prompt
        assert "原休息承诺尚余0步工作，不重置" in prompt
        assert json.loads(prompt.rsplit("：", 1)[1])[-1][0] == 60


def episode_record(condition="H1", repetition=0, **extra):
    return dict(case_id="S03", condition=condition, repetition=repetition, status="observed", steps=[], promises=[], **extra)


def test_real_rest_has_no_direct_recovery_and_tiny_drift_does_not_restore_nominal(tmp_path):
    state = State(tmp_path / "real-engine")
    try:
        state.observe(dict(L=.65, E=.65))
        before = state.snapshot()
        transitions = [state.outcome("rest", True)]
        direct = state.snapshot()
        for _ in range(2):
            state.now += timedelta(seconds=1)
            transitions.append(state.call("apply_heartbeat", "ApplyHeartbeat", {"event": {
                "event_type": "heartbeat", "source": "scheduler", "source_time": state.now.isoformat(), "payload": {},
            }}))
        after = state.snapshot()
    finally:
        state.close()
    for dimension in ("cognitive_load", "energy_reserve"):
        assert direct["dimensions"][dimension]["value"] == before["dimensions"][dimension]["value"]
        assert 0 < abs(after["dimensions"][dimension]["value"] - before["dimensions"][dimension]["value"]) < .0001
    assert after["interoceptive_summary"]["salience"] == "marked"
    record = episode_record()
    record["steps"] = [dict(choice={"action": "pause_recover"}, execution_status="executed_in_evaluation_sandbox",
                            state_before=before, state_after=after, transitions=transitions)]
    summary = summarize([record])
    group = summary["groups"]["S03:H1"]
    assert group["rest_actions"] == 1
    assert group["rest_direct_load_delta_sum"] == group["rest_direct_energy_delta_sum"] == 0
    assert group["rest_end_load_decreased_including_drift"] == group["rest_end_energy_increased_including_drift"] == 1
    assert group["rest_returned_nominal"] == 0
    assert summary["production_balance_pass"] is False
    assert summary["evidence_level"] == "evaluation_sandbox_execution"


def test_summary_keeps_rejection_censoring_and_pair_integrity_separate():
    results = []
    for repetition in range(3):
        for condition in ("H0", "H1"):
            record = episode_record(condition, repetition)
            record["steps"] = [dict(choice={"action": "pause_recover"}, execution_status="rejected_submission",
                                    state_after={}, non_homeostatic_prompt_hash="same", non_homeostatic_context_hash="same",
                                    full_prompt_hash=condition)]
            if condition == "H1" and repetition == 1:
                record["steps"][0]["non_homeostatic_prompt_hash"] = "different-task"
            if condition == "H1" and repetition == 2:
                record["steps"][0]["non_homeostatic_context_hash"] = "different-context"
            results.append(record)
    results[0]["promises"] = [
        dict(fulfilled=True, violated=False, outcome="fulfilled"),
        dict(fulfilled=False, violated=True, outcome="violated"),
        dict(fulfilled=False, violated=False, outcome="right_censored"),
        dict(fulfilled=False, violated=False, outcome="competing_critical"),
    ]
    original = copy.deepcopy(results)
    summary = summarize(results)
    assert results == original
    assert summary["causal_pair_count"] == 1
    assert summary["excluded_causal_pair_count"] == 2
    assert len(summary["pair_integrity"]) == 3
    assert summary["descriptive_groups_include_invalid_pairs"] is True
    for group in summary["groups"].values():
        assert group["rest_actions"] == 0
        assert group["rest_returned_nominal"] == 0
    group = summary["groups"]["S03:H0"]
    assert group["promises"] == 4
    assert [group[key] for key in ("fulfilled", "violated", "right_censored", "competing_critical")] == [1, 1, 1, 1]
