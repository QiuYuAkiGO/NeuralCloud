"""A QQ reply window can expire without stopping other conversations."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

import pytest

from harness.contracts.envelope import DomainCommandResult
from harness.qq.decision import QQReplyDecision
from harness.qq.shared import QQSharedPrepareRequest
from harness.runtime import PassiveHomeostasisAdapter, RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime import supervisor as module
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


def _request(*, seconds=1, sequence=1):
    deadline = module.utc_now() + timedelta(seconds=seconds)
    return QQSharedPrepareRequest.model_validate({
        "transport": "onebot", "group_id": "10001", "self_member_id": "29999",
        "trigger_member_id": "20001", "deadline_at": deadline,
        "trace": {"trace_id": f"qq-trace:{sequence:032x}",
            "event_id": f"qq-watch-event:{sequence:032x}",
            "first_seen_at": deadline - timedelta(seconds=60), "first_seen_mono": 0},
    })


def _domain_result(command, result, *, completed_at=None):
    return DomainCommandResult[dict](command_id=command.command_id, command_type=command.command_type,
        status="applied", request_hash=command.effective_request_hash(), committed_revision=0,
        acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
        completed_at=completed_at or module.utc_now(), result=result)


def _prepared(command):
    payload = command.payload
    trace = payload["trace"]
    decision = QQReplyDecision(
        decision_id="qq-decision:deadline-test", input_hash="test", created_at=trace["first_seen_at"],
        action="wait", reason="fixture wait", decision_source="model", outcome="natural_wait",
        group_id=payload["group_id"], self_member_id=payload["self_member_id"], participants=[],
        source_record_ids=[], new_observation_record_ids=[], evidence_refs=[], delivery_refs=[],
        model="fixture", persona_version="fixture", preset_id="fixture", trace_id=trace["trace_id"],
    )
    return _domain_result(command, {"decision": decision.model_dump(mode="json"),
        "trace": {"trace_id": trace["trace_id"], "event_id": trace["event_id"]}})


@pytest.fixture
def runtime(tmp_path):
    memory, _, cognitive = _pipeline_adapter(tmp_path)
    supervisor = module.RuntimeSupervisor(manifest=_manifest(), cognitive=cognitive,
        homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    supervisor.lifecycle = RuntimeLifecycle.RUNNING

    async def prepare(command):
        return _prepared(command)

    cognitive.prepare_qq_response = prepare
    yield supervisor
    memory.close()


async def _accept(runtime, request):
    event = await runtime.submit_event(event_type=RuntimeEventType.QQ_TURN,
        priority=RuntimeEventPriority.USER, source="qq_body", source_time=request.trace.first_seen_at,
        correlation_id=request.trace.trace_id, payload=request.model_dump(mode="json"))
    assert (await runtime.queue.get(timeout=0)).event_id == event.event_id
    return event


def _assert_expired(runtime, event, result):
    assert result.status == "ignored"
    assert result.decision_kind == "qq_prepare"
    assert result.detail == {"error_type": "qq_turn_expired"}
    assert runtime.lifecycle == RuntimeLifecycle.RUNNING
    assert runtime.queue.empty()
    assert not runtime._degraded_reasons
    assert not runtime.watchdog._failures.get("cognitive", 0)
    records = [record for record in runtime.journal.records() if record.event_id == event.event_id]
    assert not any(record.record_type == "event_requeued" for record in records)
    assert records[-1].record_type == "processing_completed"
    assert records[-1].data["status"] == "ignored"


@pytest.mark.parametrize("stage", ["web", "emotion", "prepare", "snapshot"])
def test_whole_qq_prepare_expires_locally_and_next_turn_runs(runtime, monkeypatch, stage):
    async def scenario():
        original_prepare = runtime.cognitive.prepare_qq_response
        original_snapshot = runtime.cognitive.get_snapshot
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def slow(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        if stage == "web":
            monkeypatch.setattr("harness.runtime.web_dialogue.prepare_web_context", slow)
        elif stage == "emotion":
            runtime.manifest = runtime.manifest.model_copy(update={"emotion_mode": "on"})
            monkeypatch.setattr(runtime.cognitive, "observe_emotion", slow)
        elif stage == "prepare":
            monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", slow)
        else:
            monkeypatch.setattr(runtime.cognitive, "get_snapshot", slow)

        request = _request(seconds=.2)
        event = await _accept(runtime, request)
        result = await asyncio.wait_for(runtime.dispatch(event), 2)
        assert started.is_set() and cancelled.is_set()
        _assert_expired(runtime, event, result)
        for record in runtime.journal.records():
            if record.record_type == "domain_command_intent":
                assert datetime.fromisoformat(record.data["envelope"]["deadline"]) <= request.deadline_at

        async def no_web(*args, **kwargs):
            return None, None

        monkeypatch.setattr("harness.runtime.web_dialogue.prepare_web_context", no_web)
        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", original_prepare)
        monkeypatch.setattr(runtime.cognitive, "get_snapshot", original_snapshot)
        runtime.manifest = runtime.manifest.model_copy(update={"emotion_mode": "off"})
        following = await _accept(runtime, _request(sequence=2))
        assert (await runtime.dispatch(following)).status == "completed"
        assert runtime.lifecycle == RuntimeLifecycle.RUNNING

    asyncio.run(scenario())


@pytest.mark.parametrize("error", [TimeoutError, ConnectionError, module.DomainCommandError])
def test_early_qq_failures_still_pause_and_requeue(runtime, monkeypatch, error):
    async def fail(command):
        raise error("early dependency failure")

    monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", fail)

    async def scenario():
        event = await _accept(runtime, _request(seconds=10))
        result = await runtime.dispatch(event)
        assert result.status == "deferred"
        assert runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert runtime.queue.snapshot()[0].event_id == event.event_id
        assert runtime.watchdog._failures["cognitive"] == 1

    asyncio.run(scenario())


def test_expiry_does_not_hide_a_non_timeout_domain_failure(runtime, monkeypatch):
    async def scenario():
        request = _request(seconds=10)
        event = await _accept(runtime, request)

        async def fail(command):
            monkeypatch.setattr(module, "utc_now", lambda: request.deadline_at)
            raise ConnectionError("real connection failure")

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", fail)
        result = await runtime.dispatch(event)
        assert result.status == "deferred"
        assert runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert runtime.watchdog._failures["cognitive"] == 1

    asyncio.run(scenario())


def test_late_qq_result_is_terminal_without_clearing_prior_dependency_failures(runtime, monkeypatch):
    async def scenario():
        request = _request(seconds=10)
        event = await _accept(runtime, request)
        runtime.watchdog.record_failure("cognitive")
        runtime._degraded_reasons.append("cognitive")

        async def late(command):
            monkeypatch.setattr(module, "utc_now", lambda: request.deadline_at + timedelta(milliseconds=1))
            return _prepared(command)

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", late)
        result = await runtime.dispatch(event)
        assert result.status == "ignored" and result.detail["error_type"] == "qq_turn_expired"
        assert runtime.lifecycle == RuntimeLifecycle.RUNNING and runtime.queue.empty()
        assert runtime.watchdog._failures["cognitive"] == 1
        assert runtime._degraded_reasons == ["cognitive"]
        assert any(record.record_type == "domain_command_late_result" for record in runtime.journal.records())

    asyncio.run(scenario())


def test_inner_qq_deadline_timeout_does_not_increment_watchdog(runtime, monkeypatch):
    async def scenario():
        request = _request(seconds=10)
        event = await _accept(runtime, request)

        async def timeout(command):
            monkeypatch.setattr(module, "utc_now", lambda: request.deadline_at)
            raise TimeoutError()

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", timeout)
        _assert_expired(runtime, event, await runtime.dispatch(event))

    asyncio.run(scenario())


@pytest.mark.parametrize("completed_late", [False, True])
@pytest.mark.parametrize(("status", "error"), [
    ("failed", "qq_shared_prepare_failed:ValueError"),
    ("conflict", "idempotency key reused with different request"),
    ("rejected", "emotion snapshot revision mismatch"),
])
def test_failed_domain_result_after_qq_deadline_remains_failure(
    runtime, monkeypatch, completed_late, status, error,
):
    async def scenario():
        request = _request(seconds=10)
        event = await _accept(runtime, request)

        async def fail(command):
            result = runtime.cognitive._failure(command, error, status=status)
            monkeypatch.setattr(module, "utc_now", lambda: request.deadline_at + timedelta(milliseconds=1))
            return result.model_copy(update={"completed_at": request.deadline_at
                + timedelta(milliseconds=1 if completed_late else -1)})

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", fail)
        result = await runtime.dispatch(event)
        assert result.status == "deferred"
        assert result.detail["error_type"] == "DomainCommandError"
        assert error in result.detail["error"]
        assert runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert runtime.queue.snapshot()[0].event_id == event.event_id

    asyncio.run(scenario())


@pytest.mark.parametrize("completed_late", [False, True])
@pytest.mark.parametrize("arrived_expired", [False, True])
@pytest.mark.parametrize(("status", "error"), [
    ("rejected", "command deadline expired"),
    ("failed", "qq_shared_prepare_failed:QQDeadlineExpired"),
])
def test_explicit_owner_deadline_failure_uses_original_qq_arrival_deadline(
    runtime, monkeypatch, completed_late, arrived_expired, status, error,
):
    async def scenario():
        request = _request(seconds=10)
        event = await _accept(runtime, request)

        async def fail(command):
            result = runtime.cognitive._failure(command, error, status=status)
            if arrived_expired:
                monkeypatch.setattr(module, "utc_now", lambda: request.deadline_at + timedelta(milliseconds=1))
            return result.model_copy(update={"completed_at": request.deadline_at
                + timedelta(milliseconds=1 if completed_late else -1)})

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", fail)
        result = await runtime.dispatch(event)
        if arrived_expired:
            _assert_expired(runtime, event, result)
        else:
            assert result.status == "deferred"
            assert result.detail["error_type"] == "DomainCommandError"
            assert runtime.lifecycle == RuntimeLifecycle.PAUSED
            assert runtime.queue.snapshot()[0].event_id == event.event_id

    asyncio.run(scenario())


def test_emotion_preflight_deadline_rejection_expires_only_its_qq_turn(runtime, monkeypatch):
    async def scenario():
        runtime.manifest = runtime.manifest.model_copy(update={"emotion_mode": "on"})
        request = _request(seconds=10)
        event = await _accept(runtime, request)

        async def reject(command):
            monkeypatch.setattr("harness.runtime.adapters._utc_now", lambda: request.deadline_at)
            monkeypatch.setattr(module, "utc_now", lambda: request.deadline_at)
            return runtime.cognitive._preflight(command)

        monkeypatch.setattr(runtime.cognitive, "observe_emotion", reject)
        _assert_expired(runtime, event, await runtime.dispatch(event))
        intents = [r.data["envelope"]["command_type"] for r in runtime.journal.records()
            if r.record_type == "domain_command_intent"]
        assert intents == ["ObserveEmotion"]

    asyncio.run(scenario())


def test_external_cancellation_is_not_converted_to_expiry(runtime, monkeypatch):
    async def scenario():
        started = asyncio.Event()

        async def pending(command):
            started.set()
            await asyncio.Future()

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", pending)
        event = await _accept(runtime, _request(seconds=10))
        task = asyncio.create_task(runtime.dispatch(event))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not any(record.record_type == "processing_completed" for record in runtime.journal.records())
        assert not runtime.watchdog._failures.get("cognitive", 0)

    asyncio.run(scenario())


def test_expired_waiter_preserves_cognitive_lock_until_its_owned_operation_finishes(runtime, monkeypatch):
    async def scenario():
        release = asyncio.Event()
        calls = []
        completed = []

        async def prepare(command):
            async def operation():
                async with runtime.cognitive._lock:
                    calls.append(command.event_id)
                    if len(calls) == 1:
                        await release.wait()
                    completed.append(command.event_id)
                    return _prepared(command)

            return await runtime.cognitive._run_command(operation)

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", prepare)
        first = await _accept(runtime, _request(seconds=.2))
        try:
            _assert_expired(runtime, first, await runtime.dispatch(first))
            assert runtime.cognitive._lock.locked()
            assert len(runtime.cognitive.command_tasks) == 1
            assert not completed
            second = await _accept(runtime, _request(sequence=2))
            next_turn = asyncio.create_task(runtime.dispatch(second))
            await asyncio.sleep(.02)
            assert not next_turn.done() and calls == [first.event_id]
            release.set()
            assert (await next_turn).status == "completed"
            assert completed == [first.event_id, second.event_id]
            assert runtime.lifecycle == RuntimeLifecycle.RUNNING
            first_completions = [r for r in runtime.journal.records()
                if r.event_id == first.event_id and r.record_type == "processing_completed"]
            assert len(first_completions) == 1 and first_completions[0].data["status"] == "ignored"
        finally:
            release.set()
            await asyncio.gather(*runtime.cognitive.command_tasks)

    asyncio.run(scenario())


def test_shorter_domain_window_late_result_still_pauses(runtime, monkeypatch):
    async def scenario():
        runtime.command_timeout_seconds = .1

        async def late(command):
            return _prepared(command).model_copy(update={
                "completed_at": command.deadline + timedelta(milliseconds=1)})

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_response", late)
        event = await _accept(runtime, _request(seconds=10))
        result = await runtime.dispatch(event)
        assert result.status == "deferred"
        assert result.detail["error_type"] == "LateDomainResultError"
        assert runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert runtime.queue.snapshot()[0].event_id == event.event_id

    asyncio.run(scenario())


@pytest.mark.parametrize("command_type", ["PrepareQQResponse", "ObserveEmotion", "PrepareWebStep"])
def test_restart_does_not_replay_expired_qq_preparations(runtime, monkeypatch, command_type):
    async def scenario():
        request = _request(seconds=-1)
        event = await _accept(runtime, request)
        envelope = runtime._make_command(event, command_type=command_type, idempotency_key="old-qq-command",
            expected_revision=None, payload={"fixture": True})
        runtime._append_journal("domain_command_intent", event=event, command_id=envelope.command_id,
            idempotency_key=envelope.idempotency_key,
            data={"service": "cognitive", "envelope": envelope.model_dump(mode="json")})
        _assert_expired(runtime, event, await runtime.dispatch(event))
        # Rebuild from the durable journal, as during a new Runtime's recovery.
        restored = module.RuntimeSupervisor(manifest=_manifest(), cognitive=runtime.cognitive,
            homeostasis=runtime.homeostasis, state_dir=runtime.state_dir)

        async def unexpected(*args, **kwargs):
            pytest.fail("recovery invoked an expired QQ preparation")

        for name in ("prepare_qq_response", "observe_emotion", "prepare_web_step"):
            monkeypatch.setattr(runtime.cognitive, name, unexpected, raising=False)
        await restored._reconcile_incomplete_commands()
        await restored._restore_pending_events()
        assert restored.queue.empty() and restored._reconciliation_backlog == 0
        expired = [r for r in restored.journal.records() if r.record_type == "domain_command_expired"]
        assert [r.command_id for r in expired] == [envelope.command_id]
        await restored._reconcile_incomplete_commands()
        assert len([r for r in restored.journal.records() if r.record_type == "domain_command_expired"]) == 1

    asyncio.run(scenario())


def test_non_qq_timeout_keeps_global_recovery(runtime, monkeypatch):
    async def fail(command):
        raise TimeoutError()

    monkeypatch.setattr(runtime.cognitive, "prepare_response", fail)

    async def scenario():
        await runtime.submit_user_message("local chat", session_id="fixture")
        event = await runtime.queue.get(timeout=0)
        result = await runtime.dispatch(event)
        assert result.status == "deferred" and runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert runtime.queue.snapshot()[0].event_id == event.event_id
        assert runtime.watchdog._failures["cognitive"] == 1

    asyncio.run(scenario())


def test_non_qq_late_failure_keeps_existing_late_result_classification(runtime, monkeypatch):
    async def late_failure(command):
        return runtime.cognitive._failure(command, "fixture failure").model_copy(update={
            "completed_at": command.deadline + timedelta(milliseconds=1)})

    monkeypatch.setattr(runtime.cognitive, "prepare_response", late_failure)

    async def scenario():
        await runtime.submit_user_message("local chat", session_id="fixture")
        event = await runtime.queue.get(timeout=0)
        result = await runtime.dispatch(event)
        assert result.status == "deferred" and result.detail["error_type"] == "LateDomainResultError"
        assert runtime.lifecycle == RuntimeLifecycle.PAUSED
        assert runtime.queue.snapshot()[0].event_id == event.event_id

    asyncio.run(scenario())
