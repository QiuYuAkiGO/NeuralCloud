"""Shared dispatch uses real watcher gates/ledgers; no live QQ or model calls."""
from datetime import timedelta
import json
import sqlite3
from types import SimpleNamespace as NS

import httpx
import pytest

from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
from harness.qq.capability_bridge import QQCapabilityClient, QQCapabilityReport, QQCapabilityClaim, QQCapabilityAck
from harness.runtime.qq_capabilities import QQCapabilityBridge
from tests import test_qq_homeostatic_watcher as watcher_fixtures


controller = watcher_fixtures.controller
model_setup = watcher_fixtures.model_setup
watcher_setup = watcher_fixtures.watcher_setup
isolated_static_parser_cache = watcher_fixtures.isolated_static_parser_cache


class LocalClient:
    def __init__(self, bridge):
        self.bridge = bridge
        self.available = True
        self.ack_dropped = False

    def post(self, path, *, json, **kwargs):
        if not self.available:
            raise httpx.ConnectError("synthetic offline")
        method, schema = {
            "/v1/qq/capabilities/report": (self.bridge.report, QQCapabilityReport),
            "/v1/qq/opportunities/claim": (self.bridge.claim, QQCapabilityClaim),
            "/v1/qq/opportunities/ack": (self.bridge.acknowledge, QQCapabilityAck),
        }[path]
        value = method(schema.model_validate(json))
        if self.ack_dropped and path.endswith("/ack"):
            self.ack_dropped = False
            raise httpx.ReadTimeout("synthetic ack lost after commit")
        return httpx.Response(200, json=value, request=httpx.Request("POST", "http://127.0.0.1" + path))


@pytest.fixture
def rig(controller, tmp_path):
    state = controller
    watch = state.watch
    watcher_path = tmp_path / "watcher.json"
    watcher_path.write_text(watch.settings.model_dump_json(), encoding="utf-8")
    owner = NS(value="shared", generation=1)
    authority = NS(owner=lambda channel: owner.value, generation=1)
    config = NS(qq_send_enabled=False)
    supervisor = NS(capabilities=authority, manifest=NS(autonomous_capabilities=config))
    bridge = QQCapabilityBridge(supervisor, watch.session, watcher_path, tmp_path / "dispatch.sqlite3", clock=state.clock.wall)
    transport = LocalClient(bridge)
    watch._capability_bridge = QQCapabilityClient(NS(client=transport), watch.ledger)
    frame = state.desktop.capture()
    verified = state.desktop.verify_group(frame=frame)
    watch._qq_accept_read(verified.chat_frame, verified.chat_screen)
    watch._report_capabilities(force=True)
    return NS(state=state, watch=watch, bridge=bridge, owner=owner, authority=authority, config=config, transport=transport)


def enqueue(rig, capability=QQ_COMMUNICATION_READ, identifier="cap-one"):
    rig.watch._report_capabilities(force=True)
    offer = next(item for item in rig.bridge.catalog() if item["capability_id"] == capability)
    row = {"opportunity_id": identifier, "generation": rig.authority.generation,
        "deadline_at": (rig.state.clock.wall() + timedelta(seconds=50)).isoformat(),
        "capability_id": capability, "scope": offer["scope"], "selection_ref": "shared-selection-one",
        "offer_id": offer["offer_id"], "body_instance_id": offer["body_instance_id"]}
    return row, rig.bridge.enqueue(row)


def test_private_read_claim_durable_receipt_and_no_model_or_send(rig):
    row, result = enqueue(rig)
    assert result["status"] == "pending"
    rig.watch.cycle()
    local = rig.watch.ledger.capability_claims()[0]
    assert local["local_status"] == "completed"
    assert not rig.state.desktop.clicked and not rig.state.calls
    rig.watch.cycle()
    result = rig.bridge.result(row["opportunity_id"])
    assert result["status"] == "completed" and result["receipt"]["capture_id"]
    assert result["evidence_refs"] == ["test://current"]


def test_send_is_not_dispatchable_by_default(rig):
    offer = next(item for item in rig.bridge.catalog() if item["capability_id"] == QQ_COMMUNICATION_SEND)
    assert offer["supported"] and not offer["available"] and not offer["ready"]
    with pytest.raises(ValueError, match="send_disabled"):
        enqueue(rig, QQ_COMMUNICATION_SEND)
    assert not rig.state.desktop.clicked


def test_shared_send_reuses_existing_decision_actor_and_receipt(rig):
    rig.config.qq_send_enabled = True
    row, _ = enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.watch.cycle()
    assert len(rig.state.desktop.clicked) == len(rig.state.calls) == 1
    intent = rig.state.calls[0]["idle_intent"]
    assert intent.opportunity_ref == row["opportunity_id"] and intent.selection_ref == row["selection_ref"]
    assert intent.deadline_at.isoformat() == row["deadline_at"]
    rig.watch._capability_bridge.acknowledge(rig.watch.ledger.capability_claims()[0])
    rig.watch.cycle()
    result = rig.bridge.result(row["opportunity_id"])
    assert result["status"] == "completed" and len(result["receipt"]["receipt_ids"]) == 1
    assert len(rig.state.desktop.clicked) == 1


def test_shared_send_revalidates_actor_group_inside_authorized_delivery(rig, monkeypatch):
    desktop = rig.state.desktop
    verify, deliver = desktop.verify_group, desktop.deliver
    issued = NS(value=None)
    def verify_owned(*, frame=None):
        issued.value = verify(frame=frame)
        return issued.value
    def deliver_owned(text, **kwargs):
        assert rig.watch._qq_operation is not None
        assert kwargs["group_verification"] is None
        # The production DesktopBackend performs this fresh verification in
        # deliver; this synthetic actor models its optional-evidence contract.
        previous = issued.value
        kwargs["group_verification"] = desktop.verify_group()
        assert kwargs["group_verification"] is issued.value and issued.value is not previous
        return deliver(text, **kwargs)
    monkeypatch.setattr(desktop, "verify_group", verify_owned)
    monkeypatch.setattr(desktop, "deliver", deliver_owned)
    rig.config.qq_send_enabled = True
    enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.watch.cycle()
    assert len(desktop.clicked) == 1


def test_shared_observation_waits_for_fresh_sensor_after_capture_gap(rig, monkeypatch):
    from harness.qq.telemetry import QQDeadline
    clock = rig.state.clock
    current = rig.watch.observer.latest()
    resume = clock.mono() + .1
    monkeypatch.setattr(rig.watch.observer, "latest", lambda: current if clock.mono() >= resume else None)
    deadline = QQDeadline(clock.mono() + 2)
    assert rig.watch._qq_wait_for_observer(deadline) is current
    assert clock.mono() >= resume and deadline.remaining() > 0
    assert not rig.state.desktop.clicked


def test_shared_observation_gap_never_renews_deadline_or_uses_stale_sensor(rig, monkeypatch):
    from harness.qq.telemetry import QQDeadline, QQDeadlineExpired
    clock = rig.state.clock
    monkeypatch.setattr(rig.watch.observer, "latest", lambda: None)
    deadline = QQDeadline(clock.mono() + .1)
    with pytest.raises(QQDeadlineExpired):
        rig.watch._qq_wait_for_observer(deadline)
    assert deadline.remaining() == 0 and not rig.state.desktop.clicked


def test_shared_observation_returns_new_context_for_existing_supersession_guard(rig, monkeypatch):
    from harness.qq.telemetry import QQDeadline
    old = rig.watch.observer.latest()
    rig.watch.observer.arrive()
    current = rig.watch.observer.latest()
    assert rig.watch._qq_wait_for_observer(QQDeadline(rig.state.clock.mono()+2)) == current
    assert current.sequence > old.sequence and current.digest != old.digest


def test_shared_send_wait_is_terminal_without_receipt(rig):
    rig.config.qq_send_enabled = True
    rig.state.client.choice = '{"action":"wait","reason":"Wait for now."}'
    row, _ = enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.watch.cycle()
    rig.watch.cycle()
    assert not rig.state.desktop.clicked
    assert rig.bridge.result(row["opportunity_id"])["status"] == "wait"


def test_claim_retries_preserve_token_and_never_rebind(rig):
    row, _ = enqueue(rig)
    payload = QQCapabilityClaim(scope=rig.watch._qq_scope(), run_id=rig.watch.ledger.run_id, generation=1)
    first, second = rig.bridge.claim(payload), rig.bridge.claim(payload)
    assert first == second
    assert rig.bridge.enqueue(row)["status"] == "claimed"
    with pytest.raises(ValueError, match="conflict"):
        rig.bridge.enqueue({**row, "selection_ref": "changed"})


def test_ack_cannot_self_assert_a_success_without_local_proof(rig):
    row, _ = enqueue(rig)
    claim = rig.bridge.claim(QQCapabilityClaim(scope=rig.watch._qq_scope(), run_id=rig.watch.ledger.run_id, generation=1))["opportunity"]
    ack = QQCapabilityAck(scope=rig.watch._qq_scope(), run_id=claim["run_id"], generation=1,
        opportunity_id=row["opportunity_id"], claim_token=claim["claim_token"])
    with pytest.raises(ValueError, match="local_claim_missing"):
        rig.bridge.acknowledge(ack)
    rig.watch.ledger.register_capability_claim(claim)
    assert rig.bridge.acknowledge(ack)["status"] == "pending"


def test_stale_or_wrong_scope_report_is_rejected(rig):
    report = QQCapabilityReport(scope=rig.watch._qq_scope(), run_id=rig.watch.ledger.run_id,
        reported_at=rig.state.clock.wall() - timedelta(seconds=20), read_ready=True, send_ready=True)
    with pytest.raises(ValueError, match="expired"):
        rig.bridge.report(report)
    with pytest.raises(ValueError, match="scope_invalid"):
        rig.bridge.report(report.model_copy(update={"scope": report.scope.model_copy(update={"group_id": "99999"})}))
    rig.state.clock.seconds += 16
    assert rig.bridge.catalog() == []


def test_owner_change_invalidates_unclaimed_work_and_blocks_legacy(rig):
    row, _ = enqueue(rig)
    rig.owner.value = "off"
    rig.authority.generation = 2
    rig.watch._report_capabilities(force=True)
    rig.watch.cycle()
    assert rig.watch._capability_bridge.owner == "off"
    assert not rig.state.calls and not rig.state.desktop.clicked


def test_lost_authority_after_shared_report_never_falls_back_to_legacy(rig):
    rig.transport.available = False
    rig.state.clock.seconds += 16
    rig.watch.cycle()
    assert rig.watch._capability_bridge.owner == "shared"
    assert not rig.state.calls and not rig.state.desktop.clicked
    restored = QQCapabilityClient(NS(client=rig.transport), rig.watch.ledger)
    assert restored.owner == "shared" and restored.valid_until is None


def test_ack_response_loss_reconciles_without_second_gui_operation(rig):
    row, _ = enqueue(rig)
    rig.watch.cycle()
    captures = rig.state.desktop.captures
    rig.transport.ack_dropped = True
    rig.watch.cycle()
    rig.watch.cycle()
    assert rig.bridge.result(row["opportunity_id"])["status"] == "completed"
    assert rig.state.desktop.captures == captures


def test_expired_claim_is_retired_without_capture(rig):
    row, _ = enqueue(rig)
    rig.watch._capability_bridge.claim(rig.watch._qq_scope(), rig.watch.ledger.run_id, now=rig.state.clock.wall())
    captures = rig.state.desktop.captures
    rig.state.clock.seconds += 51
    rig.watch.cycle()
    assert rig.bridge.result(row["opportunity_id"])["status"] == "expired"
    assert rig.state.desktop.captures == captures


def test_send_generation_changes_during_model_prevent_click(rig):
    rig.config.qq_send_enabled = True
    enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.state.after_model = lambda _: setattr(rig.authority, "generation", 2)
    rig.watch.cycle()
    assert not rig.state.desktop.clicked


@pytest.mark.parametrize("change", ["off", "shared", "roundtrip", "offline"])
def test_prepared_legacy_idle_rechecks_persisted_owner_generation(rig, change):
    rig.owner.value = "legacy"
    rig.watch._report_capabilities(force=True)
    def migrate(_):
        if change == "offline":
            rig.transport.available = False
        else:
            rig.owner.value = "legacy" if change == "roundtrip" else change
            rig.authority.generation += 2 if change == "roundtrip" else 1
    rig.state.after_model = migrate
    with pytest.raises((RuntimeError, httpx.ConnectError)):
        rig.watch.cycle()
    assert len(rig.state.calls) == 1
    assert rig.state.calls[0]["idle_intent"].owner_generation == 1
    assert not rig.state.desktop.clicked
    with sqlite3.connect(rig.watch.ledger.path) as db:
        assert db.execute("SELECT status FROM qq_watch_sends").fetchone()[0] == "not_sent"


def test_legacy_watcher_without_negotiated_capability_authority_is_unchanged(controller):
    watch = controller.watch
    transport = LocalClient(None)
    transport.available = False
    watch._capability_bridge = QQCapabilityClient(NS(client=transport), watch.ledger)
    watch.cycle()
    assert len(controller.desktop.clicked) == 1
    assert watch._capability_bridge.owner == "legacy"


def test_existing_central_call_invalidation_before_send_prevents_click(rig):
    rig.config.qq_send_enabled = True
    row, _ = enqueue(rig, QQ_COMMUNICATION_SEND)
    marker = NS(invalid=None)
    rig.authority.calls = NS(get=lambda identifier: row if identifier == row["opportunity_id"] else None)
    rig.authority.invalid_reason = lambda row: marker.invalid
    rig.state.after_model = lambda _: setattr(marker, "invalid", "superseded")
    rig.watch.cycle()
    assert not rig.state.desktop.clicked


def test_changed_homeostasis_source_does_not_run_qq_model(rig):
    rig.config.qq_send_enabled = True
    rig.watch._report_capabilities(force=True)
    offer = next(item for item in rig.bridge.catalog() if item["capability_id"] == QQ_COMMUNICATION_SEND)
    row = {"opportunity_id": "source-changed", "generation": 1,
        "deadline_at": (rig.state.clock.wall() + timedelta(seconds=50)).isoformat(),
        "capability_id": QQ_COMMUNICATION_SEND, "scope": offer["scope"], "selection_ref": "selection",
        "offer_id": offer["offer_id"], "body_instance_id": offer["body_instance_id"],
        "source_homeostasis_revision": rig.watch._homeostasis.summary.snapshot_revision + 1}
    rig.bridge.enqueue(row)
    rig.watch.cycle()
    rig.watch.cycle()
    assert not rig.state.calls and not rig.state.desktop.clicked
    assert rig.bridge.result(row["opportunity_id"])["status"] == "failed"


def test_shared_send_allows_same_episode_heartbeat_revisions(rig):
    rig.config.qq_send_enabled = True
    row, _ = enqueue(rig, QQ_COMMUNICATION_SEND)
    def heartbeat(_):
        summary = rig.watch._homeostasis.summary
        rig.watch._homeostasis.summary = summary.model_copy(
            update={"snapshot_revision": summary.snapshot_revision + 1})
    rig.state.after_model = heartbeat
    rig.watch.cycle()
    assert len(rig.state.desktop.clicked) == 1
    rig.watch._capability_bridge.acknowledge(rig.watch.ledger.capability_claims()[0])
    assert rig.bridge.result(row["opportunity_id"])["status"] == "completed"


def test_shared_send_refreshes_periodic_snapshot_before_source_check(rig):
    rig.config.qq_send_enabled = True
    rig.watch._report_capabilities(force=True)
    offer = next(item for item in rig.bridge.catalog() if item["capability_id"] == QQ_COMMUNICATION_SEND)
    source = rig.watch._homeostasis.summary
    target_revision = source.snapshot_revision + 1
    rig.bridge.enqueue({"opportunity_id": "refresh-source", "generation": 1,
        "deadline_at": (rig.state.clock.wall() + timedelta(seconds=50)).isoformat(),
        "capability_id": QQ_COMMUNICATION_SEND, "scope": offer["scope"], "selection_ref": "selection",
        "offer_id": offer["offer_id"], "body_instance_id": offer["body_instance_id"],
        "source_homeostasis_revision": target_revision, "source_episode_signature": source.episode_signature})
    def refresh():
        rig.watch._homeostasis.summary = source.model_copy(update={"snapshot_revision": target_revision})
    rig.watch._homeostasis.refresh = refresh
    rig.watch.cycle()
    assert len(rig.state.desktop.clicked) == 1


def test_shared_send_does_not_repeat_legacy_quiet_and_probe_selection(rig):
    rig.config.qq_send_enabled = True
    rig.watch._idle_next_probe = float("inf")
    rig.watch._homeostasis.eligible = lambda **options: options.get("shared_opportunity") is True
    enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.watch.cycle()
    assert len(rig.state.desktop.clicked) == 1


def test_shared_send_rejects_changed_need_episode_after_model(rig):
    rig.config.qq_send_enabled = True
    enqueue(rig, QQ_COMMUNICATION_SEND)
    def change_episode(_):
        summary = rig.watch._homeostasis.summary
        rig.watch._homeostasis.summary = summary.model_copy(update={
            "snapshot_revision": summary.snapshot_revision + 1,
            "episode_signature": "homeostasis:changed-need"})
    rig.state.after_model = change_episode
    rig.watch.cycle()
    assert not rig.state.desktop.clicked


def test_shared_segments_verify_each_existing_child_receipt(rig):
    rig.config.qq_send_enabled = True
    rig.state.client.chat_routes = {"": "First.\n\nSecond."}
    row, _ = enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.watch.cycle()
    rig.watch._capability_bridge.acknowledge(rig.watch.ledger.capability_claims()[0])
    result = rig.bridge.result(row["opportunity_id"])
    assert len(rig.state.desktop.clicked) == 2
    assert result["status"] == "completed" and len(result["receipt"]["receipt_ids"]) == 2


def test_unclaimed_dispatch_expires_without_watcher_poll(rig):
    row, _ = enqueue(rig)
    rig.state.clock.seconds += 51
    assert rig.bridge.result(row["opportunity_id"])["status"] == "expired"


def test_restarted_runtime_never_reassigns_claim_and_can_read_late_evidence(rig):
    row, _ = enqueue(rig)
    rig.watch.cycle()
    local = rig.watch.ledger.capability_claims()[0]
    restarted = QQCapabilityBridge(rig.bridge.supervisor, rig.watch.session, rig.bridge.watcher_path,
        rig.bridge.path, clock=rig.state.clock.wall)
    rig.transport.bridge = restarted
    rig.authority.calls = NS(get=lambda identifier: row)
    rig.authority.invalid_reason = lambda call: "superseded"
    assert restarted.result(row["opportunity_id"])["status"] == "uncertain"
    assert restarted.claim(QQCapabilityClaim(scope=rig.watch._qq_scope(), run_id=rig.watch.ledger.run_id,
        generation=1))["opportunity"] is None
    assert rig.watch._capability_bridge.acknowledge(local)["status"] == "completed"
    assert not rig.state.desktop.clicked


def test_uncertain_ack_keeps_polling_real_ledger_and_never_reexecutes(rig):
    rig.config.qq_send_enabled = True
    row, _ = enqueue(rig, QQ_COMMUNICATION_SEND)
    rig.watch.cycle()
    local = rig.watch.ledger.capability_claims()[0]
    with sqlite3.connect(rig.watch.ledger.path) as db:
        reservation = db.execute("SELECT record_json FROM qq_watch_sends WHERE event_id=?", (local["event_id"],)).fetchone()[0]
        uncertain = {**json.loads(reservation), "status": "uncertain"}
        db.execute("UPDATE qq_watch_sends SET status='uncertain',record_json=? WHERE event_id=?", (json.dumps(uncertain), local["event_id"]))
    assert rig.watch._capability_bridge.acknowledge(local)["status"] == "uncertain"
    assert rig.watch.ledger.capability_claims()
    with sqlite3.connect(rig.watch.ledger.path) as db:
        db.execute("UPDATE qq_watch_sends SET status='sent',record_json=? WHERE event_id=?", (reservation, local["event_id"]))
    rig.watch._capability_cycle()
    assert rig.bridge.result(row["opportunity_id"])["status"] == "completed"
    assert not rig.watch.ledger.capability_claims()
    assert len(rig.state.desktop.clicked) == 1


def test_restarted_watcher_retires_unstarted_claim_without_reexecution(rig):
    row, _ = enqueue(rig)
    rig.watch._capability_bridge.claim(rig.watch._qq_scope(), rig.watch.ledger.run_id, now=rig.state.clock.wall())
    previous_run = rig.watch.ledger.run_id
    rig.watch.ledger.start_run(now=rig.state.clock.wall())
    assert rig.watch.ledger.run_id != previous_run
    rig.watch._capability_cycle()
    assert rig.bridge.result(row["opportunity_id"])["status"] == "expired"
    assert not rig.state.desktop.clicked


def test_read_ack_rejects_rebound_local_evidence_scope(rig):
    row, _ = enqueue(rig)
    rig.watch.cycle()
    local = rig.watch.ledger.capability_claims()[0]
    with sqlite3.connect(rig.watch.ledger.path) as db:
        proof = json.loads(db.execute("SELECT result_json FROM qq_watch_capability_claims WHERE opportunity_id=?", (row["opportunity_id"],)).fetchone()[0])
        proof["scope"]["group_id"] = "99999"
        db.execute("UPDATE qq_watch_capability_claims SET result_json=? WHERE opportunity_id=?", (json.dumps(proof), row["opportunity_id"]))
    with pytest.raises(ValueError, match="read_evidence_mismatch"):
        rig.watch._capability_bridge.acknowledge(local)


def test_router_requires_authentication_and_accepts_no_success_assertion(rig, monkeypatch):
    import asyncio
    from fastapi import FastAPI, Header, HTTPException
    from harness.runtime.qq_capabilities import create_qq_capability_router
    monkeypatch.setenv("NC_QQ_WATCHER_CONFIG", str(rig.bridge.watcher_path))
    def authenticate(authorization: str | None = Header(default=None)):
        if authorization != "Bearer test-token":
            raise HTTPException(status_code=401)
    app = FastAPI()
    app.include_router(create_qq_capability_router(rig.bridge.supervisor, authenticate=authenticate,
        config_path=rig.watch.settings.session_config_path, state_path=rig.bridge.path))
    payload = QQCapabilityReport(scope=rig.watch._qq_scope(), run_id=rig.watch.ledger.run_id,
        reported_at=rig.state.clock.wall(), read_ready=True, send_ready=False).model_dump(mode="json")
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://inprocess") as client:
            assert (await client.post("/v1/qq/capabilities/report", json=payload)).status_code == 401
            result = await client.post("/v1/qq/capabilities/report", json=payload, headers={"Authorization": "Bearer test-token"})
            assert result.status_code == 200 and result.json()["owner"] == "shared"
            rejected = await client.post("/v1/qq/opportunities/ack", json={"success": True}, headers={"Authorization": "Bearer test-token"})
            assert rejected.status_code == 409
    asyncio.run(run())
