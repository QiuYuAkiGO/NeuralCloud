"""Roster offers keep independent scopes while one watcher owns the ledger."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace as NS

import pytest

from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
from harness.embodiment.qq_contracts import QQReadResult, QQScope
from harness.qq.capability_bridge import QQCapabilityAck, QQCapabilityClaim, QQCapabilityReport
from harness.qq.session import QQSessionSettings
from harness.qq.watch_ledger import WatchLedger, WatchLedgerError
from harness.runtime.qq_capabilities import QQCapabilityBridge


HOME = "1044946257"
GUEST = "1060979317"
OUTSIDE = "9999999999"


@pytest.fixture
def roster_bridge(tmp_path):
    now = datetime.now(timezone.utc)
    ledger = WatchLedger(tmp_path / "watch.sqlite3", group_id=HOME, allowed_read_group_ids=[HOME, GUEST])
    ledger.start_run(now=now)
    ledger.observe(frame_signature="a" * 64, items=[], observed_at=now)
    settings = {
        "group_id": HOME, "self_member_id": "30001", "expected_instance_id": "test-body",
        "expected_revision": 2, "window_id": "test-window", "qq_body_enabled": True,
        "automatic_send": True, "state_db": str(ledger.path),
        "group_roster": [
            {"group_id": HOME, "label": "Home", "observe": True, "send": True},
            {"group_id": GUEST, "label": "Read only", "observe": True, "send": False},
        ],
    }
    path = tmp_path / "watcher.json"
    path.write_text(json.dumps(settings), encoding="utf-8")
    config = QQSessionSettings(memory_db=tmp_path / "memory.sqlite3", token_file=tmp_path / "token",
        allowed_group_ids=[HOME, GUEST, OUTSIDE], self_member_id="30001")
    send = NS(qq_send_enabled=True)
    supervisor = NS(capabilities=NS(owner=lambda _: "shared", generation=1),
        manifest=NS(autonomous_capabilities=send))
    bridge = QQCapabilityBridge(supervisor, config, path, tmp_path / "dispatch.sqlite3", clock=lambda: now)
    return NS(bridge=bridge, ledger=ledger, settings=settings, path=path, config=config, send=send, now=now)


def scope(group_id):
    return QQScope(body_instance_id="test-body", environment_revision=2, window_id="test-window",
        group_id=group_id, self_member_id="30001")


def report(rig, group_id):
    return rig.bridge.report(QQCapabilityReport(scope=scope(group_id), run_id=rig.ledger.run_id,
        reported_at=rig.now, read_ready=True, send_ready=True, observation_refs=[f"test://group/{group_id}"]))


def dispatch(rig, group_id, *, capability=QQ_COMMUNICATION_READ, identifier="roster-read"):
    offer = next(item for item in rig.bridge.catalog()
        if item["scope"]["group_id"] == group_id and item["capability_id"] == capability)
    return {"opportunity_id": identifier, "generation": 1,
        "deadline_at": (rig.now + timedelta(seconds=50)).isoformat(), "capability_id": capability,
        "scope": offer["scope"], "selection_ref": "shared-roster-selection", "offer_id": offer["offer_id"],
        "body_instance_id": offer["body_instance_id"], "activation_revision": offer["activation_revision"]}


def save_settings(rig):
    rig.path.write_text(json.dumps(rig.settings), encoding="utf-8")


def test_roster_catalog_has_separate_read_scopes_and_only_home_send(roster_bridge):
    rig = roster_bridge
    report(rig, HOME)
    report(rig, GUEST)
    offers = rig.bridge.catalog()
    assert {(item["scope"]["group_id"], item["capability_id"]) for item in offers} == {
        (HOME, QQ_COMMUNICATION_READ), (HOME, QQ_COMMUNICATION_SEND), (GUEST, QQ_COMMUNICATION_READ)}
    assert len({item["offer_id"] for item in offers}) == 3
    for item in offers:
        group_id = item["scope"]["group_id"]
        assert item["ready"]
        assert item["observation_refs"] == [f"test://group/{group_id}"]
        assert item["observation"]["read_only"] == (group_id == GUEST)
    assert rig.ledger.group_id == HOME


def test_roster_guest_claim_uses_home_run_and_cannot_be_claimed_as_home(roster_bridge):
    rig = roster_bridge
    report(rig, GUEST)
    row = dispatch(rig, GUEST)
    assert rig.bridge.enqueue(row)["status"] == "pending"
    assert rig.bridge.claim(QQCapabilityClaim(scope=scope(HOME), run_id=rig.ledger.run_id,
        generation=1))["opportunity"] is None
    request = QQCapabilityClaim(scope=scope(GUEST), run_id=rig.ledger.run_id, generation=1)
    claimed = rig.bridge.claim(request)["opportunity"]
    assert claimed["scope"]["group_id"] == GUEST and claimed["run_id"] == rig.ledger.run_id
    assert rig.bridge.claim(request)["opportunity"] == claimed


def test_roster_guest_send_cannot_reuse_a_valid_read_offer(roster_bridge):
    rig = roster_bridge
    report(rig, GUEST)
    row = dispatch(rig, GUEST)
    with pytest.raises(ValueError, match="send_disabled"):
        rig.bridge.enqueue({**row, "capability_id": QQ_COMMUNICATION_SEND})


def test_roster_guest_read_receipt_is_verified_in_home_ledger(roster_bridge):
    rig = roster_bridge
    report(rig, GUEST)
    rig.bridge.enqueue(dispatch(rig, GUEST))
    claimed = rig.bridge.claim(QQCapabilityClaim(scope=scope(GUEST), run_id=rig.ledger.run_id,
        generation=1))["opportunity"]
    rig.ledger.register_capability_claim(claimed)
    read = QQReadResult(scope=scope(GUEST), capture_id="guest-capture", observed_at=rig.now,
        context_signature="b" * 64, evidence_refs=(f"test://group/{GUEST}",))
    rig.ledger.finish_capability_claim(claimed["opportunity_id"], status="completed", result=read.model_dump(mode="json"))
    result = rig.bridge.acknowledge(QQCapabilityAck(scope=scope(GUEST), run_id=rig.ledger.run_id,
        generation=1, opportunity_id=claimed["opportunity_id"], claim_token=claimed["claim_token"]))
    assert result["status"] == "completed"
    assert result["receipt"]["scope"]["group_id"] == GUEST
    assert result["evidence_refs"] == [f"test://group/{GUEST}"]
    assert rig.ledger.group_id == HOME


@pytest.mark.parametrize("group_id,capability", [
    (GUEST, QQ_COMMUNICATION_SEND), (OUTSIDE, QQ_COMMUNICATION_READ),
    (OUTSIDE, QQ_COMMUNICATION_SEND), (HOME, "unknown.capability.v1"),
])
def test_roster_ledger_rejects_unpermitted_claims(roster_bridge, group_id, capability):
    rig = roster_bridge
    with pytest.raises(WatchLedgerError, match="scope mismatch"):
        rig.ledger.register_capability_claim({"opportunity_id": "invalid-scope", "run_id": rig.ledger.run_id,
            "scope": scope(group_id).model_dump(mode="json"), "capability_id": capability})


def test_legacy_ledger_keeps_read_scopes_home_only(roster_bridge):
    rig = roster_bridge
    legacy = WatchLedger(rig.ledger.path, group_id=HOME)
    legacy.run_id = rig.ledger.run_id
    with pytest.raises(WatchLedgerError, match="scope mismatch"):
        legacy.register_capability_claim({"opportunity_id": "invalid-legacy-scope", "run_id": rig.ledger.run_id,
            "scope": scope(GUEST).model_dump(mode="json"), "capability_id": QQ_COMMUNICATION_READ})


@pytest.mark.parametrize("field,value", [
    ("scope", scope(GUEST).model_dump(mode="json")),
    ("capability_id", QQ_COMMUNICATION_SEND),
])
def test_roster_dispatch_cannot_replace_scope_or_capability_of_offer(roster_bridge, field, value):
    rig = roster_bridge
    report(rig, HOME)
    report(rig, GUEST)
    row = dispatch(rig, HOME)
    with pytest.raises(ValueError, match="offer_binding_invalid"):
        rig.bridge.enqueue({**row, field: value})


def test_session_allowlist_does_not_grant_observation_outside_roster(roster_bridge):
    with pytest.raises(ValueError, match="scope_invalid"):
        report(roster_bridge, OUTSIDE)


def test_roster_does_not_grant_observation_outside_session_allowlist(roster_bridge):
    rig = roster_bridge
    rig.config.allowed_group_ids = [HOME]
    with pytest.raises(ValueError, match="scope_invalid"):
        report(rig, GUEST)


def test_roster_revocation_removes_offer_and_rejects_queued_read_claim(roster_bridge):
    rig = roster_bridge
    report(rig, HOME)
    report(rig, GUEST)
    row = dispatch(rig, GUEST)
    rig.bridge.enqueue(row)
    rig.settings["group_roster"] = rig.settings["group_roster"][:1]
    save_settings(rig)
    assert {item["scope"]["group_id"] for item in rig.bridge.catalog()} == {HOME}
    with pytest.raises(ValueError, match="scope_invalid"):
        rig.bridge.claim(QQCapabilityClaim(scope=scope(GUEST), run_id=rig.ledger.run_id, generation=1))


def test_roster_observe_false_is_not_advertised_or_accepted(roster_bridge):
    rig = roster_bridge
    report(rig, GUEST)
    rig.settings["group_roster"][1]["observe"] = False
    save_settings(rig)
    assert rig.bridge.catalog() == []
    with pytest.raises(ValueError, match="scope_invalid"):
        report(rig, GUEST)


def test_roster_home_send_permission_revocation_blocks_existing_claim(roster_bridge):
    rig = roster_bridge
    report(rig, HOME)
    row = dispatch(rig, HOME, capability=QQ_COMMUNICATION_SEND)
    rig.bridge.enqueue(row)
    request = QQCapabilityClaim(scope=scope(HOME), run_id=rig.ledger.run_id, generation=1)
    assert rig.bridge.claim(request)["opportunity"]
    rig.settings["group_roster"][0]["send"] = False
    save_settings(rig)
    assert all(item["capability_id"] == QQ_COMMUNICATION_READ for item in rig.bridge.catalog())
    assert rig.bridge.claim(request)["opportunity"] is None


def test_roster_reports_keep_existing_body_and_run_binding(roster_bridge):
    rig = roster_bridge
    request = QQCapabilityReport(scope=scope(GUEST), run_id="wrong-run", reported_at=rig.now,
        read_ready=True, send_ready=False)
    with pytest.raises(ValueError, match="run_invalid"):
        rig.bridge.report(request)
    request = request.model_copy(update={"run_id": rig.ledger.run_id,
        "scope": scope(GUEST).model_copy(update={"window_id": "other-window"})})
    with pytest.raises(ValueError, match="scope_invalid"):
        rig.bridge.report(request)


def test_legacy_watcher_without_roster_keeps_home_read_and_send_offers(roster_bridge):
    rig = roster_bridge
    del rig.settings["group_roster"]
    save_settings(rig)
    rig.send.qq_send_enabled = False
    report(rig, HOME)
    offers = rig.bridge.catalog()
    assert {item["capability_id"] for item in offers} == {QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND}
    send = next(item for item in offers if item["capability_id"] == QQ_COMMUNICATION_SEND)
    assert send["supported"] and not send["available"] and not send["ready"]
    with pytest.raises(ValueError, match="scope_invalid"):
        report(rig, GUEST)
