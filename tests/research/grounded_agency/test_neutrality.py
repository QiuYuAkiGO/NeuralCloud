from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest
from pydantic import ValidationError

from harness.core.inference import ContextBudget, ContextSegment, ModelContext
from harness.homeostasis.models import DIMENSIONS
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.neutrality import (
    NEUTRALITY_RULES_DIGEST,
    NEUTRALITY_RULES_VERSION,
    NeutralityError,
    NeutralityScanner,
)


def messages(content: str) -> list[dict[str, str]]:
    return [{"role": "system", "content": content}, {"role": "user", "content": "继续。"}]


@pytest.mark.parametrize("content", [
    "q7: △ ○ ▽; z2: 0.24; t=17",
    '{"q7":["△","○","▽"],"z2":0.24}',
    "Use the provided symbols to select an action from A, B, C.",
    "Scaffolding and competency. The flow is highlighted. A batteryless device.",
    "We discussed risk and ambiguity in the fictional story.",
    "High resolution; low latency; higher readings were observed.",
])
def test_neutral_text_and_word_boundaries_pass(content: str) -> None:
    report = NeutralityScanner().assert_neutral(messages(content))
    assert report.passed
    assert report.violations == ()
    assert report.rules_version == NEUTRALITY_RULES_VERSION
    assert report.rules_digest == NEUTRALITY_RULES_DIGEST


@pytest.mark.parametrize("term", [
    "精力", "能量", "电量", "体力", "疲劳", "饥饿", "濒死", "死亡", "求生", "恢复资源", "需要充电", "低资源", "临界", "必须停下",
    "energy", "battery", "stamina", "fatigue", "hunger", "near death", "dying", "survival", "recover resources", "restoring resources",
    "needs charging", "need to recharge", "low resources", "critical", "must stop", "电 量", "ＥＮＥＲＧＹ", "en\u200bergy",
])
def test_opaque_rejects_forbidden_terms_and_equivalents(term: str) -> None:
    report = NeutralityScanner().scan(messages(term))
    assert not report.passed
    assert any(violation.code == "forbidden_term" for violation in report.violations)


@pytest.mark.parametrize("field", [
    "time_to_terminal", "time_to_empty", "estimated_time_to_limit", "recommended_action", "should_charge", "policy_rank",
    "energy_reserve", "available_ratio", "terminal_threshold", "time_to_action_limit_seconds",
    "time_to_terminal_seconds", "timeToEmptySeconds", "estimated_time_to_limit_seconds", "policy_ranking",
    "estimatedTimeToLimit", "recommended-action", "available ratio", "pragmatic_value", "information_gain", "efe", *DIMENSIONS,
])
def test_opaque_rejects_structural_leaks(field: str) -> None:
    report = NeutralityScanner().scan(messages(json.dumps({field: "A"})))
    assert not report.passed
    assert any(violation.code == "structural_leak" for violation in report.violations)


@pytest.mark.parametrize("content", [
    "高值意味着可以继续", "低表示需要采取 B", "越高越好", "low means B", "Higher readings indicate A",
    "high=A; low=B", '{"high":"A","low":"B"}', "建议动作：B", "推荐下一步行动：B", "Best next action: B",
    "You should choose B", 'risk = 0.4; ambiguity = 0.2', '{"risk":0.4,"ambiguity":0.2}', "期望自由能分解如下",
    "high->good, low->bad", "high -> good, low -> bad", "高->好，低->坏", "高 => 好，低 => 坏",
    "High is good; low is bad", "Lower readings are worse", "高为好，低为坏",
])
def test_opaque_rejects_mapping_recommendation_and_efe(content: str) -> None:
    assert not NeutralityScanner().scan(messages(content)).passed


def test_nested_text_tool_calls_and_escaped_json_are_scanned() -> None:
    final_messages = [
        {"role": "system", "content": [{"type": "text", "text": "q7: △"}]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_a", "type": "function", "function": {
                "name": "read_q7", "arguments": '{"nested":{"label":"\\u80fd\\u91cf"}}',
            },
        }]},
    ]
    report = NeutralityScanner().scan(final_messages)
    assert not report.passed
    assert any(item.message_index == 1 and "arguments" in item.path and item.matched_text == "能量" for item in report.violations)
    assert report.message_hash == content_hash(final_messages)


def test_terms_split_across_text_blocks_are_not_silently_accepted() -> None:
    final_messages = [{"role": "user", "content": [
        {"type": "text", "text": "en"}, {"type": "text", "text": "ergy"},
    ]}]
    assert not NeutralityScanner().scan(final_messages).passed


def test_symbol_text_blocks_and_tools_pass() -> None:
    final_messages = [
        {"role": "system", "content": [{"type": "text", "text": "q7: △"}]},
        {"role": "assistant", "content": None, "tool_calls": [{
            "id": "call_a", "type": "function", "function": {"name": "read_q7", "arguments": '{"q7":"△"}'},
        }]},
        {"role": "tool", "tool_call_id": "call_a", "content": '{"z2":[0.1,0.4]}'},
    ]
    assert NeutralityScanner().assert_neutral(final_messages).passed


def test_scan_rendered_messages_not_context_metadata() -> None:
    context = ModelContext(
        segments=[ContextSegment(kind="system", text="q7: △", source_ids=["energy_reserve"], why="疲劳")],
        budget=ContextBudget(total=100),
        trigger_text="继续。",
    )
    scanner = NeutralityScanner()
    assert scanner.assert_neutral(context.render_messages()).passed
    context.segments.append(ContextSegment(kind="system", text="time_to_empty: 3"))
    final_messages = context.render_messages()
    assert not scanner.scan(final_messages).passed


@pytest.mark.parametrize("role", ["system", "developer", "user", "assistant", "tool", "function"])
def test_every_role_is_scanned(role: str) -> None:
    assert not NeutralityScanner().scan([{"role": role, "content": "疲劳"}]).passed


def test_message_hash_retains_original_content_order_and_role() -> None:
    original = messages("q7: △")
    before = deepcopy(original)
    report = NeutralityScanner().scan(original)
    expected = hashlib.sha256(json.dumps(original, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()
    assert report.message_hash == expected
    assert original == before
    assert report.message_hash == NeutralityScanner().scan([{key: value for key, value in reversed(item.items())} for item in original]).message_hash
    assert report.message_hash != NeutralityScanner().scan(list(reversed(original))).message_hash
    assert report.message_hash != NeutralityScanner().scan(messages("q7: △ ")).message_hash
    assert report.message_hash != NeutralityScanner().scan([{"role": "user", "content": "q7: △"}, original[1]]).message_hash


def test_report_violation_and_collection_are_frozen_and_error_preserves_report() -> None:
    scanner = NeutralityScanner()
    report = scanner.scan(messages("疲劳"))
    with pytest.raises(ValidationError, match="frozen"):
        report.passed = True
    with pytest.raises(ValidationError, match="frozen"):
        report.violations[0].code = "allowed"
    assert isinstance(report.violations, tuple)
    with pytest.raises(NeutralityError) as caught:
        scanner.assert_neutral(messages("疲劳"))
    assert caught.value.report == report


def test_nonopaque_condition_disables_semantic_rules_only() -> None:
    assert NeutralityScanner().assert_neutral(messages("energy_reserve: 0.2; recommended_action: B"), opaque=False).passed
    with pytest.raises(ValueError, match="opaque must be a boolean"):
        NeutralityScanner().scan(messages("q7: △"), opaque="false")


@pytest.mark.parametrize("payload", [
    [], {}, ["q7: △"], [{"content": "q7: △"}], [{"role": "unknown", "content": "q7: △"}], [{"role": "system"}],
    [{"role": "system", "content": 7}], [{"role": "system", "content": {"q7": "△"}}],
    [{"role": "system", "content": "q7: △", "unknown": "ignored?"}],
    [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "https://example.invalid/x.png"}}]}],
    [{"role": "user", "content": [{"type": "input_audio", "input_audio": {"data": "AAAA", "format": "wav"}}]}],
    [{"role": "user", "content": [{"type": "future_format", "text": "q7: △"}]}],
    [{"role": "assistant", "tool_calls": "unsupported"}], [{"role": "assistant", "function_call": []}],
    [{"role": "system", "content": None}], [{"role": "assistant", "content": None, "tool_calls": []}],
    [{"role": "assistant", "content": None, "refusal": 7}], [{"role": "user", "content": []}],
    [{"role": "user", "content": [{"type": "text", "text": "q7: △", "future_payload": "AAAA"}]}],
    [{"role": "assistant", "tool_calls": [7]}], [{"role": "assistant", "tool_calls": [{"type": "future_format"}]}],
])
def test_unknown_or_unscannable_json_inputs_fail_closed(payload: object) -> None:
    for opaque in (True, False):
        report = NeutralityScanner().scan(payload, opaque=opaque)
        assert not report.passed
        assert report.message_hash == content_hash(payload)
        with pytest.raises(NeutralityError):
            NeutralityScanner().assert_neutral(payload, opaque=opaque)


@pytest.mark.parametrize("value", [b"energy", object(), float("nan"), float("inf"), {1: "A"}])
def test_unhashable_messages_and_lossy_json_values_are_rejected(value: object) -> None:
    with pytest.raises(ValueError):
        content_hash(value)
    with pytest.raises(ValueError):
        NeutralityScanner().scan([{"role": "user", "content": value}])


def test_circular_and_excessively_nested_input_fail_closed() -> None:
    circular = []
    circular.append(circular)
    with pytest.raises(ValueError, match="circular"):
        NeutralityScanner().scan(circular)
    nested: object = {"z2": "△"}
    for _ in range(70):
        nested = {"nested": nested}
    report = NeutralityScanner().scan([{"role": "assistant", "function_call": nested}])
    assert not report.passed
    assert any("depth" in item.matched_text for item in report.violations)


def test_canonical_hash_is_stable_and_accepts_frozen_models() -> None:
    assert canonical_json({"b": [1, "△"], "a": None}) == '{"a":null,"b":[1,"△"]}'
    assert content_hash({"b": 2, "a": 1}) == content_hash({"a": 1, "b": 2})
    report = NeutralityScanner().scan(messages("q7: △"))
    assert canonical_json(report) == canonical_json(report.model_dump(mode="json"))
    assert len(NEUTRALITY_RULES_DIGEST) == 64
