"""Contract regression for brief QQ planning; model calls are fully scripted."""
from copy import deepcopy
from contextlib import closing
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harness.core.inference import ContextSegment, MemoryEvidenceContext
from harness.qq import decision
from harness.qq.draft import _GROUP_DRAFT_RULES
from harness.qq.context_budget import project_group_context, select_model_context
from harness.qq.memory_evidence import build_qq_memory_evidence, current_qq_burst_ids
from harness.qq.reply_context import delivered_style_window, refresh_topic_window, select_partial_receipt_turns
from harness.qq.telemetry import QQTurnTrace
from tests.test_qq_decision import (
    NOW, add_member, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup,
)
from tests.test_qq_decision_context_budget import _gate_payload, _persona_payload
from tests.test_qq_decision_segments import aggregate, plan, segment
from tests.test_validator_projection import SEMANTIC_FIELDS, _restore


def test_compact_plan_is_optional_compatible_and_persists_without_extra_call(setup):
    _, client, _, kwargs, _ = setup
    reply_plan = {"intent": "stance", "must_address": "赞成先修复事实判断", "new_questions": ["先做哪件事？"],
                  "detail_level": "brief", "necessary_boundary": None}
    client.choice = json.dumps({"action": "speak", "reason": "直接回答选择", "reply_plan": reply_plan})
    client.chat_routes = {"": "我选先修复事实判断，回复短一点也得先说对。"}
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "ready" and result.reply_plan.model_dump() == reply_plan
    assert _persona_payload(client)["assessment"]["reply_plan"] == reply_plan
    assert len(client.gate_messages) == len(client.chat_calls) == 1
    assert result.style_diagnostics["characters"] == len(result.reply)
    assert result.reply_style == "conversational"
    replay = decision.decide_qq_reply(**kwargs)
    assert replay.reply_plan == result.reply_plan and replay.style_diagnostics == result.style_diagnostics
    legacy = result.model_dump(exclude={"reply_plan", "style_diagnostics"})
    loaded = decision.QQReplyDecision.model_validate(legacy)
    assert loaded.reply_plan is None and loaded.style_diagnostics == {}


def test_plan_uses_soft_detail_and_content_obligations_without_hard_word_budget():
    for rules in (decision._RULES, decision._PRIVATE_RULES):
        assert "用户本轮提出、Misty需要回答的新问题" in rules
        assert "must_address" in rules
        assert "固定字数" in rules
        assert "clarify" in rules
    for rules in (_GROUP_DRAFT_RULES, decision._PRIVATE_DRAFT_RULES):
        assert "must_address" in rules
        assert "固定字数" in rules
    assert "不为满足固定字数、句数或信息密度而补话" in _GROUP_DRAFT_RULES
    assert "低信息量但真实的社会性回应" in _GROUP_DRAFT_RULES


def test_group_and_private_gates_distinguish_condition_restoration_stance_clarification_and_pure_mentions():
    # Semantic behavior is checked by the live trajectory evaluation. This
    # contract ensures both gate entrypoints receive the distinctions it needs.
    for rules in (decision._RULES, decision._PRIVATE_RULES):
        assert "撤销、反转或恢复此前条件都算新的实质增量" in rules
        assert "不能仅因初题出现过而wait" in rules
        assert "‘到底’‘别绕圈’‘所以你的答案是什么’" in rules
        assert "没有理解、误解或需要澄清立场" in rules
        assert "没有新问题、没有条件变化、也没有澄清需求的纯重复才优先wait" in rules


def test_missing_detail_plan_names_the_gap_without_scripting_the_sentence():
    for rules in (decision._RULES, decision._PRIVATE_RULES):
        assert "must_address写需要澄清的具体缺口" in rules
        assert "日期、原话或必要线索" in rules
        assert "不计划‘我不记得’‘没有记录’" in rules
    assert decision.PROMPT_VERSION == "qq-group-turn-taking-v27-referenced-images"
    assert decision.PRIVATE_PROMPT_VERSION == "qq-private-turn-taking-v13-referenced-images"


def test_both_generation_stages_keep_memory_guards_but_allow_natural_social_response():
    for rules in (decision._RULES, decision._PRIVATE_RULES, _GROUP_DRAFT_RULES, decision._PRIVATE_DRAFT_RULES):
        assert "日期、原话或必要线索" in rules
        assert "不证明对方从未说过或" in rules
        assert "称赞" in rules
    assert "低信息量但真实的社会性回应" in _GROUP_DRAFT_RULES
    assert "自然停顿、语气词、害羞、笑或简短社会性回应本身不是违规" in decision._RULES
    for rules in (decision._RULES, decision._PRIVATE_RULES):
        assert "顶层严格只输出已列字段" in rules
        assert "禁止新增reply_plan_note等注释字段，即使值为null" in rules


def test_humanization_rules_preserve_substance_without_requiring_information_density_every_turn():
    assert decision._RULES.startswith(
        "你是当前人设的群聊参与者，判断此刻是否值得开口，而不是每来一条消息就回复。")
    assert "机械附和或泛泛安抚，优先wait" in decision._RULES
    assert "称赞、玩笑、关系确认、共同情绪或冲突修复" in decision._RULES
    assert "即使信息增量很低" in decision._RULES
    assert "即使信息增量很低也不必为了显得有内容而强行追加理由" in decision._PRIVATE_RULES
    assert "低信息量但真实的社会性回应" in _GROUP_DRAFT_RULES
    assert "不为满足固定字数、句数或信息密度而补话" in _GROUP_DRAFT_RULES
    assert "自然语气词、停顿、自我修正和不完整句可以偶尔出现" in _GROUP_DRAFT_RULES
    assert "不要复述action、reply_plan、reply_style、addressing等决策字段" in _GROUP_DRAFT_RULES
    assert "不要输出JSON对象" in decision._STATIC_PERSONA_INSTRUCTION
    assert "不要输出JSON对象" in decision._STATIC_PRIVATE_INSTRUCTION


def test_soft_style_violations_are_observed_without_rewrite_or_silence(setup):
    _, client, _, kwargs, _ = setup
    text = "嗯……这让我很感动，也有些不好意思，谢谢你。" + "这段解释稍微长一点。" * 10
    client.chat_routes = {"": text}
    result = decision.decide_qq_reply(**kwargs)
    assert result.reply == text and result.outcome == "ready"
    assert result.style_diagnostics["hesitant_opening"]
    assert result.style_diagnostics["relational_cycle_proxy"]
    assert result.style_diagnostics["brief_length_exceeded"]
    assert len(client.gate_messages) == len(client.chat_calls) == 1


def test_expanded_detail_remains_conversational_and_does_not_count_as_brief_failure(setup):
    _, client, _, kwargs, _ = setup
    client.choice = json.dumps({"action": "speak", "reason": "请求展开",
        "reply_plan": {"intent": "answer", "must_address": "解释三个步骤", "detail_level": "expanded"}})
    client.chat_routes = {"": "先看实际发出的内容。" * 10}
    result = decision.decide_qq_reply(**kwargs)
    assert result.reply_style == "conversational"
    assert result.reply_plan.detail_level == "expanded"
    assert not result.style_diagnostics["brief_length_exceeded"]


@pytest.mark.parametrize("changes", [{"intent": "ignore_everyone"}, {"new_questions": ["x" * 121]},
                                     {"new_questions": ["a"] * 4}, {"must_address": "x" * 241}])
def test_plan_schema_is_bounded(changes):
    with pytest.raises(ValidationError):
        decision.QQReplyPlan.model_validate(changes)


def test_nested_assessment_reserve_covers_maximum_escaped_plan():
    worst = decision._GateDecision(action="speak", reason="\x00" * 512,
        addressing_source_record_id="\x00" * 160, addressing_supporting_quote="\x00" * 1000,
        reply_style_reason="\x00" * 256,
        reply_plan=decision.QQReplyPlan(must_address="\x00" * 240,
            new_questions=["\x00" * 120] * 3, necessary_boundary="\x00" * 120))
    assert len(decision._canonical({"assessment": worst.model_dump()})) - 1 <= decision._assessment_reserve()


def test_style_window_uses_delivered_prefix_and_aggregate_as_one_turn(setup):
    frozen = plan(setup, count=3)
    first, second = [segment(setup, frozen, index) for index in range(2)]
    window = delivered_style_window(receipts=[], partial_receipts=[first, second], group_id="10001",
        self_member_id="29999", as_of=NOW + timedelta(days=1))
    turn = window["turns"][0]
    assert len(window["turns"]) == 1 and turn["segments"] == 2
    assert turn["delivery"] == "partial_prefix_not_complete_answer"
    assert turn["style_excerpt"] == "\n\n".join(frozen.reply_segments[:2])
    assert frozen.reply_segments[2] not in turn["style_excerpt"]
    third = segment(setup, frozen, 2)
    complete = aggregate(setup, frozen, [first, second, third])
    window = delivered_style_window(receipts=[complete], partial_receipts=[first, second, third],
        group_id="10001", self_member_id="29999", as_of=NOW + timedelta(days=1))
    assert len(window["turns"]) == 1
    assert window["turns"][0]["receipt_ids"] == [complete.receipt_id]
    assert window["turns"][0]["delivery"] == "complete"


def test_style_window_keeps_eight_scoped_actual_sends_and_labels_long_excerpts(setup):
    store = setup[0]
    receipts = [decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        delivered_text=f"第{index}条：" + "正文" * 1000, evidence_refs=[f"test-evidence://send-{index}"],
        verification="qq_ui_verified", delivered_at=NOW + timedelta(seconds=index)) for index in range(10)]
    wrong = receipts[-1].model_copy(update={"group_id": "10002"})
    wrong_self = receipts[-1].model_copy(update={"self_member_id": "29998"})
    window = delivered_style_window(receipts=[*receipts, wrong, wrong_self], partial_receipts=[],
        group_id="10001", self_member_id="29999", as_of=NOW + timedelta(days=1))
    assert len(window["turns"]) == 8
    assert [turn["receipt_ids"] for turn in window["turns"]] == [[receipt.receipt_id] for receipt in receipts[-8:]]
    assert all(turn["excerpt_only"] and len(turn["style_excerpt"]) < 300 for turn in window["turns"])
    assert len(json.dumps(window, ensure_ascii=False)) < 10_000


def _payload(setup):
    store, _, _, _, records = setup
    context = store.group_context(group_id="10001")
    return context, {"group_context": project_group_context(context.model_dump(mode="json")),
                     "verified_self_member_id": "29999", "delivery_receipts": [], "segment_delivery_receipts": []}


def test_three_minute_topic_references_only_retained_records_after_budget_selection(setup):
    store = setup[0]
    _, fresh = add_member(store, "20001", text="新的实质问题", capture="fresh", moment=NOW + timedelta(minutes=4))
    context, payload = _payload(setup)
    refresh_topic_window(payload, as_of=NOW + timedelta(minutes=5))
    assert payload["topic_window"]["observation_record_ids"] == [fresh.record_id]
    payload["delivered_style_window"] = {"turns": [{"receipt_ids": ["verified-style-only"]}]}
    selected = select_model_context(payload, required_observation_ids={fresh.record_id},
        handled_observation_ids={item.record_id for item in setup[4]}, max_characters=20_000, assessment_reserve=1000)
    assert selected.payload["topic_window"]["observation_record_ids"] == [fresh.record_id]
    assert "verified-style-only" in selected.delivery_ids
    assert selected.payload["delivered_style_window"] == payload["delivered_style_window"]


def test_memory_builder_separates_current_from_history_and_excludes_profiles_and_style_hints(setup):
    context, payload = _payload(setup)
    current = setup[4][1].record_id
    payload["delivered_style_window"] = {"turns": [{"style_excerpt": "你之前说过虚构的事", "receipt_ids": ["fake"]}]}
    entries = build_qq_memory_evidence(payload=payload, trusted_context=context,
        current_observation_ids={current}, receipts=[], partial_receipts=[])
    assert len(entries) == 2 and entries[0].source_id == setup[4][0].record_id
    assert entries[1].source_id == current and entries[1].kind == "current"
    assert entries[0].status == "user_claim"
    assert entries[0].speaker_id == f"qq:member:{setup[4][0].member_id}" and entries[0].observed_at == setup[4][0].observed_at
    segment = ContextSegment(kind="user", text=json.dumps(payload, ensure_ascii=False), source_ids=[entries[0].source_id])
    evidence = MemoryEvidenceContext.bind(audience="qq:group:10001", entries=entries, segment=segment)
    assert evidence.admitted([segment], trigger_ref=current).has_history
    assert not evidence.admitted([segment.model_copy(update={"text": "only an ID: " + entries[0].source_id})], trigger_ref=current).has_history
    payload["group_context"]["members"][0]["observations"][0]["text"] = "tampered"
    with pytest.raises(ValueError, match="projection mismatch"):
        build_qq_memory_evidence(payload=payload, trusted_context=context, current_observation_ids=set(), receipts=[], partial_receipts=[])


def test_trimmed_history_cannot_survive_through_source_ids_or_current_quotation(setup):
    context, payload = _payload(setup)
    old, current = setup[4]
    for member in payload["group_context"]["members"]:
        member["observations"] = [item for item in member["observations"] if item["record_id"] != old.record_id]
    payload["untrusted_request"] = {"source_ids": [old.record_id], "quoted_text": old.text}
    entries = build_qq_memory_evidence(payload=payload, trusted_context=context,
        current_observation_ids={current.record_id}, receipts=[], partial_receipts=[])
    assert [entry.source_id for entry in entries] == [current.record_id]
    assert entries[0].kind == "current"


def test_derived_projection_preserves_tentative_status_and_original_scope(setup):
    context, payload = _payload(setup)
    original = {"record_id": "derived-1", "current_impression": "暂时觉得愿意讨论细节",
                "status": "provisional", "source_channel": "local_chat", "source_conversation_id": "local:main",
                "supporting_episode_ids": ["episode-1"], "authority": "misty_tentative_interpretation"}
    trusted = SimpleNamespace(context={}, derived_memory={"entries": [original]})
    payload["derived_memory"] = deepcopy(trusted.derived_memory)
    entries = build_qq_memory_evidence(payload=payload, trusted_context=context,
        current_observation_ids={item.record_id for item in setup[4]}, receipts=[], partial_receipts=[], shared_owner=trusted)
    derived = [entry for entry in entries if entry.kind == "derived_memory"]
    assert len(derived) == 1 and derived[0].status == "provisional"
    assert derived[0].audience == "qq:group:10001" and derived[0].source_conversation_id == "local:main"
    payload["derived_memory"]["entries"].clear()
    remaining = build_qq_memory_evidence(payload=payload, trusted_context=context,
        current_observation_ids={item.record_id for item in setup[4]}, receipts=[], partial_receipts=[], shared_owner=trusted)
    assert all(entry.kind == "current" for entry in remaining)


def test_full_pipeline_uses_final_qq_history_for_compiler_and_validator(setup, monkeypatch):
    store, client, _, kwargs, old = setup
    client.choice = '{"action":"wait","reason":"先听着"}'
    decision.decide_qq_reply(**kwargs)
    _, current = add_member(store, "20002", text="说说之前聊的内容？", capture="new-question", moment=NOW + timedelta(minutes=1))
    contexts = []
    compile_original = decision._DraftCompiler.compile

    def capture(self, *args, **options):
        compiled = compile_original(self, *args, **options)
        contexts.append(compiled)
        return compiled

    monkeypatch.setattr(decision._DraftCompiler, "compile", capture)
    client.choice = '{"action":"speak","reason":"有新问题"}'
    client.chat_routes = {"": "之前讨论过这个；你想接着说哪一部分？"}
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.outcome == "ready" and contexts
    evidence = contexts[-1].memory_evidence
    assert evidence.has_history
    assert {entry.source_id for entry in evidence.entries if entry.kind == "history"} == {item.record_id for item in old}
    assert {entry.source_id for entry in evidence.entries if entry.kind == "current"} == {current.record_id}
    assert not any(segment.why == "memory_empty_guard" for segment in contexts[-1].segments)
    assert len(client.chat_calls) == 1


def test_first_import_history_reaches_compiler_and_existing_critic_without_prior_decision(setup, monkeypatch, tmp_path):
    store, client, _, kwargs, old = setup
    _, burst = add_member(store, "20002", text="打了110的DPS", capture="burst", moment=NOW + timedelta(minutes=9))
    _, trigger = add_member(store, "20002", text="吓哭了", capture="first-import-trigger",
                            moment=NOW + timedelta(minutes=10))
    settings, preset = decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    trace_dir = tmp_path / "validator-traces"
    monkeypatch.setattr(decision, "_load_configuration", lambda *args: (
        settings.model_copy(update={"validator_sample_rate": 1, "max_revisions": 0,
                                    "call_log_enabled": True, "trace_dir": trace_dir}), preset))
    contexts, critic_payloads = [], []
    original_compile, original_json = decision._DraftCompiler.compile, client.complete_json

    def capture_compile(self, *args, **options):
        compiled = original_compile(self, *args, **options)
        contexts.append(compiled)
        return compiled

    def capture_json(**options):
        try:
            payload = json.loads(options.get("user", ""))
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict) and "memory_evidence" in payload:
            critic_payloads.append(payload)
        return original_json(**options)

    monkeypatch.setattr(decision._DraftCompiler, "compile", capture_compile)
    monkeypatch.setattr(client, "complete_json", capture_json)
    client.chat_routes = {"": "110的DPS确实有点吓人……你说吓哭了，过程应该很刺激吧。"}
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.outcome == "ready" and contexts and critic_payloads
    assert set(result.new_observation_record_ids) == {item.record_id for item in [*old, burst, trigger]}
    expected = {item.record_id for item in [*old, burst, trigger]}
    assert {entry.source_id for entry in contexts[-1].memory_evidence.entries} == expected
    assert {entry.source_id for entry in contexts[-1].memory_evidence.entries if entry.kind == "current"} == {
        burst.record_id, trigger.record_id}
    assert not any(segment.why == "memory_empty_guard" for segment in contexts[-1].segments)
    assert contexts[-1].memory_evidence.current_speaker_id == "qq:member:20002"
    steps = [step for path in trace_dir.glob("calls/*/*.json")
             for step in json.loads(path.read_text(encoding="utf-8"))["steps"]]
    before, = [step["data"] for step in steps if step["kind"] == "validator_input_before"]
    after, = [step["data"] for step in steps if step["kind"] == "validator_input_after"]
    original = contexts[-1].memory_evidence.model_dump(mode="json")
    assert before["original_evidence"] == original
    assert {entry["source_id"] for entry in original["entries"]} == expected
    assert all(entry["source_refs"] for entry in original["entries"])
    projected = critic_payloads[-1]["memory_evidence"]
    mapping = after["projection"]
    assert mapping["evidence"] == projected
    assert mapping["projected_rows"] == [{"original_indices": [index]} for index in range(len(expected))]
    assert _restore(projected, mapping) == [
        {key: entry[key] for key in SEMANTIC_FIELDS} for entry in original["entries"]]
    assert mapping["speaker_alias_to_original"][projected["current_speaker_id"]] == "qq:member:20002"
    speakers = {entry["source_id"]: entry["speaker_id"] for entry in original["entries"]}
    assert speakers == {item.record_id: f"qq:member:{item.member_id}" for item in [*old, burst, trigger]}
    assert all(entry["observed_at"] for entry in original["entries"])
    assert projected["same_source_rows"] == []
    assert "entries" not in projected
    assert all(source_id not in json.dumps(projected) for source_id in expected)
    assert len(client.gate_messages) == len(client.chat_calls) == len(critic_payloads) == 1


def test_current_burst_boundary_excludes_trigger_cutoff_unknown_and_future_timestamps():
    anchor = NOW + timedelta(minutes=10)
    observations = [SimpleNamespace(record_id=record_id, observed_at=moment) for record_id, moment in [
        ("old", anchor - timedelta(minutes=3, microseconds=1)),
        ("cutoff", anchor - timedelta(minutes=3)), ("trigger", anchor),
        ("future", anchor + timedelta(seconds=1)), ("unknown", None),
    ]]
    context = SimpleNamespace(members=[SimpleNamespace(observations=observations)])
    ids = {item.record_id for item in observations} | {"missing"}
    assert current_qq_burst_ids(trusted_context=context, new_observation_ids=ids,
        trigger_record_id="trigger") == ids - {"old"}
    assert current_qq_burst_ids(trusted_context=context, new_observation_ids=ids,
        trigger_record_id=None) == ids
    assert "trigger" in current_qq_burst_ids(trusted_context=context, new_observation_ids={"old"},
        trigger_record_id="trigger")


def test_31_partial_turns_61_segments_keep_complete_groups_and_real_latest_prefix(setup):
    store, client, _, kwargs, _ = setup
    frozen = plan(setup, count=3)
    receipts, last = [], None
    for index in range(31):
        seed = frozen.model_copy(update={"decision_id": f"qq-decision:{index + 1:032x}", "input_hash": f"{index + 1:064x}"})
        with closing(decision._connect(store.path)) as db, db:
            db.execute("INSERT INTO qq_reply_decisions VALUES(?,?,?,?,?)",
                (seed.decision_id, seed.input_hash, "10001", "completed", seed.model_dump_json()))
        for part in range(1 if index == 30 else 2):
            receipts.append(segment(setup, seed, part, delivered_at=NOW + timedelta(seconds=index * 10 + part)))
        last = seed
    assert len(receipts) == 61
    retained = select_partial_receipt_turns(receipts)
    assert len(retained) == 59 and receipts[0].decision_id not in {item.decision_id for item in retained}
    grouped = {}
    for item in retained:
        grouped.setdefault(item.decision_id, []).append(item.segment_index)
    assert all(sorted(indices) == list(range(len(indices))) for indices in grouped.values())
    result = decision.decide_qq_reply(**kwargs, request="现在接着说。")
    assert result.outcome == "ready"
    payload = _gate_payload(client)
    turns = payload["delivered_style_window"]["turns"]
    assert len(turns) == 8 and turns[-1]["turn_id"] == last.decision_id
    assert turns[-1]["segments"] == 1 and turns[-1]["style_excerpt"] == last.reply_segments[0]
    assert turns[-1]["delivery"] == "partial_prefix_not_complete_answer"


def test_partial_gap_fails_context_closed_and_completes_reserved_decision(setup, monkeypatch):
    store, client, _, kwargs, _ = setup
    frozen = plan(setup, count=3)
    segment(setup, frozen, 0)
    second = segment(setup, frozen, 1)
    before = len(client.gate_messages)
    # Simulate a damaged in-memory projection after durable receipt validation.
    # The missing segment cannot be invented or used to claim a full answer.
    monkeypatch.setattr(decision, "_read_segment_receipts", lambda *args, **kw: [second])
    result = decision.decide_qq_reply(**kwargs, request="检查这个新问题。")
    assert result.outcome == "model_invalid" and result.reply is None
    assert len(client.gate_messages) == before
    with closing(decision._connect(store.path)) as db:
        row = db.execute("SELECT status,record_json FROM qq_reply_decisions WHERE decision_id=?", (result.decision_id,)).fetchone()
    assert row["status"] == "completed"
    assert decision.QQReplyDecision.model_validate_json(row["record_json"]).outcome == "model_invalid"


def test_budget_trims_partial_receipts_as_whole_turns_and_preserves_latest_prefix(setup):
    _, payload = _payload(setup)
    receipts = []
    for group in range(3):
        for index in range(2):
            receipts.append({"receipt_id": f"receipt-{group}-{index}", "decision_id": f"turn-{group}",
                "delivered_at": (NOW + timedelta(seconds=group * 10 + index)).isoformat(),
                "delivered_text": "x" * 1300, "segment_index": index})
    payload["segment_delivery_receipts"] = receipts
    baseline_size = len(decision._canonical(payload))
    result = select_model_context(payload, required_observation_ids=set(), handled_observation_ids=set(),
        max_characters=baseline_size - 3000, assessment_reserve=100)
    remaining = result.payload["segment_delivery_receipts"]
    assert remaining and len(remaining) < len(receipts)
    for group in {item["decision_id"] for item in remaining}:
        assert [item["segment_index"] for item in remaining if item["decision_id"] == group] == [0, 1]
    assert {item["decision_id"] for item in remaining} >= {"turn-2"}


def test_receipt_memory_evidence_retains_self_author_and_delivered_time(setup):
    frozen = plan(setup, count=2)
    receipt = segment(setup, frozen)
    context, payload = _payload(setup)
    payload["segment_delivery_receipts"] = [decision._model_delivery_receipt(receipt)]
    entries = build_qq_memory_evidence(payload=payload, trusted_context=context,
        current_observation_ids={item.record_id for item in setup[4]}, receipts=[], partial_receipts=[receipt])
    deliveries = [entry for entry in entries if entry.status == "verified_delivery"]
    assert len(deliveries) == 1 and deliveries[0].speaker_id == "qq:member:29999"
    assert deliveries[0].observed_at == receipt.delivered_at


def test_live_acknowledge_plan_with_extra_null_field_does_not_discard_valid_gate(setup):
    _, client, _, kwargs, _ = setup
    client.choice = json.dumps({"action": "speak", "reason": "回应维护消息。", "reply_plan": {
        "intent": "acknowledge", "must_address": "日志按天归档方便找问题", "new_questions": [],
        "detail_level": "brief", "necessary_boundary": None, "reply_style_reason": None}})
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "ready" and result.reply_plan.intent == "acknowledge"
    assert "reply_style_reason" not in _persona_payload(client)["assessment"]["reply_plan"]
    assert len(client.gate_messages) == len(client.chat_calls) == 1


@pytest.mark.parametrize("plan", [{"intent": "not-a-plan"}, {"new_questions": ["x" * 121]}, "plain-text"])
def test_invalid_optional_plan_falls_back_to_existing_prompt_and_records_diagnostic(setup, plan):
    _, client, _, kwargs, _ = setup
    client.choice = json.dumps({"action": "speak", "reason": "有新问题", "reply_plan": plan})
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "ready" and result.reply_plan is None
    assert result.style_diagnostics["reply_plan_invalid"] is True
    assert _persona_payload(client)["assessment"]["reply_plan"] is None
    assert len(client.gate_messages) == len(client.chat_calls) == 1


@pytest.mark.parametrize("invalid", [{"action": "send_now"}, {"addressing": "force_direct"},
                                      {"force_send": True}, {"reply_plan_note": None}])
# Retain strict rollback behavior in addition to default repair coverage.
def test_invalid_top_level_gate_is_still_rejected_even_with_optional_plan(legacy_json_mode, setup, invalid):
    _, client, _, kwargs, _ = setup
    client.choice = json.dumps({"action": "speak", "reason": "有新问题", "reply_plan": {"intent": "invalid"}, **invalid})
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "error_fallback" and result.reply == decision.ERROR_FALLBACK_REPLY
    assert len(client.gate_messages) == 1 and not client.chat_calls


def _leaked_gate_json() -> str:
    return json.dumps({
        "action": "speak",
        "reason": "当前有人直接点名并请求查看状态，需要给出具体回应。",
        "reply_style": "conversational",
        "addressing": "direct",
        "reply_plan": {
            "intent": "answer",
            "must_address": "说明当前运行正常。",
            "new_questions": [],
            "detail_level": "brief",
            "necessary_boundary": None,
        },
    }, ensure_ascii=False)


@pytest.mark.parametrize("text,expected", [
    (_leaked_gate_json(), True),
    ('{"action":"wait","reason":"先听着。"}', True),
    ('{"action":"speak"}', True),
    ("```json\n" + _leaked_gate_json() + "\n```", True),
    ("我选先修复事实判断。", False),
    ("配置大概是 {先修} 这样。", False),
    ('{"foo":1}', False),
    ('{"action":"delete"}', False),
    ('好的 {"action":"speak","reason":"ok"}', False),
])
def test_gate_json_body_detector_is_structural(text, expected):
    assert decision._looks_like_gate_json_reply(text) is expected


def test_gate_shaped_json_body_fails_closed_without_salvage(setup):
    _, client, _, kwargs, _ = setup
    leaked = _leaked_gate_json()
    client.chat_routes = {"": leaked}
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trace=trace)
    assert result.outcome == "error_fallback" and result.action == "speak"
    assert result.reply == decision.ERROR_FALLBACK_REPLY
    assert result.reply_segments == [decision.ERROR_FALLBACK_REPLY]
    assert result.decision_source == "model_invalid"
    assert leaked not in result.reply
    assert "must_address" not in result.reply
    assert trace.snapshot()["counters"]["gate_json_body_rejected"] == 1
    assert len(client.gate_messages) == len(client.chat_calls) == 1


def test_historical_gate_json_receipt_text_still_validates():
    leaked = _leaked_gate_json()
    assert decision._validate_reply_segments([leaked], leaked) == [leaked]
