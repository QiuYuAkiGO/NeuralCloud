"""Actual board/diary effects publish one durable cost before memory commit."""
from __future__ import annotations

import asyncio
from datetime import timedelta
from types import SimpleNamespace

import pytest

from harness.production.config import ProductionConfig
from harness.production.runtime import ProductionRuntime
from harness.production.tool_accounting import ToolAccounting
from harness.research.manifest import AutonomousCapabilitiesCondition
from harness.runtime.capabilities import CapabilityController
from harness.runtime.models import RuntimeLifecycle, utc_now
from tests.test_board_runtime import rig as board_rig  # noqa: F401
from tests.test_diary_controller import rig as diary_rig  # noqa: F401


def attach_production(supervisor, tmp_path):
    supervisor.manifest.production = ProductionConfig()
    supervisor.manifest.embodiment = SimpleNamespace(body_instance_id="physiology")
    supervisor.state_dir = tmp_path / "production"
    supervisor.embodiment = SimpleNamespace(provider=None)
    supervisor.runtime_instance_id = "runtime-test"
    production = supervisor.production_runtime = ProductionRuntime(supervisor)
    production._body = {"body_instance_id": "physiology", "episode_id": "episode-test",
        "captured_at": utc_now().isoformat(), "resting": False, "exhausted": False}
    production._body_error = None
    return production


def assert_one_cost(production, channel, operation_id, outcome):
    op = production.tool_accounting.get(channel, operation_id)
    assert op["status"] == "finished" and op["outcome"] == outcome
    usage = production.store.pending_usage()
    assert len(usage) == 1
    assert usage[0]["usage"]["source"] == "tool"
    assert usage[0]["usage"]["operation_id"] == operation_id
    assert usage[0]["usage"]["episode_id"] == "episode-test"
    assert "text" not in usage[0]["usage"] and "body" not in usage[0]["usage"]
    assert production.store.budget()["used"] == 0


@pytest.mark.parametrize("commit_failure", [False, True])
def test_board_success_is_billed_before_memory_commit_and_replay(board_rig, tmp_path, commit_failure):  # noqa: F811
    async def run():
        board, supervisor, store, _ = board_rig
        production = attach_production(supervisor, tmp_path)
        supervisor.cognitive.commit_failures = int(commit_failure)
        accepted = await board.request({"request_id": "billed-write"})
        event = supervisor.events[-1]
        result = await board.dispatch(event, 1)
        assert result.detail["status"] == ("written_pending_memory" if commit_failure else "committed")
        assert_one_cost(production, "board", accepted["entry_id"], "completed")
        await board.dispatch(event, 2)
        assert_one_cost(production, "board", accepted["entry_id"], "completed")
        assert store._db.execute("SELECT count(*) FROM board_entries").fetchone()[0] == 1
        # Diagnostic/catalog-like reads never introduce an explicit tool cost.
        await board.snapshot()
        assert len(production.store.pending_usage()) == 1
    asyncio.run(run())


def test_board_unknown_is_billed_once_and_reconciliation_updates_only_outcome(board_rig, tmp_path, monkeypatch):  # noqa: F811
    async def run():
        board, supervisor, _, driver = board_rig
        production = attach_production(supervisor, tmp_path)
        append, reconcile = driver.append, driver.reconcile
        async def lost_receipt(request):
            await append(request)
            raise TimeoutError("synthetic lost receipt")
        async def unavailable(request):
            raise TimeoutError("synthetic unavailable reconciliation")
        monkeypatch.setattr(driver, "append", lost_receipt)
        monkeypatch.setattr(driver, "reconcile", unavailable)
        accepted = await board.request({"request_id": "unknown-write"})
        event = supervisor.events[-1]
        assert (await board.dispatch(event, 1)).detail["status"] == "uncertain"
        assert_one_cost(production, "board", accepted["entry_id"], "unknown")
        original_usage = production.store.pending_usage()
        production.tool_accounting = ToolAccounting(production.store)
        monkeypatch.setattr(driver, "reconcile", reconcile)
        assert (await board.dispatch(event, 2)).detail["status"] == "committed"
        assert_one_cost(production, "board", accepted["entry_id"], "completed")
        assert production.store.pending_usage() == original_usage
    asyncio.run(run())


@pytest.mark.parametrize("change", ["pause", "rest", "exhausted", "disconnect", "epoch"])
def test_board_final_provider_guard_rejects_without_charge(board_rig, tmp_path, monkeypatch, change):  # noqa: F811
    async def run():
        board, supervisor, store, _ = board_rig
        production = attach_production(supervisor, tmp_path)
        guard = board.provider._guard
        async def changing(request, capability):
            await guard(request, capability)
            if capability != "expression.board.append.v1":
                return
            if change == "pause":
                board.turns.pause(True)
            elif change == "rest":
                production._body["resting"] = True
            elif change == "exhausted":
                production._body["exhausted"] = True
            elif change == "disconnect":
                production._body_error = "unavailable"
            else:
                production.epoch += 1
        monkeypatch.setattr(board.provider, "_guard", changing)
        accepted = await board.request({"request_id": "cancelled-write"})
        assert (await board.dispatch(supervisor.events[-1], 1)).detail["status"] == "superseded"
        assert production.tool_accounting.get("board", accepted["entry_id"])["status"] == "cancelled"
        assert not production.store.pending_usage()
        assert store._db.execute("SELECT count(*) FROM board_entries").fetchone()[0] == 0
    asyncio.run(run())


@pytest.mark.parametrize("cancel", [False, True])
def test_explicit_board_read_has_own_operation_and_late_guard(board_rig, tmp_path, monkeypatch, cancel):  # noqa: F811
    async def run():
        board, supervisor, _, _ = board_rig
        production = attach_production(supervisor, tmp_path)
        supervisor.manifest.autonomous_capabilities = AutonomousCapabilitiesCondition(enabled=True,
            owners={"board": "shared"}, allowed_capabilities=("expression.board.read.v1",))
        controller = supervisor.capabilities = CapabilityController(supervisor, tmp_path / "capabilities")
        await board.ready()
        row = {"opportunity_id": "explicit-read", "generation": controller.generation,
            "deadline_at": (utc_now() + timedelta(seconds=30)).isoformat(),
            "runtime_instance_id": supervisor.runtime_instance_id, "activity_revision": 0}
        offer = SimpleNamespace(capability_id="expression.board.read.v1", valid_until=utc_now() + timedelta(seconds=30))
        if cancel:
            guard = board.provider._guard
            async def changing(request, capability):
                await guard(request, capability)
                supervisor.lifecycle = RuntimeLifecycle.PAUSED
            monkeypatch.setattr(board.provider, "_guard", changing)
            with pytest.raises(Exception, match="board_pre_effect_cancelled"):
                await controller.dispatch_board(row, None, offer)
            assert not production.store.pending_usage()
            assert production.tool_accounting.get("board", "explicit-read")["status"] == "cancelled"
        else:
            await controller.dispatch_board(row, None, offer)
            assert row["status"] == "completed" and row["evidence_refs"]
            assert_one_cost(production, "board", "explicit-read", "completed")
        controller.calls.db.close()
    asyncio.run(run())


@pytest.mark.parametrize("commit_failure", [False, True])
def test_diary_write_cost_survives_commit_failure_and_replay(diary_rig, tmp_path, monkeypatch, commit_failure):  # noqa: F811
    async def run():
        diary, supervisor = diary_rig
        production = attach_production(supervisor, tmp_path)
        if commit_failure:
            async def fail(command):
                raise RuntimeError("synthetic memory unavailable")
            monkeypatch.setattr(supervisor.cognitive, "commit_diary_entry", fail)
        accepted = await diary.request({"request_id": "billed-diary"})
        event = supervisor.events[-1]
        result = await diary.dispatch(event, 1)
        assert result.detail["status"] == ("error" if commit_failure else "committed")
        assert diary.store.get(accepted["entry_id"]) is not None
        assert_one_cost(production, "diary", accepted["entry_id"], "completed")
        await diary.dispatch(event, 2)
        assert_one_cost(production, "diary", accepted["entry_id"], "completed")
    asyncio.run(run())


@pytest.mark.parametrize("change", ["pause", "rest", "epoch"])
def test_diary_rechecks_after_accounting_begin_without_charging(diary_rig, tmp_path, monkeypatch, change):  # noqa: F811
    async def run():
        diary, supervisor = diary_rig
        production = attach_production(supervisor, tmp_path)
        begin = production.begin_tool_operation
        async def changing(*args):
            await begin(*args)
            if change == "pause":
                diary.store.pause(True)
            elif change == "rest":
                production._body["resting"] = True
            else:
                production.epoch += 1
        monkeypatch.setattr(production, "begin_tool_operation", changing)
        accepted = await diary.request({"request_id": "cancelled-diary"})
        result = await diary.dispatch(supervisor.events[-1], 1)
        assert result.detail["status"] in {"paused", "superseded"}
        assert diary.store.get(accepted["entry_id"]) is None
        assert not production.store.pending_usage()
        assert production.tool_accounting.get("diary", accepted["entry_id"])["status"] == "cancelled"
    asyncio.run(run())
