"""Durable gate/body split, with no network or repeated model decisions."""
from datetime import datetime, timedelta, timezone
import json
import sqlite3

import pytest

from harness.qq import decision
from harness.qq.telemetry import QQDeadline, QQTurnTrace
from harness.qq.turn_checkpoints import QQTurnCheckpointStore, QQTurnStateError
from tests.test_qq_decision import add_member, setup as setup


@pytest.fixture(autouse=True)
def deterministic_prepared_body_calls(monkeypatch, setup):
    # Validator on/off behavior has separate pipeline tests. This suite counts
    # durable body reservations, not the validator's configured random sample.
    monkeypatch.setenv("NC_VALIDATOR_ENABLED", "false")
    config = setup[3]["config_path"]
    config.write_text(json.dumps({**json.loads(config.read_text()), "validator_enabled": False}), encoding="utf-8")


def _binding(**updates):
    return {"event_id": "qq-watch-event:" + "a" * 32, "transport": "desktop", "group_id": "10001",
        "self_member_id": "29999", "trigger_member_id": "20002", "runtime_instance_id": "runtime:test",
        "manifest_revision": "manifest:test", "source_digest": "1" * 64,
        "deadline_at": (datetime.now(timezone.utc) + timedelta(seconds=60)).isoformat(), **updates}


def _prepare(setup, *, choice=None, web_enabled=True):
    _, client, _, kwargs, _ = setup
    if choice is not None:
        client.choice = json.dumps(choice)
    binding = _binding()
    prepared = decision.prepare_qq_turn(**kwargs, trigger_member_id="20002", turn_binding=binding,
        web_capabilities={"enabled": web_enabled}, deadline=QQDeadline.after(60), trace=QQTurnTrace())
    return binding, prepared


def _resume(setup, binding, prepared, **overrides):
    kwargs = setup[3]
    return decision.resume_qq_response(memory_db=kwargs["memory_db"], config_path=kwargs["config_path"],
        preset_path=kwargs["preset_path"], prepare_id=prepared["prepare_id"], snapshot_hash=prepared["snapshot_hash"],
        turn_binding=binding, deadline=QQDeadline.after(60), **overrides)


def test_changed_common_prompt_version_does_not_resume_frozen_gate(setup, monkeypatch):
    binding, prepared = _prepare(setup)
    monkeypatch.setenv("NC_PROMPT_VERSION", "changed-after-prepare")
    with pytest.raises(QQTurnStateError, match="configuration_changed"):
        _resume(setup, binding, prepared)
    assert not setup[1].chat_calls


@pytest.mark.parametrize("phase", ["before_gate", "after_gate", "before_body", "after_body"])
def test_request_revision_is_rechecked_before_calls_and_frozen_completion(setup, monkeypatch, phase):
    store, client, _, kwargs, _ = setup
    invalid = phase == "before_gate"
    original = client.complete_chat_result

    def validate_request():
        if invalid:
            raise QQTurnStateError("qq_turn_request_changed")

    def response(messages, **options):
        nonlocal invalid
        result = original(messages, **options)
        is_gate = decision.is_qq_gate_messages(messages)
        if (phase == "after_gate" and is_gate) or (phase == "after_body" and not is_gate):
            invalid = True
        return result

    monkeypatch.setattr(client, "complete_chat_result", response)
    binding = _binding()
    if phase.endswith("gate"):
        with pytest.raises(QQTurnStateError, match="request_changed"):
            decision.prepare_qq_turn(**kwargs, trigger_member_id="20002", turn_binding=binding,
                request_validator=validate_request, deadline=QQDeadline.after(60))
        assert len(client.gate_messages) == int(phase == "after_gate")
    else:
        prepared = decision.prepare_qq_turn(**kwargs, trigger_member_id="20002", turn_binding=binding,
            request_validator=validate_request, deadline=QQDeadline.after(60))
        invalid = phase == "before_body"
        with pytest.raises(QQTurnStateError, match="request_changed"):
            _resume(setup, binding, prepared, request_validator=validate_request)
        assert len(client.gate_messages) == 1
    assert len(client.chat_calls) == int(phase == "after_body")
    checkpoint = QQTurnCheckpointStore(store.path).get(binding)
    assert checkpoint["status"] == "cancelled"
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT count(*) FROM qq_reply_decisions WHERE status='completed'").fetchone()[0] == 0


def test_prepared_gate_is_not_wait_and_resumes_exact_prefix_once(setup, monkeypatch):
    # This contract counts one body request. Optional post-validation has its
    # own physical call and is configured separately from body generation.
    monkeypatch.setenv("NC_VALIDATOR_ENABLED", "false")
    store, client, _, kwargs, _ = setup
    binding, prepared = _prepare(setup)
    assert prepared["kind"] == "prepared"
    assert len(client.gate_messages) == 1 and not client.chat_calls
    row = decision.get_qq_turn_checkpoint(memory_db=store.path, prepare_id=prepared["prepare_id"])
    assert row["status"] == "prepared"
    assert row["snapshot"]["messages"] == client.gate_messages[0]
    assert row["snapshot"]["raw_gate"] == client.choice
    assert row["snapshot"]["policy_version"] == prepared["policy_version"]
    with sqlite3.connect(store.path) as db:
        assert db.execute("SELECT status FROM qq_reply_decisions WHERE decision_id=?", (prepared["decision_id"],)).fetchone()[0] == "pending"
    again = decision.prepare_qq_turn(**kwargs, trigger_member_id="20002", turn_binding=binding)
    assert again == prepared and len(client.gate_messages) == 1
    first = _resume(setup, binding, prepared)
    assert first.outcome == "ready" and first.model_call_counts == {"gate": 1, "persona": 1}
    assert client.chat_calls[0][:len(row["snapshot"]["messages"])] == row["snapshot"]["messages"]
    assert client.chat_calls[0][len(row["snapshot"]["messages"])] == {"role": "assistant", "content": client.choice}
    replay = _resume(setup, binding, prepared)
    assert replay.decision_id == first.decision_id and replay.replayed
    assert len(client.gate_messages) == 1 and len(client.chat_calls) == 1


def test_prepared_legacy_body_recovers_static_parser_json(setup, monkeypatch):
    monkeypatch.setenv("NC_QQ_BODY_PIPELINE", "legacy")
    client = setup[1]
    original = client.complete_json

    def helper(**options):
        if options["system"] == decision.parser_module._SYSTEM:
            raise ValueError("synthetic malformed parser JSON")
        return original(**options)

    monkeypatch.setattr(client, "complete_json", helper)
    binding, prepared = _prepare(setup)
    trace = QQTurnTrace()
    result = _resume(setup, binding, prepared, trace=trace)
    assert result.outcome == "ready" and result.error_type is None
    assert result.reply and result.reply != decision.ERROR_FALLBACK_REPLY
    assert trace.snapshot()["counters"]["static_parse_recovered_invalid_json"] == 1
    replay = _resume(setup, binding, prepared)
    assert replay.replayed and replay.reply == result.reply


def test_wait_never_prepares_web_or_body(setup):
    _, client, _, _, _ = setup
    _, prepared = _prepare(setup, choice={"action": "wait", "reason": "他们正在互相说话。",
        "web_plan": {"steps": [{"step_id": "q", "action": "search", "query": "应当忽略"}]}})
    assert prepared["kind"] == "completed" and prepared["decision"]["outcome"] == "natural_wait"
    assert "web_plan" not in prepared and not client.chat_calls


@pytest.mark.parametrize("invalid_plan", [False, True])
def test_web_failure_or_invalid_plan_still_resumes_speech(setup, invalid_plan):
    plan = {"steps": [{"step_id": "q", "action": "search", "query": "最新版本"}]}
    if invalid_plan:
        plan = {"steps": [{"action": "read", "url": "https://invented.invalid"}]}
    binding, prepared = _prepare(setup, choice={"action": "speak", "reason": "回应明确问题。", "web_plan": plan})
    assert prepared["kind"] == "prepared"
    assert prepared["web_plan_invalid"] is invalid_plan
    result = _resume(setup, binding, prepared, runtime_web_context={"status": "unavailable", "sources": [],
        "evidence_refs": [], "failures": [{"code": "web_timeout"}]})
    assert result.outcome == "ready"
    assert "web_timeout" in "".join(item["content"] for item in setup[1].chat_calls[0] if item["role"] == "user")
    assert len(setup[1].gate_messages) == 1 and len(setup[1].chat_calls) == 1


def test_changed_source_cancels_but_unrelated_append_does_not(setup):
    store, client, _, _, records = setup
    binding, prepared = _prepare(setup)
    add_member(store, "20003", capture="unrelated-new", text="后来的另一位成员说话")
    record = records[0].model_dump(mode="json")
    record["text"] += "已经更正"
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?", (json.dumps(record), records[0].record_id))
    with pytest.raises(QQTurnStateError, match="source_changed"):
        _resume(setup, binding, prepared)
    assert not client.chat_calls
    assert decision.get_qq_turn_checkpoint(memory_db=store.path, prepare_id=prepared["prepare_id"])["status"] == "cancelled"


def test_unrelated_append_preserves_snapshot_and_no_new_gate(setup):
    store, client, _, _, _ = setup
    binding, prepared = _prepare(setup)
    add_member(store, "20003", capture="unrelated-new", text="FRESH_OTHER_MEMBER")
    result = _resume(setup, binding, prepared)
    assert result.outcome == "ready" and len(client.gate_messages) == 1
    assert "FRESH_OTHER_MEMBER" not in str(client.chat_calls[0])


def test_original_deadline_and_scope_cannot_be_renewed(setup):
    binding, prepared = _prepare(setup)
    for changed in ({"deadline_at": (datetime.now(timezone.utc) + timedelta(minutes=2)).isoformat()},
                    {"group_id": "10002"}, {"runtime_instance_id": "runtime:replacement"}):
        with pytest.raises(QQTurnStateError, match="binding_conflict"):
            _resume(setup, {**binding, **changed}, prepared)
    with pytest.raises(QQTurnStateError, match="snapshot_conflict"):
        _resume(setup, binding, {**prepared, "snapshot_hash": "0" * 64})
    assert not setup[1].chat_calls


def test_uncertain_body_reservation_is_not_repeated_or_called_natural_wait(setup):
    binding, prepared = _prepare(setup)
    with sqlite3.connect(setup[0].path) as db:
        db.execute("UPDATE qq_turn_checkpoints SET status='persona_running' WHERE prepare_id=?", (prepared["prepare_id"],))
    with pytest.raises(QQTurnStateError, match="in_progress_or_interrupted"):
        _resume(setup, binding, prepared)
    assert len(setup[1].gate_messages) == 1 and not setup[1].chat_calls


def test_checkpoint_uses_full_wal_durability(setup):
    binding, prepared = _prepare(setup)
    store = QQTurnCheckpointStore(setup[0].path)
    db = store._connect()
    try:
        assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert db.execute("PRAGMA synchronous").fetchone()[0] == 2
    finally:
        db.close()
    assert store.get(binding)["snapshot_hash"] == prepared["snapshot_hash"]


def test_anchor_and_supplement_can_have_different_verified_authors(setup):
    store, client, _, kwargs, records = setup
    with sqlite3.connect(store.path) as db:
        for index, item in enumerate(records):
            value = item.model_dump(mode="json")
            value["capture_item_id"] = f"verified-event-{index}"
            db.execute("UPDATE qq_visual_observations SET capture_item_id=?,record_json=? WHERE record_id=?",
                       (value["capture_item_id"], json.dumps(value), item.record_id))
    binding = _binding(source_event_ids=["verified-event-1", "verified-event-0"])
    prepared = decision.prepare_qq_turn(**kwargs, trigger_member_id="20002", turn_binding=binding,
        _request_source_event_ids=binding["source_event_ids"])
    assert prepared["kind"] == "prepared"
    payload = decision.combined_user_payload(client.gate_messages[0])
    assert set(payload["current_request_observation_ids"]) == {item.record_id for item in records}
    assert payload["effective_query"] == records[1].text
    assert {member["member_id"] for member in payload["group_context"]["members"]} == {"20001", "20002"}


@pytest.mark.parametrize("mutate_artifact", [False, True])
def test_old_image_row_normalizes_new_optional_field_but_real_ref_change_cancels(setup, mutate_artifact):
    store, _, _, _, records = setup
    value = records[0].model_dump(mode="json")
    image_ref = "qq-direct-image:sha256:" + "a" * 64
    value["evidence"].append(image_ref)
    value["image_analysis"] = [{"authority": "model_inference", "image_index": 1, "status": "described",
        "evidence_ref": image_ref, "description": "原来的图片说明", "ocr_text": "原始OCR",
        "uncertainties": [], "error": None}]
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?",
                   (json.dumps(value), records[0].record_id))
    binding, prepared = _prepare(setup)
    if mutate_artifact:
        value["image_analysis"][0]["analysis_ref"] = "qq-image-analysis:sha256:" + "b" * 64
        with sqlite3.connect(store.path) as db:
            db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?",
                       (json.dumps(value), records[0].record_id))
        with pytest.raises(QQTurnStateError, match="source_changed"):
            _resume(setup, binding, prepared)
    else:
        assert _resume(setup, binding, prepared).outcome == "ready"


@pytest.mark.parametrize("status", ["cancelled", "expired", "interrupted"])
def test_late_body_cannot_revive_terminal_checkpoint_or_complete_decision(setup, status):
    binding, prepared = _prepare(setup)
    store = QQTurnCheckpointStore(setup[0].path)
    frozen = store.load(prepared["prepare_id"])["snapshot"]["decision"]
    store.claim_resume(binding, prepared["snapshot_hash"], "resume")
    store.terminate(binding, status)
    with pytest.raises(QQTurnStateError, match="qq_turn_" + status):
        store.complete(binding, frozen)
    assert store.load(prepared["prepare_id"])["status"] == status
    with sqlite3.connect(setup[0].path) as db:
        assert db.execute("SELECT status FROM qq_reply_decisions WHERE decision_id=?",
                          (frozen["decision_id"],)).fetchone()[0] == "pending"


def test_completed_atomic_claim_replays_without_second_body(setup, monkeypatch):
    binding, prepared = _prepare(setup)
    original_claim = QQTurnCheckpointStore.claim_resume

    def completing_owner(store, binding, snapshot_hash, resume_hash):
        first = original_claim(store, binding, snapshot_hash, resume_hash)
        value = first["snapshot"]["decision"]
        value.update(action="speak", outcome="ready", reply="已由另一个调用完成。", reply_segments=["已由另一个调用完成。"])
        store.complete(binding, value)
        return original_claim(store, binding, snapshot_hash, resume_hash)

    monkeypatch.setattr(QQTurnCheckpointStore, "claim_resume", completing_owner)
    result = _resume(setup, binding, prepared)
    assert result.replayed and result.outcome == "ready"
    assert not setup[1].chat_calls


def test_active_authority_must_be_the_exact_verified_request_anchor(setup):
    with pytest.raises(decision.QQDraftError, match="qq_current_authority_invalid"):
        decision.decide_qq_reply(**setup[3], trigger_member_id="20002",
            _request_authority_record_id=setup[4][0].record_id)
    assert not setup[1].gate_messages
