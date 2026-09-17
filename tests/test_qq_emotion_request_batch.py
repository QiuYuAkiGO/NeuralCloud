from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.emotion import EmotionInput, EmotionService
from harness.qq.direct_store import normalize_message
from harness.qq.emotion_window import build_qq_emotion_messages
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput, protocol_message_evidence_ref
from harness.runtime.emotion import shared_observation_input
from tests.test_emotion_service import FakeAux, settings
from tests.test_qq_emotion_protocol_time import source, SELF, PEER, GROUP
from tests.test_qq_emotion_window import add_observation, NOW


def test_revision_appraises_only_new_supplement_and_gets_new_guidance(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "memory.db")
    anchor = add_observation(store, event="anchor", text="刚才的话令人难过。")
    base = dict(memory_db=store.path, group_id=GROUP, self_member_id=SELF,
                trigger_event_id="anchor", trigger_member_id=PEER)
    scope, messages, ids = build_qq_emotion_messages(**base, as_of=NOW, required_event_ids=["anchor"])
    llm = FakeAux()
    service = EmotionService(settings(), llm)
    first = EmotionInput(context_id="request-revision-1", scope=scope, as_of=NOW, messages=messages, new_source_ids=ids)
    frame = service.observe(first)
    old_guidance = service.guidance(frame)
    supplement = add_observation(store, event="supplement", at=NOW + timedelta(seconds=1), text="而且并不是开玩笑。")
    scope, messages, ids = build_qq_emotion_messages(**base, as_of=NOW + timedelta(seconds=1),
                                                    required_event_ids=["anchor", "supplement"])
    assert ids == [anchor.record_id, supplement.record_id]
    revised = EmotionInput(context_id="request-revision-2", scope=scope, as_of=NOW + timedelta(seconds=1),
                           messages=messages, new_source_ids=ids)
    new_frame = service.observe(revised)
    new_guidance = service.guidance(new_frame)
    assert [call[1]["new_source_ids"] for call in llm.calls if "EMOTION_APPRAISAL" in call[0]] == [
        [anchor.record_id], [supplement.record_id]]
    assert new_frame.revision == frame.revision + 1
    assert new_guidance.context_id != old_guidance.context_id
    assert len(llm.calls) == 4
    service.observe(revised)
    service.guidance(new_frame)
    assert len(llm.calls) == 4


def test_all_current_sources_displace_old_history_without_exceeding_window(tmp_path):
    store = QQVisualMemoryStore(tmp_path / "memory.db")
    anchor = add_observation(store, event="anchor")
    second = add_observation(store, event="supplement", member="20002", at=NOW + timedelta(seconds=1))
    for index in range(85):
        add_observation(store, event=f"history-{index}", member="20003", at=NOW + timedelta(seconds=2 + index))
    _, messages, ids = build_qq_emotion_messages(memory_db=store.path, group_id=GROUP, self_member_id=SELF,
        trigger_event_id="anchor", trigger_member_id=PEER, as_of=NOW + timedelta(seconds=90),
        required_event_ids=["anchor", "supplement"])
    assert len(messages) == 20
    assert ids == [anchor.record_id, second.record_id]
    assert set(ids) <= {item.source_id for item in messages}
    assert {item.speaker for item in messages if item.source_id in ids} == {PEER, "20002"}


@pytest.mark.parametrize("events", [["anchor", "missing"], ["anchor", "self"], ["anchor", "foreign"],
                                    ["anchor", "anchor"], ["anchor", *[f"extra-{i}" for i in range(20)]]])
def test_invalid_batch_never_invents_or_silently_drops_current_sources(tmp_path, events):
    store = QQVisualMemoryStore(tmp_path / "memory.db")
    add_observation(store, event="anchor")
    add_observation(store, event="self", member=SELF)
    add_observation(store, event="foreign", group="10002")
    with pytest.raises(ValueError):
        build_qq_emotion_messages(memory_db=store.path, group_id=GROUP, self_member_id=SELF,
            trigger_event_id="anchor", trigger_member_id=PEER, as_of=NOW, required_event_ids=events)


def test_shared_owner_verifies_batch_revision_and_protocol_supplement(tmp_path, monkeypatch):
    host, command, memory, direct, event, anchor, moment = source(tmp_path, monkeypatch, skew=0)
    first = direct.get(event["event_id"])["event"]
    direct.reply_requests.ingest(first, addressing="ambient")
    when = moment + timedelta(seconds=.1)
    raw = {"post_type": "message", "message_type": "group", "self_id": int(SELF), "user_id": int(PEER),
           "message_id": 124, "time": when.timestamp(), "group_id": int(GROUP),
           "sender": {"user_id": int(PEER)}, "message": [{"type": "text", "data": {"text": "补充当前请求"}}]}
    second = normalize_message(raw, self_id=SELF, groups={GROUP}, private_peers=set(), received_at=when)
    ref = direct.evidence(raw)
    direct.receive(second, run_id=first["run_id"], evidence_ref=ref)
    profile = memory.record_protocol_profile(QQVerifiedProfileInput(group_id=GROUP, member_id=PEER,
        display_name="测试成员", display_name_source="qq_metadata", verification="qq_protocol_verified",
        capture_id=second["event_id"], observed_at=when, group_evidence=[ref], member_evidence=[ref])).profile
    bound = protocol_message_evidence_ref(GROUP, PEER, "124")
    observation = memory.record_protocol_observation(QQVisualObservationInput(group_id=GROUP, member_id=PEER,
        profile_record_id=profile.record_id, capture_id=second["event_id"], capture_item_id=second["event_id"],
        observed_at=datetime.fromisoformat(second["message_time"]), text=second["text"], evidence=[ref, bound],
        author_attribution="qq_protocol_verified", attribution_evidence=[ref, bound],
        message_id="124", message_id_source="qq_metadata")).observation
    direct.update(second["event_id"], state="bound", source_record_id=observation.record_id,
                  observation=observation.model_dump(mode="json"))
    batch = direct.reply_requests.ingest(direct.get(second["event_id"])["event"], addressing="ambient")
    direct.update(first["event_id"], state="preparing")
    payload = dict(command.payload)
    payload["source_payload"] = {**payload["source_payload"], **{key: batch[key] for key in
        ("request_id", "revision", "source_event_ids", "authority_anchor")}}
    request = shared_observation_input(SimpleNamespace(**host.__dict__), SimpleNamespace(
        issued_at=command.issued_at, deadline=command.deadline, payload=payload))
    assert request.new_source_ids == [anchor.record_id, observation.record_id]
    payload["source_payload"]["revision"] -= 1
    with pytest.raises(ValueError):
        shared_observation_input(host, SimpleNamespace(issued_at=command.issued_at, deadline=command.deadline, payload=payload))
