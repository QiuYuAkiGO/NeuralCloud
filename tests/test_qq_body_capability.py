"""Negotiated communication uses mocked driver callbacks and no real desktop."""

import asyncio
from datetime import datetime, timedelta, timezone
import threading
from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from harness.embodiment.capabilities import (
    EVIDENCE_REFERENCE, QQ_COMMUNICATION_READ, QQ_COMMUNICATION_RECONCILE,
    QQ_COMMUNICATION_SEND, CapabilityRequirement, EmbodimentState, HealthStatus,
)
from harness.embodiment.negotiation import CapabilityNegotiator
from harness.embodiment.providers.qq import QQCommunicationError, QQCommunicationProvider
from harness.embodiment.qq_contracts import (
    QQDeliveryResult, QQDriverStatus, QQFrozenSendPlan, QQReadRequest, QQReadResult,
    QQReconcileRequest, QQScope, QQSendRequest, QQVerifiedReceipt,
)
from harness.research.manifest import EmbodimentCondition

NOW = datetime(2026, 9, 12, tzinfo=timezone.utc)
REF = "evidence://sha256/" + "d" * 64
CAPS = (QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND, QQ_COMMUNICATION_RECONCILE, EVIDENCE_REFERENCE)


def run(coroutine):
    return asyncio.run(coroutine)


@pytest.fixture
def setup():
    scope = QQScope(body_instance_id="synthetic-body", environment_revision=4,
                    window_id="0x123", group_id="10001", self_member_id="30001")
    request = QQSendRequest(scope=scope, decision_id="qq-decision:one", reservation_id="qq-watch-send:one",
        segment_index=0, segment_count=2, text="完整的第一段。", requested_at=NOW,
        deadline=NOW + timedelta(seconds=60), context_signature="a" * 64)
    plan = QQFrozenSendPlan(scope=scope, decision_id=request.decision_id, reservation_id=request.reservation_id,
        input_hash="b" * 64, reply_segments=(request.text, "完整的第二段。"), first_seen_at=NOW, deadline=request.deadline)
    clock = [NOW]
    state = QQDriverStatus(observed_at=NOW, active_scope=scope, context_signature=request.context_signature,
                          connected=True, observer_ready=True, send_ready=True, paused=False)
    data = SimpleNamespace(scope=scope, request=request, plan=plan, clock=clock, state=state,
                           store={}, reads=[], sends=[], reconciles=[], lookups=[], plans=[], threads=[])

    def driver_status():
        return data.state.model_copy(update={"observed_at": clock[0]})

    def read_executor(item):
        data.reads.append(item)
        return QQReadResult(scope=scope, capture_id="synthetic-capture", observed_at=clock[0],
                            context_signature=data.state.context_signature, evidence_refs=(REF,))

    def load_plan(item):
        data.plans.append(item)
        return data.plan

    def load_receipt(item):
        data.lookups.append(item)
        return data.store.get((item.reservation_id, item.segment_index))

    def persist(item):
        receipt = QQVerifiedReceipt(scope=item.scope, decision_id=item.decision_id, reservation_id=item.reservation_id,
            segment_index=item.segment_index, segment_count=item.segment_count,
            receipt_id=f"qq-segment-delivery:{item.segment_index}", delivered_text=item.text,
            delivered_at=clock[0], evidence_refs=(REF,), verification="qq_ui_verified")
        data.store[(item.reservation_id, item.segment_index)] = receipt
        return receipt

    def sent_result(item, receipt):
        return QQDeliveryResult(scope=item.scope, decision_id=item.decision_id, reservation_id=item.reservation_id,
            segment_index=item.segment_index, segment_count=item.segment_count, status="sent", reason_code="gui_verified",
            receipt_id=receipt.receipt_id, delivered_at=receipt.delivered_at, evidence_refs=receipt.evidence_refs)

    def send_executor(item):
        data.sends.append(item)
        data.threads.append(threading.get_ident())
        return sent_result(item, persist(item))

    def reconcile_executor(item):
        data.reconciles.append(item)
        return sent_result(item.send_request, persist(item.send_request))

    condition = EmbodimentCondition(mode="grounded", body_instance_id=scope.body_instance_id,
        capability_requirements=[CapabilityRequirement(capability=cap, required=True) for cap in CAPS])
    data.provider = QQCommunicationProvider(condition=condition, allowed_scopes=[scope], driver_status=driver_status,
        read_executor=read_executor, send_executor=send_executor, reconcile_executor=reconcile_executor,
        load_frozen_plan=load_plan, load_verified_receipt=load_receipt, clock=lambda: clock[0])
    data.persist = persist
    data.sent_result = sent_result
    return data


def test_standard_negotiator_and_manager_activate_the_real_operation_gates(setup):
    async def scenario():
        provider = setup.provider
        with pytest.raises(QQCommunicationError, match="not_activated"):
            await provider.send(setup.request)
        status = await provider.activate()
        assert isinstance(provider.manager.negotiator, CapabilityNegotiator)
        assert status.embodiment.state == EmbodimentState.AVAILABLE
        assert status.embodiment.effective_capabilities == set(CAPS)
        read = await provider.read(QQReadRequest(scope=setup.scope, requested_at=NOW, deadline=NOW + timedelta(seconds=5)))
        assert read.scope == setup.scope and read.evidence_refs == (REF,) and len(setup.reads) == 1
        result = await provider.send(setup.request)
        assert result.status == "sent" and result.receipt_id == "qq-segment-delivery:0"
        assert len(setup.sends) == 1 and len(setup.lookups) == 2
        assert setup.threads == [threading.get_ident()]
        replay = await provider.send(setup.request)
        assert replay == result and len(setup.sends) == 1  # Independent durable lookup; no second GUI action.
    run(scenario())


@pytest.mark.parametrize("mode", ["off", "observe"])
def test_manifest_disabled_or_observe_only_never_invokes_send(setup, mode):
    async def scenario():
        condition = EmbodimentCondition(mode=mode, capability_requirements=[CapabilityRequirement(capability=cap) for cap in CAPS])
        setup.provider.manager.condition = condition
        setup.provider._condition_hash = __import__("harness.embodiment.providers.qq", fromlist=["_digest"])._digest(condition.model_dump(mode="json"))
        await setup.provider.activate()
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_unknown_major_required_capability_prevents_all_send(setup):
    async def scenario():
        setup.provider.manager.negotiator = CapabilityNegotiator(understood=set(CAPS) - {QQ_COMMUNICATION_SEND})
        status = await setup.provider.activate()
        assert QQ_COMMUNICATION_SEND in status.embodiment.negotiated.incompatible_required
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


@pytest.mark.parametrize("change", [
    {"paused": True}, {"has_uncertain_delivery": True}, {"observer_ready": False},
    {"send_ready": False}, {"connected": False}, {"context_signature": None},
])
def test_dynamic_driver_health_removes_send_without_reactivation(setup, change):
    async def scenario():
        await setup.provider.activate()
        setup.state = setup.state.model_copy(update=change)
        status = await setup.provider.status()
        assert not setup.provider.manager.supports(QQ_COMMUNICATION_SEND)
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
        setup.state = setup.state.model_copy(update={key: getattr(QQDriverStatus(observed_at=NOW, active_scope=setup.scope,
            context_signature=setup.request.context_signature, connected=True, observer_ready=True, send_ready=True, paused=False), key) for key in change})
        assert (await setup.provider.status()).embodiment.effective_capabilities == set(CAPS)
        assert status.embodiment.activated_capabilities == set(CAPS)
    run(scenario())


@pytest.mark.parametrize("field,value", [("group_id", "10002"), ("self_member_id", "30002"),
    ("window_id", "0x999"), ("body_instance_id", "different-body"), ("environment_revision", 5)])
def test_request_or_live_scope_change_never_reaches_actor(setup, field, value):
    async def scenario():
        await setup.provider.activate()
        different = setup.scope.model_copy(update={field: value})
        with pytest.raises(QQCommunicationError, match="scope"):
            await setup.provider.send(setup.request.model_copy(update={"scope": different}))
        setup.state = setup.state.model_copy(update={"active_scope": different})
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_observer_signature_change_is_rechecked_after_readonly_plan_lookup(setup):
    async def scenario():
        original = setup.provider._load_frozen_plan
        def mutate(item):
            setup.state = setup.state.model_copy(update={"context_signature": "f" * 64})
            return original(item)
        setup.provider._load_frozen_plan = mutate
        await setup.provider.activate()
        with pytest.raises(QQCommunicationError, match="chat_context_changed"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_required_health_and_terminal_are_stronger_than_supports(setup):
    async def scenario():
        await setup.provider.activate()
        setup.provider.manager.set_terminal("synthetic_terminal")
        assert setup.provider.manager.supports(QQ_COMMUNICATION_SEND)  # Legacy manager only removes action.execute.
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


@pytest.mark.parametrize("change", ["provider", "revision", "limits", "condition", "new_capability"])
def test_activation_identity_limits_and_permissions_cannot_grow_after_fault(setup, change):
    async def scenario():
        if change == "new_capability":
            executor = setup.provider._send_executor
            setup.provider._send_executor = None
        await setup.provider.activate()
        original = setup.provider.capabilities
        if change == "condition":
            setup.provider.manager.condition.capability_requirements.clear()
        elif change == "new_capability":
            setup.provider._send_executor = executor
        else:
            async def mutated():
                value = await original()
                if change == "provider":
                    return value.model_copy(update={"provider_id": "different-provider"})
                if change == "revision":
                    return value.model_copy(update={"provider_revision": "new-version"})
                descriptor = value.capabilities[0].model_copy(update={"limits": {"max_turn_seconds": 600}})
                return value.model_copy(update={"capabilities": [descriptor, *value.capabilities[1:]]})
            setup.provider.capabilities = mutated
        await setup.provider.activate()  # Calling activate again is not permission to thaw the set.
        with pytest.raises(QQCommunicationError, match="activation_changed"):
            await setup.provider.send(setup.request)
        assert not setup.sends and setup.provider.manager.status().state == EmbodimentState.TERMINAL
    run(scenario())


@pytest.mark.parametrize("change", [{"text": "偷换段正文"}, {"decision_id": "qq-decision:other"},
    {"reservation_id": "qq-watch-send:other"}, {"segment_count": 1}, {"segment_index": 1}])
def test_send_must_match_independently_loaded_frozen_plan(setup, change):
    async def scenario():
        await setup.provider.activate()
        with pytest.raises(QQCommunicationError, match="frozen_plan"):
            await setup.provider.send(setup.request.model_copy(update=change))
        assert not setup.sends
    run(scenario())


def test_original_deadline_cannot_be_extended_and_expired_send_has_no_actor_call(setup):
    async def scenario():
        await setup.provider.activate()
        setup.plan = setup.plan.model_copy(update={"deadline": NOW + timedelta(seconds=50)})
        with pytest.raises(QQCommunicationError, match="frozen_plan_mismatch"):
            await setup.provider.send(setup.request)
        setup.clock[0] += timedelta(seconds=61)
        with pytest.raises(QQCommunicationError, match="request_expired"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_executor_cannot_self_certify_delivery_and_unknown_blocks_new_send(setup):
    async def scenario():
        await setup.provider.activate()
        def false_claim(item):
            setup.sends.append(item)
            claim = setup.sent_result(item, setup.persist(item))
            setup.store.clear()  # No independently durable receipt exists.
            return claim
        setup.provider._send_executor = false_claim
        result = await setup.provider.send(setup.request)
        assert result.status == "unknown" and result.receipt_id is None and result.delivered_at is None
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert len(setup.sends) == 1
        query = QQReconcileRequest(send_request=setup.request, requested_at=NOW, deadline=NOW + timedelta(seconds=10))
        reconciled = await setup.provider.reconcile(query)
        assert reconciled.status == "sent" and len(setup.sends) == len(setup.reconciles) == 1
        assert QQ_COMMUNICATION_SEND in (await setup.provider.status()).embodiment.effective_capabilities
    run(scenario())


@pytest.mark.parametrize("field,value", [("delivered_text", "not frozen"), ("decision_id", "qq-decision:wrong"),
    ("reservation_id", "qq-watch-send:wrong"), ("segment_index", 1), ("segment_count", 3)])
def test_unrelated_durable_receipt_cannot_confirm_this_send(setup, field, value):
    async def scenario():
        await setup.provider.activate()
        receipt = setup.persist(setup.request).model_copy(update={field: value})
        setup.store[(setup.request.reservation_id, 0)] = receipt
        with pytest.raises(QQCommunicationError, match="verified_receipt_mismatch"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_reconcile_can_read_existing_receipt_while_paused_and_original_send_expired(setup):
    async def scenario():
        await setup.provider.activate()
        setup.persist(setup.request)
        setup.clock[0] += timedelta(minutes=5)
        setup.state = setup.state.model_copy(update={"paused": True, "has_uncertain_delivery": True})
        query = QQReconcileRequest(send_request=setup.request, requested_at=setup.clock[0], deadline=setup.clock[0] + timedelta(seconds=10))
        result = await setup.provider.reconcile(query)
        assert result.status == "sent" and not setup.sends and not setup.reconciles
        assert not setup.provider.manager.supports(QQ_COMMUNICATION_SEND)
        assert setup.provider.manager.supports(QQ_COMMUNICATION_RECONCILE)
    run(scenario())


def test_readonly_reconcile_not_sent_claim_does_not_clear_unknown(setup):
    async def scenario():
        await setup.provider.activate()
        request = setup.request
        def uncertain(item):
            return QQDeliveryResult(scope=request.scope, decision_id=request.decision_id, reservation_id=request.reservation_id,
                segment_index=0, segment_count=2, status="not_sent", reason_code="not_found")
        setup.provider._reconcile_executor = uncertain
        query = QQReconcileRequest(send_request=request, requested_at=NOW, deadline=NOW + timedelta(seconds=10))
        assert (await setup.provider.reconcile(query)).status == "unknown"
        assert not setup.provider.manager.supports(QQ_COMMUNICATION_SEND) or (
            QQ_COMMUNICATION_SEND not in (await setup.provider.status()).embodiment.effective_capabilities)
        assert not setup.sends
    run(scenario())


def test_async_callbacks_use_same_negotiation_and_no_parallel_gui_actor(setup):
    async def scenario():
        await setup.provider.activate()
        entered, release = asyncio.Event(), asyncio.Event()
        async def suspended(item):
            setup.sends.append(item)
            entered.set()
            await release.wait()
            return setup.sent_result(item, setup.persist(item))
        setup.provider._send_executor = suspended
        first = asyncio.create_task(setup.provider.send(setup.request))
        await entered.wait()
        with pytest.raises(QQCommunicationError, match="operation_busy"):
            await setup.provider.send(setup.request)
        release.set()
        assert (await first).status == "sent" and len(setup.sends) == 1
    run(scenario())


def test_actor_exception_does_not_leak_text_or_authorize_retry(setup):
    async def scenario():
        await setup.provider.activate()
        def broken(item):
            setup.sends.append(item)
            raise RuntimeError("PRIVATE_TEXT_AND_TOKEN")
        setup.provider._send_executor = broken
        result = await setup.provider.send(setup.request)
        assert result.status == "unknown" and result.reason_code == "driver_delivery_uncertain"
        assert "PRIVATE" not in result.model_dump_json()
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.send(setup.request)
        assert len(setup.sends) == 1
    run(scenario())


def test_slow_sync_callback_returns_unknown_after_original_deadline_without_extension(setup):
    async def scenario():
        await setup.provider.activate()
        def slow(item):
            setup.sends.append(item)
            setup.clock[0] += timedelta(seconds=61)
            return setup.sent_result(item, setup.persist(item))
        setup.provider._send_executor = slow
        assert (await setup.provider.send(setup.request)).status == "unknown"
        assert len(setup.sends) == 1 and setup.request.deadline == NOW + timedelta(seconds=60)
    run(scenario())


@pytest.mark.parametrize("change", [{"text": "bad\nline"}, {"segment_index": 2}, {"segment_count": True},
    {"deadline": NOW + timedelta(seconds=61)}, {"requested_at": NOW.replace(tzinfo=None)}, {"unexpected": "field"}])
def test_strict_requests_reject_malformed_or_unbounded_send_before_actor(setup, change):
    with pytest.raises(ValidationError):
        QQSendRequest.model_validate({**setup.request.model_dump(), **change})
    assert not setup.sends


def test_reconcile_budget_cannot_extend_past_fifteen_seconds(setup):
    with pytest.raises(ValidationError, match="15 seconds"):
        QQReconcileRequest(send_request=setup.request, requested_at=NOW, deadline=NOW + timedelta(seconds=16))


def test_health_failure_is_sanitized_and_does_not_invoke_read_or_send(setup):
    async def scenario():
        await setup.provider.activate()
        def failure():
            raise RuntimeError("PRIVATE_DRIVER_CONTENT")
        setup.provider._driver_status = failure
        status = await setup.provider.status()
        assert "PRIVATE" not in status.model_dump_json()
        with pytest.raises(QQCommunicationError, match="unavailable"):
            await setup.provider.read(QQReadRequest(scope=setup.scope, requested_at=NOW, deadline=NOW + timedelta(seconds=5)))
        assert not setup.reads and not setup.sends
    run(scenario())


def test_required_reconcile_health_failure_blocks_send_even_when_send_supported(setup):
    async def scenario():
        await setup.provider.activate()
        original = setup.provider.health
        async def unhealthy_required_capability():
            value = await original()
            return value.model_copy(update={"capability_health": {
                **value.capability_health, QQ_COMMUNICATION_RECONCILE: HealthStatus.UNAVAILABLE}})
        setup.provider.health = unhealthy_required_capability
        state = await setup.provider.status()
        assert setup.provider.manager.supports(QQ_COMMUNICATION_SEND)
        assert state.embodiment.state == EmbodimentState.UNAVAILABLE
        with pytest.raises(QQCommunicationError, match="required_capability_unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_different_provider_major_cannot_satisfy_frozen_send_requirement(setup):
    async def scenario():
        original = setup.provider.capabilities
        async def wrong_major():
            value = await original()
            return value.model_copy(update={"capabilities": [item.model_copy(update={
                "capability": "communication.qq.send.v2"}) if item.capability == QQ_COMMUNICATION_SEND
                else item for item in value.capabilities]})
        setup.provider.capabilities = wrong_major
        state = await setup.provider.activate()
        assert QQ_COMMUNICATION_SEND in state.embodiment.negotiated.incompatible_required
        with pytest.raises(QQCommunicationError, match="capability_unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


def test_cancelled_actor_remains_uncertain_and_releases_only_execution_lease(setup):
    async def scenario():
        await setup.provider.activate()
        entered = asyncio.Event()
        async def interrupted(item):
            setup.sends.append(item)
            entered.set()
            await asyncio.Event().wait()
        setup.provider._send_executor = interrupted
        task = asyncio.create_task(setup.provider.send(setup.request))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        with pytest.raises(QQCommunicationError, match="capability_unavailable"):
            await setup.provider.send(setup.request)
        request = QQReconcileRequest(send_request=setup.request, requested_at=NOW,
                                     deadline=NOW + timedelta(seconds=10))
        assert (await setup.provider.reconcile(request)).status == "sent"
        assert len(setup.sends) == len(setup.reconciles) == 1
    run(scenario())


def test_missing_frozen_plan_cannot_enter_actor(setup):
    async def scenario():
        await setup.provider.activate()
        setup.plan = None
        with pytest.raises(QQCommunicationError, match="frozen_plan_unavailable"):
            await setup.provider.send(setup.request)
        assert not setup.sends
    run(scenario())


@pytest.mark.parametrize("stale_field", ["status", "read"])
def test_stale_driver_or_read_evidence_is_not_usable(setup, stale_field):
    async def scenario():
        await setup.provider.activate()
        if stale_field == "status":
            setup.provider._driver_status = lambda: setup.state.model_copy(update={
                "observed_at": NOW - timedelta(seconds=6)})
        else:
            original = setup.provider._read_executor
            setup.provider._read_executor = lambda request: original(request).model_copy(update={
                "observed_at": NOW - timedelta(seconds=6)})
        request = QQReadRequest(scope=setup.scope, requested_at=NOW,
                                deadline=NOW + timedelta(seconds=5))
        with pytest.raises(QQCommunicationError, match="unavailable|stale_or_unbound_read"):
            await setup.provider.read(request)
        assert not setup.sends
    run(scenario())
