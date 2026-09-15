"""Negotiated QQ port delegating to the one existing GUI/ledger driver.

No transport, GUI executor, thread or receipt store is constructed here. Sync
callbacks execute in the calling driver's thread and must honor their supplied
deadlines. Async callbacks are awaited within the same remaining budget.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, datetime, timedelta
import hashlib
import inspect
import json
import threading
from typing import Any, TypeVar

from harness.embodiment.capabilities import (
    EVIDENCE_REFERENCE, QQ_COMMUNICATION_READ, QQ_COMMUNICATION_RECONCILE,
    QQ_COMMUNICATION_SEND, CapabilityDescriptor, CapabilityStatus, EmbodimentState,
    HealthStatus, ProviderCapabilities, ProviderHealth,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.qq_contracts import (
    QQCommunicationStatus, QQDeliveryResult, QQDriverStatus, QQFrozenSendPlan,
    QQReadRequest, QQReadResult, QQReconcileRequest, QQScope, QQSendRequest,
    QQVerifiedReceipt,
)
from harness.research.manifest import EmbodimentCondition

T = TypeVar("T")
Callback = Callable[..., T | Awaitable[T]]
PROVIDER_ID = "qq-communication-driver"
QQ_CAPABILITIES = (QQ_COMMUNICATION_READ, QQ_COMMUNICATION_SEND, QQ_COMMUNICATION_RECONCILE, EVIDENCE_REFERENCE)


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


class QQCommunicationError(RuntimeError):
    """A fixed diagnostic code; never prints driver exception text or messages."""

    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


class QQCommunicationProvider:
    def __init__(
        self, *, condition: EmbodimentCondition, allowed_scopes: Sequence[QQScope],
        driver_status: Callback[QQDriverStatus], read_executor: Callback[QQReadResult] | None,
        send_executor: Callback[QQDeliveryResult] | None,
        reconcile_executor: Callback[QQDeliveryResult] | None,
        load_frozen_plan: Callback[QQFrozenSendPlan | None],
        load_verified_receipt: Callback[QQVerifiedReceipt | None],
        clock: Callable[[], datetime] = _utc_now,
    ):
        scopes = tuple(QQScope.model_validate(item.model_dump() if isinstance(item, QQScope) else item)
                       for item in allowed_scopes)
        if not scopes or len(scopes) > 16 or len(set(scopes)) != len(scopes):
            raise ValueError("QQ provider requires one to sixteen distinct frozen scopes")
        if len({scope.body_instance_id for scope in scopes}) != 1:
            raise ValueError("QQ provider binds exactly one body instance")
        if (any(not callable(item) for item in (driver_status, load_frozen_plan, load_verified_receipt))
                or any(item is not None and not callable(item) for item in (read_executor, send_executor, reconcile_executor))):
            raise ValueError("QQ provider requires explicit driver callbacks")
        self._allowed_scopes = scopes
        self._clock = clock
        self._driver_status = driver_status
        self._read_executor = read_executor
        self._send_executor = send_executor
        self._reconcile_executor = reconcile_executor
        self._load_frozen_plan = load_frozen_plan
        self._load_verified_receipt = load_verified_receipt
        self.manager = EmbodimentManager(condition.model_copy(deep=True), self)
        self._condition_hash = _digest(self.manager.condition.model_dump(mode="json"))
        self._scope_hash = _digest([scope.model_dump(mode="json") for scope in scopes])
        self._capability_hash: str | None = None
        self._activated = False
        self._driver: QQDriverStatus | None = None
        self._uncertain: set[tuple[str, int]] = set()
        # Shared across caller event loops; this is only an exclusion gate, not
        # another executor. No second send/reconciliation may enter the actor.
        self._execution_lock = threading.Lock()

    async def _call(self, callback, *args, deadline: datetime):
        remaining = (deadline - self._clock()).total_seconds()
        if remaining <= 0:
            raise QQCommunicationError("request_expired")
        try:
            value = callback(*args)
            if inspect.isawaitable(value):
                remaining = (deadline - self._clock()).total_seconds()
                if remaining <= 0:
                    if inspect.iscoroutine(value):
                        value.close()
                    raise QQCommunicationError("request_expired")
                value = await asyncio.wait_for(value, timeout=remaining)
        except QQCommunicationError:
            raise
        except Exception as exc:
            raise QQCommunicationError("driver_callback_failed") from exc
        if self._clock() >= deadline:
            raise QQCommunicationError("request_expired")
        return value

    async def capabilities(self) -> ProviderCapabilities:
        implemented = {
            QQ_COMMUNICATION_READ: self._read_executor is not None,
            QQ_COMMUNICATION_SEND: self._send_executor is not None,
            QQ_COMMUNICATION_RECONCILE: self._reconcile_executor is not None,
            EVIDENCE_REFERENCE: True,
        }
        return ProviderCapabilities(
            provider_id=PROVIDER_ID, provider_kind="qq_existing_gui_driver",
            provider_revision="qq-communication-v1:" + self._scope_hash,
            body_instance_id=self._allowed_scopes[0].body_instance_id,
            observed_at=self._clock(), capabilities=[CapabilityDescriptor(
                capability=name, status=CapabilityStatus.AVAILABLE if implemented[name] else CapabilityStatus.UNAVAILABLE,
                implementation_version="1", limits={"scope_sha256": self._scope_hash,
                    "max_segments": 3, "max_segment_characters": 4000, "max_turn_seconds": 60,
                    "max_reconcile_seconds": 15, "one_existing_driver": True,
                    "durable_receipt_required": True},
            ) for name in QQ_CAPABILITIES],
        )

    async def health(self) -> ProviderHealth:
        try:
            value = await self._call(self._driver_status, deadline=self._clock() + timedelta(seconds=3))
            status = QQDriverStatus.model_validate(value.model_dump() if isinstance(value, QQDriverStatus) else value)
            self._driver = status
            age = (self._clock() - status.observed_at).total_seconds()
            bound = status.active_scope in self._allowed_scopes
            connected = status.connected and bound and -2 <= age <= 5
            read = connected and status.observer_ready and status.context_signature is not None
            send = read and status.send_ready and not status.paused and not status.has_uncertain_delivery and not self._uncertain
            health = {QQ_COMMUNICATION_READ: read, QQ_COMMUNICATION_SEND: send,
                      QQ_COMMUNICATION_RECONCILE: connected, EVIDENCE_REFERENCE: connected}
            overall = HealthStatus.HEALTHY if all(health.values()) else HealthStatus.DEGRADED if connected else HealthStatus.UNAVAILABLE
            reason = status.reason_code or ("driver_ready" if send else "driver_send_unavailable")
        except Exception:
            self._driver = None
            health = dict.fromkeys(QQ_CAPABILITIES, False)
            overall, reason = HealthStatus.UNAVAILABLE, "driver_status_unavailable"
        return ProviderHealth(
            provider_id=PROVIDER_ID, body_instance_id=self._allowed_scopes[0].body_instance_id,
            status=overall, capability_health={key: HealthStatus.HEALTHY if value else HealthStatus.UNAVAILABLE for key, value in health.items()},
            observed_at=self._clock(), detail={"reason_code": reason},
        )

    @staticmethod
    def _capability_identity(value: ProviderCapabilities) -> str:
        return _digest(value.model_dump(mode="json", exclude={"observed_at"}))

    async def activate(self) -> QQCommunicationStatus:
        if self._activated:
            return await self.status()
        self._activated = True
        self._capability_hash = self._capability_identity(await self.capabilities())
        await self.manager.activate()
        return await self.status()

    async def _refresh(self):
        if not self._activated:
            raise QQCommunicationError("provider_not_activated")
        if (_digest(self.manager.condition.model_dump(mode="json")) != self._condition_hash
                or _digest([scope.model_dump(mode="json") for scope in self._allowed_scopes]) != self._scope_hash
                or self._capability_identity(await self.capabilities()) != self._capability_hash):
            self.manager.set_terminal("qq_provider_activation_changed")
            raise QQCommunicationError("provider_activation_changed")
        state = await self.manager.refresh()
        if self._capability_identity(await self.capabilities()) != self._capability_hash:
            self.manager.set_terminal("qq_provider_activation_changed")
            raise QQCommunicationError("provider_activation_changed")
        return state

    async def status(self) -> QQCommunicationStatus:
        if self._activated:
            try:
                await self._refresh()
            except QQCommunicationError:
                pass
        return QQCommunicationStatus(embodiment=self.manager.status(), driver=self._driver)

    async def _guard(self, scope: QQScope, capability: str, *, context_signature: str | None = None):
        if scope not in self._allowed_scopes:
            raise QQCommunicationError("scope_not_allowed")
        state = await self._refresh()
        negotiated = state.negotiated
        if (negotiated is None or not negotiated.required_satisfied
                or state.state in {EmbodimentState.DISABLED, EmbodimentState.TERMINAL}
                or state.health is None or state.health.status == HealthStatus.UNAVAILABLE
                or not self.manager.supports(capability) or not self.manager.supports(EVIDENCE_REFERENCE)):
            raise QQCommunicationError("capability_unavailable")
        if self._driver is None or self._driver.active_scope != scope:
            raise QQCommunicationError("active_scope_changed")
        if capability == QQ_COMMUNICATION_SEND:
            required = {item.capability for item in self.manager.condition.capability_requirements if item.required}
            if (self.manager.condition.mode not in {"grounded", "active"}
                    or state.state not in {EmbodimentState.AVAILABLE, EmbodimentState.DEGRADED}
                    or not required.issubset(state.effective_capabilities)):
                raise QQCommunicationError("required_capability_unavailable")
            if self._driver.context_signature != context_signature:
                raise QQCommunicationError("chat_context_changed")

    def _request_time(self, request):
        moment = self._clock()
        if request.requested_at > moment + timedelta(seconds=2):
            raise QQCommunicationError("request_time_invalid")
        if moment >= request.deadline:
            raise QQCommunicationError("request_expired")

    async def read(self, request: QQReadRequest) -> QQReadResult:
        request = QQReadRequest.model_validate(request.model_dump())
        self._request_time(request)
        await self._guard(request.scope, QQ_COMMUNICATION_READ)
        value = await self._call(self._read_executor, request, deadline=request.deadline)
        try:
            result = QQReadResult.model_validate(value.model_dump() if isinstance(value, QQReadResult) else value)
        except Exception as exc:
            raise QQCommunicationError("invalid_read_result") from exc
        await self._guard(request.scope, QQ_COMMUNICATION_READ)
        if (result.scope != request.scope or self._driver.context_signature != result.context_signature
                or not -2 <= (self._clock() - result.observed_at).total_seconds() <= 5):
            raise QQCommunicationError("stale_or_unbound_read")
        self._request_time(request)
        return result

    async def _plan(self, request: QQSendRequest, *, lookup_deadline: datetime, sending: bool) -> QQFrozenSendPlan:
        value = await self._call(self._load_frozen_plan, request, deadline=lookup_deadline)
        try:
            plan = QQFrozenSendPlan.model_validate(value.model_dump() if isinstance(value, QQFrozenSendPlan) else value)
        except Exception as exc:
            raise QQCommunicationError("frozen_plan_unavailable") from exc
        if (plan.scope != request.scope or plan.decision_id != request.decision_id
                or plan.reservation_id != request.reservation_id or len(plan.reply_segments) != request.segment_count
                or plan.reply_segments[request.segment_index] != request.text
                or request.deadline > plan.deadline or request.requested_at < plan.first_seen_at - timedelta(seconds=2)):
            raise QQCommunicationError("frozen_plan_mismatch")
        if sending and self._clock() >= plan.deadline:
            raise QQCommunicationError("request_expired")
        return plan

    @staticmethod
    def _bound(value, request: QQSendRequest) -> bool:
        return (value.scope == request.scope and value.decision_id == request.decision_id
                and value.reservation_id == request.reservation_id and value.segment_index == request.segment_index
                and value.segment_count == request.segment_count)

    @staticmethod
    def _result(request, status, reason, **fields):
        return QQDeliveryResult(scope=request.scope, decision_id=request.decision_id, reservation_id=request.reservation_id,
            segment_index=request.segment_index, segment_count=request.segment_count, status=status, reason_code=reason, **fields)

    async def _receipt(self, request, plan, *, deadline) -> QQDeliveryResult | None:
        value = await self._call(self._load_verified_receipt, request, deadline=deadline)
        if value is None:
            return None
        try:
            receipt = QQVerifiedReceipt.model_validate(value.model_dump() if isinstance(value, QQVerifiedReceipt) else value)
        except Exception as exc:
            raise QQCommunicationError("invalid_verified_receipt") from exc
        if (not self._bound(receipt, request) or receipt.delivered_text != request.text
                or not plan.first_seen_at <= receipt.delivered_at <= self._clock() + timedelta(seconds=2)):
            raise QQCommunicationError("verified_receipt_mismatch")
        self._uncertain.discard((request.reservation_id, request.segment_index))
        return self._result(request, "sent", "durable_receipt_verified", receipt_id=receipt.receipt_id,
                            delivered_at=receipt.delivered_at, evidence_refs=receipt.evidence_refs)

    async def send(self, request: QQSendRequest) -> QQDeliveryResult:
        request = QQSendRequest.model_validate(request.model_dump())
        self._request_time(request)
        if not self._execution_lock.acquire(blocking=False):
            raise QQCommunicationError("driver_operation_busy")
        try:
            await self._guard(request.scope, QQ_COMMUNICATION_SEND, context_signature=request.context_signature)
            plan = await self._plan(request, lookup_deadline=request.deadline, sending=True)
            existing = await self._receipt(request, plan, deadline=request.deadline)
            if existing is not None:
                return existing
            # Read-only loaders may yield. Recheck pause, identity, health and
            # context immediately before entering the existing driver's actor.
            await self._guard(request.scope, QQ_COMMUNICATION_SEND, context_signature=request.context_signature)
            self._request_time(request)
            try:
                value = await self._call(self._send_executor, request, deadline=request.deadline)
                result = QQDeliveryResult.model_validate(value.model_dump() if isinstance(value, QQDeliveryResult) else value)
                if not self._bound(result, request):
                    raise QQCommunicationError("unbound_delivery_result")
                if result.status == "not_sent":
                    return result
                receipt = await self._receipt(request, plan, deadline=request.deadline)
                if receipt is not None:
                    if result.receipt_id is not None and result.receipt_id != receipt.receipt_id:
                        raise QQCommunicationError("verified_receipt_mismatch")
                    return receipt
                self._uncertain.add((request.reservation_id, request.segment_index))
                return self._result(request, "unknown", "delivery_not_independently_verified", evidence_refs=result.evidence_refs)
            except BaseException as exc:
                self._uncertain.add((request.reservation_id, request.segment_index))
                if not isinstance(exc, Exception):
                    raise
                return self._result(request, "unknown", "driver_delivery_uncertain")
        finally:
            self._execution_lock.release()

    async def reconcile(self, request: QQReconcileRequest) -> QQDeliveryResult:
        request = QQReconcileRequest.model_validate(request.model_dump())
        self._request_time(request)
        original = request.send_request
        if not self._execution_lock.acquire(blocking=False):
            raise QQCommunicationError("driver_operation_busy")
        try:
            await self._guard(original.scope, QQ_COMMUNICATION_RECONCILE)
            plan = await self._plan(original, lookup_deadline=request.deadline, sending=False)
            receipt = await self._receipt(original, plan, deadline=request.deadline)
            if receipt is not None:
                return receipt
            try:
                value = await self._call(self._reconcile_executor, request, deadline=request.deadline)
                result = QQDeliveryResult.model_validate(value.model_dump() if isinstance(value, QQDeliveryResult) else value)
                if not self._bound(result, original):
                    raise QQCommunicationError("unbound_delivery_result")
                receipt = await self._receipt(original, plan, deadline=request.deadline)
                if receipt is not None:
                    return receipt
                # A missing receipt or executor's 'not_sent' assertion cannot
                # clear a possibly executed click, nor permit another send.
                self._uncertain.add((original.reservation_id, original.segment_index))
                return self._result(original, "unknown", "delivery_not_independently_verified", evidence_refs=result.evidence_refs)
            except BaseException as exc:
                self._uncertain.add((original.reservation_id, original.segment_index))
                if not isinstance(exc, Exception):
                    raise
                return self._result(original, "unknown", "driver_reconciliation_uncertain")
        finally:
            self._execution_lock.release()
