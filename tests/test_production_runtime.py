from __future__ import annotations

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from harness.production.config import ProductionConfig
from harness.production.models import ProductionGoalUpdate
from harness.production.runtime import ProductionRuntime
from harness.production.store import ProductionBudgetExceeded, ProductionStore
from harness.research.manifest import ExperimentManifest
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now
from harness.runtime.queue import PriorityEventQueue
from harness.runtime.scheduler import RealTimeScheduler
from tests.test_runtime_supervisor import _manifest


def test_production_explicit_and_legacy_canonical_hash_unchanged():
    original = _manifest()
    assert "production" not in json.loads(original.canonical_json())
    old_wire = original.model_dump(mode="json")
    old_wire.pop("production")
    assert ExperimentManifest.model_validate(old_wire).content_hash() == original.content_hash()
    production = original.model_copy(update={"production": ProductionConfig()})
    assert production.content_hash() != original.content_hash()
    with pytest.raises(ValueError, match="semantic"):
        production.model_copy(update={"homeostatic_expression": {"mode": "responses"}})


def test_budget_is_atomic_durable_and_counts_attempts_not_success(tmp_path):
    path = tmp_path / "production.sqlite3"
    store = ProductionStore(path)
    now = datetime(2026, 9, 22, tzinfo=UTC)

    def reserve(number):
        try:
            return store.reserve_attempt(f"call-{number}", purpose="exploration", now=now)
        except ProductionBudgetExceeded:
            return None

    with ThreadPoolExecutor(max_workers=16) as pool:
        results = list(pool.map(reserve, range(30)))
    assert sum(value is not None for value in results) == 12
    restarted = ProductionStore(path)
    assert restarted.budget(now=now)["remaining"] == 0
    first = next(value for value in results if value)
    assert restarted.reserve_attempt(first["attempt_id"], purpose="exploration", now=now) == first
    restarted.reserve_attempt("user-reply", purpose="reply", autonomous=False, now=now)
    assert restarted.budget(now=now)["used"] == 12
    restarted.reserve_attempt("next-hour", purpose="exploration", now=now + timedelta(hours=1))
    assert restarted.budget(now=now + timedelta(hours=1))["used"] == 1


def test_usage_outbox_and_goal_limits_survive_restart(tmp_path):
    path = tmp_path / "production.sqlite3"
    store = ProductionStore(path)
    store.reserve_attempt("x", purpose="reflection")
    first = store.record_usage("x", {"outcome": "unknown", "source": "unavailable"})
    assert store.record_usage("x", {"outcome": "unknown", "source": "unavailable"}) == first
    store = ProductionStore(path)
    assert len(store.pending_usage()) == 1
    store.ack_usage(first["usage_id"], {"body_receipt": "durable"})
    assert not ProductionStore(path).pending_usage()
    for number in range(4):
        store.save_goal(str(number), {"description": "Learn from an observation"})
    with pytest.raises(ValueError, match="active_goal_limit"):
        store.save_goal("fifth", {})
    for number in range(3):
        row = store.record_goal_outcome("0", evidence_id=str(number), progress=False)
    assert row["status"] == "suspended"
    assert store.record_goal_outcome("0", evidence_id="2", progress=False)["steps"] == 3
    store.save_goal("fifth", {})


class FakeSupervisor:
    def __init__(self, tmp_path):
        self.manifest = SimpleNamespace(production=ProductionConfig(), embodiment=SimpleNamespace(body_instance_id="body-test"))
        self.state_dir = tmp_path
        self.embodiment = SimpleNamespace(provider=None)
        self.runtime_instance_id = "runtime-test"
        self.lifecycle = RuntimeLifecycle.RUNNING
        self.queue = PriorityEventQueue()
        self.scheduler = RealTimeScheduler(.01)
        self.scheduler.start()
        self.records, self.started, self.finished = [], [], []
        self.release = asyncio.Event()
        self.seq = 0

    def _dispatch_predicate(self):
        return lambda event: True

    def _append_journal(self, kind, *, event, data):
        self.records.append((kind, event.event_id))
        return object()

    async def submit_event(self, **kwargs):
        self.seq += 1
        kwargs.setdefault("source_time", utc_now())
        event = RuntimeEvent(event_id=f"event-{self.seq}", receive_seq=self.seq,
            correlation_id=f"event-{self.seq}", received_at=utc_now(), manifest_revision=1,
            idempotency_key=f"event-{self.seq}", **kwargs)
        await self.queue.put(event)
        return event

    async def dispatch(self, event):
        self.started.append((event.event_type, event.payload.get("session_id"), event.event_id))
        if event.event_type == RuntimeEventType.STOP:
            self.lifecycle = RuntimeLifecycle.STOPPED
            await self.queue.close()
        elif event.event_type == RuntimeEventType.USER_MESSAGE:
            await self.release.wait()
        self.finished.append(event.event_id)


def test_scheduling_keeps_control_and_heartbeat_live_and_sessions_serial(tmp_path):
    async def run():
        s = FakeSupervisor(tmp_path)
        runtime = ProductionRuntime(s)
        first = await s.submit_event(event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
            source="user", source_time=utc_now(), payload={"session_id": "a"})
        second = await s.submit_event(event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
            source="user", source_time=utc_now(), payload={"session_id": "a"})
        third = await s.submit_event(event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
            source="user", source_time=utc_now(), payload={"session_id": "b"})
        runner = asyncio.create_task(runtime.run())
        async with asyncio.timeout(1):
            while not any(kind == RuntimeEventType.HEARTBEAT for kind, _, _ in s.started):
                await asyncio.sleep(.001)
        started_ids = {entry[2] for entry in s.started}
        assert first.event_id in started_ids and third.event_id in started_ids
        assert second.event_id not in started_ids
        assert first.event_id not in s.finished
        assert ("job_scheduled", first.event_id) in s.records
        stop = await s.submit_event(event_type=RuntimeEventType.STOP, priority=RuntimeEventPriority.CONTROL,
            source="operator", source_time=utc_now(), payload={})
        await asyncio.wait_for(runner, 1)
        assert stop.event_id in s.finished
        assert first.event_id not in s.finished  # cancellation is not completion
    asyncio.run(run())


def test_body_disconnect_uses_explicit_restricted_context(tmp_path):
    async def run():
        runtime = ProductionRuntime(FakeSupervisor(tmp_path))
        context = await runtime.context("qq:person")
        assert context["restricted_dialogue"]
        assert context["body"]["available"] is False
        assert "homeostatic_summary" not in context
        assert context["scope"] == "qq:person"
        assert runtime.status()["energy"] is None
        runtime._body = {"body_instance_id": "body-test", "episode_id": "e", "captured_at": utc_now().isoformat(),
            "resting": True, "exhausted": False, "energy": {"available_units": 50}, "sample_sequence": 9, "ledger_revision": 4}
        runtime._body_error = None
        assert runtime.status()["energy"] == {"available_units": 50}
        assert runtime.status()["resting"] is True
        runtime._body["captured_at"] = (utc_now() - timedelta(minutes=1)).isoformat()
        stale = runtime.status()
        assert not stale["body_available"]
        assert stale["energy"] is None and stale["resting"] is None and stale["exhausted"] is None
        assert stale["sample_sequence"] is None and stale["ledger_revision"] is None
    asyncio.run(run())


@pytest.mark.parametrize("lost_receipt", [False, True])
def test_rest_intent_and_outcome_are_durable_without_retrying_unknown_effect(tmp_path, lost_receipt):
    from harness.embodiment.contracts import BodyRestReceipt, BodyStateSnapshot

    class Body:
        def __init__(self):
            self.calls = 0
            self.resting = False

        async def snapshot(self):
            return BodyStateSnapshot(provider_id="test", body_instance_id="body-test", episode_id="episode-1",
                revision=self.calls, sample_sequence=self.calls + 1, captured_at=utc_now(),
                physiology_mode="production_v1", resting=self.resting,
                energy={"capacity_units": 100, "available_units": 50, "available_ratio": .5, "delta_units_per_minute": 0},
                evidence_refs=["test:body-proof"])

        async def start_rest(self, command):
            self.calls += 1
            self.resting = True
            if lost_receipt:
                raise TimeoutError("effect happened but receipt was lost")
            state = await self.snapshot()
            return BodyRestReceipt(command_id=command.command_id, body_instance_id=command.body_instance_id,
                episode_id=command.episode_id, resting=True, ledger_revision=state.revision,
                evidence_refs=state.evidence_refs, body_state=state)

    async def run():
        runtime = ProductionRuntime(FakeSupervisor(tmp_path))
        body = Body()
        runtime.attach_body_provider(body)
        if lost_receipt:
            with pytest.raises(TimeoutError):
                await runtime.rest("rest-id", resting=True)
        else:
            assert (await runtime.rest("rest-id", resting=True)).resting
        restarted = ProductionRuntime(FakeSupervisor(tmp_path))
        restarted.attach_body_provider(body)
        await restarted.refresh_body()
        if lost_receipt:
            with pytest.raises(ValueError, match="outcome_unknown"):
                await restarted.rest("rest-id", resting=True)
            assert restarted.store.unresolved_rest()[0]["observed_current_state"]["resting"]
        else:
            assert (await restarted.rest("rest-id", resting=True)).resting
            assert restarted.store.unresolved_rest() == []
        assert body.calls == 1
    asyncio.run(run())


def test_goal_updates_require_bound_evidence_and_preserve_late_outcomes(tmp_path):
    store = ProductionStore(tmp_path / "production.sqlite3")
    proposed = ProductionGoalUpdate(status="active", description="Follow an unfinished question",
        success_criteria=["A sourced answer exists"], reason="It remains unresolved")
    saved = store.apply_goal_update(proposed, event_id="e1", verified_evidence=["e1"])
    assert store.apply_goal_update(proposed, event_id="e1", verified_evidence=["e1"]) == saved
    with pytest.raises(ValueError, match="evidence_unverified"):
        store.apply_goal_update(proposed.model_copy(update={"goal_id": saved["id"], "status": "completed",
            "evidence_refs": ["invented"]}), event_id="e2", verified_evidence=["e2"])
    store.apply_goal_update(proposed.model_copy(update={"goal_id": saved["id"], "status": "paused"}),
        event_id="e3", verified_evidence=["e3"])
    outcome = store.record_goal_outcome(saved["id"], evidence_id="verified-provider-result", progress=True, completed=True)
    assert outcome["status"] == "paused" and outcome["steps"] == 1
    completed = store.apply_goal_update(proposed.model_copy(update={"goal_id": saved["id"], "status": "completed",
        "evidence_refs": ["verified-provider-result"]}), event_id="e4", verified_evidence=outcome["outcomes"])
    assert completed["status"] == "completed"


def test_goals_are_bound_to_their_audience_and_cannot_be_read_or_mutated_cross_scope(tmp_path):
    store = ProductionStore(tmp_path / "production.sqlite3")
    update = ProductionGoalUpdate(status="active", description="Owner private detail",
        success_criteria=["Private criterion"], reason="Private reason")
    saved = store.apply_goal_update(update, event_id="e1", verified_evidence=[], audience="owner")
    assert store.goals(audience="12345") == []
    assert store.goal(saved["id"], audience="12345") is None
    with pytest.raises(ValueError, match="audience_mismatch"):
        store.apply_goal_update(update.model_copy(update={"goal_id": saved["id"], "status": "completed", "evidence_refs": ["e2"]}),
            event_id="e2", verified_evidence=["e2"], audience="12345")
    runtime = ProductionRuntime(FakeSupervisor(tmp_path))
    assert runtime.context_snapshot("12345")["goals"] == []
    assert runtime.context_snapshot("owner")["goals"][0]["goal"]["description"] == "Owner private detail"


def test_capability_outcomes_advance_selected_goal_once_and_recover_commit_gap(tmp_path):
    from harness.runtime.capabilities import CapabilityController
    from harness.research.manifest import AutonomousCapabilitiesCondition
    supervisor = FakeSupervisor(tmp_path)
    supervisor.manifest.autonomous_capabilities = AutonomousCapabilitiesCondition()
    supervisor.add_listener = lambda _: None
    supervisor.production_runtime = ProductionRuntime(supervisor)
    controller = CapabilityController(supervisor, tmp_path)
    goal = supervisor.production_runtime.store.save_goal("learn", {"description": "Learn from verified findings"})
    row = {"opportunity_id": "step1", "status": "pending", "production_goal_id": goal["id"], "channel": "web"}
    controller._finish(row, "completed", evidence_refs=["web-evidence:test"], channel_result={"effect": "external_information_only"})
    assert supervisor.production_runtime.store.goal(goal["id"])["steps"] == 1
    controller._finish(row, "completed")
    assert supervisor.production_runtime.store.goal(goal["id"])["steps"] == 1
    # A crash between the capability journal and goal projection is repaired.
    controller.calls.put({"opportunity_id": "step2", "status": "wait", "production_goal_id": goal["id"]})
    asyncio.run(controller.reconcile())
    updated = supervisor.production_runtime.store.goal(goal["id"])
    assert updated["steps"] == 2 and updated["no_progress"] == 1
    assert updated["results"][0]["evidence_refs"] == ["web-evidence:test"]


def test_goal_deadline_triggers_one_bounded_opportunity_before_periodic_review(tmp_path):
    async def run():
        supervisor = FakeSupervisor(tmp_path)
        runtime = ProductionRuntime(supervisor)
        runtime._body = {"body_instance_id": "body-test", "episode_id": "e", "captured_at": utc_now().isoformat()}
        runtime._body_error = None
        runtime.store.put_state("last_opportunity_at", utc_now().isoformat())
        runtime.store.apply_goal_update(ProductionGoalUpdate(status="active", description="Review at deadline",
            success_criteria=["Verified answer"], reason="A deadline is due", deadline_at=utc_now() - timedelta(seconds=1)),
            event_id="deadline-goal", verified_evidence=[])
        await runtime.schedule_opportunity()
        assert supervisor.queue.qsize() == 1
        await runtime.schedule_opportunity()
        assert supervisor.queue.qsize() == 1
    asyncio.run(run())


def test_body_usage_outbox_retries_same_id_and_never_rebinds_episode(tmp_path):
    class Body:
        def __init__(self):
            self.ids = []
            self.fail = True

        async def snapshot(self):
            return {"body_instance_id": "body-test", "episode_id": "episode-1", "sample_sequence": 7,
                "physiology_mode": "production_v1", "captured_at": utc_now().isoformat(), "energy": {"terminal": False},
                "resting": False, "exhausted": False}

        async def record_usage(self, command):
            assert command.metering == "tokens" and command.quantity == 100
            self.ids.append(command.inference_id)
            if self.fail:
                self.fail = False
                raise TimeoutError("receipt lost after applying cost")
            return SimpleNamespace(model_dump=lambda **_: {"inference_id": command.inference_id})

    async def run():
        runtime = ProductionRuntime(FakeSupervisor(tmp_path))
        body = Body()
        runtime.attach_body_provider(body)
        assert not (await runtime.context("user"))["restricted_dialogue"]
        for identity, episode in (("current", "episode-1"), ("other", "episode-0"), ("unbound", None)):
            runtime.store.reserve_attempt(identity, purpose="reply", autonomous=False)
            runtime.store.record_usage(identity, {"source": "api", "raw_usage": {"total_tokens": 100},
                "body_instance_id": "body-test", "episode_id": episode})
        await runtime.flush_usage()
        assert runtime.store.pending_usage_count() == 3
        await runtime.flush_usage()
        assert body.ids == ["current", "current"]
        assert runtime.store.pending_usage_count() == 2
        restarted = ProductionRuntime(FakeSupervisor(tmp_path))
        offline = await restarted.context("user")
        assert offline["restricted_dialogue"] and not offline["body"]["available"]
        assert offline["last_known_body_identity"]["episode_id"] == "episode-1"
    asyncio.run(run())


def test_actual_raw_only_and_delivery_costs_are_separate_from_model_budget(tmp_path):
    class Body:
        def __init__(self):
            self.charges = []

        async def record_usage(self, command):
            self.charges.append(command)
            return SimpleNamespace(model_dump=lambda **_: {"inference_id": command.inference_id})

    async def run():
        runtime = ProductionRuntime(FakeSupervisor(tmp_path))
        body = Body()
        runtime.attach_body_provider(body)
        runtime._body = {"body_instance_id": "body-test", "episode_id": "episode-1", "captured_at": utc_now().isoformat()}
        runtime._body_error = None
        binding = {"body_instance_id": "body-test", "episode_id": "episode-1"}
        for identity, usage in (("estimated", {"source": "api", "total_tokens": 900}),
                ("actual_pair", {"source": "api", "raw_usage": {"input_tokens": 30, "output_tokens": 20}}),
                ("partial", {"source": "api", "raw_usage": {"input_tokens": 30}})):
            runtime.store.reserve_attempt(identity, purpose="reflection")
            runtime.store.record_usage(identity, usage | binding)
        runtime.store.record_delivery("verified-receipt", **binding)
        runtime.store.record_delivery("verified-receipt", **binding)
        assert runtime.store.budget()["used"] == 3
        await runtime.flush_usage()
        assert [(item.metering, item.quantity, item.source) for item in body.charges] == [
            ("call", 1, "cognition"), ("tokens", 50, "cognition"), ("call", 1, "cognition"), ("operation", 1, "delivery")]
        assert runtime.store.pending_usage_count() == 0
        assert runtime.store.budget()["used"] == 3
    asyncio.run(run())


def test_real_runtime_production_reply_never_applies_semantic_homeostasis(tmp_path):
    from harness.runtime import PassiveHomeostasisAdapter, RuntimeSupervisor
    from tests.test_runtime_supervisor import _pipeline_adapter

    class Homeostasis(PassiveHomeostasisAdapter):
        async def apply_event(self, *args, **kwargs):
            raise AssertionError("production wrote legacy semantic state")

        async def apply_heartbeat(self, *args, **kwargs):
            raise AssertionError("production ticked legacy semantic state")

    class Model:
        def complete_chat_result(self, *args, **kwargs):
            return SimpleNamespace(finish_reason="stop", content_type="text", has_tool_calls=False,
                                   has_refusal=False, text='{"kind":"reply","reason":"Answer the question","text":"I remember."}')

    async def run():
        _, pipeline, cognitive = _pipeline_adapter(tmp_path)
        pipeline.llm = Model()
        s = RuntimeSupervisor(manifest=_manifest().model_copy(update={"production": ProductionConfig()}),
            cognitive=cognitive, homeostasis=Homeostasis(), state_dir=tmp_path / "runtime")
        cognitive.production_meter = SimpleNamespace(reserve_attempt=s.production_runtime.store.reserve_attempt,
            record_usage=s.production_runtime.store.record_usage, context=s.production_runtime.context_snapshot)
        await s.boot_and_recover()
        with pytest.raises(ValueError, match="reserved"):
            await s.submit_event(event_type=RuntimeEventType.USER_MESSAGE, priority=RuntimeEventPriority.USER,
                payload={"production_context": {"autonomous": False}})
        event = await s.submit_user_message("Do you remember?", session_id="person-a")
        result = await s.dispatch(event)
        assert result.status == "completed", result.detail
        assert result.detail["delivered"] is True
        assert s.status().production["body_available"] is False
        await s.stop()
    asyncio.run(run())
