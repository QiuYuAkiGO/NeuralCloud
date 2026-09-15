"""Natural drift, real shared cognition, durable QQ dispatch and queue progress."""
import asyncio
from datetime import timedelta
import json
from pathlib import Path
import sqlite3
import time
from types import SimpleNamespace as NS
from uuid import uuid4

from harness.embodiment.capabilities import QQ_COMMUNICATION_READ
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.llm.client import MockLLM
from harness.qq.memory import QQVisualMemoryStore, QQVerifiedProfileInput, QQVisualObservationInput
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.watch_ledger import WatchEvent
from harness.research.manifest import AutonomousCapabilitiesCondition, load_experiment_manifest
from harness.runtime.models import RuntimeEventType, RuntimeEventPriority, utc_now
from harness.runtime.qq_turns import QQTurnIngress
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_basic_persona_continuity import _host
from tests import test_qq_capability_bridge as bridge_fixtures


rig = bridge_fixtures.rig
controller = bridge_fixtures.controller
model_setup = bridge_fixtures.model_setup
watcher_setup = bridge_fixtures.watcher_setup
isolated_static_parser_cache = bridge_fixtures.isolated_static_parser_cache


class RuntimeLoopClient(bridge_fixtures.LocalClient):
    def __init__(self, bridge, loop):
        super().__init__(bridge)
        self.loop = loop

    def post(self, *args, **kwargs):
        try:
            on_loop = asyncio.get_running_loop() is self.loop
        except RuntimeError:
            on_loop = False
        if on_loop:
            return super().post(*args, **kwargs)
        async def dispatch():
            return super(RuntimeLoopClient, self).post(*args, **kwargs)
        return asyncio.run_coroutine_threadsafe(dispatch(), self.loop).result(timeout=3)


def incoming(rig):
    now = utc_now()
    identity = "qq-watch-event:" + uuid4().hex
    store = QQVisualMemoryStore(rig.watch.session.memory_db)
    person = store.record_profile(QQVerifiedProfileInput(group_id=rig.watch.settings.group_id, member_id="29999",
        display_name="Synthetic participant", display_name_source="qq_profile", verification="qq_ui_verified",
        capture_id="e2e-profile", observed_at=now, group_evidence=["test://group"], member_evidence=["test://profile"])).profile
    observation = store.record_observation(QQVisualObservationInput(group_id=rig.watch.settings.group_id, member_id="29999",
        profile_record_id=person.record_id, capture_id="e2e-observation", capture_item_id=identity,
        text="A synthetic conversation observation.", observed_at=now, evidence=["test://observed-message"],
        author_attribution="qq_ui_verified", attribution_evidence=["test://profile"])).observation
    with sqlite3.connect(rig.watch.ledger.path) as db:
        revision = db.execute("SELECT revision FROM qq_watch_state WHERE singleton=1").fetchone()[0]
        event = WatchEvent(event_id=identity, run_id=rig.watch.ledger.run_id, group_id=rig.watch.settings.group_id,
            screen_id="e2e-observation", stream_revision=revision, item_index=0,
            item={"direction": "incoming", "text": observation.text}, observed_at=now, state="bound", source_record_id=observation.record_id)
        db.execute("INSERT INTO qq_watch_events VALUES(?,?,?,?,?)", (identity, event.run_id, event.group_id, event.state, event.model_dump_json()))
    return QQSharedPrepareRequest(group_id=event.group_id, self_member_id=rig.watch.settings.self_member_id,
        trigger_member_id="29999", deadline_at=now + timedelta(seconds=40),
        trace={"trace_id": "qq-trace:" + uuid4().hex, "event_id": identity, "first_seen_at": now, "first_seen_mono": 1.0})


def test_natural_read_dispatch_returns_before_qq_turn_reenters_same_queue(rig, tmp_path, monkeypatch):
    async def run():
        import harness.qq.telemetry as telemetry
        import harness.qq.watcher as watcher
        # The actor fixture replaces this constructor to use a synthetic
        # monotonic clock. Shared cognition also calls its class factory.
        monkeypatch.setattr(telemetry.QQDeadline, "after", watcher.QQDeadline.after, raising=False)
        rig.state.clock.wall = utc_now
        rig.bridge.clock = utc_now
        rig.watch._qq_provider._clock = utc_now
        monkeypatch.setattr("harness.qq.watcher.now", utc_now)
        monkeypatch.setenv("NC_QQ_SESSION_CONFIG", str(rig.watch.settings.session_config_path))
        monkeypatch.setenv("NC_QQ_WATCHER_CONFIG", str(rig.bridge.watcher_path))
        monkeypatch.delenv("NC_BOARD_CONFIG", raising=False)
        monkeypatch.delenv("NC_SHARED_MEMORY_DB", raising=False)
        monkeypatch.setattr("gui.service.probe_llm", lambda *a, **k: True)
        host, _ = _host(tmp_path / "owner", monkeypatch, ["unused"])
        original = host.chat.llm
        profile = load_profile("presets/homeostasis/qq-social-drive-v1.json")
        started = utc_now() - timedelta(hours=8)
        engine = HomeostasisEngine(profile, state_dir=tmp_path / "real-homeostasis", now=started, clock=utc_now)
        manifest = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json")).model_copy(update={
            "autonomous_capabilities": AutonomousCapabilitiesCondition(enabled=True,
                owners={"board": "off", "qq": "shared", "desktop": "off"}, allowed_capabilities=(QQ_COMMUNICATION_READ,))})
        sup = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=engine, state_dir=tmp_path / "real-runtime")
        rig.bridge.supervisor = sup
        sup.qq_capability_bridge = rig.bridge
        rig.watch._capability_bridge.client = RuntimeLoopClient(rig.bridge, asyncio.get_running_loop())
        rig.watch._report_capabilities(force=True)
        offer = next(item for item in rig.bridge.catalog() if item["capability_id"] == QQ_COMMUNICATION_READ)
        model = MockLLM(chat_script=[json.dumps({"kind": "observe", "offer_id": offer["offer_id"]}),
            json.dumps({"action": "wait", "reason": "Continue observing this synthetic conversation."})])
        host.chat.llm = model
        read_errors = []
        read = rig.watch._read_shared_capability
        def checked_read(*args):
            try:
                return read(*args)
            except Exception:
                import traceback
                read_errors.append(traceback.format_exc())
                raise
        monkeypatch.setattr(rig.watch, "_read_shared_capability", checked_read)
        try:
            await sup.boot_and_recover()
            sup.scheduler.suspend()
            while (event := await sup.queue.get(timeout=0)) is not None:
                await sup.dispatch(event)
            sup.capabilities.last_activity = time.monotonic() - 61
            initial = engine.snapshot.dimensions["social_connection"].value
            for step in range(1, 241):
                beat = await sup.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                    priority=RuntimeEventPriority.HEARTBEAT, source="isolated-natural-clock",
                    source_time=started + timedelta(seconds=step * profile.max_heartbeat_step_seconds))
                assert (await sup.queue.get()).event_id == beat.event_id
                await sup.dispatch(beat)
                if sup.queue.qsize():
                    break
            assert step > 1 and engine.snapshot.dimensions["social_connection"].value < initial
            event = await sup.queue.get(timeout=1)
            assert event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY
            assert {item.channel for item in await sup.capabilities.catalog()} == {"qq"}
            dispatched = await asyncio.wait_for(sup.dispatch(event), timeout=5)
            assert dispatched.detail["status"] == "pending", dispatched.detail
            assert rig.bridge.result(event.payload["opportunity_id"])["status"] == "pending"
            # Actor runs independently from the Runtime event loop. Delay ack
            # so another QQ turn reenters while the central call is pending.
            await asyncio.to_thread(rig.watch.cycle)
            assert rig.watch.ledger.capability_claims()[0]["local_status"] == "completed", read_errors
            # Read capability does not need a model callback. A separate QQ
            # observation demonstrates that the same queue/host remain usable
            # while the first cross-process capability is still unacknowledged.
            request = incoming(rig)
            ingress = QQTurnIngress(NS(supervisor=sup), rig.watch.session, watcher_path=rig.bridge.watcher_path)
            prepared = asyncio.create_task(ingress.prepare(request))
            qq_event = await sup.queue.get(timeout=1)
            assert qq_event.event_type == RuntimeEventType.QQ_TURN
            result = await asyncio.wait_for(sup.dispatch(qq_event), timeout=5)
            answer = await asyncio.wait_for(prepared, timeout=1)
            assert result.status == "completed", result.detail
            assert answer["decision"]["action"] == "wait"
            # The existing actor is the only place that captures and reads.
            await asyncio.to_thread(rig.watch._capability_cycle)
            await sup.capabilities.reconcile()
            completed = sup.capabilities.calls.get(event.payload["opportunity_id"])
            assert completed["status"] == "completed", completed
            assert completed["channel_result"]["receipt"]["scope"] == offer["scope"]
            assert not rig.state.desktop.clicked
            assert len(model.chat_calls) == 2
        finally:
            host.chat.llm = original
            await sup.stop()
    asyncio.run(run())
