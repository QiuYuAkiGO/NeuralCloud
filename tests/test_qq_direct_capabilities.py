"""Protocol roster read/search offers preserve shared ownership and evidence."""

from contextlib import closing
from datetime import timedelta
import json
from types import SimpleNamespace

import pytest

from harness.cognitive.capabilities import _bound_choice
from harness.contracts.capability_calls import CapabilityChoice, CapabilityOffer
from harness.qq.direct_capabilities import QQCapabilityTransports, QQDirectCapabilityBridge
from harness.qq.direct_store import canonical
from tests import test_qq_direct_source as fixtures


direct = fixtures.direct


@pytest.fixture
def bridge(direct, tmp_path):
    supervisor = SimpleNamespace(capabilities=SimpleNamespace(owner=lambda _: "shared", generation=2))
    instance = QQDirectCapabilityBridge(supervisor, direct.config, tmp_path / "direct.json", tmp_path / "caps.sqlite3",
                                        clock=lambda: direct.now)
    status = {"service": "qq-direct-onebot-v1", "running": True, "connected": True, "heartbeat_at": direct.now.isoformat(), "run_id": direct.run,
        "self_member_id": fixtures.SELF, "group_roster": [group.model_dump(mode="json") for group in direct.settings.group_roster],
        "paused": False, "desktop_owner_active": False, "observation_gap": False}
    direct.settings.status_file.write_text(canonical(status), encoding="utf-8")
    return instance, status


def dispatch(bridge, group_id=fixtures.HOME, **updates):
    offer = next(item for item in bridge.catalog() if item["scope"]["group_id"] == group_id)
    return {"opportunity_id": "opportunity:read", "generation": 2, "capability_id": offer["capability_id"],
        "scope": offer["scope"], "body_instance_id": offer["body_instance_id"], "offer_id": offer["offer_id"],
        "selection_ref": "capability-choice:1", "activation_revision": offer["activation_revision"],
        "deadline_at": (bridge.clock() + timedelta(seconds=30)).isoformat(), "arguments": {}, **updates}


def test_both_roster_groups_have_only_read_offers_with_protocol_scopes(bridge):
    instance, _ = bridge
    offers = [CapabilityOffer.model_validate(item) for item in instance.catalog()]
    assert {item.scope["group_id"] for item in offers} == {fixtures.HOME, fixtures.GUEST}
    assert all(item.capability_id == "communication.qq.read.v1" and item.available and item.ready for item in offers)
    assert all(item.provider_id == "qq-onebot" and "window_id" not in item.scope for item in offers)
    assert all(item.observation["current_live_observation"] is False for item in offers)


@pytest.mark.parametrize("updates", [{"running": False}, {"connected": False}, {"paused": True},
    {"desktop_owner_active": True}, {"observation_gap": True}, {"self_member_id": "999"},
    {"run_id": "old-run"}, {"group_roster": []}, {"heartbeat_at": "2020-01-01T00:00:00+00:00"}])
def test_unhealthy_stale_or_wrong_owner_status_never_claims_available(direct, bridge, updates):
    instance, status = bridge
    direct.settings.status_file.write_text(canonical(status | updates), encoding="utf-8")
    assert instance.catalog() == []


def test_read_and_search_return_exact_retained_source_without_send_or_new_capture(direct, bridge):
    home = direct.create()
    guest = direct.create(group=fixtures.GUEST, message_id=124)
    instance, _ = bridge
    result = instance.enqueue(dispatch(instance))
    receipt = result["receipt"]
    assert result["status"] == "completed" and receipt["kind"] == "retained_protocol_memory_read"
    assert not receipt["current_live_observation"] and receipt["messages"][0]["observation"]["text"] == home.event["text"]
    assert all(item["observation"]["group_id"] == fixtures.HOME for item in receipt["messages"])
    assert receipt["last_received_at"] == home.event["received_at"]
    assert instance.enqueue(dispatch(instance)) == result
    assert instance.result("opportunity:read") == result
    query = dispatch(instance, fixtures.GUEST, opportunity_id="opportunity:search", arguments={"query": "真实原文", "limit": 1})
    searched = instance.enqueue(query)
    assert searched["receipt"]["messages"][0]["observation"]["record_id"] == guest.observation.record_id
    assert not direct.store.counts()["sends"]


@pytest.mark.parametrize("artifact", ["absent", "same", "changed"])
def test_search_can_find_image_interpretation_but_keeps_it_separate_from_author_text(direct, bridge, artifact):
    source = direct.create()
    ref = "qq-direct-image:sha256:" + "c" * 64
    value = source.observation.model_dump(mode="json")
    value["evidence"].append(ref)
    value["image_analysis"] = [{"authority": "model_inference", "image_index": 1, "status": "described",
        "evidence_ref": ref, "description": "绿色小猫图案", "ocr_text": "HELLO", "uncertainties": [], "error": None}]
    if artifact != "absent":
        value["image_analysis"][0]["analysis_ref"] = "qq-image-analysis:sha256:" + "a" * 64
    with closing(direct.memory._connect()) as db, db:
        db.execute("UPDATE qq_visual_observations SET record_json=? WHERE record_id=?", (canonical(value), source.observation.record_id))
    bound = json.loads(canonical(value))
    if artifact == "changed":
        bound["image_analysis"][0]["analysis_ref"] = "qq-image-analysis:sha256:" + "b" * 64
    fixtures.rewrite_event(direct, source.event["event_id"], bound_observation=bound)
    instance, _ = bridge
    if artifact == "changed":
        with pytest.raises(ValueError, match="direct_read_source_invalid"):
            instance.enqueue(dispatch(instance, arguments={"query": "绿色小猫", "limit": 1}))
        return
    result = instance.enqueue(dispatch(instance, arguments={"query": "绿色小猫", "limit": 1}))
    retained = result["receipt"]["messages"][0]["observation"]
    assert "绿色小猫" not in retained["text"] and retained["image_analysis"][0]["authority"] == "model_inference"


def test_raw_evidence_change_invalidates_read_instead_of_returning_unverified_context(direct, bridge):
    source = direct.create()
    path = direct.store.evidence_dir / (source.evidence.rsplit("/", 1)[1] + ".json")
    path.write_text("{}")
    instance, _ = bridge
    with pytest.raises(ValueError, match="evidence_changed"):
        instance.enqueue(dispatch(instance))
    assert instance.result("opportunity:read") is None


@pytest.mark.parametrize("updates", [{"capability_id": "communication.qq.send.v1"}, {"generation": 1},
    {"scope": {"transport": "onebot", "group_id": "999", "self_member_id": fixtures.SELF}},
    {"activation_revision": "old"}, {"arguments": {"group_id": fixtures.GUEST}}, {"arguments": {"limit": 100}},
    {"arguments": {"query": " "}}, {"arguments": {"text": "发送这条"}}])
def test_read_offer_cannot_expand_scope_send_or_accept_unbounded_arguments(direct, bridge, updates):
    direct.create()
    instance, _ = bridge
    with pytest.raises(ValueError):
        instance.enqueue(dispatch(instance, **updates))
    assert not direct.store.counts()["sends"]


def test_model_may_supply_query_only_for_protocol_read_not_gui_qq_or_send(bridge):
    instance, _ = bridge
    offer = CapabilityOffer.model_validate(instance.catalog()[0])
    choice = CapabilityChoice(kind="observe", offer_id=offer.offer_id, arguments={"query": "图片", "limit": 5})
    _bound_choice(choice, offer, now=instance.clock())
    with pytest.raises(ValueError):
        _bound_choice(choice, offer.model_copy(update={"provider_id": "qq-watcher"}), now=instance.clock())
    with pytest.raises(ValueError):
        _bound_choice(choice, offer.model_copy(update={"capability_id": "communication.qq.send.v1"}), now=instance.clock())


def test_native_mode_hides_gui_send_offers_even_when_direct_is_offline(direct, bridge, tmp_path):
    instance, status = bridge
    desktop = SimpleNamespace(catalog=lambda: [{"capability_id": "communication.qq.send.v1"}], result=lambda _: None)
    combined = QQCapabilityTransports(desktop, instance)
    assert all(item["capability_id"].endswith("read.v1") for item in combined.catalog())
    direct.settings.status_file.write_text(canonical(status | {"connected": False}), encoding="utf-8")
    assert combined.catalog() == []
    path = tmp_path / "direct.json"
    config = json.loads(path.read_text(encoding="utf-8")) | {"enabled": False}
    path.write_text(canonical(config), encoding="utf-8")
    assert combined.catalog() == desktop.catalog()
