"""Private retained evidence stays outside the generic Runtime goal channel."""

from contextlib import closing
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from harness.qq.conversation import private_scope_id
from harness.qq.direct_capabilities import QQDirectReadArguments, QQDirectReadScope
from harness.qq.direct_config import DirectQQSettings
from harness.qq.direct_store import canonical, normalize_evidence
from harness.qq.memory import QQVerifiedProfileInput, QQVisualObservationInput, protocol_message_evidence_ref
from tests import test_qq_direct_capabilities as capabilities
from tests import test_qq_direct_source as sources


direct = sources.direct
bridge = capabilities.bridge
SELF, PEER, OTHER = sources.SELF, sources.MEMBER, "123456789"
PRIVATE = private_scope_id(SELF, PEER)


@pytest.fixture
def private_bridge(direct, bridge):
    instance, status = bridge
    original_offers = instance.catalog()
    settings = DirectQQSettings.model_validate(direct.settings.model_dump() | {
        "private_contacts": [{"peer_id": PEER, "label": "秋语随风", "observe": True, "send": True}]})
    direct.settings = settings
    direct.config.allowed_private_peer_ids = [PEER]
    instance.config_path.write_text(settings.model_dump_json(), encoding="utf-8")
    status["private_contacts"] = [contact.model_dump(mode="json") for contact in settings.private_contacts]
    settings.status_file.write_text(canonical(status), encoding="utf-8")
    return SimpleNamespace(instance=instance, status=status, original_offers=original_offers)


def private_source(direct, *, own=False, message_id=321, image=False):
    member = SELF if own else PEER
    raw = {"post_type": "message", "message_type": "private", "sub_type": "friend", "self_id": int(SELF),
        "user_id": int(member), "message_id": message_id, "time": int(direct.now.timestamp()),
        "sender": {"user_id": int(member), "nickname": "Misty" if own else "秋语随风"},
        "message": [{"type": "text", "data": {"text": "只属于私聊的原文"}}]}
    if image:
        raw["message"].append({"type": "image", "data": {"file": "private-image",
            "url": "https://multimedia.nt.qq.com.cn/private-test"}})
    evidence_payload = {"action": "get_friend_msg_history", "self_id": SELF, "peer_id": PEER, "result": raw} if own else raw
    event = normalize_evidence(evidence_payload, self_id=SELF, groups=set(), private_peers={PEER}, received_at=direct.now)
    ref = direct.store.evidence(evidence_payload)
    direct.store.receive(event, run_id=direct.run, evidence_ref=ref, historical=own)
    profile = direct.memory.record_protocol_profile(QQVerifiedProfileInput(group_id=PRIVATE, member_id=member,
        display_name=raw["sender"]["nickname"], display_name_source="qq_metadata", verification="qq_protocol_verified",
        capture_id=event["event_id"] + ":profile", observed_at=direct.now,
        group_evidence=[ref], member_evidence=[ref])).profile
    message_ref = protocol_message_evidence_ref(PRIVATE, member, str(message_id))
    pixel_ref = "qq-direct-image:sha256:" + "c" * 64
    image_analysis = [{"image_index": 1, "status": "described", "evidence_ref": pixel_ref,
        "description": "一只绿色小猫", "ocr_text": "秘密纸条", "uncertainties": []}] if image else []
    observation = direct.memory.record_protocol_observation(QQVisualObservationInput(group_id=PRIVATE, member_id=member,
        profile_record_id=profile.record_id, capture_id=event["event_id"], capture_item_id=event["event_id"],
        observed_at=datetime.fromisoformat(event["message_time"]), text=event["text"],
        evidence=[ref, message_ref] + ([pixel_ref] if image else []),
        author_attribution="qq_protocol_verified", attribution_evidence=[ref, message_ref],
        extraction_method="onebot11_message_array", message_id=str(message_id), message_id_source="qq_metadata",
        image_analysis=image_analysis)).observation
    direct.store.update(event["event_id"], state="bound", source_record_id=observation.record_id,
                        observation=observation.model_dump(mode="json"))
    return SimpleNamespace(event=event, observation=observation, evidence=evidence_payload, ref=ref)


def test_private_roster_does_not_change_group_offers_or_expose_private_capability(private_bridge):
    instance = private_bridge.instance
    assert instance.catalog() == private_bridge.original_offers
    assert {item["scope"]["group_id"] for item in instance.catalog()} == {sources.HOME, sources.GUEST}


@pytest.mark.parametrize("change", ["session_whitelist", "status_contacts", "wrong_peer"])
def test_private_configuration_must_match_running_actor_and_cached_session(direct, private_bridge, change):
    if change == "session_whitelist":
        direct.config.allowed_private_peer_ids = []
    else:
        status = private_bridge.status | {"private_contacts": [] if change == "status_contacts" else [
            {"peer_id": OTHER, "label": "秋语随风", "observe": True, "send": True}]}
        direct.settings.status_file.write_text(canonical(status), encoding="utf-8")
    assert private_bridge.instance.catalog() == []


def test_private_scope_requires_matching_self_and_distinct_peer():
    with pytest.raises(ValueError):
        QQDirectReadScope(group_id=PRIVATE, self_member_id=PEER)
    with pytest.raises(ValueError):
        QQDirectReadScope(group_id=f"private:{SELF}:{SELF}", self_member_id=SELF)
    scope = QQDirectReadScope(group_id=PRIVATE, self_member_id=SELF)
    assert scope.model_dump(mode="json") == {"transport": "onebot", "group_id": PRIVATE, "self_member_id": SELF}


def test_group_search_never_returns_private_text_or_image_analysis(direct, private_bridge):
    group = direct.create()
    private_source(direct, image=True)
    instance = private_bridge.instance
    found = instance.enqueue(capabilities.dispatch(instance, arguments={"query": "秘密纸条"}))
    assert found["receipt"]["messages"] == []
    ordinary = instance.enqueue(capabilities.dispatch(instance, opportunity_id="opportunity:ordinary"))
    assert [item["observation"]["record_id"] for item in ordinary["receipt"]["messages"]] == [group.observation.record_id]


def test_private_enqueue_and_cached_result_never_feed_generic_goal_review(direct, private_bridge):
    instance = private_bridge.instance
    source = private_source(direct)
    row = capabilities.dispatch(instance, scope={"transport": "onebot", "group_id": PRIVATE, "self_member_id": SELF})
    with pytest.raises(ValueError, match="private_audience_required"):
        instance.enqueue(row)
    # A historical cache row cannot bypass this boundary after code reload.
    with closing(instance._db()) as db, db:
        db.execute("INSERT INTO qq_direct_capability_results VALUES(?,?,?)", (row["opportunity_id"], canonical(row),
            canonical({"status": "completed", "receipt": {"messages": [source.observation.model_dump(mode="json")]}})))
    assert instance.result(row["opportunity_id"]) is None
    with pytest.raises(ValueError, match="private_audience_required"):
        instance.enqueue(row)


@pytest.mark.parametrize("own", [False, True])
def test_private_evidence_verification_accepts_only_exact_peer_binding(direct, private_bridge, own):
    source = private_source(direct, own=own, image=True)
    instance = private_bridge.instance
    assert instance._verify(direct.settings, source.observation) is None
    # The retained private reader is internal; there is no Runtime offer for it.
    result = instance._read(direct.settings, QQDirectReadScope(group_id=PRIVATE, self_member_id=SELF),
        QQDirectReadArguments(query="秘密纸条"), direct.now + timedelta(seconds=30))
    receipt = result["receipt"]
    assert receipt["conversation"] == {"kind": "private", "self_member_id": SELF, "peer_id": PEER}
    assert receipt["privacy"] == "private_conversation_only"
    retained = receipt["messages"][0]["observation"]
    assert retained["image_analysis"][0]["ocr_text"] == "秘密纸条"
    assert "秘密纸条" not in retained["text"]
    assert retained["member_id"] == (SELF if own else PEER)


@pytest.mark.parametrize("change", ["history_peer", "history_self", "unbound_own", "temp_session", "group_scope"])
def test_private_proof_cannot_be_retargeted_even_with_fresh_raw_hash(direct, private_bridge, change):
    source = private_source(direct, own=change in {"history_peer", "history_self", "unbound_own"})
    raw = json.loads(canonical(source.evidence))
    if change == "history_peer":
        raw["peer_id"] = OTHER
    elif change == "history_self":
        raw["self_id"] = PEER
    elif change == "unbound_own":
        raw = raw["result"]
    elif change == "temp_session":
        raw["sub_type"] = "group"
    else:
        raw.update(message_type="group", group_id=int(sources.HOME))
    new_ref = direct.store.evidence(raw)
    value = source.observation.model_dump(mode="json")
    for field in ("evidence", "attribution_evidence"):
        value[field] = [new_ref if ref == source.ref else ref for ref in value[field]]
    observation = type(source.observation).model_validate(value)
    sources.rewrite_event(direct, source.event["event_id"], event_evidence_ref=new_ref, bound_observation=value)
    with pytest.raises(ValueError):
        private_bridge.instance._verify(direct.settings, observation)


def test_revoked_private_peer_cannot_read_retained_records_internally(direct, private_bridge):
    source = private_source(direct)
    direct.config.allowed_private_peer_ids = []
    with pytest.raises(ValueError, match="scope_invalid"):
        private_bridge.instance._verify(direct.settings, source.observation)
