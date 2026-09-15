"""Serial roster observation with a real QQ provider and synthetic GUI only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from datetime import timedelta
import hashlib
import json
import sqlite3
from types import SimpleNamespace as NS

import pytest

from harness.embodiment.capabilities import QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND
from harness.embodiment.providers.qq import QQCommunicationError, QQCommunicationProvider
from harness.embodiment.qq_contracts import QQReadResult, QQSendRequest
from harness.qq import telemetry, watcher as watcher_module
from harness.qq.capability_bridge import QQCapabilityClient
from harness.qq.desktop import DesktopError
from harness.qq.roster import QQGroupRosterEntry
from harness.qq.watcher import QQWatcher
from harness.qq.watch_ledger import VisibleItem
from harness.runtime.qq_capabilities import QQCapabilityBridge
from tests import test_qq_homeostatic_watcher as fixtures
from tests.test_qq_capability_bridge import LocalClient
from tests.test_qq_watcher import message


controller = fixtures.controller
model_setup = fixtures.model_setup
watcher_setup = fixtures.watcher_setup
isolated_static_parser_cache = fixtures.isolated_static_parser_cache
HOME, GUEST, OUTSIDE = "10001", "1060979317", "9999999999"


class RosterDesktop(fixtures.IdleDesktop):
    def __init__(self, clock):
        super().__init__(clock)
        self.active_group = HOME
        self.selected = []
        self.call_order = []
        self.before_select = lambda group_id: None
        self.fail_home_restore = False
        self.wrong_guest_identity = False
        self.config = NS(expected_group_id=HOME)
        self.home_messages = [f"message in {HOME}"]
        self.home_signature = "synthetic-chat"

    def capture(self):
        frame = super().capture()
        frame.evidence_ref = f"test://group/{self.active_group}/{frame.capture_id}"
        frame.content_digest = hashlib.sha256(f"{self.active_group}:{frame.capture_id}".encode()).hexdigest()
        frame.a11y_tree = {"signature": self.home_signature if self.active_group == HOME else "foreign-chat"}
        return frame

    def verify_group(self, *, frame=None):
        self.call_order.append(("verify_group", self.active_group))
        frame = frame or self.capture()
        self.current = frame
        self.verifications += 1
        group_id = OUTSIDE if self.wrong_guest_identity and self.active_group == GUEST else self.active_group
        texts = self.home_messages if self.active_group == HOME else [f"message in {self.active_group}"]
        return NS(group_id=group_id, chat_frame=frame,
            chat_screen=NS(messages=[message(text) for text in texts], frame_id=frame.capture_id,
                evidence_ref=frame.evidence_ref, scene="group_chat", blocked=False, opened_profile=None,
                group_title=NS(display_name=f"group {self.active_group}")),
            evidence_refs=[f"test://numeric-group/{group_id}"])

    def select_group(self, group_id, *, allowed_group_ids, display_name=None):
        assert group_id in allowed_group_ids
        self.call_order.append(("select_group", group_id))
        self.before_select(group_id)
        self.selected.append(group_id)
        if group_id == HOME and self.fail_home_restore:
            raise RuntimeError("synthetic home restore failed")
        self.active_group = self.config.expected_group_id = group_id
        return self.verify_group()

    def verify_self(self):
        self.call_order.append(("verify_self", self.active_group))
        if self.active_group != HOME:
            raise DesktopError("unique_visible_self_candidate_required")
        return NS(member_id="30001", evidence_refs=["test://account/30001", "test://account/30001"])


@pytest.fixture
def roster_watch(controller, monkeypatch):
    state = controller
    # Preserve the trace type check in the full incoming-message pipeline;
    # the parent fixture's lambda factory is sufficient only for idle turns.
    class SyntheticTrace(watcher_module.QQTurnTrace):
        def __init__(self, **values):
            values.setdefault("clock", state.clock.mono)
            super().__init__(**values)
    class SyntheticDeadline(watcher_module.QQDeadline):
        def __init__(self, mono, **values):
            values.setdefault("clock", state.clock.mono)
            super().__init__(mono, **values)
    monkeypatch.setattr(telemetry, "QQTurnTrace", SyntheticTrace)
    monkeypatch.setattr(watcher_module, "QQTurnTrace", SyntheticTrace)
    monkeypatch.setattr(telemetry, "QQDeadline", SyntheticDeadline)
    monkeypatch.setattr(watcher_module, "QQDeadline", SyntheticDeadline)
    session_path = state.watch.settings.session_config_path
    session = json.loads(session_path.read_text(encoding="utf-8"))
    session["allowed_group_ids"] = [HOME, GUEST]
    session_path.write_text(json.dumps(session), encoding="utf-8")
    settings = state.watch.settings.model_copy(update={"group_roster": [
        QQGroupRosterEntry(group_id=HOME, label="Home", send=True),
        QQGroupRosterEntry(group_id=GUEST, label="Observe only"),
    ]})
    desktop = RosterDesktop(state.clock)
    watch = QQWatcher(settings, desktop=desktop, decide=state.watch.decide)
    watch.observer = state.watch.observer
    watch.ledger.observe(frame_signature="a" * 64, items=[VisibleItem(direction="incoming", text="home anchor")],
        observed_at=state.clock.wall())
    watch.last_digest = "a" * 64
    watch.last_items = [VisibleItem(direction="incoming", text="home anchor")]
    watch._observer_sequence, watch._observer_baselined, watch._identity_prepared = 1, True, True
    yield NS(watch=watch, desktop=desktop, clock=state.clock, calls=state.calls, client=state.client)
    watch._close_embodied()


def read_guest(rig):
    return rig.watch._read_roster_group(GUEST, deadline_at=rig.clock.wall() + timedelta(seconds=50))


def home_state(watch):
    with closing(sqlite3.connect(watch.ledger.path)) as db:
        return db.execute("SELECT group_id,previous_frame_signature,previous_items_json,revision FROM qq_watch_state").fetchone()


def test_roster_read_uses_negotiated_provider_and_separate_store(roster_watch):
    rig = roster_watch
    before = home_state(rig.watch)
    result = read_guest(rig)
    assert isinstance(rig.watch._qq_provider, QQCommunicationProvider)
    assert result.scope.group_id == GUEST
    assert [item.text for item in result.messages] == [f"message in {GUEST}"]
    assert f"test://numeric-group/{GUEST}" in result.evidence_refs
    saved = rig.watch._roster_store.rows()
    assert set(saved) == {GUEST} and saved[GUEST]["read_count"] == 1
    assert QQReadResult.model_validate_json(saved[GUEST]["result_json"]) == result
    assert home_state(rig.watch) == before
    assert rig.desktop.selected == [GUEST, HOME]
    assert rig.desktop.active_group == rig.watch._qq_read_group_id == HOME
    assert rig.watch._roster_visiting is None and not rig.watch._identity_prepared
    assert not rig.desktop.clicked and not rig.calls


def test_roster_verifies_account_in_home_before_reading_guest_without_self_candidate(roster_watch, monkeypatch):
    rig = roster_watch
    original = rig.watch._qq_read_executor
    def record_read(request):
        rig.desktop.call_order.append(("read", request.scope.group_id))
        return original(request)
    monkeypatch.setattr(rig.watch, "_qq_read_executor", record_read)
    result = read_guest(rig)
    assert rig.desktop.call_order == [
        ("verify_self", HOME), ("select_group", GUEST), ("verify_group", GUEST),
        ("read", GUEST), ("select_group", HOME), ("verify_group", HOME),
    ]
    assert result.scope.group_id == GUEST and result.scope.self_member_id == "30001"
    assert result.evidence_refs.count("test://account/30001") == 1
    assert f"test://numeric-group/{GUEST}" in result.evidence_refs
    assert all(item.direction == "incoming" for item in result.messages)
    saved = QQReadResult.model_validate_json(rig.watch._roster_store.rows()[GUEST]["result_json"])
    assert saved.evidence_refs == result.evidence_refs
    assert not rig.desktop.clicked and not rig.calls


@pytest.mark.parametrize("unprepared", ["identity", "scope"])
def test_roster_requires_prepared_home_identity_before_any_navigation(roster_watch, unprepared):
    rig = roster_watch
    if unprepared == "identity":
        rig.watch._identity_prepared = False
    else:
        rig.watch._qq_read_group_id = GUEST
    with pytest.raises(ValueError, match="home_identity_required"):
        read_guest(rig)
    assert not rig.desktop.call_order
    assert not rig.watch._roster_store.rows()


def test_roster_visit_and_home_return_hold_observer_gate(roster_watch, monkeypatch):
    rig = roster_watch
    probes = []
    with ThreadPoolExecutor(max_workers=1) as observer:
        def probe():
            acquired = rig.watch._observation_lock.acquire(blocking=False)
            if acquired:
                rig.watch._observation_lock.release()
            return acquired
        rig.desktop.before_select = lambda group_id: probes.append((group_id, observer.submit(probe).result(timeout=1)))
        original = rig.watch._qq_read_executor
        def inspect_read(request):
            probes.append(("read", observer.submit(probe).result(timeout=1)))
            assert rig.watch._roster_visiting == GUEST
            return original(request)
        monkeypatch.setattr(rig.watch, "_qq_read_executor", inspect_read)
        read_guest(rig)
        assert observer.submit(probe).result(timeout=1)
    assert probes == [(GUEST, False), ("read", False), (HOME, False)]
    assert rig.watch.observer.sequence == 1


def test_roster_guest_send_is_rejected_by_real_provider(roster_watch, monkeypatch):
    rig = roster_watch
    original = rig.watch._qq_read_executor
    async def attempt_send(request):
        driver = rig.watch._qq_driver_status()
        assert driver.active_scope.group_id == GUEST and not driver.send_ready
        send = QQSendRequest(scope=request.scope, requested_at=request.requested_at, deadline=request.deadline,
            decision_id="synthetic-decision", reservation_id="synthetic-reservation", segment_index=0,
            segment_count=1, text="must not be sent", context_signature=driver.context_signature)
        with pytest.raises(QQCommunicationError, match="capability_unavailable"):
            await rig.watch._qq_provider.send(send)
        return original(request)
    monkeypatch.setattr(rig.watch, "_qq_read_executor", attempt_send)
    read_guest(rig)
    assert not rig.desktop.clicked and not rig.desktop.calls and not rig.calls


def test_roster_restore_failure_pauses_without_foreign_home_attribution(roster_watch):
    rig = roster_watch
    before = home_state(rig.watch)
    rig.desktop.fail_home_restore = True
    result = read_guest(rig)
    assert result.scope.group_id == GUEST
    assert rig.desktop.active_group == GUEST and rig.watch._qq_read_group_id == GUEST
    assert rig.watch._qq_read_frame is None and rig.watch._qq_read_screen is None
    assert not rig.watch._identity_prepared and rig.watch.should_stop()
    assert rig.watch.status["state"] == "roster_restore_failed"
    assert not rig.watch.status["automatic_send"]
    assert json.loads(rig.watch.settings.control_file.read_text())["reason"] == "qq_roster_home_restore_failed"
    assert home_state(rig.watch) == before
    with pytest.raises(InterruptedError):
        rig.watch.cycle()
    assert not rig.desktop.clicked and not rig.calls


def test_roster_wrong_numeric_identity_is_never_saved(roster_watch):
    rig = roster_watch
    before = home_state(rig.watch)
    rig.desktop.wrong_guest_identity = True
    with pytest.raises(ValueError, match="numeric_group_mismatch"):
        read_guest(rig)
    assert not rig.watch._roster_store.rows()
    assert rig.desktop.selected == [GUEST, HOME] and rig.desktop.active_group == HOME
    assert home_state(rig.watch) == before


@pytest.mark.parametrize("busy", ["pending", "operation", "attribution", "uncertain"])
def test_roster_read_does_not_interrupt_existing_actor_work(roster_watch, monkeypatch, busy):
    rig = roster_watch
    if busy == "uncertain":
        monkeypatch.setattr(rig.watch.ledger, "has_uncertain_send", lambda: True)
    else:
        setattr(rig.watch, {"pending": "_pending_turn", "operation": "_qq_operation", "attribution": "_attribution_retry"}[busy], True)
    with pytest.raises(ValueError, match="actor_busy"):
        read_guest(rig)
    assert not rig.desktop.selected


def test_roster_periodic_probe_observes_interval_without_model(roster_watch):
    rig = roster_watch
    rig.watch._maybe_roster_observation()
    assert rig.desktop.selected == [GUEST, HOME]
    rig.watch._identity_prepared = True
    rig.watch._maybe_roster_observation()
    assert rig.desktop.selected == [GUEST, HOME]
    rig.clock.sleep(rig.watch.settings.group_observation_seconds)
    rig.watch._maybe_roster_observation()
    assert rig.desktop.selected == [GUEST, HOME, GUEST, HOME]
    assert rig.watch._roster_store.rows()[GUEST]["read_count"] == 2
    assert not rig.calls and not rig.desktop.clicked


def test_roster_return_restores_normal_home_message_processing(roster_watch):
    rig = roster_watch
    rig.client.choice = '{"action":"wait","reason":"Synthetic home response."}'
    read_guest(rig)
    # The normal watcher rechecks the restored identity and takes a home baseline.
    rig.watch.cycle()
    assert rig.watch._identity_prepared and rig.watch._qq_read_group_id == HOME
    assert not rig.calls
    rig.desktop.home_messages.append("Misty, a new home-group message")
    rig.watch.observer.arrive()
    rig.desktop.home_signature = rig.watch.observer.digest
    rig.watch.cycle()
    assert len(rig.calls) == 1 and rig.calls[0]["group_id"] == HOME, {
        key: rig.watch.status.get(key) for key in ("state", "last_error_type", "backlog", "last_transition")}
    records = rig.watch.store.reply_context(group_id=HOME, member_id="20001", limit=20).observations
    assert any(item.text == "Misty, a new home-group message" for item in records)
    assert not any(item.text == f"message in {GUEST}" for item in records)
    assert rig.watch.status["last_decision_action"] == "wait"
    assert not rig.desktop.clicked


def test_shared_roster_read_completes_through_real_runtime_bridge(roster_watch, tmp_path):
    rig = roster_watch
    watch = rig.watch
    verified = rig.desktop.verify_group()
    watch._qq_accept_read(verified.chat_frame, verified.chat_screen)
    config_path = tmp_path / "watcher.json"
    config_path.write_text(watch.settings.model_dump_json(), encoding="utf-8")
    supervisor = NS(capabilities=NS(owner=lambda _: "shared", generation=1),
        manifest=NS(autonomous_capabilities=NS(qq_send_enabled=True)))
    bridge = QQCapabilityBridge(supervisor, watch.session, config_path,
        tmp_path / "runtime-capabilities.sqlite3", clock=rig.clock.wall)
    watch._capability_bridge = QQCapabilityClient(NS(client=LocalClient(bridge)), watch.ledger)
    watch._report_capabilities(force=True)
    offers = bridge.catalog()
    assert not any(item["scope"]["group_id"] == GUEST and item["capability_id"] == QQ_COMMUNICATION_SEND for item in offers)
    offer = next(item for item in offers if item["scope"]["group_id"] == GUEST)
    assert offer["capability_id"] == QQ_COMMUNICATION_READ and not offer["observation_refs"]
    bridge.enqueue({"opportunity_id": "shared-roster", "generation": 1,
        "deadline_at": (rig.clock.wall() + timedelta(seconds=50)).isoformat(), "capability_id": QQ_COMMUNICATION_READ,
        "scope": offer["scope"], "selection_ref": "roster-selection", "offer_id": offer["offer_id"],
        "body_instance_id": offer["body_instance_id"]})
    before = home_state(watch)
    watch._maybe_shared_capability(watch.observer.latest())
    assert watch.ledger.capability_claims()[0]["local_status"] == "completed"
    watch._capability_cycle()
    result = bridge.result("shared-roster")
    assert result["status"] == "completed" and result["receipt"]["scope"]["group_id"] == GUEST
    assert home_state(watch) == before
    assert rig.desktop.selected == [GUEST, HOME]
    assert not rig.calls and not rig.desktop.clicked


def test_legacy_watcher_has_no_roster_store_or_background_group_switch(controller):
    watch = controller.watch
    assert watch.settings.group_roster is None and watch._roster_store is None
    assert watch.ledger.allowed_read_group_ids == {HOME}
    watch._maybe_roster_observation()
    assert controller.desktop.captures == 0 and not controller.calls
