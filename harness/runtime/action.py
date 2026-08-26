"""Single-foreground action coordination and cooperative preemption."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Protocol

from harness.contracts.decisions import GoalProposal
from harness.contracts.envelope import VersionVector
from harness.runtime.models import (
    ActionBackendResult,
    ActionExecution,
    ActionExecutionState,
    DeliveryResult,
)


def _utc_now() -> datetime:
    return datetime.now(UTC)


class ActionBackend(Protocol):
    async def execute(self, execution: ActionExecution) -> ActionBackendResult: ...

    async def request_safe_point(self, execution: ActionExecution) -> ActionBackendResult: ...

    async def verify(self, execution: ActionExecution) -> ActionBackendResult: ...

    async def reconcile(self, execution: ActionExecution) -> ActionBackendResult: ...


class DeliveryBackend(Protocol):
    async def deliver(
        self,
        *,
        text: str,
        segments: list[str],
        channel: str,
        idempotency_key: str,
        deadline: datetime,
    ) -> DeliveryResult: ...


class NoopActionBackend:
    async def execute(self, execution: ActionExecution) -> ActionBackendResult:
        return ActionBackendResult(success=False, summary="no ActionBackend configured")

    async def request_safe_point(self, execution: ActionExecution) -> ActionBackendResult:
        return ActionBackendResult(
            success=False,
            summary="no active backend step",
            safe_point="not_started",
            continuation=execution.continuation,
            retry_safe=True,
        )

    async def verify(self, execution: ActionExecution) -> ActionBackendResult:
        return ActionBackendResult(success=False, summary="result cannot be verified", unknown=True)

    async def reconcile(self, execution: ActionExecution) -> ActionBackendResult:
        return await self.verify(execution)


class InMemoryDeliveryBackend:
    """Deterministic delivery backend useful for chat adapters and tests."""

    def __init__(self) -> None:
        self.deliveries: dict[str, DeliveryResult] = {}

    async def deliver(
        self,
        *,
        text: str,
        segments: list[str],
        channel: str,
        idempotency_key: str,
        deadline: datetime,
    ) -> DeliveryResult:
        if idempotency_key in self.deliveries:
            return self.deliveries[idempotency_key]
        if _utc_now() >= deadline:
            return DeliveryResult(success=False, error="delivery deadline expired")
        delivered = list(segments) or ([text] if text else [])
        if not delivered:
            return DeliveryResult(success=False, error="empty reply")
        result = DeliveryResult(
            success=True,
            delivered_segments=delivered,
            external_ref=f"memory-delivery:{idempotency_key}",
        )
        self.deliveries[idempotency_key] = result
        return result


ConsistencyBarrier = Callable[[ActionExecution], Awaitable[tuple[bool, VersionVector, str | None]]]


class ActionCoordinator:
    def __init__(
        self,
        backend: ActionBackend | None = None,
        *,
        consistency_barrier: ConsistencyBarrier | None = None,
    ) -> None:
        self.backend = backend or NoopActionBackend()
        self.consistency_barrier = consistency_barrier
        self._lock = asyncio.Lock()
        self._actions: dict[str, ActionExecution] = {}
        self._current_action_id: str | None = None
        self._revision = 0

    @property
    def revision(self) -> int:
        return self._revision

    @property
    def current(self) -> ActionExecution | None:
        return self._actions.get(self._current_action_id) if self._current_action_id else None

    def get(self, action_id: str) -> ActionExecution | None:
        return self._actions.get(action_id)

    def pending(self) -> list[ActionExecution]:
        return [action for action in self._actions.values() if not action.terminal]

    def restore(self, action: ActionExecution) -> None:
        """Restore the latest journaled action state before reconciliation."""

        self._actions[action.action_id] = action
        if action.state in {ActionExecutionState.RUNNING, ActionExecutionState.VERIFYING}:
            if self._current_action_id not in {None, action.action_id}:
                raise RuntimeError("journal contains multiple foreground actions")
            self._current_action_id = action.action_id
        self._revision += 1

    def set_revision_floor(self, revision: int) -> None:
        if revision < 0:
            raise ValueError("revision must be non-negative")
        self._revision = max(self._revision, revision)

    async def propose(
        self,
        proposal: GoalProposal,
        *,
        idempotency_key: str,
        deadline: datetime,
        version_vector: VersionVector,
        risk_class: str | None = None,
    ) -> ActionExecution:
        async with self._lock:
            existing = next(
                (action for action in self._actions.values() if action.idempotency_key == idempotency_key),
                None,
            )
            if existing is not None:
                return existing
            requires_approval = bool(proposal.required_approvals) or risk_class in {
                "high",
                "critical",
                "high_impact",
            }
            state = (
                ActionExecutionState.AWAITING_APPROVAL
                if requires_approval
                else ActionExecutionState.READY
            )
            action = ActionExecution(
                goal_id=proposal.goal_id,
                goal=proposal.goal,
                idempotency_key=idempotency_key,
                state=state,
                risk_class=risk_class or proposal.permission_class,
                reversible=proposal.reversible,
                verification_method=proposal.verification_method,
                required_approvals=list(proposal.required_approvals),
                version_vector=version_vector,
                deadline=deadline,
            )
            self._actions[action.action_id] = action
            self._revision += 1
            return action

    async def approve(self, action_id: str, *, approval: str) -> ActionExecution:
        async with self._lock:
            action = self._require(action_id)
            approvals = list(dict.fromkeys([*action.approvals, approval]))
            required = set(action.required_approvals)
            ready = not required or required.issubset(approvals)
            action = action.model_copy(
                update={
                    "approvals": approvals,
                    "state": ActionExecutionState.READY if ready else ActionExecutionState.AWAITING_APPROVAL,
                    "updated_at": _utc_now(),
                }
            )
            self._actions[action_id] = action
            self._revision += 1
            return action

    async def reject(self, action_id: str, *, reason: str) -> ActionExecution:
        async with self._lock:
            action = self._require(action_id).model_copy(
                update={
                    "state": ActionExecutionState.CANCELLED,
                    "error": reason,
                    "updated_at": _utc_now(),
                }
            )
            self._actions[action_id] = action
            if self._current_action_id == action_id:
                self._current_action_id = None
            self._revision += 1
            return action

    async def execute(self, action_id: str) -> ActionExecution:
        async with self._lock:
            action = self._require(action_id)
            if action.state == ActionExecutionState.READY:
                action = self._reserve_locked(action)
            elif not (
                action.state == ActionExecutionState.RUNNING
                and self._current_action_id == action_id
            ):
                raise RuntimeError(f"action {action_id} is not reserved for execution")

        if action.high_impact:
            if self.consistency_barrier is None:
                return await self._finish_failed(action, "high-impact consistency barrier is unavailable")
            allowed, versions, reason = await self.consistency_barrier(action)
            if not allowed:
                return await self._finish_failed(action, reason or "consistency barrier rejected action")
            async with self._lock:
                action = self._replace(action, version_vector=versions)

        try:
            result = await self.backend.execute(action)
        except Exception as exc:
            result = ActionBackendResult(success=False, summary=str(exc), unknown=not action.reversible)

        async with self._lock:
            latest = self._require(action_id)
            if latest.state in {
                ActionExecutionState.CANCELLED,
                ActionExecutionState.UNKNOWN,
            }:
                return latest
            action = latest

        if result.unknown:
            return await self._finish(action, result, ActionExecutionState.UNKNOWN)
        if not result.success:
            return await self._finish(action, result, ActionExecutionState.FAILED)

        async with self._lock:
            action = self._replace(
                action,
                state=ActionExecutionState.VERIFYING,
                safe_point=result.safe_point,
                continuation=result.continuation,
            )
        try:
            verified = await self.backend.verify(action)
        except Exception as exc:
            verified = ActionBackendResult(success=False, summary=str(exc), unknown=True)
        if verified.unknown:
            return await self._finish(action, verified, ActionExecutionState.UNKNOWN)
        return await self._finish(
            action,
            verified,
            ActionExecutionState.SUCCEEDED if verified.success else ActionExecutionState.FAILED,
        )

    async def reserve(self, action_id: str) -> ActionExecution:
        async with self._lock:
            return self._reserve_locked(self._require(action_id))

    async def request_preemption(self, *, reason: str) -> ActionExecution | None:
        async with self._lock:
            action = self.current
            if action is None:
                return None
            if action.state not in {ActionExecutionState.RUNNING, ActionExecutionState.VERIFYING}:
                return action
        try:
            result = await self.backend.request_safe_point(action)
        except Exception as exc:
            result = ActionBackendResult(success=False, summary=str(exc), unknown=not action.reversible)
        if result.unknown or (not action.reversible and not result.safe_point):
            return await self._finish(action, result, ActionExecutionState.UNKNOWN)
        async with self._lock:
            latest = self._require(action.action_id)
            if latest.terminal:
                return latest
            updated = self._replace(
                latest,
                state=ActionExecutionState.CANCELLED,
                safe_point=result.safe_point,
                continuation=result.continuation,
                error=reason,
                outcome={"summary": result.summary},
            )
            self._current_action_id = None
            return updated

    async def reconcile(self, action_id: str) -> ActionExecution:
        async with self._lock:
            action = self._require(action_id)
        result = await self.backend.reconcile(action)
        if result.unknown:
            state = ActionExecutionState.UNKNOWN
        elif result.success:
            state = ActionExecutionState.SUCCEEDED
        elif result.retry_safe:
            state = ActionExecutionState.FAILED
        else:
            state = ActionExecutionState.UNKNOWN
        return await self._finish(action, result, state)

    async def resolve_unknown(
        self,
        action_id: str,
        *,
        result: ActionBackendResult,
    ) -> ActionExecution:
        async with self._lock:
            action = self._require(action_id)
            if action.state != ActionExecutionState.UNKNOWN:
                raise RuntimeError(f"action {action_id} is not UNKNOWN")
        state = ActionExecutionState.SUCCEEDED if result.success else ActionExecutionState.FAILED
        return await self._finish(action, result, state)

    async def _finish_failed(self, action: ActionExecution, error: str) -> ActionExecution:
        result = ActionBackendResult(success=False, summary=error)
        return await self._finish(action, result, ActionExecutionState.FAILED)

    async def _finish(
        self,
        action: ActionExecution,
        result: ActionBackendResult,
        state: ActionExecutionState,
    ) -> ActionExecution:
        async with self._lock:
            latest = self._require(action.action_id)
            if latest.terminal and not action.terminal:
                return latest
            action = latest
            updated = self._replace(
                action,
                state=state,
                safe_point=result.safe_point,
                continuation=result.continuation,
                outcome=result.model_dump(mode="json"),
                error=None if result.success else result.summary,
            )
            if self._current_action_id == action.action_id:
                self._current_action_id = None
            return updated

    def _replace(self, action: ActionExecution, **updates: object) -> ActionExecution:
        updated = action.model_copy(update={**updates, "updated_at": _utc_now()})
        self._actions[action.action_id] = updated
        self._revision += 1
        return updated

    def _reserve_locked(self, action: ActionExecution) -> ActionExecution:
        if action.state != ActionExecutionState.READY:
            raise RuntimeError(f"action {action.action_id} is not READY")
        if self._current_action_id is not None and self._current_action_id != action.action_id:
            raise RuntimeError("another foreground action is already running")
        if _utc_now() >= action.deadline:
            return self._replace(
                action,
                state=ActionExecutionState.FAILED,
                error="action deadline expired",
            )
        self._current_action_id = action.action_id
        return self._replace(action, state=ActionExecutionState.RUNNING)

    def _require(self, action_id: str) -> ActionExecution:
        action = self._actions.get(action_id)
        if action is None:
            raise KeyError(action_id)
        return action
