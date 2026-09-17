"""Web planning reads an attributed small window without full cognitive recall."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from gui.presets import load_builtin
from harness.cognitive import web
from harness.contracts.capability_calls import WEB_READ
from harness.pipeline.session import SessionStore
from harness.qq.memory import QQVerifiedProfileInput, QQVisualMemoryStore, QQVisualObservationInput, protocol_message_evidence_ref
from tests.test_capability_cognition import FakeLLM
from tests.test_web_cognition import now, offer, request, sources


EVENT = "qq-watch-event:" + "a" * 32
IMAGE_REF = "qq-direct-image:sha256:" + "c" * 64


@pytest.fixture
def source(tmp_path, monkeypatch):
    from harness.runtime import qq_turns

    store = QQVisualMemoryStore(tmp_path / "memory.sqlite3")
    persona = load_builtin("misty").persona
    llm = FakeLLM({"kind": "finish"})
    sessions = SessionStore(window=20)
    owner = SimpleNamespace(
        chat=SimpleNamespace(memory=SimpleNamespace(get_persona=lambda: persona), llm=llm,
                             active_session_id="local", pipeline=SimpleNamespace(session_store=sessions)),
        shared=SimpleNamespace(qq_settings=SimpleNamespace(memory_db=store.path),
                               qq_binding=lambda **kwargs: pytest.fail("selector called full QQ recall"),
                               local_context=lambda *args: pytest.fail("selector called full local recall")),
    )
    checked = []
    monkeypatch.setattr(qq_turns, "_source", lambda *args: checked.append(args[-1].trace.event_id))
    return store, owner, checked


def observation(store, *, event=EVENT, group="10001", author="20001", text="请查证这个版本的最新规则",
                timestamp=None, images=None, capture=None):
    timestamp = timestamp or now() - timedelta(seconds=1)
    private = group.startswith("private:")
    proof = "qq-protocol://sha256/" + "d" * 64
    attribution = [proof, protocol_message_evidence_ref(group, author, "123")] if private else ["test://attribution"]
    profile_writer = store.record_protocol_profile if private else store.record_profile
    profile = profile_writer(QQVerifiedProfileInput(
        group_id=group, member_id=author, display_name="source author",
        display_name_source="qq_metadata" if private else "qq_profile",
        verification="qq_protocol_verified" if private else "qq_ui_verified",
        capture_id="profile:" + event, observed_at=timestamp,
        group_evidence=[proof] if private else ["test://group"],
        member_evidence=[proof] if private else ["test://author"],
    )).profile
    writer = store.record_protocol_observation if private else store.record_observation
    return writer(QQVisualObservationInput(
        group_id=group, member_id=author, profile_record_id=profile.record_id,
        capture_id=capture or "capture:" + event, capture_item_id=event, observed_at=timestamp, text=text,
        author_attribution="qq_protocol_verified" if private else "qq_ui_verified", attribution_evidence=attribution,
        evidence=[*attribution, *([IMAGE_REF] if images else [])], image_analysis=images or [],
        message_id="123" if private else None, message_id_source="qq_metadata" if private else None,
    )).observation


def qq_command(**updates):
    first_seen = now() - timedelta(seconds=2)
    payload = {"group_id": "10001", "self_member_id": "29999", "trigger_member_id": "20001",
        "deadline_at": (first_seen + timedelta(seconds=60)).isoformat(),
        "trace": {"trace_id": "qq-trace:" + "b" * 32, "event_id": EVENT,
                  "first_seen_at": first_seen.isoformat(), "first_seen_mono": 1}}
    payload.update(updates)
    return request(channel="qq", event_payload=payload)


def projected(owner, command):
    return web._context(owner, command, web.WebStepRequest.model_validate(command.payload))


def test_current_trigger_and_complete_image_ocr_survive_newer_other_speakers(source):
    store, owner, checked = source
    ocr = "图片原始文字" * 500 + "末尾也保留"
    current = observation(store, images=[{"image_index": 1, "status": "described", "evidence_ref": IMAGE_REF,
        "description": "完整图片说明", "ocr_text": ocr}])
    observation(store, event="later-person", author="20002", text="OTHER_PERSON_UNTRUSTED",
                timestamp=now() - timedelta(milliseconds=100))
    observation(store, event=EVENT, group="10002", text="WRONG_GROUP_SECRET")
    observation(store, event="future", text="FUTURE_MESSAGE", timestamp=now() + timedelta(minutes=1))
    command = qq_command()
    command = command.model_copy(update={"payload": {**command.payload, "runtime_web_context": sources(content="page text")}})

    assert asyncio.run(web.prepare(owner, command))["choice"]["kind"] == "finish"
    system, user = owner.chat.llm.calls[0][0]
    context = json.loads(user["content"])["context"]
    assert checked == [EVENT]
    assert context["current_request"]["source_id"] == current.record_id
    assert context["current_request"]["source_event_id"] == EVENT
    assert context["current_request"]["text"] == current.text
    assert context["current_request"]["image_analysis"][0]["ocr_text"] == ocr
    assert context["current_request"]["author_id"] == "20001"
    assert "OTHER_PERSON_UNTRUSTED" in user["content"] and "OTHER_PERSON_UNTRUSTED" not in system["content"]
    assert "WRONG_GROUP_SECRET" not in user["content"] and "FUTURE_MESSAGE" not in user["content"]
    assert "shared_memory" not in context and "group_context" not in context
    assert '"profile"' in system["content"] and '"speech_style"' in system["content"]
    assert json.loads(user["content"])["web_context"]["sources"][0]["url"] == "https://example.org/article"


@pytest.mark.parametrize("case", ["missing", "wrong_author", "ambiguous", "future"])
def test_missing_ambiguous_or_invalid_trigger_never_falls_back_to_latest(source, case):
    store, owner, checked = source
    observation(store, event="unrelated", author="20001", text="LATEST_MUST_NOT_REPLACE_TRIGGER")
    if case == "wrong_author":
        observation(store, author="20002")
    elif case == "ambiguous":
        observation(store, text="first")
        observation(store, text="second", timestamp=now() - timedelta(milliseconds=500), capture="different-capture")
    elif case == "future":
        observation(store, timestamp=now() + timedelta(seconds=2))
    with pytest.raises(web.WebSelectionError, match="web_qq_trigger_"):
        asyncio.run(web.prepare(owner, qq_command()))
    assert checked == [EVENT] and not owner.chat.llm.calls


def test_source_authority_still_checked_before_projection(source, monkeypatch):
    from harness.runtime import qq_turns

    _, owner, _ = source
    def denied(*args):
        raise ValueError("direct_qq_scope_invalid")
    monkeypatch.setattr(qq_turns, "_source", denied)
    monkeypatch.setattr(web, "_qq_selector_context", lambda *args, **kwargs: pytest.fail("read after source denial"))
    with pytest.raises(web.WebSelectionError, match="web_step_context_unavailable"):
        asyncio.run(web.prepare(owner, qq_command()))
    assert not owner.chat.llm.calls


def test_oversized_current_text_and_ocr_fail_without_partial_trigger_or_model(source):
    store, owner, _ = source
    current = observation(store, text="完整原文" * 8000,
        images=[{"image_index": index, "status": "described", "evidence_ref": IMAGE_REF,
                 "ocr_text": "完整的OCR" * 1000} for index in range(1, 7)])
    command = qq_command()
    context = projected(owner, command)
    before = deepcopy(context["current_request"])
    with pytest.raises(web.WebSelectionError, match="web_step_context_too_large"):
        web.fit_web_step_payload({"context": context})
    assert context["current_request"] == before
    assert before["text"] == current.text
    with pytest.raises(web.WebSelectionError, match="web_step_context_too_large"):
        asyncio.run(web.prepare(owner, command))
    assert not owner.chat.llm.calls


def test_ocr_and_two_web_sources_can_exceed_history_target_without_losing_current_input(source):
    store, owner, _ = source
    for index in range(2):
        observation(store, event=f"old-history-{index}", text="可省略的整条历史" * 500,
                    timestamp=now() - timedelta(minutes=3 - index))
    ocr = "完整的OCR" * 1000
    current = observation(store, images=[{"image_index": 1, "status": "described", "evidence_ref": IMAGE_REF,
                                        "ocr_text": ocr}])
    web_context = sources(content="资料正文" * 1000)
    second = deepcopy(web_context["sources"][0])
    second.update(source_ref="web-source:2", url="https://example.org/second", title="Second source")
    web_context["sources"].append(second)
    command = qq_command()
    command = command.model_copy(update={"payload": {**command.payload,
        "offers": [item.model_dump(mode="json") for item in [offer(), offer(WEB_READ)]],
        "runtime_web_context": web_context}})
    before = projected(owner, command)
    assert len(before["recent_dialogue"]) == 2
    assert asyncio.run(web.prepare(owner, command))["choice"]["kind"] == "finish"
    payload = json.loads(owner.chat.llm.calls[0][0][1]["content"])
    assert web.WEB_STEP_HISTORY_TARGET < web._payload_size(payload) <= web.WEB_STEP_PAYLOAD_LIMIT
    assert payload["context"]["recent_dialogue"] == []
    assert payload["context"]["history_omitted_count"] == before["history_omitted_count"] + 2
    assert payload["context"]["current_request"] == before["current_request"]
    assert payload["context"]["current_request"]["source_id"] == current.record_id
    assert payload["context"]["current_request"]["image_analysis"][0]["ocr_text"] == ocr
    assert payload["web_context"] == web.normalize_web_context(web_context)


def test_explicit_payload_limit_remains_a_hard_limit():
    current = {"source_id": EVENT, "text": "完整当前消息" * 2000}
    payload = {"context": {"current_request": current, "recent_dialogue": [{"text": "old history"}]}}
    before = deepcopy(current)
    with pytest.raises(web.WebSelectionError, match="web_step_context_too_large"):
        web.fit_web_step_payload(payload, limit=4000)
    assert current == before and payload["context"]["recent_dialogue"] == []
    assert payload["context"]["history_omitted_count"] == 1


def test_recent_history_is_small_ordered_and_whole_record_budgeted(source):
    store, owner, _ = source
    for index in range(30):
        observation(store, event=f"history-{index}", text=f"HISTORY_{index}_" + "整条记录" * 40,
                    timestamp=now() - timedelta(minutes=30 - index))
    current = observation(store)
    context = projected(owner, qq_command())
    history = context["recent_dialogue"]
    assert len(history) == web.RECENT_DIALOGUE_LIMIT
    assert history == sorted(history, key=lambda item: (item["timestamp"], item["source_id"]))
    assert context["history_omitted_count"] > 0 and context["history_candidates_limited"]
    assert context["current_request"]["source_id"] == current.record_id
    saved = deepcopy(history)
    original_omitted = context["history_omitted_count"]
    payload = {"context": context}
    fitted = web.fit_web_step_payload(payload, limit=web._payload_size(payload) - 1)
    assert fitted["context"]["recent_dialogue"] == saved[1:]
    assert fitted["context"]["history_omitted_count"] == original_omitted + 1
    assert fitted["context"]["current_request"]["text"] == current.text


def test_private_projection_reads_only_bound_peer_scope(tmp_path, monkeypatch):
    from tests.test_qq_emotion_protocol_time import source as protocol_source

    owner, emotion_command, store, _, event, trigger, received_at = protocol_source(tmp_path, monkeypatch, private=True)
    owner.chat.memory.get_persona = lambda: load_builtin("misty").persona
    owner.shared.qq_binding = lambda **kwargs: pytest.fail("full recall must not run")
    group = event["group_id"]
    observation(store, group="private:29999:20002", author="20002", text="OTHER_PRIVATE_SECRET")
    observation(store, group="10001", text="GROUP_SECRET")
    command = request(channel="qq", event_payload=emotion_command.payload["source_payload"])
    command = command.model_copy(update={"issued_at": received_at + timedelta(seconds=.5)})
    context = projected(owner, command)
    assert context["conversation"] == {"kind": "private", "self_member_id": "29999", "peer_id": "20001"}
    assert context["current_request"]["group_id"] == group
    assert context["current_request"]["source_id"] == trigger.record_id
    assert context["current_request"]["timestamp"] == received_at.isoformat()
    assert "OTHER_PRIVATE_SECRET" not in json.dumps(context) and "GROUP_SECRET" not in json.dumps(context)


def test_local_selector_uses_current_session_and_fresh_persona_without_recall(source):
    _, owner, _ = source
    sessions = owner.chat.pipeline.session_store
    for index in range(10):
        sessions.append("chosen-session", "user", f"SAME_SESSION_{index}", event_id=f"local-{index}")
    sessions.append("elsewhere", "user", "OTHER_SESSION_SECRET")
    command = request()
    before = sessions.snapshot()
    first = projected(owner, command)
    assert first["current_request"]["text"] == command.payload["event_payload"]["text"]
    assert len(first["recent_dialogue"]) == 6 and first["history_omitted_count"] == 4
    assert "OTHER_SESSION_SECRET" not in json.dumps(first)
    assert sessions.snapshot() == before
    persona = owner.chat.memory.get_persona().model_copy(deep=True)
    persona.identity.name = "即时更新的人格名"
    owner.chat.memory.get_persona = lambda: persona
    second = projected(owner, command)
    assert second["persona"]["identity"]["name"] == "即时更新的人格名"
