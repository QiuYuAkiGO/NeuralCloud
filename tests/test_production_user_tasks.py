"""Durable task/notification vertical slice using real stores and synthetic QQ."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
import json
from types import SimpleNamespace

import pytest

from harness.production.runtime import ProductionRuntime
from harness.production.store import ProductionStore
from harness.production.user_tasks import UserTaskStore
from harness.qq.direct import DirectQQActor
from harness.qq.direct_capabilities import QQCapabilityTransports, QQDirectCapabilityBridge
from harness.qq.shared import QQSharedPrepareRequest
from harness.qq.task_notifications import TaskNotifications, process_notice, reconcile_notices
from harness.qq.telemetry import QQDeadline
from harness.qq.onebot import OneBotError
from tests.test_launch_foundation import private_only  # noqa: F401
from tests.test_qq_private_actor import private_actor, private_message, PRIVATE, MEMBER  # noqa: F401
from tests.test_qq_direct_actor import actor_factory, consume  # noqa: F401
from tests.test_production_runtime import FakeSupervisor


@pytest.fixture
def rig(private_only, tmp_path, monkeypatch):  # noqa: F811
    actor, cognition = private_only
    path = tmp_path / "direct.json"
    path.write_text(actor.settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_QQ_DIRECT_CONFIG", str(path))
    supervisor = FakeSupervisor(tmp_path / "runtime")
    runtime = ProductionRuntime(supervisor)
    supervisor.production_runtime = runtime
    supervisor.qq_capability_bridge = QQCapabilityTransports(None,
        QQDirectCapabilityBridge(supervisor, actor.session, path, tmp_path / "bridge.db"))
    return SimpleNamespace(actor=actor, cognition=cognition, runtime=runtime, runner=runtime.user_tasks)


async def source(rig, number=1):
    actor = rig.actor
    await actor.verify_connection()
    await actor.on_event(private_message(number))
    row = actor.store.pending(actor.run_id)
    await actor.observe(row["event"], QQDeadline.after(30))
    seen = datetime.fromisoformat(row["received_at"])
    return QQSharedPrepareRequest(transport="onebot", group_id=PRIVATE,
        self_member_id=actor.settings.self_member_id, trigger_member_id=MEMBER,
        deadline_at=seen + timedelta(seconds=60), trace={"trace_id": "qq-trace:" + "b" * 32,
            "event_id": row["event_id"], "first_seen_at": seen, "first_seen_mono": 0})


def test_background_task_releases_chat_and_separates_result_delivery(rig):
    async def scenario():
        release = asyncio.Event()
        calls = []

        async def handler(row):
            calls.append(row["id"])
            await release.wait()
            return {"text": "处理后的测试结果", "evidence_refs": ["fixture:processed"]}

        rig.runner.register("fixture.process", handler, lambda args: dict(args))
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {"input": "fixture"})
        assert rig.runner.accept(request, "fixture.process", {"input": "fixture"})["id"] == task["id"]
        with pytest.raises(ValueError, match="source_conflict"):
            rig.runner.accept(request, "fixture.process", {"input": "changed"})
        # The capability's handoff ends the short turn; there is no long-lived
        # dispatch or special QQ deadline for the background handler.
        rig.actor.store.update(request.trace.event_id, state="wait")
        rig.runner.tick()
        await asyncio.sleep(0)
        assert rig.runner.store.get(task["id"])["status"] == "running"
        assert await process_notice(rig.actor)
        rig.runner.flush_notices()
        current = rig.runner.store.get(task["id"])
        assert current["status"] == "running"
        assert current["notifications"][0]["delivery"]["status"] == "delivered"
        assert (await consume(rig.actor, private_message(2)))["state"] == "sent"
        assert len(rig.cognition.calls) == 1
        release.set()
        await rig.runner.task
        rig.runner.flush_notices()
        current = rig.runner.store.get(task["id"])
        assert current["status"] == "succeeded"
        assert current["notifications"][1]["delivery"]["status"] == "pending"
        assert await process_notice(rig.actor)
        rig.runner.flush_notices()
        assert rig.runner.store.get(task["id"])["notifications"][1]["delivery"]["receipt_id"]
        rig.runner.tick()
        assert not await process_notice(rig.actor)
        assert len(calls) == 1
        assert len(rig.actor.client.sent) == 3  # acceptance, normal reply, result
    asyncio.run(scenario())


def test_unknown_notification_reconciles_after_restart_without_resend(rig):
    async def scenario():
        rig.runner.register("fixture.process", lambda row: None, lambda args: args)
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {})
        rig.runner.flush_notices()
        rig.actor.client.history_error = True
        assert await process_notice(rig.actor)
        assert rig.actor.store.uncertain()
        assert rig.actor.store.outstanding_reconciliation() == []  # service facts are not model decisions
        new_actor = DirectQQActor(rig.actor.settings, cognition=rig.cognition, llm=rig.actor.llm)
        new_actor.client = rig.actor.client
        new_actor.connected = True
        new_actor.client.history_error = False
        await reconcile_notices(new_actor)
        rig.runner.flush_notices()
        assert rig.runner.store.get(task["id"])["notifications"][0]["delivery"]["status"] == "delivered"
        assert not new_actor.store.uncertain()
        assert not await process_notice(new_actor)
        assert len(new_actor.client.sent) == 1
    asyncio.run(scenario())


def test_runtime_notice_cannot_be_forged_or_rewritten(rig):
    async def scenario():
        rig.runner.register("fixture.process", lambda row: None, lambda args: args)
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {})
        bridge = rig.runtime.supervisor.qq_capability_bridge.direct
        notice = task["notifications"][0]
        with pytest.raises(ValueError, match="not_runtime_authorized"):
            bridge.publish_task_notice(task, notice | {"text": "内部进度伪装通知"})
        with pytest.raises(ValueError, match="not_runtime_authorized"):
            bridge.publish_task_notice(task, notice | {"purpose": "approval_required"})
        assert bridge.publish_task_notice(task, notice)["status"] == "pending"
        with pytest.raises(ValueError, match="immutable"):
            TaskNotifications(rig.actor.store).publish(task, notice | {"text": "篡改"})
    asyncio.run(scenario())


def test_task_revocation_before_dispatch_cannot_execute(rig):
    async def scenario():
        calls = []

        async def handler(row):
            calls.append(row)
            return {"text": "done"}

        rig.runner.register("fixture.process", handler, lambda args: args)
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {})
        path = rig.runtime.supervisor.qq_capability_bridge.direct.config_path
        data = json.loads(path.read_text(encoding="utf-8"))
        data["private_contacts"][0]["send"] = False
        path.write_text(json.dumps(data), encoding="utf-8")
        rig.runner.tick()
        await rig.runner.task
        assert not calls
        assert rig.runner.store.get(task["id"])["status"] == "failed"
        assert not await process_notice(rig.actor)
    asyncio.run(scenario())


def test_notification_crash_before_protocol_never_fakes_delivery(rig):
    async def scenario():
        rig.runner.register("fixture.process", lambda row: None, lambda args: args)
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {})
        rig.runner.flush_notices()
        notices = TaskNotifications(rig.actor.store)
        notices.reserve(notices.pending())
        # Startup cancels proven-unsent reservations in the shared send ledger.
        restarted = DirectQQActor(rig.actor.settings, cognition=rig.cognition, llm=rig.actor.llm)
        restarted.client, restarted.connected = rig.actor.client, True
        await reconcile_notices(restarted)
        rig.runner.flush_notices()
        assert rig.runner.store.get(task["id"])["notifications"][0]["delivery"]["status"] == "cancelled"
        assert not restarted.client.sent
    asyncio.run(scenario())


def test_task_claim_recovery_and_completion_are_durable(tmp_path):
    production = ProductionStore(tmp_path / "production.db")
    store = UserTaskStore(production)
    task = store.accept({"event_id": "source"}, "test", {})
    row = store.claim({"test"})
    assert row["id"] == task["id"]
    assert UserTaskStore(production).claim({"test"}) is None
    recovered = UserTaskStore(ProductionStore(production.path))
    recovered.recover()
    assert recovered.get(task["id"])["status"] == "uncertain"
    assert recovered.claim({"test"}) is None
    with pytest.raises(ValueError, match="terminal_immutable"):
        recovered.finish(row["id"], row["worker"], "succeeded", "cannot invent success")
    with production._db() as db:
        assert db.execute("SELECT count(*) FROM user_task_notices").fetchone()[0] == 2


def test_task_queue_is_bounded_and_duplicate_accept_does_not_consume_capacity(tmp_path):
    store = UserTaskStore(ProductionStore(tmp_path / "production.db"))
    for index in range(4):
        store.accept({"event_id": str(index)}, "test", {})
    assert store.accept({"event_id": "0"}, "test", {})["status"] == "queued"
    with pytest.raises(ValueError, match="queue_full"):
        store.accept({"event_id": "overflow"}, "test", {})


def test_concurrent_claims_allow_only_one_worker(tmp_path):
    store = UserTaskStore(ProductionStore(tmp_path / "production.db"))
    for index in range(4):
        store.accept({"event_id": str(index)}, "test", {})
    with ThreadPoolExecutor(max_workers=8) as pool:
        claims = list(pool.map(lambda _: store.claim({"test"}), range(8)))
    assert sum(row is not None for row in claims) == 1
    assert store.summary() == {"queued": 3, "running": 1}


def test_lost_protocol_ack_never_reissues_notice(rig):
    async def scenario():
        rig.runner.register("fixture.process", lambda row: None, lambda args: args)
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {})
        rig.runner.flush_notices()
        original = rig.actor.transmit_text

        async def lose_ack(*args, **kwargs):
            await original(*args, **kwargs)
            raise OneBotError("onebot_request_timeout")

        rig.actor.transmit_text = lose_ack
        assert await process_notice(rig.actor)
        for _ in range(2):
            await reconcile_notices(rig.actor)
            rig.runner.flush_notices()
            assert not await process_notice(rig.actor)
        assert rig.runner.store.get(task["id"])["notifications"][0]["delivery"]["status"] == "unknown"
        assert len(rig.actor.client.sent) == 1
    asyncio.run(scenario())


@pytest.mark.parametrize("started", [False, True])
def test_cancel_before_or_after_handler_start_never_leaves_running_claim(rig, started):
    async def scenario():
        calls = []

        async def handler(row):
            calls.append(row["id"])
            await asyncio.Event().wait()

        rig.runner.register("fixture.process", handler, lambda args: args)
        request = await source(rig)
        task = rig.runner.accept(request, "fixture.process", {})
        rig.runner.tick()
        if started:
            await asyncio.sleep(0)
        await rig.runner.close()
        assert rig.runner.store.get(task["id"])["status"] == "uncertain"
        rig.runner.tick()
        await asyncio.sleep(0)
        assert len(calls) == int(started)
        assert rig.runner.store.summary() == {"uncertain": 1}
    asyncio.run(scenario())


def test_storage_error_does_not_kill_heartbeat(rig, monkeypatch):
    import sqlite3

    def failed_claim(*args):
        raise sqlite3.OperationalError("synthetic storage unavailable")

    monkeypatch.setattr(rig.runner.store, "claim", failed_claim)
    rig.runner.tick()
    assert rig.runner.last_error == "OperationalError"
