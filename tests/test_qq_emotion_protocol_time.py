"""Accepted OneBot clock skew keeps one immutable emotion source timestamp."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest

from harness.emotion import EmotionInput, EmotionService
from harness.llm.client import MockLLM
from harness.qq.conversation import conversation_audience
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_store import DirectStore, normalize_message
from harness.qq.emotion_window import build_qq_emotion_messages
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput, protocol_message_evidence_ref
from harness.qq.shared import QQSharedPrepareRequest
from harness.runtime.emotion import shared_observation_input
from eval.emotional_expression_diagnostics.emotion import offline_settings

SELF, PEER, GROUP = "29999", "20001", "10001"


def source(tmp_path, monkeypatch, *, private=False, skew=2):
    moment = datetime.now(timezone.utc)
    memory = QQVisualMemoryStore(tmp_path / "memory.db")
    store = DirectStore(tmp_path / "direct.db", tmp_path / "evidence")
    run = store.start_run(SELF)
    raw = {"post_type": "message", "message_type": "private" if private else "group", "sub_type": "friend",
        "self_id": int(SELF), "user_id": int(PEER), "message_id": 123,
        "time": (moment + timedelta(seconds=min(skew, 5))).timestamp(),
        "sender": {"user_id": int(PEER)}, "message": [{"type": "text", "data": {"text": "当前真实刺激"}}]}
    if not private:
        raw["group_id"] = int(GROUP)
    event = normalize_message(raw, self_id=SELF, groups={GROUP}, private_peers={PEER}, received_at=moment)
    # Exercise defense in depth for a too-far future row, even though ingress
    # already rejects such protocol timestamps before creating a source.
    if skew > 5:
        event["message_time"] = (moment + timedelta(seconds=skew)).isoformat()
    ref = store.evidence(raw)
    store.receive(event, run_id=run, evidence_ref=ref)
    group = event["group_id"]
    profile = memory.record_protocol_profile(QQVerifiedProfileInput(group_id=group, member_id=PEER,
        display_name="测试成员", display_name_source="qq_metadata", verification="qq_protocol_verified",
        capture_id=event["event_id"], observed_at=moment, group_evidence=[ref], member_evidence=[ref])).profile
    bound = protocol_message_evidence_ref(group, PEER, "123")
    observation = memory.record_protocol_observation(QQVisualObservationInput(group_id=group, member_id=PEER,
        profile_record_id=profile.record_id, capture_id=event["event_id"], capture_item_id=event["event_id"],
        observed_at=datetime.fromisoformat(event["message_time"]), text=event["text"], evidence=[ref, bound],
        author_attribution="qq_protocol_verified", attribution_evidence=[ref, bound],
        message_id="123", message_id_source="qq_metadata")).observation
    store.update(event["event_id"], state="bound", source_record_id=observation.record_id,
                  observation=observation.model_dump(mode="json"))
    paths = {key: tmp_path / key for key in ("token_file", "session_config_path", "status_file", "control_file",
        "image_cache_dir", "desktop_status_file", "shared_control_token_file")}
    paths["session_config_path"].write_text("{}")
    config = DirectQQSettings(enabled=True, self_member_id=SELF,
        group_roster=[{"group_id": GROUP, "observe": True, "send": True}],
        private_contacts=[{"peer_id": PEER, "observe": True, "send": True}],
        state_db=store.path, evidence_dir=store.evidence_dir, **paths)
    config_path = tmp_path / "direct.json"
    config_path.write_text(config.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(config_path))
    request = QQSharedPrepareRequest(transport="onebot", group_id=group, self_member_id=SELF,
        trigger_member_id=PEER, deadline_at=moment + timedelta(seconds=60), trace={
            "trace_id": "qq-trace:" + "1" * 32, "event_id": event["event_id"],
            "first_seen_at": moment, "first_seen_mono": 0})
    host = SimpleNamespace(chat=SimpleNamespace(memory=SimpleNamespace(get_persona=lambda: None)),
        shared=SimpleNamespace(qq_settings=SimpleNamespace(memory_db=memory.path, self_member_id=SELF,
            allowed_group_ids=[GROUP], allowed_private_peer_ids=[PEER], watcher_status_file=None)))
    command = SimpleNamespace(issued_at=moment + timedelta(seconds=.5), deadline=request.deadline_at,
        payload={"context_id": "first", "scope": conversation_audience(group),
                 "source_command_type": "PrepareQQResponse", "source_payload": request.model_dump(mode="json")})
    return host, command, memory, store, event, observation, moment


@pytest.mark.parametrize("private", [False, True])
def test_runtime_protocol_source_accepts_future_skew_and_later_window_is_stable(tmp_path, monkeypatch, private):
    host, command, memory, store, event, observation, received_at = source(tmp_path, monkeypatch, private=private)
    request = shared_observation_input(host, command)  # Includes real _source/DTO/config validation.
    assert request.new_source_ids == [observation.record_id]
    assert request.messages[0].timestamp == received_at
    assert observation.observed_at == received_at + timedelta(seconds=2)
    service = EmotionService(offline_settings("rule-fallback"), MockLLM())
    service.observe(request)
    for index, seconds in enumerate((1, 3), 1):
        scope, messages, new_ids = build_qq_emotion_messages(memory_db=memory.path, group_id=event["group_id"],
            self_member_id=SELF, as_of=received_at + timedelta(seconds=seconds), protocol_state_db=store.path)
        assert new_ids == [] and messages == request.messages
        service.observe(EmotionInput(context_id=f"later-{index}", scope=scope,
            as_of=received_at + timedelta(seconds=seconds), messages=messages, new_source_ids=[]))
        window = service.snapshot()["reservations"][f"later-{index}"]["window"]
        assert len(window) == 1 and window[0]["source_id"] == observation.record_id
        assert window[0]["timestamp"] == request.messages[0].model_dump(mode="json")["timestamp"]


@pytest.mark.parametrize("private", [False, True])
def test_protocol_skew_greater_than_five_seconds_still_rejected(tmp_path, monkeypatch, private):
    _, command, memory, store, event, _, _ = source(tmp_path, monkeypatch, private=private, skew=6)
    with pytest.raises(ValueError, match="protocol_clock_skew"):
        build_qq_emotion_messages(memory_db=memory.path, group_id=event["group_id"], self_member_id=SELF,
            as_of=command.issued_at, trigger_event_id=event["event_id"], trigger_member_id=PEER,
            protocol_state_db=store.path)


def test_protocol_receive_time_requires_matching_bound_observation(tmp_path, monkeypatch):
    _, command, memory, store, event, _, _ = source(tmp_path, monkeypatch)
    with store.connect() as db:
        row = db.execute("SELECT record_json FROM qq_direct_events WHERE event_id=?", (event["event_id"],)).fetchone()
        value = json.loads(row["record_json"])
        value["bound_observation"]["text"] = "被替换的文本"
        db.execute("UPDATE qq_direct_events SET record_json=? WHERE event_id=?", (json.dumps(value), event["event_id"]))
    with pytest.raises(ValueError, match="protocol_time_binding"):
        build_qq_emotion_messages(memory_db=memory.path, group_id=event["group_id"], self_member_id=SELF,
            as_of=command.issued_at, trigger_event_id=event["event_id"], trigger_member_id=PEER,
            protocol_state_db=store.path)


def test_affect_input_accepts_completed_replay_after_decision_is_written(tmp_path, monkeypatch):
    from harness.runtime.emotion import affect_observation_input
    from harness.runtime.qq_turns import _source

    host, command, memory, store, event, observation, moment = source(tmp_path, monkeypatch)
    store.update(event["event_id"], state="prepared", decision_id="qq-decision:done")
    settings = host.shared.qq_settings
    request = QQSharedPrepareRequest.model_validate(command.payload["source_payload"])
    with pytest.raises(ValueError, match="source_not_pending"):
        _source(settings, None, request)
    affect_command = SimpleNamespace(
        command_type="AppraiseAffect", issued_at=command.issued_at, deadline=command.deadline,
        payload={"context_id": "affect:replay", "scope": conversation_audience(event["group_id"]),
                 "group_id": event["group_id"], "self_member_id": SELF, "transport": "onebot",
                 "triggers": [{"event_id": event["event_id"], "member_id": PEER,
                               "trace_id": request.trace.trace_id,
                               "first_seen_at": request.trace.first_seen_at.isoformat(),
                               "first_seen_mono": request.trace.first_seen_mono,
                               "deadline_at": request.deadline_at.isoformat()}],
                 "homeostasis": {}})
    observed = affect_observation_input(host, affect_command)
    assert observed.new_source_ids == [observation.record_id]
    assert observed.isolated is True
