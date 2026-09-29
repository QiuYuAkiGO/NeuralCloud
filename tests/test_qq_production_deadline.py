"""Virtual-time production QQ budgets, including the real domain HTTP boundary."""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import httpx
import pytest

from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
from harness.llm.client import ChatResult
from harness.qq.decision import _QQScopedLLM
from harness.qq.policy import QQLatencyOptions
from harness.qq.production_deadline import downstream_reserve, stage_deadline
from harness.qq.telemetry import QQDeadline, QQDeadlineExpired, QQTurnTrace
from harness.runtime import qq_pipeline, rpc, supervisor as runtime_module
from harness.runtime.adapters import DomainSnapshot
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType
from tests.test_runtime_qq_deadline import _request, runtime as runtime


NOW = datetime(2026, 9, 22, tzinfo=timezone.utc)


def _command(*, command_type="PrepareQQTurn", production=True, policy=None):
    payload = {"deadline_at": (NOW + timedelta(seconds=60)).isoformat(),
               "qq_frozen_policy": (policy or QQLatencyOptions()).model_dump()}
    if production:
        payload["production_context"] = {"mode": "production"}
    return DomainCommandEnvelope[dict](command_type=command_type, event_id="evt_budget", receive_seq=1,
        correlation_id="trace-budget", runtime_instance_id="runtime_budget", manifest_revision=1,
        idempotency_key="budget:" + command_type, issued_at=NOW, deadline=NOW + timedelta(seconds=30),
        payload=payload)


def _event(request):
    return RuntimeEvent(event_id="evt_budget", receive_seq=1, correlation_id=request.trace.trace_id,
        source="qq_body", event_type=RuntimeEventType.QQ_TURN, priority=RuntimeEventPriority.USER,
        source_time=NOW, received_at=NOW, idempotency_key="event_budget", manifest_revision=1,
        payload=request.model_dump(mode="json"))


def _result(command, payload, completed_at):
    return DomainCommandResult[dict](command_id=command.command_id, command_type=command.command_type,
        status="applied", request_hash=command.effective_request_hash(), committed_revision=0,
        acknowledged_event_id=command.event_id, acknowledged_receive_seq=command.receive_seq,
        completed_at=completed_at, result=payload)


@pytest.mark.parametrize("production", [False, True])
def test_prepare_keeps_original_deadline_and_downstream_reserves(monkeypatch, production):
    elapsed = [5]  # Queueing is already part of the original sixty-second window.
    monkeypatch.setattr(qq_pipeline, "utc_now", lambda: NOW + timedelta(seconds=elapsed[0]))
    command = _command(production=production)
    request = SimpleNamespace(deadline_at=NOW + timedelta(seconds=60))
    bounded = qq_pipeline._command_window(command, request)
    assert (bounded.deadline - NOW).total_seconds() == (47 if production else 30)
    assert (bounded.deadline - qq_pipeline.utc_now()).total_seconds() == (42 if production else 25)
    assert bounded.payload["deadline_at"] == command.payload["deadline_at"]
    assert bounded.effective_request_hash() == command.effective_request_hash()
    elapsed[0] = 47
    if production:
        with pytest.raises(qq_pipeline.QQTurnRejected) as caught:
            qq_pipeline._command_window(command, request)
        assert caught.value.failure_detail == {
            "failure_category": "timeout", "error_code": "qq_stage_deadline_exhausted"}


def test_resume_and_replay_use_frozen_policy_not_a_new_transport_window(monkeypatch):
    monkeypatch.setattr(qq_pipeline, "utc_now", lambda: NOW + timedelta(seconds=20))
    frozen = QQLatencyOptions(qq_body_reserve_seconds=20.0, qq_delivery_reserve_seconds=5.0)
    command = _command(policy=frozen)
    # An idempotent transport renewal changes timestamps, not the frozen request.
    replay = command.model_copy(update={"issued_at": NOW + timedelta(seconds=20),
                                        "deadline": NOW + timedelta(seconds=80)})
    later_request = SimpleNamespace(deadline_at=NOW + timedelta(seconds=90))
    assert qq_pipeline._command_window(replay, later_request).deadline == NOW + timedelta(seconds=35)
    resume = replay.model_copy(update={"command_type": "ResumeQQResponse"})
    assert qq_pipeline._command_window(resume, later_request).deadline == NOW + timedelta(seconds=55)
    assert downstream_reserve(command) == 25 and downstream_reserve(resume) == 5


def test_journal_replay_does_not_take_a_new_policy_or_reply_window(runtime, monkeypatch):
    elapsed = [0.0]
    def now():
        return NOW + timedelta(seconds=elapsed[0])
    monkeypatch.setattr(runtime_module, "utc_now", now)
    monkeypatch.setattr(qq_pipeline, "utc_now", now)
    request = _request(seconds=60)
    event = _event(request)
    command = _command(policy=QQLatencyOptions(qq_delivery_reserve_seconds=5.0))
    command = qq_pipeline._command_window(command, request)
    runtime._append_journal("domain_command_intent", event=event, command_id=command.command_id,
        idempotency_key=command.idempotency_key, data={"service": "cognitive", "envelope": command.model_dump(mode="json")})
    elapsed[0] = 40
    renewed = runtime._make_command(event, command_type=command.command_type,
        idempotency_key=command.idempotency_key, expected_revision=None,
        payload=_command().payload)  # GUI now requests a smaller delivery reserve.
    assert renewed.issued_at == now() and renewed.deadline == now() + timedelta(seconds=30)
    replay = qq_pipeline._command_window(renewed, request)
    assert replay.deadline == NOW + timedelta(seconds=45)
    assert replay.payload == command.payload
    assert replay.command_id == command.command_id
    assert replay.effective_request_hash() == command.effective_request_hash()


@pytest.mark.parametrize("enabled", [False, True])
def test_prepare_over_thirty_seconds_can_resume_without_renewing_window(runtime, monkeypatch, enabled):
    elapsed, commands = [0.0], []
    def now():
        return NOW + timedelta(seconds=elapsed[0])
    monkeypatch.setattr(qq_pipeline, "utc_now", now)
    monkeypatch.setattr(runtime_module, "utc_now", now)
    selected = QQLatencyOptions(qq_web_policy="off", qq_stage_budgets_enabled=enabled)

    async def context(*args, **kwargs):
        return {"mode": "production"}

    runtime.production_runtime = SimpleNamespace(context=context, bind_qq_decision=lambda *args: None)
    runtime._cognitive_snapshot = DomainSnapshot(service="cognitive", revision=0, payload={
        "qq_turn_commands": ["PrepareQQTurn", "ResumeQQResponse"], "qq_latency_policy": selected.model_dump()})

    async def scenario():
        request = _request(seconds=60)
        event = _event(request)

        def trace():
            return {"trace_id": request.trace.trace_id, "event_id": request.trace.event_id,
                    "first_seen_mono": 0, "elapsed_ms": elapsed[0] * 1000, "stages": [], "counters": {}}

        async def prepare(command):
            commands.append(command)
            assert command.deadline == NOW + timedelta(seconds=47)
            elapsed[0] = 33  # Appraisal + gate exceed the old command's 30 seconds.
            return _result(command, {"kind": "prepared", "prepare_id": "prep-budget", "snapshot_hash": "a" * 64,
                "turn_binding": {"event_id": event.event_id, "group_id": request.group_id,
                    "self_member_id": request.self_member_id, "deadline_at": request.deadline_at.isoformat(),
                    "runtime_instance_id": runtime.runtime_instance_id},
                "policy": selected.model_dump(), "policy_version": selected.version, "web_plan": None,
                "trace": trace()}, now())

        async def resume(command):
            commands.append(command)
            assert command.deadline == NOW + timedelta(seconds=57)
            elapsed[0] += 8
            return _result(command, {"decision": {"fixture": "ready"}, "trace": trace()}, now())

        monkeypatch.setattr(runtime.cognitive, "prepare_qq_turn", prepare, raising=False)
        monkeypatch.setattr(runtime.cognitive, "resume_qq_response", resume, raising=False)
        result = await qq_pipeline.prepare(runtime, event, request)
        assert result.result["decision"] == {"fixture": "ready"}
        assert [item.command_type for item in commands] == ["PrepareQQTurn", "ResumeQQResponse"]
        assert elapsed[0] == 41
        assert runtime.command_timeout_seconds == 30

    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["timeout", "late_result"])
def test_stage_expiry_is_terminal_without_faulting_runtime(runtime, monkeypatch, failure):
    elapsed, calls = [0.0], []
    def now():
        return NOW + timedelta(seconds=elapsed[0])
    monkeypatch.setattr(runtime_module, "utc_now", now)

    async def scenario():
        request = _request(seconds=60)
        event = _event(request)
        command = _command().model_copy(update={"deadline": NOW + timedelta(seconds=47)})

        async def expired(envelope):
            calls.append(envelope.command_id)
            elapsed[0] = 47
            if failure == "timeout":
                raise TimeoutError("fixture model deadline")
            return _result(envelope, {}, now() + timedelta(milliseconds=1))

        async def prepare(*args):
            return await runtime._invoke_domain(service="cognitive", envelope=command, method=expired, event=event)

        monkeypatch.setattr(runtime, "_prepare_qq_turn", prepare)
        result = await runtime._dispatch_qq_turn(event, 1)
        assert result.status == "ignored"
        assert result.detail == {"error_type": "qq_turn_rejected", "failure_category": "timeout",
                                 "error_code": "qq_stage_deadline_exhausted"}
        assert len(calls) == 1
        assert now() < request.deadline_at
        assert not runtime._degraded_reasons and not runtime.watchdog._failures.get("cognitive", 0)
        # A replay of the exhausted stage cannot start a new request.
        await runtime._dispatch_qq_turn(event, 2)
        assert len(calls) == 1

    asyncio.run(scenario())


@pytest.mark.parametrize("outcome", ["timeout", "late_snapshot"])
def test_final_snapshot_preserves_journaled_delivery_reserve(runtime, monkeypatch, outcome):
    from tests.test_runtime_qq_deadline import _prepared
    elapsed, snapshots = [0.0], []
    def now():
        return NOW + timedelta(seconds=elapsed[0])
    monkeypatch.setattr(runtime_module, "utc_now", now)
    monkeypatch.setattr(qq_pipeline, "utc_now", now)
    runtime.production_runtime = SimpleNamespace()

    async def scenario():
        request = _request(seconds=60)
        event = _event(request)
        frozen = QQLatencyOptions(qq_delivery_reserve_seconds=5.0)
        command = _command(policy=frozen).model_copy(update={
            "idempotency_key": f"cognitive:qq-prepare-turn:{event.event_id}",
            "payload": {**request.model_dump(mode="json"), "production_context": {},
                        "qq_frozen_policy": frozen.model_dump()}})
        runtime._append_journal("domain_command_intent", event=event, command_id=command.command_id,
            idempotency_key=command.idempotency_key, data={"service": "cognitive", "envelope": command.model_dump(mode="json")})

        later = request.model_copy(update={"deadline_at": NOW + timedelta(seconds=90)})
        assert qq_pipeline.completion_deadline(runtime, event, later, fixed_notice=True) == NOW + timedelta(seconds=70)

        async def prepared(*args):
            elapsed[0] = 40
            return _prepared(command)

        async def slow_snapshot():
            snapshots.append(now())
            elapsed[0] = 55  # The original turn still has its five-second send reserve.
            if outcome == "timeout":
                raise TimeoutError("fixture snapshot deadline")
            return DomainSnapshot(service="cognitive", revision=0, payload={})

        monkeypatch.setattr(qq_pipeline, "prepare", prepared)
        monkeypatch.setattr(runtime.cognitive, "get_snapshot", slow_snapshot)
        assert qq_pipeline.completion_deadline(runtime, event, request) == NOW + timedelta(seconds=55)
        result = await runtime._dispatch_qq_turn(event, 1)
        assert result.status == "ignored" and result.detail["error_code"] == "qq_stage_deadline_exhausted"
        assert len(snapshots) == 1 and now() < request.deadline_at
        assert not runtime._degraded_reasons and not runtime.watchdog._failures.get("cognitive", 0)

    asyncio.run(scenario())


def test_domain_rejection_preserves_only_fixed_safe_diagnostics(runtime, monkeypatch):
    monkeypatch.setattr(runtime_module, "utc_now", lambda: NOW)

    async def scenario():
        request = _request(seconds=60)
        event = _event(request)
        command = _command()

        async def rejected(envelope):
            return _result(envelope, {"failure_category": "evidence_rejected",
                "error_code": "production_memory_proposals_rejected", "private_exception": "never expose"}, NOW).model_copy(
                    update={"status": "rejected", "error": "private domain message"})

        with pytest.raises(qq_pipeline.QQTurnRejected) as caught:
            await runtime._invoke_domain(service="cognitive", envelope=command, method=rejected, event=event)
        assert caught.value.failure_detail == {"failure_category": "evidence_rejected",
                                               "error_code": "production_memory_proposals_rejected"}
        assert str(caught.value) == "qq_turn_rejected"

    asyncio.run(scenario())


@pytest.mark.parametrize("replayed", [False, True])
def test_only_fresh_fixed_notice_keeps_existing_final_validation_grace(runtime, monkeypatch, replayed):
    from tests.test_runtime_qq_deadline import _fallback
    elapsed, calls = [0.0], []
    def now():
        return NOW + timedelta(seconds=elapsed[0])
    monkeypatch.setattr(runtime_module, "utc_now", now)
    monkeypatch.setattr(qq_pipeline, "utc_now", now)
    monkeypatch.setattr(runtime, "_submit_affect_after_qq", lambda *args: None)
    runtime.production_runtime = SimpleNamespace()

    async def scenario():
        request = _request(seconds=60)
        event = _event(request)
        command = _command().model_copy(update={
            "idempotency_key": f"cognitive:qq-prepare-turn:{event.event_id}",
            "payload": {**request.model_dump(mode="json"), "production_context": {},
                        "qq_frozen_policy": QQLatencyOptions().model_dump()}})
        runtime._append_journal("domain_command_intent", event=event, command_id=command.command_id,
            idempotency_key=command.idempotency_key, data={"service": "cognitive", "envelope": command.model_dump(mode="json")})

        async def prepared(*args):
            elapsed[0] = 40  # The fixed notice completed before the model-stage boundary.
            result = _fallback(command)
            if replayed:
                result.result["decision"].update(replayed=True, new_decision=False)
            return result

        async def snapshot():
            calls.append("snapshot")
            elapsed[0] = 65
            return DomainSnapshot(service="cognitive", revision=0, payload={})

        monkeypatch.setattr(qq_pipeline, "prepare", prepared)
        monkeypatch.setattr(runtime.cognitive, "get_snapshot", snapshot)
        result = await runtime._dispatch_qq_turn(event, 1)
        assert calls == ["snapshot"]
        if replayed:
            assert result.status == "ignored" and result.detail["error_code"] == "qq_stage_deadline_exhausted"
        else:
            assert result.status == "completed" and result.detail["decision"]["outcome"] == "error_fallback"
        # A new dispatch after the original reply window cannot start model work.
        result = await runtime._dispatch_qq_turn(event, 2)
        assert result.status == "ignored" and calls == ["snapshot"]

    asyncio.run(scenario())


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("stage,reserve,remaining,expected", [("gate", 13, 10, 10), ("persona", 3, 15, 15)])
def test_stage_caps_do_not_subtract_downstream_reserve_twice(stage, reserve, remaining, expected, enabled):
    clock, calls = [0.0], []

    class Model:
        def complete_chat_result(self, messages, **kwargs):
            calls.append(kwargs["timeout_seconds"])
            return ChatResult(text="ready")

    deadline = QQDeadline.after(remaining, clock=lambda: clock[0], downstream_reserved_seconds=reserve)
    llm = _QQScopedLLM(Model(), deadline=deadline, timeout=60, trace=QQTurnTrace(),
                       policy=QQLatencyOptions(qq_stage_budgets_enabled=enabled))
    llm.stage_name = stage
    llm.complete_chat_result([])
    assert calls == [expected]
    clock[0] = remaining
    with pytest.raises(QQDeadlineExpired):
        llm.complete_chat_result([])
    assert len(calls) == 1


@pytest.mark.parametrize("production,command_type", [(True, "PrepareQQTurn"), (True, "ResumeQQResponse"),
                                                    (False, "PrepareQQTurn"), (True, "PrepareResponse")])
def test_http_command_timeout_is_scoped_and_recomputed_per_retry(monkeypatch, production, command_type):
    elapsed, calls = [0.0], []
    monkeypatch.setattr(rpc, "utc_now", lambda: NOW + timedelta(seconds=elapsed[0]))
    command = _command(production=production, command_type=command_type)
    staged = production and command_type in {"PrepareQQTurn", "ResumeQQResponse"}
    if staged:
        command = command.model_copy(update={"deadline": stage_deadline(command)})

    def handler(request):
        calls.append(request.extensions["timeout"]["read"])
        if len(calls) == 1:
            elapsed[0] = 2
            raise httpx.ConnectError("fixture transient", request=request)
        result = _result(command, {}, NOW + timedelta(seconds=elapsed[0]))
        return httpx.Response(200, json=result.model_dump(mode="json"))

    async def scenario():
        async with httpx.AsyncClient(base_url="http://cognitive.test", timeout=30,
                                     transport=httpx.MockTransport(handler)) as transport:
            client = rpc.HttpCognitiveClient("http://cognitive.test", service_token="fixture", client=transport)
            assert (await client.command(command)).status == "applied"
        expected = 47 if command_type == "PrepareQQTurn" else 57
        assert calls == ([expected, expected - 2] if staged else [30, 30])

    asyncio.run(scenario())


def test_http_retry_never_starts_after_stage_deadline(monkeypatch):
    elapsed, calls = [0.0], []
    monkeypatch.setattr(rpc, "utc_now", lambda: NOW + timedelta(seconds=elapsed[0]))
    command = _command().model_copy(update={"deadline": NOW + timedelta(seconds=47)})

    def handler(request):
        calls.append(request)
        elapsed[0] = 47
        raise httpx.ReadTimeout("fixture deadline", request=request)

    async def scenario():
        async with httpx.AsyncClient(base_url="http://cognitive.test", transport=httpx.MockTransport(handler)) as transport:
            client = rpc.HttpCognitiveClient("http://cognitive.test", service_token="fixture", client=transport)
            with pytest.raises(TimeoutError, match="qq_stage_deadline_exhausted"):
                await client.command(command)
        assert len(calls) == 1

    asyncio.run(scenario())
