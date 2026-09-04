from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

from harness.contracts.decisions import GoalProposal, PreparedResponse
from harness.contracts.envelope import DomainCommandResult, VersionVector
from harness.embodiment.capabilities import EmbodimentState, EmbodimentStatus
from harness.research.manifest import EmbodimentCondition
from harness.runtime.action import ActionCoordinator
from harness.runtime.adapters import DomainSnapshot, PassiveHomeostasisAdapter
from harness.runtime.models import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle
from harness.runtime.supervisor import RuntimeSupervisor
from tests.test_runtime_supervisor import _event, _manifest


class _CommitOnlyCognitive:
    def __init__(self, decision: GoalProposal | None = None) -> None:
        self.revision = 0
        self.commits = []
        self.decision = decision

    async def prepare_response(self, command):
        assert self.decision is not None
        now = datetime.now(UTC)
        prepared = PreparedResponse(
            event_id=command.event_id,
            correlation_id=command.correlation_id,
            session_id="fixture",
            decision=self.decision,
            read_versions=command.version_vector or VersionVector(),
            prepared_at=now,
            expires_at=command.deadline,
            model_version="fixture",
            prompt_version="fixture",
        )
        return DomainCommandResult(
            command_id=command.command_id,
            command_type=command.command_type,
            status="applied",
            request_hash=command.effective_request_hash(),
            committed_revision=self.revision,
            acknowledged_event_id=command.event_id,
            acknowledged_receive_seq=command.receive_seq,
            result=prepared,
            completed_at=now,
        )

    async def commit_interaction(self, command):
        self.commits.append(command)
        self.revision += 1
        return DomainCommandResult(
            command_id=command.command_id,
            command_type=command.command_type,
            status="applied",
            request_hash=command.effective_request_hash(),
            committed_revision=self.revision,
            acknowledged_event_id=command.event_id,
            acknowledged_receive_seq=command.receive_seq,
            result={"revision": self.revision},
            completed_at=datetime.now(UTC),
        )

    async def get_snapshot(self):
        return DomainSnapshot(
            service="cognitive",
            revision=self.revision,
            acknowledged_receive_seq=1,
            component_revisions={"session": self.revision},
        )


class _AvailableEmbodiment:
    @staticmethod
    def status() -> EmbodimentStatus:
        return EmbodimentStatus(state=EmbodimentState.AVAILABLE)

    @staticmethod
    def supports(_capability: str) -> bool:
        return True


class _ForbiddenActionCoordinator(ActionCoordinator):
    def __init__(self) -> None:
        super().__init__()
        self.proposal_calls = 0

    async def propose(self, *args, **kwargs):
        self.proposal_calls += 1
        raise AssertionError("a goal without embodied_action must not be proposed")


def _unstructured_goal() -> GoalProposal:
    return GoalProposal(
        policy_id="fixture-policy",
        goal="perform an unstructured task",
        success_criteria=["task completed"],
        permission_class="low",
        reversible=True,
        verification_method="fixture",
        embodied_action=None,
    )


def test_goal_without_embodied_action_commits_defer_once_and_returns(tmp_path) -> None:
    async def scenario() -> None:
        condition = EmbodimentCondition(
            mode="grounded",
            body_provider="mistyai",
            body_instance_id="body-1",
        )
        cognitive = _CommitOnlyCognitive()
        coordinator = _ForbiddenActionCoordinator()
        supervisor = RuntimeSupervisor(
            manifest=_manifest().model_copy(update={"embodiment": condition}),
            cognitive=cognitive,
            homeostasis=PassiveHomeostasisAdapter(),
            state_dir=tmp_path,
            embodiment_manager=_AvailableEmbodiment(),
            action_coordinator=coordinator,
        )
        event = _event(1, RuntimeEventType.USER_MESSAGE, RuntimeEventPriority.USER)
        now = datetime.now(UTC)
        prepared = PreparedResponse(
            event_id=event.event_id,
            correlation_id=event.correlation_id,
            session_id="fixture",
            decision=_unstructured_goal(),
            read_versions=VersionVector(),
            prepared_at=now,
            expires_at=now + timedelta(seconds=30),
            model_version="fixture",
            prompt_version="fixture",
        )

        detail = await supervisor._handle_decision(event, prepared, prepared.decision)

        assert detail == {
            "prepare_id": prepared.prepare_id,
            "reason": "goal has no structured embodied action and cannot be sent to a provider",
            "cognitive_revision": 1,
            "effective_decision_kind": "defer",
        }
        assert len(cognitive.commits) == 1
        assert cognitive.commits[0].payload["evidence"]["decision_kind"] == "defer"
        assert coordinator.proposal_calls == 0
        assert coordinator.pending() == []
        assert not any(record.record_type == "action_proposed" for record in supervisor.journal.records())

    asyncio.run(scenario())


def test_public_dispatch_reports_effective_defer_for_unstructured_goal(tmp_path) -> None:
    async def scenario() -> None:
        condition = EmbodimentCondition(
            mode="grounded",
            body_provider="mistyai",
            body_instance_id="body-1",
        )
        cognitive = _CommitOnlyCognitive(_unstructured_goal())
        coordinator = _ForbiddenActionCoordinator()
        supervisor = RuntimeSupervisor(
            manifest=_manifest().model_copy(update={"embodiment": condition}),
            cognitive=cognitive,
            homeostasis=PassiveHomeostasisAdapter(),
            state_dir=tmp_path,
            embodiment_manager=_AvailableEmbodiment(),
            action_coordinator=coordinator,
        )
        supervisor.lifecycle = RuntimeLifecycle.RUNNING
        result = await supervisor.dispatch(_event(1, RuntimeEventType.USER_MESSAGE, RuntimeEventPriority.USER))

        assert result.status == "completed"
        assert result.decision_kind == "defer"
        assert result.detail["effective_decision_kind"] == "defer"
        assert len(cognitive.commits) == 1
        assert coordinator.proposal_calls == 0

    asyncio.run(scenario())
