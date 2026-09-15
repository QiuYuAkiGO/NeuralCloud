"""The last execution guard applies to every risk tier and cannot revive work."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from harness.contracts.decisions import GoalProposal
from harness.contracts.envelope import VersionVector
from harness.runtime.action import ActionCoordinator
from harness.runtime.models import ActionBackendResult, ActionExecutionState, utc_now


class Backend:
    def __init__(self, order=None):
        self.executed = []
        self.order = [] if order is None else order

    async def execute(self, action):
        self.executed.append(action)
        self.order.append("execute")
        return ActionBackendResult(success=True, summary="effect", evidence_refs=["test:effect"])

    async def verify(self, action):
        self.order.append("verify")
        return ActionBackendResult(success=True, summary="verified", evidence_refs=["test:verification"])


async def propose(coordinator, risk="low", *, deadline=None):
    action = await coordinator.propose(GoalProposal(policy_id="shared-choice", goal="test effect",
        success_criteria=["verified effect"], permission_class=risk, reversible=True,
        verification_method="test-independent-verification"), idempotency_key="one-effect",
        deadline=deadline or utc_now() + timedelta(seconds=10), version_vector=VersionVector(), risk_class=risk)
    if action.state == ActionExecutionState.AWAITING_APPROVAL:
        action = await coordinator.approve(action.action_id, approval="researcher")
    return action


@pytest.mark.parametrize("risk", ["low", "medium", "high", "critical", "high_impact"])
def test_guard_rejection_prevents_backend_for_every_risk_level(risk):
    async def scenario():
        order = []
        backend = Backend(order)

        async def barrier(action):
            order.append("barrier")
            return True, VersionVector(runtime_action=4), None

        async def guard(action):
            order.append("guard")
            if action.high_impact:
                assert action.version_vector.runtime_action == 4
            return False, "capability ownership changed"

        coordinator = ActionCoordinator(backend, consistency_barrier=barrier, execution_guard=guard)
        action = await propose(coordinator, risk)
        result = await coordinator.execute(action.action_id)
        assert result.state == ActionExecutionState.FAILED
        assert result.error == "capability ownership changed"
        assert result.outcome["evidence_refs"] == []
        assert backend.executed == [] and coordinator.current is None
        assert order == (["barrier", "guard"] if action.high_impact else ["guard"])
    asyncio.run(scenario())


def test_no_guard_preserves_legacy_execution():
    async def scenario():
        backend = Backend()
        coordinator = ActionCoordinator(backend)
        action = await propose(coordinator)
        result = await coordinator.execute(action.action_id)
        assert result.state == ActionExecutionState.SUCCEEDED
        assert backend.order == ["execute", "verify"]
    asyncio.run(scenario())


def test_guard_exception_does_not_expose_exception_text_or_execute():
    async def scenario():
        backend = Backend()

        async def guard(action):
            raise RuntimeError("PRIVATE_PROVIDER_OR_CREDENTIAL_DATA")

        coordinator = ActionCoordinator(backend, execution_guard=guard)
        action = await propose(coordinator)
        result = await coordinator.execute(action.action_id)
        assert result.state == ActionExecutionState.FAILED and result.error == "execution guard failed"
        assert "PRIVATE" not in result.model_dump_json()
        assert backend.executed == []
    asyncio.run(scenario())


def test_guard_restore_is_reread_before_backend_execute():
    async def scenario():
        backend = Backend()

        async def guard(action):
            coordinator.restore(action.model_copy(update={"continuation": {"fresh_observation_id": "observation-2"}}))
            return True, None

        coordinator = ActionCoordinator(backend, execution_guard=guard)
        action = await propose(coordinator)
        result = await coordinator.execute(action.action_id)
        assert result.state == ActionExecutionState.SUCCEEDED
        assert backend.executed[0].continuation == {"fresh_observation_id": "observation-2"}
    asyncio.run(scenario())


@pytest.mark.parametrize("blocked_stage", ["guard", "barrier"])
def test_preemption_while_validation_awaits_never_revives_cancelled_action(blocked_stage):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        backend = Backend()

        async def barrier(action):
            if blocked_stage == "barrier":
                entered.set()
                await release.wait()
            return True, VersionVector(runtime_action=4), None

        async def guard(action):
            if blocked_stage == "guard":
                entered.set()
                await release.wait()
            return True, None

        coordinator = ActionCoordinator(backend, consistency_barrier=barrier, execution_guard=guard)
        action = await propose(coordinator, "high" if blocked_stage == "barrier" else "low")
        task = asyncio.create_task(coordinator.execute(action.action_id))
        await entered.wait()
        await coordinator.reject(action.action_id, reason="new user activity")
        release.set()
        result = await task
        assert result.state == ActionExecutionState.CANCELLED
        assert result.error == "new user activity"
        assert backend.executed == [] and coordinator.current is None
    asyncio.run(scenario())


def test_deadline_expiring_inside_allowing_guard_prevents_dispatch():
    async def scenario():
        backend = Backend()

        async def guard(action):
            await asyncio.sleep(.04)
            return True, None

        coordinator = ActionCoordinator(backend, execution_guard=guard)
        action = await propose(coordinator, deadline=utc_now() + timedelta(seconds=.02))
        result = await coordinator.execute(action.action_id)
        assert result.state == ActionExecutionState.FAILED and result.error == "action deadline expired"
        assert backend.executed == [] and coordinator.current is None
    asyncio.run(scenario())


def test_expired_reservation_never_reaches_backend():
    async def scenario():
        backend = Backend()
        coordinator = ActionCoordinator(backend)
        action = await propose(coordinator, deadline=utc_now() - timedelta(seconds=1))
        result = await coordinator.execute(action.action_id)
        assert result.state == ActionExecutionState.FAILED
        assert backend.executed == []
    asyncio.run(scenario())
