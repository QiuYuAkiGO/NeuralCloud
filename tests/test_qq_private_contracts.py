"""Private QQ shares the owner and image authority, never a group's scope."""

from datetime import timedelta
import json

import pytest

from harness.core.inference import TokenUsage
from harness.llm.client import ChatResult
from harness.qq import decision
from harness.qq.conversation import private_scope_id
from harness.qq.memory import QQPersonContext, QQVerifiedProfileInput, protocol_message_evidence_ref
from harness.qq.shared import QQSharedPrepareRequest
from tests.test_qq_decision import NOW, isolated_static_parser_cache as isolated_static_parser_cache, setup as setup
from tests.test_qq_protocol_memory import EVIDENCE, protocol_observation, protocol_profile


SELF = "29999"
PEER = "2285027817"
SCOPE = private_scope_id(SELF, PEER)


def prepare_private(setup, monkeypatch, *, choice="speak"):
    store, client, _, kwargs, _ = setup
    _, profile = protocol_profile(store, group=SCOPE, member=PEER)
    observation = store.record_protocol_observation(protocol_observation(
        profile, text="PRIVATE_CONVERSATION_SECRET 看看这个 [图片 1]")).observation
    delegate = client.complete_chat_result

    def complete(messages, **options):
        if messages[0]["content"].startswith(decision._PRIVATE_RULES):
            client.gate_messages.append(messages)
            return ChatResult(text=json.dumps({"action": choice, "reason": "这是一对一私聊。"}),
                              usage=TokenUsage(prompt_tokens=30, completion_tokens=10, source="api"))
        return delegate(messages, **options)

    monkeypatch.setattr(client, "complete_chat_result", complete)
    options = {**kwargs, "group_id": SCOPE, "trigger_member_id": PEER}
    return decision.decide_qq_reply(**options), observation, options


def test_private_scope_has_distinct_message_evidence_and_only_two_accounts():
    private = protocol_message_evidence_ref(SCOPE, PEER, "-123")
    assert private == f"qq-protocol-private-message:{SELF}:{PEER}:{PEER}:-123"
    assert private != protocol_message_evidence_ref(PEER, PEER, "-123")
    with pytest.raises(ValueError):
        protocol_message_evidence_ref(SCOPE, "77777", "-123")
    with pytest.raises(ValueError):
        QQPersonContext(group_id=SCOPE, member_id="77777", profiles=[], observations=[])
    with pytest.raises(ValueError):
        QQPersonContext(group_id=f"private:{SELF}:{SELF}", member_id=SELF, profiles=[], observations=[])


@pytest.mark.parametrize("updates", [
    {"member_id": "77777"},
    {"verification": "qq_ui_verified", "display_name_source": "qq_profile"},
])
def test_private_profiles_reject_group_participants_and_ui_authority(setup, updates):
    store = setup[0]
    incoming, _ = protocol_profile(store, group=SCOPE, member=PEER)
    with pytest.raises(ValueError):
        QQVerifiedProfileInput.model_validate({**incoming.model_dump(), **updates})


@pytest.mark.parametrize("updates", [
    {"author_attribution": "qq_ui_verified"},
    {"member_id": "77777"},
    {"attribution_evidence": [EVIDENCE, f"qq-protocol-message:{PEER}:{PEER}:-123"]},
])
def test_private_observations_need_private_protocol_authority(setup, updates):
    _, profile = protocol_profile(setup[0], group=SCOPE, member=PEER)
    with pytest.raises(ValueError):
        protocol_observation(profile, **updates)


def private_request(**updates):
    return QQSharedPrepareRequest.model_validate({
        "transport": "onebot", "group_id": SCOPE, "self_member_id": SELF, "trigger_member_id": PEER,
        "deadline_at": NOW + timedelta(seconds=60),
        "trace": {"trace_id": "qq-trace:" + "1" * 32, "event_id": "qq-watch-event:" + "2" * 32,
                  "first_seen_at": NOW, "first_seen_mono": 0}, **updates})


@pytest.mark.parametrize("updates", [
    {"transport": "desktop"}, {"self_member_id": "77777"},
    {"trigger_member_id": None}, {"trigger_member_id": SELF}, {"member_ids": [PEER, "77777"]},
])
def test_private_request_cannot_widen_transport_or_peer(updates):
    assert private_request().group_id == SCOPE
    with pytest.raises(ValueError):
        private_request(**updates)


def test_private_images_and_text_reach_same_persona_without_group_recall(setup, monkeypatch):
    result, observation, options = prepare_private(setup, monkeypatch)
    assert result.outcome == "ready", result.reason
    assert result.addressing == "direct" and result.addressing_source == "verified_private_message"
    assert result.addressing_source_record_ids == [observation.record_id]
    assert result.prompt_version == "qq-private-turn-taking-v1"
    assert result.participants == [PEER]
    assert result.source_record_ids == [observation.record_id]
    client = setup[1]
    for call in (client.gate_messages[0], client.chat_calls[0]):
        user = "\n".join(message["content"] for message in call if message["role"] == "user")
        system = "\n".join(message["content"] for message in call if message["role"] == "system")
        assert '"kind":"private"' in user and f'"peer_id":"{PEER}"' in user
        assert "PRIVATE_CONVERSATION_SECRET" in user and "MODEL_IMAGE_DESCRIPTION" in user
        assert "MODEL_IMAGE_OCR" in user and "MODEL_IMAGE_UNCERTAINTY" in user
        assert "PRIVATE_CONVERSATION_SECRET" not in system
        assert "UNTRUSTED_A" not in user and "WRONG_GROUP_SECRET" not in user
        assert "一对一私聊" in system and "不是作者原话" in system
    replay = decision.decide_qq_reply(**options)
    assert replay.replayed and replay.decision_id == result.decision_id
    assert len(client.gate_messages) == 1
    group = decision.decide_qq_reply(**setup[3])
    assert group.outcome == "ready"
    group_prompt = json.dumps(client.gate_messages[-1], ensure_ascii=False)
    assert "PRIVATE_CONVERSATION_SECRET" not in group_prompt
    assert "MODEL_IMAGE_DESCRIPTION" not in group_prompt


def test_private_direct_address_does_not_force_speaking(setup, monkeypatch):
    result, _, _ = prepare_private(setup, monkeypatch, choice="wait")
    assert result.addressing == "direct" and result.action == "wait"
    assert result.outcome == "natural_wait" and not setup[1].chat_calls


def test_private_delivery_receipt_is_scoped_and_not_a_group_send(setup, monkeypatch):
    result, observation, _ = prepare_private(setup, monkeypatch)
    options = dict(memory_db=setup[0].path, group_id=SCOPE, self_member_id=SELF,
                   decision_id=result.decision_id, delivered_text=result.reply_segments[0],
                   source_record_ids=[observation.record_id], delivered_at=NOW + timedelta(seconds=1),
                   evidence_refs=[EVIDENCE, protocol_message_evidence_ref(SCOPE, SELF, "999")])
    first = decision.record_qq_protocol_segment_delivery(**options, segment_index=0, protocol_message_id="999")
    receipt = decision.record_qq_protocol_delivery(**options, protocol_message_ids=["999"])
    assert receipt.segment_receipt_ids == [first.receipt_id]
    assert decision.get_qq_delivery(memory_db=setup[0].path, group_id=SCOPE, decision_id=result.decision_id) == receipt
    assert decision.get_qq_delivery(memory_db=setup[0].path, group_id=PEER, decision_id=result.decision_id) is None
    with pytest.raises(ValueError):
        decision.record_qq_delivery(**options, verification="qq_ui_verified")
    with pytest.raises(ValueError):
        decision.QQDeliveryReceipt.model_validate({**receipt.model_dump(), "group_id": PEER})
    with pytest.raises(ValueError):
        decision.QQDeliveryReceipt.model_validate({**receipt.model_dump(), "self_member_id": "77777"})


def test_group_dtos_do_not_gain_private_fields(setup):
    result = decision.decide_qq_reply(**setup[3])
    assert "conversation" not in result.model_dump()
    assert result.prompt_version == decision.PROMPT_VERSION
    assert "conversation" not in setup[0].group_context(group_id="10001").model_dump()
