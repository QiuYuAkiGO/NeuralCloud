"""Isolated Runtime authority, quota, approval and goal evidence for the server body."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from harness.embodiment.capabilities import EmbodimentState, EmbodimentStatus, SERVER_EXECUTE, SERVER_OBSERVE
from harness.embodiment.server_contracts import ServerCommandResult, ServerError
from harness.research.manifest import ServerAccessCondition, load_experiment_manifest
from harness.runtime.models import RuntimeLifecycle, utc_now
from harness.runtime.server import EXECUTE, OBSERVE, ServerController
from harness.runtime.server_config import ServerSettings


class FakeProvider:
    def __init__(self, *, stdout="ok\n"):
        self.runs = []
        self.stdout = stdout
        self.closed = False

    async def status(self):
        return EmbodimentStatus(
            state=EmbodimentState.AVAILABLE, provider_id="fake-ssh", body_instance_id="server-01",
            activated_capabilities={SERVER_OBSERVE, SERVER_EXECUTE},
            effective_capabilities={SERVER_OBSERVE, SERVER_EXECUTE},
        )

    async def run(self, request, *, deadline=None, capability=None):
        self.runs.append(request)
        return ServerCommandResult(status="completed", exit_code=0, stdout=self.stdout, stderr="",
                                   risk_class="routine", duration_ms=1)

    async def close(self):
        self.closed = True


def rig(tmp_path, *, quota=100, provider=None, approval_timeout=1800):
    base = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    access = ServerAccessCondition(enabled=True, approval_timeout_seconds=approval_timeout)
    auto = base.autonomous_capabilities.model_copy(update={"enabled": True,
        "owners": {"server": "shared"}, "allowed_capabilities": (OBSERVE, EXECUTE)})
    manifest = base.model_copy(update={"server_access": access, "autonomous_capabilities": auto})
    sup = SimpleNamespace(manifest=manifest, lifecycle=RuntimeLifecycle.RUNNING, runtime_instance_id="runtime-1",
        capabilities=SimpleNamespace(activity_revision=1), _append_journal=lambda *args, **kwargs: None)
    settings = ServerSettings(daily_commands=quota, autonomous_daily_commands=min(20, quota),
                              approval_timeout_seconds=approval_timeout)
    fake = provider or FakeProvider()
    sup.server = ServerController(sup, settings, tmp_path, provider=fake)
    return sup, fake


async def offer(sup, capability=EXECUTE, *, scope="goal:1", autonomous=True):
    return next(item for item in await sup.server.catalog(scope, autonomous=autonomous) if item.capability_id == capability)


def test_routine_execute_is_evidenced_and_replayed(tmp_path):
    async def run():
        sup, fake = rig(tmp_path)
        try:
            selected = await offer(sup)
            result = await sup.server.execute("first", selected, {"command": "mkdir /tmp/misty-goal"},
                                              deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            assert result.status == "completed" and result.evidence_refs
            assert fake.runs[0].command == "mkdir /tmp/misty-goal"
            replay = await sup.server.execute("first", selected, {"command": "mkdir /tmp/misty-goal"},
                                              deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            assert replay == result and len(fake.runs) == 1
            with pytest.raises(ServerError, match="server_call_conflict"):
                await sup.server.execute("first", selected, {"command": "uptime"},
                                         deadline=utc_now() + timedelta(seconds=10), autonomous=True)
        finally:
            await sup.server.close()
    asyncio.run(run())


def test_observe_rejects_non_readonly_commands(tmp_path):
    async def run():
        sup, fake = rig(tmp_path)
        try:
            selected = await offer(sup, OBSERVE)
            with pytest.raises(ServerError, match="server_observe_not_read_only"):
                await sup.server.execute("obs", selected, {"command": "rm /tmp/x"},
                                         deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            assert fake.runs == []
        finally:
            await sup.server.close()
    asyncio.run(run())


def test_high_risk_requires_approval_then_executes(tmp_path):
    async def run():
        sup, fake = rig(tmp_path)
        try:
            selected = await offer(sup)
            pending = await sup.server.execute("danger", selected, {"command": "rm /tmp/misty-goal"},
                                               deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            assert pending.status == "pending_approval" and fake.runs == []
            assert sup.server.calls.command("danger")["status"] == "pending_approval"
            result = await sup.server.approve("danger")
            assert result.status == "completed" and result.evidence_refs
            assert fake.runs[0].command == "rm /tmp/misty-goal"
        finally:
            await sup.server.close()
    asyncio.run(run())


def test_approval_timeout_cancels_without_running(tmp_path, monkeypatch):
    async def run():
        sup, fake = rig(tmp_path, approval_timeout=60)
        try:
            selected = await offer(sup)
            await sup.server.execute("late", selected, {"command": "rm /tmp/x"},
                                     deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            monkeypatch.setattr("harness.runtime.server.utc_now", lambda: utc_now() + timedelta(seconds=120))
            await sup.server.expire_pending_approvals()
            assert sup.server.calls.command("late")["status"] == "cancelled"
            with pytest.raises(ServerError):
                await sup.server.approve("late")
            assert fake.runs == []
        finally:
            await sup.server.close()
    asyncio.run(run())


def test_cancelled_run_is_uncertain_and_never_replayed(tmp_path):
    async def run():
        started, release = asyncio.Event(), asyncio.Event()

        class Blocked(FakeProvider):
            async def run(self, request, *, deadline=None, capability=None):
                self.runs.append(request)
                started.set()
                await release.wait()
                return await super().run(request, deadline=deadline, capability=capability)

        sup, fake = rig(tmp_path, provider=Blocked())
        item = await offer(sup)
        task = asyncio.create_task(sup.server.execute(
            "cancelled", item, {"command": "mkdir /tmp/x"},
            deadline=utc_now() + timedelta(seconds=10), autonomous=True))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert sup.server.calls.command("cancelled")["status"] == "uncertain"
        await sup.server.close()
        restored, _ = rig(tmp_path)
        try:
            with pytest.raises(ServerError):
                await restored.server.execute("cancelled", await offer(restored), {"command": "mkdir /tmp/x"},
                                              deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            assert restored.server.calls.command("cancelled")["status"] == "uncertain"
        finally:
            await restored.server.close()
    asyncio.run(run())


def test_quota_counts_pending_and_survives_restart(tmp_path):
    async def run():
        sup, fake = rig(tmp_path, quota=1)
        item = await offer(sup)
        await sup.server.execute("one", item, {"command": "rm /tmp/a"},
                                 deadline=utc_now() + timedelta(seconds=10), autonomous=True)
        with pytest.raises(ServerError, match="server_budget_exhausted"):
            await sup.server.execute("two", item, {"command": "uptime"},
                                     deadline=utc_now() + timedelta(seconds=10), autonomous=True)
        await sup.server.close()
        restored, _ = rig(tmp_path, quota=1)
        try:
            assert not (await offer(restored)).ready
            assert (await restored.server.status())["usage"]["commands"] == 1
        finally:
            await restored.server.close()
    asyncio.run(run())


def test_guard_failure_cancels_pending_without_running(tmp_path):
    async def run():
        sup, fake = rig(tmp_path)
        try:
            selected = await offer(sup)
            pending = await sup.server.execute("goal-cancel", selected, {"command": "rm /tmp/x"},
                                               deadline=utc_now() + timedelta(seconds=10), autonomous=True)
            assert pending.status == "pending_approval"
            with pytest.raises(ServerError, match="server_call_superseded"):
                await sup.server.approve("goal-cancel", guard=lambda: True)
            assert fake.runs == []
            assert sup.server.calls.command("goal-cancel")["status"] == "cancelled"
        finally:
            await sup.server.close()
    asyncio.run(run())


def test_goal_server_observe_marks_step_executed(tmp_path, monkeypatch):
    import json
    from harness.llm.client import ChatResult
    from tests.test_goal_runtime import GoalModel, drain_effects, goal_rig, next_goal_event, submit

    class ServerGoalModel(GoalModel):
        def complete_chat_result(self, messages, **kwargs):
            result = super().complete_chat_result(messages, **kwargs)
            payload = json.loads(messages[-1]["content"])
            if "goal_context" not in payload:
                return result
            data = json.loads(result.text)
            offer_id = data.get("choice", {}).get("offer_id") or ""
            if offer_id.startswith("server:"):
                observe = offer_id.endswith("observe.v1")
                data["choice"]["kind"] = "observe" if observe else "act"
                data["choice"]["arguments"] = {"command": "uptime" if observe else "mkdir /tmp/misty-goal"}
                result = ChatResult(text=json.dumps(data), usage=result.usage)
            return result

    async def scenario():
        async with goal_rig(tmp_path, monkeypatch, activities=("server.observe.v1",)) as (
                sup, host, engine, model, clock, requests, _):
            fake = FakeProvider(stdout=" 19:00:01 up 3 days\n")
            options = sup.capabilities.settings.model_dump()
            options["owners"]["server"] = "shared"
            options["allowed_capabilities"] = (*options["allowed_capabilities"], OBSERVE, EXECUTE)
            sup.manifest = sup.manifest.model_copy(update={
                "server_access": ServerAccessCondition(enabled=True),
                "autonomous_capabilities": options,
            })
            sup.server = ServerController(sup, ServerSettings(), tmp_path / "runtime", provider=fake)
            host.chat.llm = ServerGoalModel(("server.observe.v1",))
            await submit(sup)
            event = await next_goal_event(sup)
            result = await sup.dispatch(event)
            assert result.status == "completed", (result.detail, sup.capabilities.calls.all())
            await drain_effects(sup)
            episode = sup.goals.snapshot()["active_episode"]
            assert episode["steps"][0]["status"] == "executed", episode
            assert episode["results"][0]["evidence_refs"]
            assert fake.runs and fake.runs[0].command == "uptime"

    asyncio.run(scenario())


def test_goal_high_risk_waits_for_approval(tmp_path, monkeypatch):
    import json
    from harness.llm.client import ChatResult
    from tests.test_goal_runtime import GoalModel, goal_rig, next_goal_event, submit

    class ServerGoalModel(GoalModel):
        def complete_chat_result(self, messages, **kwargs):
            result = super().complete_chat_result(messages, **kwargs)
            payload = json.loads(messages[-1]["content"])
            if "goal_context" not in payload:
                return result
            data = json.loads(result.text)
            if (data.get("choice", {}).get("offer_id") or "").endswith("execute.v1"):
                data["choice"]["kind"] = "act"
                data["choice"]["arguments"] = {"command": "rm /tmp/misty-goal"}
                result = ChatResult(text=json.dumps(data), usage=result.usage)
            return result

    async def scenario():
        async with goal_rig(tmp_path, monkeypatch, activities=("server.execute.v1",)) as (
                sup, host, engine, model, clock, requests, _):
            fake = FakeProvider()
            options = sup.capabilities.settings.model_dump()
            options["owners"]["server"] = "shared"
            options["allowed_capabilities"] = (*options["allowed_capabilities"], OBSERVE, EXECUTE)
            sup.manifest = sup.manifest.model_copy(update={
                "server_access": ServerAccessCondition(enabled=True),
                "autonomous_capabilities": options,
            })
            sup.server = ServerController(sup, ServerSettings(), tmp_path / "runtime", provider=fake)
            host.chat.llm = ServerGoalModel(("server.execute.v1",))
            await submit(sup)
            event = await next_goal_event(sup)
            result = await sup.dispatch(event)
            assert result.status == "completed", result.detail
            row = next(item for item in sup.capabilities.calls.all() if item.get("channel") == "server")
            assert row["status"] == "pending"
            assert fake.runs == []
            command_id = row["server_call_id"]
            await sup.server.approve(command_id, guard=lambda: sup.capabilities._server_guard(row))
            await sup.capabilities.reconcile()
            episode = sup.goals.snapshot()["active_episode"]
            assert episode["steps"][0]["status"] == "executed", episode
            assert fake.runs[0].command == "rm /tmp/misty-goal"

    asyncio.run(scenario())
