"""Shared capability opportunities through real drift, cognition and executors."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from datetime import timedelta
import json
from pathlib import Path
import time

import httpx
import pytest

from harness.board.config import BoardSettings
from harness.board.service import create_app
from harness.board.store import BoardStore
from harness.embodiment.capabilities import CapabilityRequirement
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.board import BOARD_CAPABILITIES, BoardClient, BoardProvider
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.llm.client import MockLLM
from harness.memory.shared import SharedReadScope
from harness.research.manifest import AutonomousCapabilitiesCondition, EmbodimentCondition, load_experiment_manifest
from harness.runtime.action import ActionCoordinator
from harness.runtime.board import BoardController
from harness.runtime.capabilities import CapabilityCalls
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, utc_now
from harness.runtime.perception import PerceptionGuard
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_basic_persona_continuity import _host
from tests.test_basic_terminal_runtime import Desktop, APP


@asynccontextmanager
async def rig(tmp_path, monkeypatch, answers, *, desktop=False):
    token_path = tmp_path / "body.token"
    token_path.write_text("synthetic-private-board-token-for-isolated-tests")
    settings = BoardSettings(token_file=token_path)
    config_path = tmp_path / "board.json"
    config_path.write_text(settings.model_dump_json(), encoding="utf-8")
    monkeypatch.setenv("NC_BOARD_CONFIG", str(config_path))
    monkeypatch.delenv("NC_QQ_SESSION_CONFIG", raising=False)
    monkeypatch.delenv("NC_SHARED_MEMORY_DB", raising=False)
    monkeypatch.setattr("gui.service.probe_llm", lambda *a, **k: True)
    host, _ = _host(tmp_path / "owner", monkeypatch, ["unused"])
    original_llm, llm = host.chat.llm, MockLLM(chat_script=[json.dumps(answer) for answer in answers])
    host.chat.llm = llm
    started = utc_now() - timedelta(hours=8)
    profile = load_profile("presets/homeostasis/qq-social-drive-v1.json")
    engine = HomeostasisEngine(profile, state_dir=tmp_path / "homeostasis", now=started, clock=utc_now)
    base = load_experiment_manifest(Path("presets/runtime/shared-misty-v1.json"))
    changes = {"autonomous_capabilities": AutonomousCapabilitiesCondition(enabled=True,
        owners={"board": "off" if desktop else "shared", "qq": "off", "desktop": "shared" if desktop else "off"},
        allowed_capabilities=("app.open.v1", "ui.type_text.v1", "control.observe.v1") if desktop else
            ("expression.board.read.v1", "expression.board.append.v1"))}
    desktop_body, kwargs = None, {}
    if desktop:
        changes["embodiment"] = EmbodimentCondition(mode="observe", body_provider="mistyai", body_instance_id="sim-body-01",
            capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in (
                "action.execute.v1", "action.verify.v1", "observation.snapshot.v1", "evidence.reference.v1")])
    manifest = base.model_copy(update=changes)
    if desktop:
        desktop_body = Desktop()
        manager = EmbodimentManager(manifest.embodiment, desktop_body)
        kwargs = dict(embodiment_manager=manager, action_coordinator=ActionCoordinator(desktop_body, capability_check=manager.supports))
    supervisor = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=engine, state_dir=tmp_path / "runtime", **kwargs)
    if desktop:
        supervisor.attach_perception_guard(PerceptionGuard(desktop_body, supervisor.submit_event, interval_seconds=60))
    store = BoardStore(tmp_path / "body" / "board.sqlite3", settings.scope)
    app = create_app(store=store, token=token_path.read_text())
    requests = []

    @app.middleware("http")
    async def record(request, call_next):
        requests.append((request.method, request.url.path))
        return await call_next(request)

    monkeypatch.setattr(BoardSettings, "client", lambda self: BoardClient(self.base_url, token_path.read_text(), transport=httpx.ASGITransport(app=app)))
    provider = BoardProvider(EmbodimentCondition(mode="grounded", body_instance_id=settings.body_instance_id,
        capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES]), settings.client(), settings.scope)
    supervisor.board = BoardController(supervisor, settings, tmp_path / "runtime", provider=provider)
    try:
        async with app.router.lifespan_context(app):
            await supervisor.boot_and_recover()
            supervisor.scheduler.suspend()
            if supervisor._perception_guard:
                await supervisor._perception_guard.stop()
            while (queued := await supervisor.queue.get(timeout=0)) is not None:
                await supervisor.dispatch(queued)
            assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
            supervisor.capabilities.last_activity = time.monotonic() - 61
            supervisor.board.last_activity = time.monotonic() - 61
            supervisor.board.next_opportunity = 0
            initial_social = engine.snapshot.dimensions["social_connection"].value
            for step in range(1, 241):
                event = await supervisor.submit_event(event_type=RuntimeEventType.HEARTBEAT, priority=RuntimeEventPriority.HEARTBEAT,
                    source="isolated-natural-elapsed-replay", source_time=started + timedelta(seconds=step * profile.max_heartbeat_step_seconds))
                assert (await supervisor.queue.get()).event_id == event.event_id
                result = await supervisor.dispatch(event)
                assert result.status == "completed", result.detail
                if supervisor._response_homeostatic_interoception()["salience"] != "nominal":
                    break
            assert step > 1
            assert engine.snapshot.dimensions["social_connection"].value < initial_social
            assert supervisor.queue.qsize() == 1, supervisor.capabilities.last_error
            event = await supervisor.queue.get()
            assert event.event_type == RuntimeEventType.CAPABILITY_OPPORTUNITY
            assert not supervisor.board.turns.all()  # shared disables old selection
            yield supervisor, host, engine, llm, event, requests, desktop_body
    finally:
        host.chat.llm = original_llm
        await supervisor.stop()
        await supervisor.board.close()


@pytest.mark.parametrize("choice,body_action", [("act", "write"), ("act", "wait"), ("observe", None), ("wait", None), ("defer", None), ("no_action", None)])
def test_natural_drift_selects_one_channel_and_replay_has_no_effect(tmp_path, monkeypatch, choice, body_action):
    async def run():
        answer = {"kind": choice}
        if choice in {"act", "observe"}:
            answer["offer_id"] = "board:expression.board." + ("append.v1" if choice == "act" else "read.v1")
        answers = [answer]
        if body_action:
            answers.append({"action": body_action, "title": "", "text": "记下一点此刻的想法。" if body_action == "write" else ""})
        async with rig(tmp_path, monkeypatch, answers) as (sup, host, engine, llm, event, requests, _):
            revision = engine.snapshot.revision
            result = await sup.dispatch(event)
            assert result.status == "completed", (result.detail, sup.capabilities.last_error)
            row = sup.capabilities.calls.get(event.payload["opportunity_id"])
            if body_action:
                assert row["status"] == "pending", row
                board_event = await sup.queue.get(timeout=0)
                assert board_event.event_type == RuntimeEventType.BOARD_REFLECTION
                board_result = await sup.dispatch(board_event)
                assert board_result.status == "completed", board_result.detail
                await sup.capabilities.reconcile()
            row = sup.capabilities.calls.get(row["opportunity_id"])
            assert row["status"] == ({"write": "committed", "wait": "wait"}.get(body_action) or ("completed" if choice == "observe" else choice)), row
            assert engine.snapshot.revision == revision
            memories = host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",)))
            assert len(memories.records) == (body_action == "write")
            assert requests.count(("POST", "/v1/board/append")) == (body_action == "write")
            assert len(llm.chat_calls) == len(answers)
            if body_action:
                turn = sup.board.turns.get(row["entry_id"])
                assert turn["deadline_at"] == row["deadline_at"]
            if body_action == "write":
                # Simulate the process dying after the channel committed, but
                # before its completion reached the central opportunity log.
                row.update(status="pending", deadline_at=(utc_now() - timedelta(seconds=1)).isoformat(),
                    runtime_instance_id="previous-runtime")
                sup.capabilities.calls.put(row)
            await sup.capabilities.dispatch(event, 999)
            if body_action == "write":
                assert sup.capabilities.calls.get(row["opportunity_id"])["status"] == "committed"
            await sup.capabilities.heartbeat()
            assert sup.queue.empty()
            assert len(llm.chat_calls) == len(answers)
    asyncio.run(run())


@pytest.mark.parametrize("invalidation", ["owner", "disabled", "user", "deadline", "restart"])
def test_queued_call_invalidates_before_model(tmp_path, monkeypatch, invalidation):
    async def run():
        async with rig(tmp_path, monkeypatch, [{"kind": "wait"}]) as (sup, _, _, llm, event, _, _):
            row = sup.capabilities.calls.get(event.payload["opportunity_id"])
            if invalidation in {"owner", "disabled"}:
                config = sup.capabilities.settings.model_dump()
                if invalidation == "owner":
                    config["owners"]["board"] = "legacy"
                else:
                    config["enabled"] = False
                sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities": config})
            elif invalidation == "user":
                await sup.submit_user_message("先停一下")
            elif invalidation == "deadline":
                row["deadline_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
                sup.capabilities.calls.put(row)
            else:
                sup.runtime_instance_id = "other-runtime"
            result = await sup.dispatch(event)
            assert result.detail["status"] in {"expired", "superseded"}
            assert not llm.chat_calls
            assert not sup.board.turns.all()
            # Do not process the synthetic user input in teardown.
            while await sup.queue.get(timeout=0) is not None:
                pass
    asyncio.run(run())


def test_desktop_actual_execution_verifies_and_does_not_reward_success(tmp_path, monkeypatch):
    async def run():
        answer = {"kind": "act", "offer_id": "desktop:app.open.v1", "arguments": {"app_id": APP},
            "success_criteria": [{"kind": "window", "app_id": APP, "condition": "present_and_focused"}]}
        async with rig(tmp_path, monkeypatch, [answer], desktop=True) as (sup, host, engine, _, event, _, desktop):
            competence = engine.snapshot.dimensions["competence"].value
            safety = engine.snapshot.dimensions["safety_predictability"].value
            result = await sup.dispatch(event)
            assert result.status == "completed", result.detail
            row = sup.capabilities.calls.get(event.payload["opportunity_id"])
            assert row["status"] == "pending", row
            await asyncio.gather(*list(sup._action_tasks.values()))
            while (queued := await sup.queue.get(timeout=0)) is not None:
                result = await sup.dispatch(queued)
                assert result.status == "completed", result.detail
            await sup.capabilities.reconcile()
            row = sup.capabilities.calls.get(row["opportunity_id"])
            assert row["status"] == "completed", row
            assert desktop.executed == desktop.verified == ["app.open.v1"]
            assert row["outcome"]["evidence_refs"]
            assert not any(record.data.get("envelope", {}).get("command_type") == "ApplyOutcome" for record in sup.journal.iter_records())
            assert engine.snapshot.dimensions["competence"].value <= competence
            assert engine.snapshot.dimensions["safety_predictability"].value <= safety
            assert not any(host.chat.pipeline.session_store.snapshot().values())
    asyncio.run(run())


def test_configuration_generation_survives_process_reopen(tmp_path):
    calls = CapabilityCalls(tmp_path / "calls.sqlite3")
    assert calls.configure("legacy") == 1
    calls.db.close()
    calls = CapabilityCalls(tmp_path / "calls.sqlite3")
    assert calls.configure("legacy") == 1
    assert calls.configure("shared") == 2
    assert calls.configure("legacy") == 3
    calls.db.close()


@pytest.mark.parametrize("change", ["user", "owner", "capability"])
def test_body_dispatch_rechecks_changes_during_cognition(tmp_path, monkeypatch, change):
    async def run():
        answers = [{"kind": "act", "offer_id": "board:expression.board.append.v1"}]
        async with rig(tmp_path, monkeypatch, answers) as (sup, _, _, llm, event, requests, _):
            prepare = sup.cognitive.prepare_capability_call

            async def changing(command):
                response = await prepare(command)
                if change == "user":
                    await sup.submit_user_message("先等等")
                else:
                    config = sup.capabilities.settings.model_dump()
                    if change == "owner":
                        config["owners"]["board"] = "off"
                    else:
                        config["allowed_capabilities"] = ("expression.board.read.v1",)
                    sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities": config})
                return response

            monkeypatch.setattr(sup.cognitive, "prepare_capability_call", changing)
            result = await sup.dispatch(event)
            assert result.detail["status"] == "superseded"
            assert len(llm.chat_calls) == 1
            assert not sup.board.turns.all()
            assert ("POST", "/v1/board/append") not in requests
            while await sup.queue.get(timeout=0) is not None:
                pass
    asyncio.run(run())


def test_board_pending_cancel_and_legacy_generation_switch(tmp_path, monkeypatch):
    async def run():
        async with rig(tmp_path, monkeypatch, [{"kind": "act", "offer_id": "board:expression.board.append.v1"}]) as (sup, _, _, llm, event, requests, _):
            assert (await sup.dispatch(event)).detail["status"] == "pending"
            board_event = await sup.queue.get(timeout=0)
            await sup.submit_user_message("暂时别写")
            result = await sup.dispatch(board_event)
            assert result.detail["status"] == "superseded"
            await sup.capabilities.reconcile()
            assert sup.capabilities.calls.get(event.payload["opportunity_id"])["status"] == "superseded"
            assert len(llm.chat_calls) == 1
            assert ("POST", "/v1/board/append") not in requests
            while await sup.queue.get(timeout=0) is not None:
                pass
            config = sup.capabilities.settings.model_dump()
            config["owners"]["board"] = "legacy"
            sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities":config})
            await sup.board.request({"request_id":"old-generation"}, trigger="homeostasis")
            legacy_event = await sup.queue.get(timeout=0)
            for owner in ("shared", "legacy"):
                config["owners"]["board"] = owner
                sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities":config})
                assert sup.capabilities.owner("board") == owner
            assert (await sup.dispatch(legacy_event)).detail["status"] == "superseded"
            assert len(llm.chat_calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize("change", ["user", "disabled", "deadline"])
def test_final_desktop_guard_blocks_changes_after_reservation(tmp_path, monkeypatch, change):
    async def run():
        answer = {"kind":"act", "offer_id":"desktop:app.open.v1", "arguments":{"app_id":APP},
            "success_criteria":[{"kind":"window", "app_id":APP, "condition":"present_and_focused"}]}
        async with rig(tmp_path, monkeypatch, [answer], desktop=True) as (sup, _, _, _, event, _, desktop):
            await sup._foreground_lock.acquire()
            try:
                result = await sup.dispatch(event)
                assert result.detail["status"] == "pending", result.detail
                if change == "user":
                    await sup.submit_user_message("停止当前动作")
                elif change == "disabled":
                    config = sup.capabilities.settings.model_dump()
                    config["enabled"] = False
                    sup.manifest = sup.manifest.model_copy(update={"autonomous_capabilities":config})
                else:
                    row = sup.capabilities.calls.get(event.payload["opportunity_id"])
                    row["deadline_at"] = (utc_now() - timedelta(seconds=1)).isoformat()
                    sup.capabilities.calls.put(row)
            finally:
                sup._foreground_lock.release()
            await asyncio.gather(*list(sup._action_tasks.values()))
            assert not desktop.executed
            while (queued := await sup.queue.get(timeout=0)) is not None:
                if queued.event_type == RuntimeEventType.ACTION_RESULT:
                    assert (await sup.dispatch(queued)).status == "completed"
            await sup.capabilities.reconcile()
            assert sup.capabilities.calls.get(event.payload["opportunity_id"])["status"] == "rejected"
    asyncio.run(run())
