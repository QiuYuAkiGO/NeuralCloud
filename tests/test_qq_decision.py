from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import httpx
import pytest

from harness.core.inference import ParsedInteraction, TokenUsage
from harness.core.models import InteractionEvent
from harness.llm.client import ChatResult, MockLLM
from harness.pipeline.pipeline import InferencePipeline
from harness.qq import decision
from harness.qq import draft
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput
from harness.qq.telemetry import QQDeadline, QQTurnTrace


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 11, 2, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def isolated_static_parser_cache():
    with decision._STATIC_PARSE_LOCK:
        decision._STATIC_PARSE_CACHE.clear()
    yield
    with decision._STATIC_PARSE_LOCK:
        decision._STATIC_PARSE_CACHE.clear()


class DecisionLLM(MockLLM):
    def __init__(self, choice='{"action":"speak","reason":"当前话题值得自然参与。"}'):
        super().__init__(chat_routes={"": "嗯，我也想听听。"})
        self.choice = choice
        self.gate_messages = []
        self.gate_options = []
        self.closed = False

    def complete_chat_result(self, messages, **kwargs):
        if decision.is_qq_gate_messages(messages):
            self.gate_messages.append(messages)
            self.gate_options.append(kwargs)
            if isinstance(self.choice, Exception):
                raise self.choice
            return ChatResult(text=self.choice, usage=TokenUsage(prompt_tokens=30, completion_tokens=10, source="api"))
        return super().complete_chat_result(messages, **kwargs)

    def close(self):
        self.closed = True


def add_member(store, member, *, group="10001", text=None, capture=None, moment=NOW):
    capture = capture or f"capture-{member}"
    profile = store.record_profile(QQVerifiedProfileInput(
        group_id=group, member_id=member, display_name=f"UNTRUSTED_NAME_{member}",
        display_name_source="qq_profile", verification="qq_ui_verified", capture_id=f"profile-{capture}",
        observed_at=moment, group_evidence=[f"test-evidence://group-{group}"], member_evidence=[f"test-evidence://member-{member}"],
    )).profile
    if text is None:
        return profile, None
    observation = store.record_observation(QQVisualObservationInput(
        group_id=group, member_id=member, profile_record_id=profile.record_id, capture_id=capture,
        capture_item_id="bubble:1", observed_at=moment, text=text,
        evidence=[f"test-evidence://chat-{capture}"], attribution_evidence=[f"test-evidence://author-{capture}"],
        author_attribution="qq_ui_verified", claims=[{"statement": "UNTRUSTED_CLAIM", "supporting_quote": text}],
    )).observation
    return profile, observation


@pytest.fixture
def setup(tmp_path, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("A decision test attempted a network request or interaction commit")

    monkeypatch.setattr(httpx.Client, "send", forbidden)
    monkeypatch.setattr(InferencePipeline, "respond", forbidden)
    config = tmp_path / "model.json"
    config.write_text(json.dumps({"mode": "live", "model": "synthetic-model", "base_url": "https://model.example.invalid/v1", "api_key": "synthetic-private-api-key"}))
    preset = tmp_path / "persona.json"
    preset.write_bytes((ROOT / "presets/builtin/misty.json").read_bytes())
    store = QQVisualMemoryStore(tmp_path / "private.sqlite3")
    _, first = add_member(store, "20001", text="UNTRUSTED_A 我记得之前讨论过这个。")
    _, second = add_member(store, "20002", text="UNTRUSTED_B 你怎么看？")
    add_member(store, "20001", group="10002", text="WRONG_GROUP_SECRET", capture="elsewhere")
    client = DecisionLLM()
    builds = []
    monkeypatch.setattr(decision, "build_llm_client", lambda settings: (builds.append(settings), client)[1])
    kwargs = dict(memory_db=store.path, group_id="10001", self_member_id="29999", config_path=config, preset_path=preset)
    return store, client, builds, kwargs, [first, second]


def test_multimember_speak_uses_user_side_history_and_is_replayable(setup):
    store, client, builds, kwargs, records = setup
    before = store.group_context(group_id="10001").model_dump()
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002", request="REQUEST_DATA", query="之前")
    assert result.action == "speak" and result.reply
    assert result.participants == ["20001", "20002"]
    assert result.automatic_send is False and result.new_decision is True
    assert set(result.source_record_ids) == {item.record_id for item in records}
    assert result.model == "synthetic-model" and result.persona_version
    assert result.prompt_version == decision.PROMPT_VERSION
    assert result.usage.calls >= 2
    assert builds[0].call_log_enabled is False and builds[0].record_interaction is False
    all_messages = client.gate_messages + client.chat_calls
    system = "\n".join(m["content"] for call in all_messages for m in call if m["role"] == "system")
    user = "\n".join(m["content"] for call in all_messages for m in call if m["role"] == "user")
    for marker in ["UNTRUSTED_A", "UNTRUSTED_B", "UNTRUSTED_NAME", "UNTRUSTED_CLAIM", "REQUEST_DATA"]:
        assert marker in user and marker not in system
    assert "WRONG_GROUP_SECRET" not in user + system
    assert '"message_id":null' in user
    assert '"coverage":"partial_verified_observations"' in user
    for call in [client.gate_messages[0], client.chat_calls[0]]:
        static_rules = "\n".join(message["content"] for message in call if message["role"] == "system")
        assert "观察库只覆盖部分已核验采集" in static_rules
        assert "目前记录中" in static_rules and "我记得的" in static_rules
        assert "不能把没有记录" in static_rules or "缺少记录不证明" in static_rules
    assert store.group_context(group_id="10001").model_dump() == before
    replay = decision.decide_qq_reply(**kwargs, trigger_member_id="20002", request="REQUEST_DATA", query="之前")
    assert replay.decision_id == result.decision_id and replay.reply == result.reply
    assert replay.replayed is True and replay.new_decision is False
    assert len(client.gate_messages) == 1 and len(builds) == 1


def test_persona_model_can_wait_even_when_a_member_triggers(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"两位正在彼此闲聊，先听着。"}'
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kwargs: pytest.fail("wait generated a draft"))
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.action == "wait" and result.reply is None and result.decision_source == "model"
    assert result.usage.calls == 1


def test_qq_gate_and_persona_use_low_reasoning_on_official_deepseek(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    settings, preset = decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    tuned = settings.model_copy(update={"llm_model": "deepseek-v4-flash", "llm_base_url": "https://api.deepseek.com/v1"})
    monkeypatch.setattr(decision, "_load_configuration", lambda *args: (tuned, preset))
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.action == "speak" and result.outcome == "ready"
    assert client.gate_options[0].get("reasoning_effort") == "low"
    assert client.gate_options[0].get("response_format") == {"type": "json_object"}
    persona_options = [item for item in client.chat_call_options if item.get("reasoning_effort") == "low"]
    assert persona_options
    assert all(item.get("response_format") is None for item in persona_options)


def test_qq_gate_and_persona_skip_low_reasoning_off_official_deepseek(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    settings, preset = decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    tuned = settings.model_copy(update={"llm_model": "deepseek-v4-flash", "llm_base_url": "https://proxy.invalid/v1"})
    monkeypatch.setattr(decision, "_load_configuration", lambda *args: (tuned, preset))
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.action == "speak" and result.outcome == "ready"
    assert all("reasoning_effort" not in item for item in client.gate_options)
    assert client.gate_options[0].get("response_format") == {"type": "json_object"}
    assert all(item.get("reasoning_effort") != "low" for item in client.chat_call_options)


@pytest.mark.parametrize("configured,expected", [(512, 8192), (12288, 12288), (32768, 16384)])
def test_qq_helper_budget_reaches_persona_parser_without_mutating_loaded_settings(setup, monkeypatch, configured, expected):
    monkeypatch.setenv("NC_QQ_BODY_PIPELINE", "legacy")
    _, client, builds, kwargs, _ = setup
    settings, preset = decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    original = settings.model_copy(update={"llm_json_max_tokens": configured})
    original_snapshot = original.model_dump()
    monkeypatch.setattr(decision, "_load_configuration", lambda *args: (original, preset))
    helper_budgets = []
    complete_json = client.complete_json

    def record_helper(**options):
        helper_budgets.append(options.get("max_tokens"))
        return complete_json(**options)

    monkeypatch.setattr(client, "complete_json", record_helper)
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.action == "speak" and result.outcome == "ready"
    assert builds[0] is not original and builds[0].llm_json_max_tokens == expected
    assert expected in helper_budgets
    assert original.model_dump() == original_snapshot


def test_profile_only_update_is_not_a_new_reply_demand(setup):
    store, client, builds, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"无需接话。"}'
    first = decision.decide_qq_reply(**kwargs)
    add_member(store, "20001", capture="new-profile", moment=NOW + timedelta(minutes=1))
    result = decision.decide_qq_reply(**kwargs)
    assert result.decision_id != first.decision_id
    assert result.action == "wait" and result.decision_source == "no_new_observations"
    assert result.reply is None and len(builds) == 1


def test_new_self_bubble_does_not_trigger_answering_oneself(setup):
    store, _, builds, kwargs, _ = setup
    decision.decide_qq_reply(**kwargs)
    add_member(store, "29999", text="这是我刚说过的话。", moment=NOW + timedelta(minutes=1))
    result = decision.decide_qq_reply(**kwargs)
    assert result.action == "wait" and result.decision_source == "self_only_update"
    assert len(builds) == 1


def test_external_verified_receipt_marks_only_its_explicit_sources_handled(setup):
    store, _, builds, kwargs, records = setup
    receipt = decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        delivered_text="此前独立核验过的回复。", evidence_refs=["test-evidence://sent-bubble"], verification="qq_ui_verified",
        source_record_ids=[item.record_id for item in records])
    same = decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        delivered_text="此前独立核验过的回复。", evidence_refs=["test-evidence://sent-bubble"], verification="qq_ui_verified",
        source_record_ids=[item.record_id for item in records])
    assert same.receipt_id == receipt.receipt_id
    result = decision.decide_qq_reply(**kwargs)
    assert result.action == "wait" and result.decision_source == "no_new_observations"
    assert result.delivery_refs == [receipt.receipt_id] and not builds


def test_decision_is_not_delivery_and_verified_receipt_requires_exact_text(setup):
    store, _, _, kwargs, _ = setup
    result = decision.decide_qq_reply(**kwargs)
    assert result.action == "speak" and result.delivery_refs == []
    common = dict(memory_db=store.path, group_id="10001", self_member_id="29999", decision_id=result.decision_id,
                  evidence_refs=["test-evidence://verified-bubble"], verification="qq_ui_verified")
    with pytest.raises(decision.QQDraftError, match="exactly match"):
        decision.record_qq_delivery(**common, delivered_text="a different text")
    receipt = decision.record_qq_delivery(**common, delivered_text=result.reply)
    assert receipt.source_record_ids == []
    after = decision.decide_qq_reply(**kwargs)
    assert after.action == "wait" and receipt.receipt_id in after.delivery_refs


def test_delivery_lookup_is_readonly_and_strictly_scoped(setup, monkeypatch):
    store, _, _, kwargs, records = setup
    result = decision.decide_qq_reply(**kwargs)
    assert decision.get_qq_delivery(memory_db=store.path, group_id="10001", decision_id=result.decision_id) is None
    receipt = decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        decision_id=result.decision_id, delivered_text=result.reply, source_record_ids=[records[0].record_id],
        evidence_refs=["test-evidence://durable-receipt"], verification="qq_ui_verified")
    actual_connect, calls = sqlite3.connect, []

    def read_only_connect(database, **kwargs):
        assert database.endswith("?mode=ro") and kwargs["uri"] is True
        calls.append(database)
        return actual_connect(database, **kwargs)

    monkeypatch.setattr(decision.sqlite3, "connect", read_only_connect)
    assert decision.get_qq_delivery(memory_db=store.path, group_id="10001", decision_id=result.decision_id) == receipt
    assert decision.get_qq_delivery(memory_db=store.path, group_id="10002", decision_id=result.decision_id) is None
    assert decision.get_qq_delivery(memory_db=store.path, group_id="10001", decision_id="qq-decision:missing") is None
    assert len(calls) == 3


def test_delivery_lookup_does_not_create_missing_database_or_tables(tmp_path):
    path = tmp_path / "nonexistent.sqlite3"
    with pytest.raises(decision.QQDraftError, match="existing"):
        decision.get_qq_delivery(memory_db=path, group_id="10001", decision_id="qq-decision:missing")
    assert not path.exists()
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE synthetic_existing (value TEXT)")
    original = path.read_bytes()
    assert decision.get_qq_delivery(memory_db=path, group_id="10001", decision_id="qq-decision:missing") is None
    assert path.read_bytes() == original


@pytest.mark.parametrize("mutation", ["json", "group", "decision", "receipt", "hash", "origin"])
def test_delivery_lookup_refuses_malformed_or_misindexed_receipt(setup, mutation):
    store, _, _, kwargs, _ = setup
    result = decision.decide_qq_reply(**kwargs)
    receipt = decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        decision_id=result.decision_id, delivered_text=result.reply,
        evidence_refs=["test-evidence://durable-receipt"], verification="qq_ui_verified")
    record = receipt.model_dump(mode="json")
    record.update({"group": {"group_id": "10002"}, "decision": {"decision_id": "qq-decision:other"},
                   "receipt": {"receipt_id": "qq-delivery:other"}, "origin": {"origin": "external_verification"}}.get(mutation, {}))
    encoded = "PRIVATE invalid json" if mutation == "json" else json.dumps(record)
    with sqlite3.connect(store.path) as connection:
        connection.execute("UPDATE qq_reply_deliveries SET record_json=?", (encoded,))
        if mutation == "hash":
            connection.execute("UPDATE qq_reply_deliveries SET receipt_hash='invalid'")
    with pytest.raises(decision.QQDraftError, match="unavailable or inconsistent") as error:
        decision.get_qq_delivery(memory_db=store.path, group_id="10001", decision_id=result.decision_id)
    assert "PRIVATE" not in str(error.value)


@pytest.mark.parametrize("group,identifier", [(True, "id"), ("01", "id"), ("10001", ""), ("10001", "id with space"), ("10001", None)])
def test_delivery_lookup_rejects_bad_identity_before_open(tmp_path, group, identifier):
    with pytest.raises(decision.QQDraftError, match="exact group"):
        decision.get_qq_delivery(memory_db=tmp_path / "unused", group_id=group, decision_id=identifier)


def test_delivery_addresses_explicit_subset_not_all_considered_history(setup):
    store, _, _, kwargs, records = setup
    result = decision.decide_qq_reply(**kwargs)
    receipt = decision.record_qq_delivery(memory_db=store.path, group_id="10001", self_member_id="29999",
        decision_id=result.decision_id, delivered_text=result.reply,
        evidence_refs=["test-evidence://sent-one-answer"], verification="qq_ui_verified",
        source_record_ids=[records[0].record_id])
    assert len(result.source_record_ids) == 2
    assert receipt.source_record_ids == [records[0].record_id]


@pytest.mark.parametrize("choice,origin", [
    ('{"action":"speak","reason":""}', "model_invalid"),
    (RuntimeError("synthetic-secret PRIVATE_CHAT"), "model_error"),
])
def test_invalid_or_failed_gate_never_generates_text(setup, monkeypatch, choice, origin):
    _, client, builds, kwargs, _ = setup
    client.choice = choice
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kwargs: pytest.fail("invalid gate generated speech"))
    result = decision.decide_qq_reply(**kwargs)
    assert result.action == "speak" and result.outcome == "error_fallback"
    assert result.reply == decision.ERROR_FALLBACK_REPLY and result.decision_source == origin
    assert "PRIVATE_CHAT" not in result.model_dump_json()
    replay = decision.decide_qq_reply(**kwargs)
    assert replay.decision_id == result.decision_id and replay.replayed
    assert len(builds) == 1


def test_concurrent_identical_input_reserves_once_and_pending_replays_wait(setup, monkeypatch):
    _, client, builds, kwargs, _ = setup
    entered, release = threading.Event(), threading.Event()
    original = client.complete_chat_result

    def slow(messages, **options):
        if decision.is_qq_gate_messages(messages):
            entered.set()
            assert release.wait(5)
        return original(messages, **options)

    monkeypatch.setattr(client, "complete_chat_result", slow)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(decision.decide_qq_reply, **kwargs)
        assert entered.wait(5)
        try:
            pending = decision.decide_qq_reply(**kwargs)
            assert pending.action == "wait" and pending.decision_source == "pending"
            assert pending.replayed is True
        finally:
            release.set()
        completed = future.result(timeout=5)
    assert pending.decision_id == completed.decision_id
    assert len(builds) == 1


def test_unknown_trigger_is_not_guessed(setup):
    _, _, builds, kwargs, _ = setup
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="27777")
    assert result.action == "wait" and result.decision_source == "unknown_trigger" and not builds


def test_cli_keeps_reply_reason_and_secrets_in_private_file(setup, tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    store, _, _, kwargs, _ = setup
    output = tmp_path / "decision.json"
    args = ["--development-mode", "--memory-db", str(store.path), "--group-id", "10001", "--self-member-id", "29999", "--trigger-member-id", "20001",
            "--config", str(kwargs["config_path"]), "--preset", str(kwargs["preset_path"]), "--output", str(output)]
    assert decision.main(args) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    stdout = capsys.readouterr().out
    assert result["query_source"] == "trigger_observation" and result["effective_query"] not in stdout
    assert result["reply"] not in stdout and result["reason"] not in stdout
    assert "UNTRUSTED" not in stdout and "synthetic-private-api-key" not in stdout
    assert decision.main(args) == 2


def test_draft_group_mode_uses_wait_decision_instead_of_forcing_reply(setup, tmp_path, monkeypatch):
    monkeypatch.setenv("NC_CONTROL_MODE", "development_debug")
    store, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"继续听大家聊。"}'
    output = tmp_path / "group-decision.json"
    assert draft.main(["--development-mode", "--mode", "group", "--memory-db", str(store.path), "--group-id", "10001",
                       "--member-id", "20002", "--self-member-id", "29999",
                       "--config", str(kwargs["config_path"]), "--preset", str(kwargs["preset_path"]),
                       "--output", str(output)]) == 0
    result = json.loads(output.read_text(encoding="utf-8"))
    assert result["action"] == "wait" and result["reply"] is None
    assert not client.chat_calls


def test_default_trigger_query_retrieves_middle_history_beyond_recent_and_earliest(setup):
    store, client, _, kwargs, _ = setup
    _, middle = add_member(store, "20001", capture="middle-topic", text="鸢尾花计划在周末进行。",
                           moment=NOW + timedelta(days=1))
    add_member(store, "20001", capture="later-other", text="今天看见了一只猫。", moment=NOW + timedelta(days=2))
    _, trigger = add_member(store, "20001", capture="latest-question", text="还记得鸢尾花计划吗？",
                            moment=NOW + timedelta(days=3))
    without_query = store.group_context(group_id="10001", recent_limit_per_member=1, history_limit_per_member=1)
    assert middle.record_id not in {item.record_id for member in without_query.members for item in member.observations}
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001",
                                      recent_limit_per_member=1, history_limit_per_member=1)
    assert result.query_source == "trigger_observation" and result.effective_query == trigger.text
    assert result.derived_query_truncated is False
    assert middle.record_id in result.source_record_ids
    payload = decision.combined_user_payload(client.gate_messages[0])
    assert payload["query_source"] == "trigger_observation"
    assert payload["group_context"]["retrieval"]["query"] == trigger.text
    historical = {item["record_id"]: item for member in payload["group_context"]["members"] for item in member["observations"]}
    assert historical[middle.record_id]["text"] == middle.text
    assert middle.text not in client.gate_messages[0][0]["content"]
    replay = decision.decide_qq_reply(**kwargs, trigger_member_id="20001",
                                      recent_limit_per_member=1, history_limit_per_member=1)
    assert replay.decision_id == result.decision_id and replay.replayed
    assert len(client.gate_messages) == 1


def test_derived_query_is_bounded_without_truncating_original_observation(setup):
    store, client, _, kwargs, _ = setup
    full_text = "鸢尾花计划 " + "记录内容" * 160 + " ORIGINAL_END"
    _, trigger = add_member(store, "20001", capture="long-trigger", text=full_text, moment=NOW + timedelta(days=1))
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001")
    assert result.effective_query == full_text[:512] and result.derived_query_truncated is True
    payload = decision.combined_user_payload(client.gate_messages[0])
    assert payload["derived_query_truncated"] is True
    actual = next(item for member in payload["group_context"]["members"] for item in member["observations"] if item["record_id"] == trigger.record_id)
    assert actual["text"] == full_text
    assert full_text not in client.gate_messages[0][0]["content"]


def test_explicit_query_takes_precedence_over_trigger_text(setup):
    _, client, _, kwargs, _ = setup
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001", query="显式历史主题")
    assert result.query_source == "explicit" and result.effective_query == "显式历史主题"
    assert result.derived_query_truncated is False
    payload = decision.combined_user_payload(client.gate_messages[0])
    assert payload["group_context"]["retrieval"]["query"] == "显式历史主题"


@pytest.mark.parametrize("text", ["🙂！！", "——……"])
def test_nonsearchable_trigger_keeps_query_none_without_failing(setup, text):
    store, client, _, kwargs, _ = setup
    add_member(store, "20001", capture="punctuation", text=text, moment=NOW + timedelta(days=1))
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001")
    assert result.query_source == "none" and result.effective_query is None
    assert result.decision_source == "model" and len(client.gate_messages) == 1


def test_host_request_without_trigger_does_not_derive_query_and_old_records_still_load(setup):
    _, _, _, kwargs, _ = setup
    result = decision.decide_qq_reply(**kwargs, request="检查记录中的历史。")
    assert result.query_source == "none" and result.effective_query is None
    old_json = result.model_dump(mode="json")
    for field in ["query_source", "effective_query", "derived_query_truncated"]:
        old_json.pop(field)
    restored = decision.QQReplyDecision.model_validate(old_json)
    assert restored.query_source == "none" and restored.effective_query is None
    assert restored.derived_query_truncated is False


class Clock:
    def __init__(self):
        self.value = 100.0

    def __call__(self):
        return self.value


def static_event(**updates):
    return InteractionEvent(timestamp=NOW, session_id="synthetic-session", speaker="operator",
                            raw_text=decision._STATIC_PERSONA_INSTRUCTION).model_copy(update=updates)


def parser_cache_case(setup, *, result=None, error=None):
    _, client, _, kwargs, _ = setup
    settings, preset = decision._load_configuration(kwargs["config_path"], kwargs["preset_path"])
    parsed = result or ParsedInteraction(speech_act="request", entities=["operator"], gate_vote="fast")
    calls = []

    def parse(event):
        calls.append(event)
        if error:
            raise error
        return parsed.model_copy(deep=True)

    llm = SimpleNamespace(client=client, trace=QQTurnTrace(), deadline=None, failed_error_type=None)
    parser = decision._StaticInstructionParser(SimpleNamespace(parse=parse), settings=settings, preset=preset, llm=llm)
    return parser, settings, preset, llm, calls


def test_only_fixed_instruction_parse_is_cached_with_deep_copy_and_fresh_identity(setup):
    parser, _, _, llm, calls = parser_cache_case(setup)
    first = parser.parse(static_event())
    first.entities.append("mutated-by-first-pipeline")
    second = parser.parse(static_event(session_id="another-session", timestamp=NOW + timedelta(hours=1)))
    assert len(calls) == 1 and second.gate_vote == "fast"
    assert second.parse_id != first.parse_id and second.entities == ["operator"]
    second.entities.clear()
    third = parser.parse(static_event())
    assert third.entities == ["operator"] and third.parse_id != second.parse_id
    assert llm.trace.snapshot()["counters"] == {"static_parse_cache_miss": 1, "static_parse_cache_hit": 2}


@pytest.mark.parametrize("updates", [{"raw_text": "actual QQ user message"}, {"speaker": "member"},
                                     {"raw_text": decision._STATIC_PERSONA_INSTRUCTION + " "}])
def test_actual_message_or_different_speaker_never_enters_static_cache(setup, updates):
    parser, _, _, llm, calls = parser_cache_case(setup)
    parser.parse(static_event(**updates))
    parser.parse(static_event(**updates))
    assert len(calls) == 2 and not decision._STATIC_PARSE_CACHE
    assert llm.trace.snapshot()["counters"] == {}


@pytest.mark.parametrize("failure", ["exception", "rule_fallback", "client_failure", "expired"])
def test_parser_failures_fallbacks_and_expired_results_are_never_cached(setup, failure):
    parser, _, _, llm, calls = parser_cache_case(setup, error=ValueError("synthetic error") if failure == "exception" else None)
    if failure == "rule_fallback":
        parser.delegate.parse = lambda event: ParsedInteraction(speech_act="statement", gate_vote=None)
    elif failure == "client_failure":
        llm.failed_error_type = "model_invalid"
    elif failure == "expired":
        clock = Clock()
        llm.deadline = QQDeadline.after(1, clock=clock)
        original = parser.delegate.parse

        def late(event):
            result = original(event)
            clock.value += 2
            return result

        parser.delegate.parse = late
    if failure == "exception":
        with pytest.raises(ValueError):
            parser.parse(static_event())
    else:
        parser.parse(static_event())
    assert not decision._STATIC_PARSE_CACHE


@pytest.mark.parametrize("change", ["model", "endpoint", "credential", "budget", "preset", "parser_prompt"])
def test_static_cache_is_invalidated_by_parser_model_settings_or_persona(setup, monkeypatch, change):
    parser, settings, preset, llm, calls = parser_cache_case(setup)
    parser.parse(static_event())
    updates = {"model": {"llm_model": "changed"}, "endpoint": {"llm_base_url": "https://changed.invalid"},
               "credential": {"llm_api_key": "synthetic-changed-key"}, "budget": {"llm_json_max_tokens": 16000}}
    if change in updates:
        settings = settings.model_copy(update=updates[change])
    elif change == "preset":
        preset = preset.model_copy(update={"persona": preset.persona.model_copy(update={"version": "changed-version"})})
    else:
        monkeypatch.setattr(decision.parser_module, "_SYSTEM", decision.parser_module._SYSTEM + " Changed.")
    changed = decision._StaticInstructionParser(parser.delegate, settings=settings, preset=preset, llm=llm)
    changed.parse(static_event())
    assert len(calls) == 2 and changed.key != parser.key


def test_payload_context_size_does_not_invalidate_invariant_parser_input(setup):
    parser, settings, preset, llm, calls = parser_cache_case(setup)
    parser.parse(static_event())
    settings = settings.model_copy(update={"context_token_budget": settings.context_token_budget + 4000})
    changed = decision._StaticInstructionParser(parser.delegate, settings=settings, preset=preset, llm=llm)
    changed.parse(static_event())
    assert len(calls) == 1 and changed.key == parser.key


def test_static_cache_has_absolute_24h_ttl_and_at_most_eight_entries(setup, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(decision, "_STATIC_PARSE_CLOCK", clock)
    parser, settings, preset, llm, calls = parser_cache_case(setup)
    parser.parse(static_event())
    clock.value += 8 * 3600
    parser.parse(static_event())
    assert len(calls) == 1
    clock.value += 16 * 3600
    parser.parse(static_event())
    assert len(calls) == 2
    for index in range(9):
        different = decision._StaticInstructionParser(parser.delegate, settings=settings.model_copy(update={"llm_model": f"synthetic-{index}"}), preset=preset, llm=llm)
        different.parse(static_event())
    assert len(decision._STATIC_PARSE_CACHE) == 8 and parser.key not in decision._STATIC_PARSE_CACHE


def test_new_qq_context_still_runs_fresh_speak_gate_and_persona_with_only_static_parse_cached(setup, monkeypatch):
    monkeypatch.setenv("NC_QQ_BODY_PIPELINE", "legacy")
    store, client, _, kwargs, _ = setup
    calls = []
    original = client.complete_json

    def parse_json(**options):
        if options["system"] == decision.parser_module._SYSTEM:
            calls.append(options)
            return {"speech_act": "request", "entities": ["operator"], "gate": {"path": "fast", "reason": "static instruction"}}
        return original(**options)

    monkeypatch.setattr(client, "complete_json", parse_json)
    first_trace, next_trace = QQTurnTrace(), QQTurnTrace()
    first = decision.decide_qq_reply(**kwargs, trigger_member_id="20002", trace=first_trace)
    add_member(store, "20002", text="ENTIRELY_NEW_QQ_PAYLOAD", capture="fresh-next")
    second = decision.decide_qq_reply(**kwargs, trigger_member_id="20002", trace=next_trace)
    assert len(calls) == 1 and len(client.gate_messages) == 2 and len(client.chat_calls) >= 2
    assert first.action == second.action == "speak" and first.decision_id != second.decision_id
    assert first_trace.snapshot()["counters"]["static_parse_cache_miss"] == 1
    assert next_trace.snapshot()["counters"]["static_parse_cache_hit"] == 1
    assert "ENTIRELY_NEW_QQ_PAYLOAD" in json.dumps(client.chat_calls[-1], ensure_ascii=False)


def alias(**updates):
    return decision.QQVerifiedSelfAlias.model_validate({
        "group_id": "10001", "self_member_id": "29999", "alias": "Misty",
        "evidence_refs": ["test-evidence://verified-self"], **updates,
    })


def test_natural_wait_is_successful_model_only_and_has_stage_timing(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    clock = Clock()
    trace = QQTurnTrace(event_id="event:synthetic", first_seen_at=NOW, clock=clock)
    original = client.complete_chat_result

    def measured(*args, **options):
        clock.value += 2
        return original(*args, **options)

    client.choice = '{"action":"wait","reason":"继续听其他成员聊。"}'
    monkeypatch.setattr(client, "complete_chat_result", measured)
    result = decision.decide_qq_reply(**kwargs, trace=trace, deadline=QQDeadline.after(20, clock=clock))
    assert result.outcome == "natural_wait" and result.error_type is None
    assert result.completed_at and result.timings_ms == {"gate": 2000, "persona": 0, "total": 2000}
    assert result.model_call_counts == {"gate": 1, "persona": 0}
    assert trace.snapshot()["decision_id"] == result.decision_id
    replay_trace = QQTurnTrace(clock=clock)
    replay = decision.decide_qq_reply(**kwargs, trace=replay_trace)
    assert replay.replayed and replay_trace.snapshot()["counters"] == {
        "decision_cache_hits": 1,
        "context_source_characters": trace.snapshot()["counters"]["context_source_characters"],
        "context_base_characters": trace.snapshot()["counters"]["context_base_characters"],
    }


def test_already_expired_turn_never_calls_model_or_returns_active_intention(setup):
    _, _, builds, kwargs, _ = setup
    clock = Clock()
    trace = QQTurnTrace(clock=clock)
    result = decision.decide_qq_reply(**kwargs, deadline=QQDeadline.after(0, clock=clock), trace=trace)
    assert result.outcome == "expired" and result.decision_source == "expired"
    assert result.action == "wait" and result.reply is None and not builds
    assert result.model_call_counts == {"gate": 0, "persona": 0}
    replay = decision.decide_qq_reply(**kwargs)
    assert replay.outcome == "expired" and replay.replayed and not builds


@pytest.mark.parametrize("choice", ['{"action":"speak","reason":"需要回应。"}', '{"action":"wait","reason":"不用说话。"}'])
def test_late_gate_response_is_expired_including_late_wait(setup, monkeypatch, choice):
    _, client, _, kwargs, _ = setup
    clock = Clock()
    trace = QQTurnTrace(clock=clock)
    original = client.complete_chat_result
    client.choice = choice

    def late(*args, **options):
        assert options["timeout_seconds"] == 10
        result = original(*args, **options)
        clock.value += 11
        return result

    monkeypatch.setattr(client, "complete_chat_result", late)
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kw: pytest.fail("late gate reached persona"))
    result = decision.decide_qq_reply(**kwargs, deadline=QQDeadline.after(10, clock=clock), trace=trace)
    assert result.outcome == "error_fallback" and result.reply == decision.ERROR_FALLBACK_REPLY
    assert result.decision_source == "expired" and result.error_type == "deadline_expired"
    assert result.model_call_counts == {"gate": 1, "persona": 0}
    assert trace.snapshot()["counters"]["late_results"] == 1


def test_gate_and_persona_share_remaining_timeout_without_changing_user_config(setup, monkeypatch):
    _, client, builds, kwargs, _ = setup
    clock = Clock()
    timeouts = []
    original = client.complete_chat_result
    config_before = kwargs["config_path"].read_bytes()

    def measured(*args, **options):
        timeouts.append(options["timeout_seconds"])
        result = original(*args, **options)
        clock.value += 3
        return result

    monkeypatch.setattr(client, "complete_chat_result", measured)
    result = decision.decide_qq_reply(**kwargs, deadline=QQDeadline.after(20, clock=clock),
                                      trace=QQTurnTrace(clock=clock))
    assert result.outcome == "ready" and result.action == "speak"
    assert timeouts[:2] == [20, 17]
    assert all(0 < value <= 20 for value in timeouts)
    assert result.timings_ms["gate"] == 3000 and result.timings_ms["persona"] >= 3000
    assert builds[0].llm_timeout == 60
    assert kwargs["config_path"].read_bytes() == config_before


def test_persona_late_text_cannot_become_send_intention(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    clock = Clock()
    original = client.complete_chat_result

    def late_persona(messages, **options):
        result = original(messages, **options)
        if not decision.is_qq_gate_messages(messages):
            clock.value += 11
        return result

    monkeypatch.setattr(client, "complete_chat_result", late_persona)
    result = decision.decide_qq_reply(**kwargs, deadline=QQDeadline.after(10, clock=clock),
                                      trace=QQTurnTrace(clock=clock))
    assert result.outcome == "error_fallback" and result.action == "speak"
    assert result.reply == decision.ERROR_FALLBACK_REPLY
    assert result.model_call_counts["persona"] >= 1
    replay = decision.decide_qq_reply(**kwargs)
    assert replay.outcome == "error_fallback" and replay.reply == decision.ERROR_FALLBACK_REPLY


def test_caught_persona_helper_error_is_not_silently_a_ready_reply(setup, monkeypatch):
    _, client, _, kwargs, _ = setup

    def helper(**options):
        raise httpx.ReadTimeout("PRIVATE_BODY synthetic-secret")

    def prepare(**options):
        try:
            options["llm"].complete_json(system="synthetic", user="private")
        except httpx.ReadTimeout:
            pass  # Represents a normal pipeline helper's fallback.
        return SimpleNamespace(text="fallback response", usage=TokenUsage(), validation=None)

    monkeypatch.setattr(client, "complete_json", helper)
    monkeypatch.setattr(decision, "_prepare_group_speech", prepare)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, deadline=QQDeadline.after(30), trace=trace)
    assert result.outcome == "error_fallback" and result.error_type == "model_timeout"
    assert result.reply == decision.ERROR_FALLBACK_REPLY
    assert result.reply != "fallback response"
    assert trace.snapshot()["counters"]["failed_model_calls"] == 2
    assert trace.snapshot()["counters"]["model_retries"] == 1
    assert "PRIVATE_BODY" not in result.model_dump_json() + json.dumps(trace.snapshot())


@pytest.mark.parametrize("body_failure", [None, "invalid", "http"])
def test_recovered_static_parser_json_does_not_discard_valid_body(setup, monkeypatch, body_failure):
    monkeypatch.setenv("NC_QQ_BODY_PIPELINE", "legacy")
    monkeypatch.setenv("NC_VALIDATOR_ENABLED", "false")
    _, client, _, kwargs, _ = setup
    original_json = client.complete_json
    original_chat = client.complete_chat_result

    def helper(**options):
        if options["system"] == decision.parser_module._SYSTEM:
            raise ValueError("synthetic malformed parser JSON")
        return original_json(**options)

    def body(messages, **options):
        if not decision.is_qq_gate_messages(messages):
            if body_failure == "http":
                raise httpx.ConnectError("synthetic body transport failure")
            if body_failure == "invalid":
                return ChatResult(text='{"action":"speak","reason":"leaked decision"}')
            return ChatResult(text="在的，刚看到消息[END]")
        return original_chat(messages, **options)

    monkeypatch.setattr(client, "complete_json", helper)
    monkeypatch.setattr(client, "complete_chat_result", body)
    trace = QQTurnTrace()
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002", trace=trace)
    assert trace.snapshot()["counters"]["static_parse_recovered_invalid_json"] == 1
    assert not decision._STATIC_PARSE_CACHE
    if body_failure is None:
        assert result.outcome == "ready" and result.error_type is None
        assert result.reply_segments == ["在的，刚看到消息"]
    else:
        assert result.outcome == "error_fallback" and result.reply == decision.ERROR_FALLBACK_REPLY
        assert result.error_type == ("model_http" if body_failure == "http" else "model_invalid")


def test_static_parser_recovery_preserves_an_earlier_failure(setup):
    parser, _, _, llm, _ = parser_cache_case(setup)
    llm.failed_error_type = "model_http"

    def recovered(event):
        llm.failed_error_type = "model_invalid"
        return decision.parser_module.rule_parse(event)

    parser.delegate.parse = recovered
    parser.parse(static_event())
    assert llm.failed_error_type is not None
    assert not decision._STATIC_PARSE_CACHE
    assert not llm.trace.snapshot()["counters"].get("static_parse_recovered_invalid_json")


@pytest.mark.parametrize("failure,expected", [
    (httpx.ReadTimeout("private"), "model_timeout"),
    (RuntimeError("private"), "model_error"),
    ('{"action":"wait","reason":""}', "model_invalid"),
])
def test_model_failures_are_never_natural_wait(setup, failure, expected):
    _, client, _, kwargs, _ = setup
    client.choice = failure
    result = decision.decide_qq_reply(**kwargs)
    assert result.outcome == "error_fallback" and result.outcome != "natural_wait"
    assert result.error_type == expected and result.reply == decision.ERROR_FALLBACK_REPLY
    assert result.decision_source in {"model_error", "model_invalid"}
    assert result.model_call_counts["gate"] == (2 if isinstance(failure, httpx.ReadTimeout) else 1)


def test_verified_self_mention_has_exact_scoped_evidence_and_keeps_alias_user_side(setup):
    store, client, _, kwargs, _ = setup
    _, source = add_member(store, "20001", text="@Misty 你怎么看？", capture="direct")
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001", verified_self_aliases=[alias()])
    assert result.addressing == "direct" and result.addressing_source == "verified_self_mention"
    assert result.addressing_source_record_ids == [source.record_id]
    assert "test-evidence://verified-self" in result.addressing_evidence_refs
    assert result.automatic_send is False
    system = client.gate_messages[0][0]["content"]
    payload = decision.combined_user_payload(client.gate_messages[0])
    assert "test-evidence://verified-self" not in system
    assert payload["verified_self_aliases"][0]["alias"] == "Misty"


@pytest.mark.parametrize("text", ["@MistyOther 你怎么看？", "Misty你怎么看？", "@Misty2 你好"])
def test_unmentioned_or_prefix_matching_alias_remains_ambient(setup, text):
    store, _, _, kwargs, _ = setup
    add_member(store, "20001", text=text, capture="not-direct")
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001", verified_self_aliases=[alias()])
    assert result.addressing == "ambient" and not result.addressing_source_record_ids


def test_nickname_without_verified_self_evidence_cannot_be_direct(setup):
    store, _, _, kwargs, _ = setup
    add_member(store, "20001", text="@Misty 你怎么看？", capture="unverified-alias")
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001")
    assert result.addressing == "ambient"


def test_same_alias_known_other_person_is_ambiguous_not_identity_merge(setup):
    store, _, _, kwargs, _ = setup
    other_profile, _ = add_member(store, "20002", capture="same-alias")
    store.record_profile(QQVerifiedProfileInput.model_validate(
        other_profile.model_dump(exclude={"record_id", "stored_at"}) |
        {"capture_id": "same-alias-new", "display_name": "Misty"}))
    add_member(store, "20001", text="@Misty 你好", capture="ambiguous")
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20001", verified_self_aliases=[alias()])
    assert result.addressing == "ambient" and result.participants == ["20001", "20002"]


@pytest.mark.parametrize("updates", [{"group_id": "10002"}, {"self_member_id": "20001"}])
def test_other_group_or_account_alias_evidence_is_rejected_before_models(setup, updates):
    _, _, builds, kwargs, _ = setup
    with pytest.raises(decision.QQDraftError, match="exact group"):
        decision.decide_qq_reply(**kwargs, verified_self_aliases=[alias(**updates)])
    assert not builds


@pytest.mark.parametrize("valid_source,valid_quote", [(True, True), (False, True), (True, False)])
def test_gate_direct_followup_requires_current_attributed_source_and_quote(setup, valid_source, valid_quote):
    _, client, _, kwargs, records = setup
    client.choice = json.dumps({"action": "wait", "reason": "暂时听着。", "addressing": "direct",
        "addressing_source_record_id": records[1].record_id if valid_source else "qq-observation:invented",
        "addressing_supporting_quote": "你怎么看？" if valid_quote else "invented text"})
    result = decision.decide_qq_reply(**kwargs, trigger_member_id="20002")
    assert result.addressing == ("direct" if valid_source and valid_quote else "ambient")
    assert result.outcome == "natural_wait"  # Classification does not force speaking.
    if valid_source and valid_quote:
        assert result.addressing_source == "gate_direct_followup"
        assert result.addressing_source_record_ids == [records[1].record_id]


def test_old_record_default_is_not_counted_as_natural_wait(setup):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"没有必要开口。"}'
    result = decision.decide_qq_reply(**kwargs)
    raw = result.model_dump(mode="json")
    for field in ("outcome", "completed_at", "timings_ms", "model_call_counts", "trace_id", "error_type",
                  "addressing", "addressing_source", "addressing_source_record_ids", "addressing_evidence_refs"):
        raw.pop(field)
    old = decision.QQReplyDecision.model_validate(raw)
    assert old.outcome == "cached_legacy" and old.addressing_source == "legacy_unknown"
    assert old.completed_at is None and old.timings_ms == {}


def test_trace_flush_contains_no_group_text_names_quotes_or_persona(setup, tmp_path):
    _, _, _, kwargs, _ = setup
    trace = QQTurnTrace(event_id="event:test", first_seen_at=NOW)
    result = decision.decide_qq_reply(**kwargs, trace=trace, request="PRIVATE_OPERATOR_REQUEST")
    destination = tmp_path / "trace.jsonl"
    trace.flush(destination)
    raw = destination.read_text(encoding="utf-8")
    assert result.decision_id in raw and trace.trace_id in raw
    for private in ("UNTRUSTED", "PRIVATE_OPERATOR_REQUEST", "synthetic-private-api-key", result.reply):
        assert private not in raw


def test_extra_gate_fields_are_ignored_without_generating_body(setup, monkeypatch):
    _, client, _, kwargs, _ = setup
    client.choice = '{"action":"wait","reason":"skip","reply":"not allowed"}'
    monkeypatch.setattr(decision, "_prepare_group_speech", lambda **kwargs: pytest.fail("wait generated speech"))
    result = decision.decide_qq_reply(**kwargs)
    assert result.action == "wait" and result.outcome != "error_fallback"
