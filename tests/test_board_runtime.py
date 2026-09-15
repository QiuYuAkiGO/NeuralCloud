"""Runtime queue policy against the real negotiated SQLite body, no live model."""
from __future__ import annotations

import asyncio
from datetime import timedelta
import hashlib
import json
import time
from types import SimpleNamespace

import httpx
import pytest

from harness.board.config import BoardSettings
from harness.board.service import LocalBoardDriver
from harness.board.store import BoardStore
from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
from harness.embodiment.capabilities import CapabilityRequirement
from harness.embodiment.providers.board import BOARD_CAPABILITIES, BoardProvider
from harness.research.manifest import EmbodimentCondition
from harness.runtime.board import BoardController
from harness.runtime.control import ControlPlane, create_control_app
from harness.runtime.models import RuntimeEvent, RuntimeEventType, RuntimeLifecycle, utc_now


class FakeCognitive:
    def __init__(self):
        self.revision = 0
        self.action = "write"
        self.prepare_calls = []
        self.commit_calls = []
        self.prepare_hook = None
        self.commit_failures = 0

    async def get_snapshot(self):
        return SimpleNamespace(revision=self.revision)

    def result(self, command, payload):
        return DomainCommandResult(command_id=command.command_id, command_type=command.command_type,
            status="applied", request_hash=command.effective_request_hash(), committed_revision=self.revision,
            acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
            result=payload, completed_at=utc_now())

    async def prepare_board_entry(self, command):
        self.prepare_calls.append(command)
        if self.prepare_hook:
            await self.prepare_hook()
        payload = command.payload
        return self.result(command, {"decision": {
            "entry_id": payload["entry_id"], "decision_id": "board-decision:" + hashlib.sha256(command.command_id.encode()).hexdigest(),
            "body_instance_id": payload["body_instance_id"], "board_id": payload["board_id"],
            "action": self.action, "text": "雨声让我想起一些轻轻敲下来的句子。" if self.action == "write" else "",
            "title": "雨声" if self.action == "write" else "", "source_event_ids": [command.event_id],
            "prepared_at": utc_now().isoformat(), "deadline_at": payload["deadline_at"]},
            "timing": {"model_calls": 1, "model_ms": 10, "total_ms": 12}})

    async def commit_board_entry(self, command):
        self.commit_calls.append(command)
        if self.commit_failures:
            self.commit_failures -= 1
            raise RuntimeError("synthetic memory unavailable")
        self.revision += 1
        return self.result(command, {"memory_created": True})


class FakeSupervisor:
    def __init__(self):
        self.lifecycle = RuntimeLifecycle.RUNNING
        self.cognitive = FakeCognitive()
        self._cognitive_snapshot = SimpleNamespace(revision=0)
        self.listeners = []
        self.events = []
        self.queue = SimpleNamespace(empty=lambda: True)
        self.manifest = SimpleNamespace(control_mode="study_runtime")

    def add_listener(self, listener):
        self.listeners.append(listener)

    async def submit_event(self, **kwargs):
        now = utc_now()
        kwargs.setdefault("event_id", f"runtime-event:{len(self.events)+1}")
        kwargs.setdefault("idempotency_key", kwargs["event_id"])
        event = RuntimeEvent(**kwargs, receive_seq=len(self.events)+1, correlation_id=kwargs["event_id"],
            source_time=now, received_at=now, manifest_revision=1)
        self.events.append(event)
        for listener in self.listeners:
            await listener("event.accepted", event.model_dump(mode="json"))
        return event

    def _response_homeostatic_interoception(self):
        # Runtime's public semantic helper returns a JSON projection, not a DTO.
        return {"salience": "noticeable"}

    def _make_command(self, event, **kwargs):
        now = utc_now()
        return DomainCommandEnvelope(**kwargs, event_id=event.event_id, receive_seq=event.receive_seq,
            correlation_id=event.correlation_id, runtime_instance_id="test-runtime", manifest_revision=1,
            issued_at=now, deadline=now+timedelta(seconds=60))

    async def _invoke_domain(self, *, envelope, method, **kwargs):
        result = await method(envelope)
        if result.status not in {"applied", "idempotent_replay"}:
            raise RuntimeError("synthetic rejected domain command")
        return result

    def _validate_snapshot(self, snapshot, service):
        assert service == "cognitive" and snapshot.revision >= 0


@pytest.fixture
def rig(tmp_path, monkeypatch):
    settings = BoardSettings(token_file=tmp_path / "token")
    store = BoardStore(tmp_path / "linux" / "board.sqlite3", settings.scope)
    driver = LocalBoardDriver(store)
    monkeypatch.setattr(BoardSettings, "client", lambda self: driver)
    supervisor = FakeSupervisor()
    provider = BoardProvider(EmbodimentCondition(mode="grounded", body_instance_id=settings.body_instance_id,
        capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES]),
        driver, settings.scope)
    controller = BoardController(supervisor, settings, tmp_path / "runtime", provider=provider)
    supervisor.board = controller
    yield controller, supervisor, store, driver
    asyncio.run(controller.close())


async def first_turn(rig, **payload):
    controller, supervisor, _, _ = rig
    accepted = await controller.request({"request_id": "request-one", **payload})
    event = supervisor.events[-1]
    result = await controller.dispatch(event, 1)
    return accepted, event, result


@pytest.mark.parametrize("action", ["write", "wait"])
def test_real_negotiated_write_and_natural_wait_are_distinct(rig, action):
    async def run():
        controller, supervisor, store, _ = rig
        supervisor.cognitive.action = action
        accepted, event, result = await first_turn(rig)
        row = controller.turns.get(accepted["entry_id"])
        assert result.status == "completed" and row["status"] == ("committed" if action == "write" else "wait")
        assert len(supervisor.cognitive.prepare_calls) == 1
        assert len(supervisor.cognitive.commit_calls) == (1 if action == "write" else 0)
        # The Linux authority, rather than an in-memory send flag, proves writing.
        assert store._db.execute("SELECT count(*) FROM board_entries").fetchone()[0] == (1 if action == "write" else 0)
        prior = await controller.request({"request_id": "request-one"})
        assert prior["entry_id"] == accepted["entry_id"] and len(supervisor.events) == 1
        await controller.dispatch(event, 2)
        assert len(supervisor.cognitive.prepare_calls) == 1
    asyncio.run(run())


def test_model_exception_is_error_not_wait_and_never_appends(rig):
    async def run():
        controller, supervisor, _, _ = rig
        async def fail():
            raise TimeoutError("synthetic model network failure")
        supervisor.cognitive.prepare_hook = fail
        accepted, _, result = await first_turn(rig)
        row = controller.turns.get(accepted["entry_id"])
        assert result.status == "failed" and row["status"] == "error"
        assert row["error_type"] == "TimeoutError" and not supervisor.cognitive.commit_calls
        assert (await controller.snapshot())["entries"] == []
    asyncio.run(run())


@pytest.mark.parametrize("when", ["queued", "after_model"])
def test_expired_turn_never_appends(rig, monkeypatch, when):
    async def run():
        from harness.runtime import board
        controller, supervisor, _, _ = rig
        accepted = await controller.request({"request_id": "expire-one"})
        if when == "queued":
            row = controller.turns.get(accepted["entry_id"])
            row["deadline_at"] = (utc_now()-timedelta(seconds=1)).isoformat()
            controller.turns.put(row)
        else:
            async def expire():
                later = utc_now()+timedelta(seconds=61)
                monkeypatch.setattr(board, "utc_now", lambda: later)
            supervisor.cognitive.prepare_hook = expire
        result = await controller.dispatch(supervisor.events[-1], 1)
        assert result.detail["status"] == "expired"
        assert not supervisor.cognitive.commit_calls
        assert len(supervisor.cognitive.prepare_calls) == (0 if when == "queued" else 1)
    asyncio.run(run())


@pytest.mark.parametrize("event_type", ["user_message", "qq_turn", "qq_fact"])
def test_new_inbound_activity_supersedes_prepared_board_draft(rig, event_type):
    async def run():
        controller, supervisor, _, _ = rig
        async def incoming():
            await controller.on_event("event.accepted", {"event_type": event_type,
                "payload": {"qq_fact": {"kind": "verified_message"}}})
        supervisor.cognitive.prepare_hook = incoming
        _, _, result = await first_turn(rig)
        assert result.detail["status"] == "superseded"
        assert not supervisor.cognitive.commit_calls
        assert (await controller.snapshot())["entries"] == []
    asyncio.run(run())


@pytest.mark.parametrize("when", ["before_request", "queued", "after_model"])
def test_pause_blocks_new_writes_at_each_boundary(rig, when):
    async def run():
        controller, supervisor, _, _ = rig
        if when == "before_request":
            controller.turns.pause(True)
            with pytest.raises(ValueError, match="board_paused"):
                await controller.request({"request_id": "pause-one"})
            assert not supervisor.events
            return
        await controller.request({"request_id": "pause-one"})
        if when == "queued":
            controller.turns.pause(True)
        else:
            async def pause():
                controller.turns.pause(True)
            supervisor.cognitive.prepare_hook = pause
        result = await controller.dispatch(supervisor.events[-1], 1)
        assert result.detail["status"] == "paused" and not supervisor.cognitive.commit_calls
        assert (await controller.snapshot())["entries"] == []
    asyncio.run(run())


def test_lost_append_response_reconciles_real_receipt_without_rewriting(rig, monkeypatch):
    async def run():
        controller, supervisor, _, driver = rig
        append = driver.append
        calls = []
        async def lost_reply(request):
            calls.append(request)
            await append(request)
            raise TimeoutError("connection lost after physical write")
        monkeypatch.setattr(driver, "append", lost_reply)
        _, _, result = await first_turn(rig)
        assert result.detail["status"] == "committed", result.detail
        assert len(calls) == 1 and len(supervisor.cognitive.commit_calls) == 1
        assert len((await controller.snapshot())["entries"]) == 1
    asyncio.run(run())


def test_unknown_append_pauses_further_writes_and_exhausts_bounded_readonly_recovery(rig, monkeypatch):
    async def run():
        controller, supervisor, _, driver = rig
        append_calls = []
        async def unknown(request):
            append_calls.append(request)
            raise TimeoutError("write status unknown")
        monkeypatch.setattr(driver, "append", unknown)
        accepted, _, result = await first_turn(rig)
        assert result.detail["status"] == "uncertain"
        with pytest.raises(ValueError, match="board_delivery_uncertain"):
            await controller.request({"request_id": "request-two"})
        for number in range(3):
            controller._recovery_next.clear()
            await controller.heartbeat()
            result = await controller.dispatch(supervisor.events[-1], number+2)
            assert result.detail["status"] == "uncertain"
        count = len(supervisor.events)
        controller._recovery_next.clear()
        await controller.heartbeat()
        assert len(supervisor.events) == count
        assert controller.turns.get(accepted["entry_id"])["recovery_attempts"] == 3
        assert len(append_calls) == 1 and not supervisor.cognitive.commit_calls
    asyncio.run(run())


def test_written_memory_failure_recovers_from_heartbeat_even_while_board_paused(rig):
    async def run():
        controller, supervisor, _, _ = rig
        supervisor.cognitive.commit_failures = 1
        accepted, _, result = await first_turn(rig)
        assert result.detail["status"] == "written_pending_memory"
        controller.turns.pause(True)
        controller._recovery_next.clear()
        await controller.heartbeat()
        recovery = supervisor.events[-1]
        assert recovery.source == "board_receipt_recovery" and recovery.payload["reconcile_only"]
        result = await controller.dispatch(recovery, 2)
        assert result.detail["status"] == "committed"
        assert len(supervisor.cognitive.prepare_calls) == 1 and len(supervisor.cognitive.commit_calls) == 2
        assert len((await controller.snapshot())["entries"]) == 1
        assert controller.turns.get(accepted["entry_id"])["recovery_attempts"] == 1
    asyncio.run(run())


def test_activation_errors_also_consume_recovery_attempt_budget(rig, monkeypatch):
    async def run():
        controller, supervisor, _, _ = rig
        supervisor.cognitive.commit_failures = 1
        accepted, _, _ = await first_turn(rig)
        async def unavailable():
            raise RuntimeError("negotiation offline")
        monkeypatch.setattr(controller, "ready", unavailable)
        for number in range(3):
            controller._recovery_next.clear()
            await controller.heartbeat()
            await controller.dispatch(supervisor.events[-1], number+2)
        count = len(supervisor.events)
        controller._recovery_next.clear()
        await controller.heartbeat()
        assert len(supervisor.events) == count
        assert controller.turns.get(accepted["entry_id"])["recovery_attempts"] == 3
    asyncio.run(run())


def test_dedicated_route_is_authenticated_and_generic_events_cannot_inject(rig, monkeypatch):
    async def run():
        _, supervisor, _, _ = rig
        monkeypatch.delenv("NC_QQ_SESSION_CONFIG", raising=False)
        app = create_control_app(ControlPlane(supervisor, token="synthetic-control-token"))
        async with httpx.AsyncClient(base_url="http://test.local", transport=httpx.ASGITransport(app=app)) as client:
            assert (await client.post("/v1/board/reflect", json={"request_id": "blocked"})).status_code == 401
            headers = {"Authorization": "Bearer synthetic-control-token"}
            response = await client.post("/v1/events", headers=headers, json={"event_type": RuntimeEventType.BOARD_REFLECTION,
                "payload": {"entry_id": "forged"}})
            assert response.status_code == 403 and response.json()["detail"] == "dedicated board ingress required"
            assert not supervisor.events
            assert (await client.post("/v1/board/pause", headers=headers)).json() == {"paused": True}
            assert (await client.post("/v1/board/reflect", headers=headers, json={"request_id": "paused"})).status_code == 409
            assert (await client.post("/v1/board/resume", headers=headers)).json() == {"paused": False}
    asyncio.run(run())


def test_homeostatic_opportunity_respects_quiet_queue_and_durable_cadence(rig):
    async def run():
        controller, supervisor, _, _ = rig
        supervisor.cognitive.action = "wait"
        controller.next_opportunity = 0
        await controller.heartbeat()
        assert not supervisor.events  # Still within the quiet interval.
        controller.last_activity = time.monotonic() - controller.settings.quiet_seconds - 1
        supervisor.queue.empty = lambda: False
        await controller.heartbeat()
        assert not supervisor.events
        supervisor.queue.empty = lambda: True
        await controller.heartbeat()
        assert len(supervisor.events) == 1 and supervisor.events[-1].source == "board_opportunity"
        row = controller.turns.get(supervisor.events[-1].payload["entry_id"])
        assert row["trigger"] == "homeostasis"
        result = await controller.dispatch(supervisor.events[-1], 1)
        assert result.detail["status"] == "wait"
        controller.next_opportunity = 0  # A process restart loses this in-memory deadline.
        await controller.heartbeat()
        assert len(supervisor.events) == 1  # Retained last-opportunity time still blocks.
    asyncio.run(run())


def test_restarted_controller_recovers_expired_written_entry_without_regenerating(rig):
    async def run():
        controller, supervisor, store, _ = rig
        supervisor.cognitive.commit_failures = 1
        accepted, _, result = await first_turn(rig)
        assert result.detail["status"] == "written_pending_memory"
        row = controller.turns.get(accepted["entry_id"])
        row["deadline_at"] = (utc_now()-timedelta(seconds=5)).isoformat()
        controller.turns.put(row)
        path, scope = store.path, store.scope
        await controller.close()
        reopened = BoardStore(path, scope)
        driver = LocalBoardDriver(reopened)
        provider = BoardProvider(EmbodimentCondition(mode="grounded", body_instance_id=scope.body_instance_id,
            capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES]),
            driver, scope)
        fresh_supervisor = FakeSupervisor()
        fresh = BoardController(fresh_supervisor, controller.settings, path.parents[1] / "runtime", provider=provider)
        try:
            await fresh.heartbeat()
            event = fresh_supervisor.events[-1]
            assert event.payload["reconcile_only"] is True
            result = await fresh.dispatch(event, 2)
            assert result.detail["status"] == "committed"
            assert not fresh_supervisor.cognitive.prepare_calls
            assert len(fresh_supervisor.cognitive.commit_calls) == 1
            assert len((await fresh.snapshot())["entries"]) == 1
        finally:
            await fresh.close()
    asyncio.run(run())


def test_owner_generation_receives_latest_eight_board_entries(rig):
    async def run():
        from harness.embodiment.board_contracts import BoardAppendRequest
        _, supervisor, store, _ = rig
        for number in range(10):
            now = utc_now()
            store.append(BoardAppendRequest(scope=store.scope, entry_id=f"seed-entry:{number}",
                decision_id=f"seed-decision:{number}", text=f"已核验的旧留言 {number}",
                requested_at=now, deadline=now+timedelta(seconds=30)))
        supervisor.cognitive.action = "wait"
        await first_turn(rig)
        read = supervisor.cognitive.prepare_calls[0].payload["read_entries"]
        assert [entry["receipt"]["entry_id"] for entry in read] == [f"seed-entry:{i}" for i in range(2, 10)]
    asyncio.run(run())


def test_terminal_replay_keeps_first_delivery_and_dispatch_timings(rig, monkeypatch):
    async def run():
        controller, _, _, _ = rig
        accepted, event, initial = await first_turn(rig)
        assert initial.detail["status"] == "committed"
        before = controller.turns.get(accepted["entry_id"])
        assert "end_to_end_wall_ms" in before and "dispatch_monotonic_ms" in before
        async def no_new_body_io():
            raise AssertionError("terminal replay must not reactivate the body")
        monkeypatch.setattr(controller, "ready", no_new_body_io)
        replay = await controller.dispatch(event, 2)
        assert replay.detail == initial.detail
        assert controller.turns.get(accepted["entry_id"]) == before
    asyncio.run(run())


def test_activity_while_homeostatic_request_is_queued_cancels_before_model(rig):
    async def run():
        controller, supervisor, _, _ = rig
        controller.next_opportunity = 0
        controller.last_activity = time.monotonic() - controller.settings.quiet_seconds - 1
        await controller.heartbeat()
        assert len(supervisor.events) == 1
        await controller.on_event("event.accepted", {"event_type": "qq_fact",
            "payload": {"qq_fact": {"kind": "verified_message"}}})
        result = await controller.dispatch(supervisor.events[-1], 1)
        assert result.detail["status"] == "superseded"
        assert not supervisor.cognitive.prepare_calls and not supervisor.cognitive.commit_calls
    asyncio.run(run())


@pytest.mark.parametrize("trigger,action", [
    ("user_request", "write"), ("user_request", "wait"),
    ("homeostasis", "write"), ("homeostasis", "wait"),
])
def test_real_supervisor_and_cognitive_host_board_contract(tmp_path, monkeypatch, trigger, action):
    """Exercise real ingress, heartbeat, command envelopes, owner ledger and HTTP.

    Only the model answer and the Homeostasis engine fixture are deterministic;
    none of Runtime's summary/command/dispatch helpers are replaced.
    """
    from harness.board.service import create_app
    from harness.embodiment.providers.board import BoardClient
    from harness.llm.client import MockLLM
    from harness.memory.shared import SharedReadScope
    from harness.runtime.models import RuntimeEventPriority
    from harness.runtime.supervisor import RuntimeSupervisor
    from tests.test_basic_persona_continuity import _host
    from tests.test_runtime_supervisor import _manifest, _SummaryHomeostasis

    async def run():
        token = "synthetic-body-token-for-inprocess-test-only"
        token_path = tmp_path / "body.token"
        token_path.write_text(token)
        settings = BoardSettings(token_file=token_path)
        config_path = tmp_path / "board.json"
        config_path.write_text(settings.model_dump_json(), encoding="utf-8")
        monkeypatch.setenv("NC_BOARD_CONFIG", str(config_path))
        monkeypatch.delenv("NC_QQ_SESSION_CONFIG", raising=False)
        monkeypatch.delenv("NC_SHARED_MEMORY_DB", raising=False)
        monkeypatch.setattr("gui.service.probe_llm", lambda *args, **kwargs: True)
        host, _ = _host(tmp_path / "owner", monkeypatch, ["unused"])
        original_llm = host.chat.llm
        llm = MockLLM(chat_script=[json.dumps({"action": action, "title": "",
            "text": "我觉得偶尔留下一句没有任务的话也挺好的。" if action == "write" else ""}, ensure_ascii=False)])
        host.chat.llm = llm
        homeostasis = _SummaryHomeostasis(salience="noticeable", state_path=tmp_path / "homeostasis.json")
        manifest = _manifest(expression_mode="responses").model_copy(update={"autonomous_chat_enabled": False})
        supervisor = RuntimeSupervisor(manifest=manifest, cognitive=host, homeostasis=homeostasis,
            state_dir=tmp_path / "runtime")
        store = BoardStore(tmp_path / "linux" / "board.sqlite3", settings.scope)
        body_app = create_app(store=store, token=token)
        monkeypatch.setattr(BoardSettings, "client", lambda self: BoardClient(self.base_url, token,
            transport=httpx.ASGITransport(app=body_app)))
        provider = BoardProvider(EmbodimentCondition(mode="grounded", body_instance_id=settings.body_instance_id,
            capability_requirements=[CapabilityRequirement(capability=name, required=True) for name in BOARD_CAPABILITIES]),
            settings.client(), settings.scope)
        controller = BoardController(supervisor, settings, tmp_path / "runtime", provider=provider)
        supervisor.board = controller
        try:
            async with body_app.router.lifespan_context(body_app):
                await supervisor.boot_and_recover()
                assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
                summary = supervisor._response_homeostatic_interoception()
                assert type(summary) is dict and summary["salience"] == "nominal"
                if trigger == "homeostasis":
                    controller.next_opportunity = 0
                    controller.last_activity = time.monotonic() - settings.quiet_seconds - 1
                    heartbeat = await supervisor.submit_event(event_type=RuntimeEventType.HEARTBEAT,
                        priority=RuntimeEventPriority.HEARTBEAT, source="test-existing-runtime-ticker")
                    assert (await supervisor.queue.get()).event_id == heartbeat.event_id
                    heartbeat_result = await supervisor.dispatch(heartbeat)
                    assert heartbeat_result.status == "completed", heartbeat_result.detail
                    summary = supervisor._response_homeostatic_interoception()
                    assert type(summary) is dict and summary["salience"] == "noticeable"
                    assert homeostasis.apply_event_calls == 0
                else:
                    await controller.request({"request_id": "real-owner-contract"})
                assert supervisor.queue.qsize() == 1
                event = await supervisor.queue.get()
                assert event.event_type == RuntimeEventType.BOARD_REFLECTION
                result = await supervisor.dispatch(event)
                assert result.status == "completed", result.detail
                assert result.detail["status"] == ("committed" if action == "write" else "wait"), result.detail
                assert len(llm.chat_calls) == 1
                body_payload = json.loads(llm.chat_calls[0][1]["content"])
                assert body_payload["homeostatic_summary"] == summary
                assert body_payload["trigger"] == trigger
                assert not any(host.chat.pipeline.session_store.snapshot().values())
                selected = host.shared.store.query(SharedReadScope(audience="local:main", kinds=("board_entry",)))
                assert len(selected.records) == (1 if action == "write" else 0)
                assert len((await controller.snapshot())["entries"]) == (1 if action == "write" else 0)
                intents = [record for record in supervisor.journal.iter_records()
                    if record.record_type == "domain_command_intent" and record.event_id == event.event_id]
                assert [item.data["envelope"]["command_type"] for item in intents] == (
                    ["PrepareBoardEntry", "CommitBoardEntry"] if action == "write" else ["PrepareBoardEntry"])
                assert {item.data["envelope"]["receive_seq"] for item in intents} == {event.receive_seq}
                assert len({item.data["envelope"]["command_id"] for item in intents}) == len(intents)
                results = [record.data["result"] for record in supervisor.journal.iter_records()
                    if record.record_type == "domain_command_result" and record.event_id == event.event_id]
                assert all(item["status"] == "applied" for item in results)
                assert (await host.get_snapshot()).acknowledged_receive_seq == event.receive_seq
                assert supervisor.version_vector.cognitive_memory == (1 if action == "write" else 0)
                host.chat.llm = original_llm
                await supervisor.stop()
                await controller.close()
        finally:
            if supervisor.lifecycle != RuntimeLifecycle.STOPPED:
                host.chat.llm = original_llm
                await supervisor.stop()
                await controller.close()
    asyncio.run(run())
