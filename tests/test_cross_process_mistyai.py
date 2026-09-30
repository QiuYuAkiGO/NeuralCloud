"""Opt-in TCP integration gate against a separately running MistyAI API."""

from __future__ import annotations

import asyncio
import os
from uuid import uuid4
from datetime import UTC, datetime, timedelta

import pytest

from harness.cognitive.action_planner import ActionDecisionPlanner
from harness.contracts.decisions import GoalProposal
from harness.contracts.envelope import VersionVector
from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    ACTION_VERIFY,
    EVIDENCE_REFERENCE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
    CapabilityRequirement,
)
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.mistyai import (
    MistyAIActionBackend,
    MistyAIBodyStateProvider,
    MistyAICapabilityProvider,
    MistyAIClient,
    MistyAIError,
    MistyAIPerceptionBackend,
)
from harness.research.manifest import EmbodimentCondition
from harness.runtime.action import ActionCoordinator
from harness.runtime.models import ActionExecutionState


@pytest.mark.integration
def test_neuralcloud_talks_to_real_mistyai_http_process() -> None:
    base_url = os.environ.get("NC_CROSS_PROCESS_MISTYAI_URL")
    token = os.environ.get("NC_CROSS_PROCESS_MISTYAI_TOKEN")
    if not base_url or not token:
        pytest.skip("set NC_CROSS_PROCESS_MISTYAI_URL and NC_CROSS_PROCESS_MISTYAI_TOKEN")

    async def scenario() -> None:
        client = MistyAIClient(base_url, token=token, timeout_seconds=10)
        wrong_client = MistyAIClient(base_url, token="definitely-wrong-token", timeout_seconds=10)
        try:
            with pytest.raises(MistyAIError) as unauthorized:
                await wrong_client.health()
            assert unauthorized.value.status_code == 401

            condition = EmbodimentCondition(
                mode="grounded",
                required_for_runtime=True,
                body_instance_id="misty-body-01",
                capability_requirements=[
                    CapabilityRequirement(capability=ACTION_EXECUTE, required=True),
                    CapabilityRequirement(capability=ACTION_VERIFY, required=True),
                    CapabilityRequirement(capability=OBSERVATION_SNAPSHOT, required=True),
                    CapabilityRequirement(capability=EVIDENCE_REFERENCE, required=True),
                    CapabilityRequirement(capability=PHYSIOLOGY_BODY_STATE, required=False),
                ],
            )
            manager = EmbodimentManager(condition, MistyAICapabilityProvider(client))
            activated = await manager.activate()
            assert activated.state.value == "available"

            observation = await MistyAIPerceptionBackend(client).observe()
            assert observation.body_instance_id == "misty-body-01"
            assert observation.evidence_refs

            body = await MistyAIBodyStateProvider(client).snapshot()
            assert body.body_instance_id == "misty-body-01"
            assert body.revision >= 1
            assert body.evidence_refs

            coordinator = ActionCoordinator(
                MistyAIActionBackend(client, poll_interval_seconds=0.01),
                capability_check=manager.supports,
            )
            proposal = ActionDecisionPlanner().decide(
                {
                    "policies": [
                        {
                            "policy_id": "ci.control.wait",
                            "label": "Wait briefly in the deterministic body",
                            "rank": 1,
                        }
                    ],
                    "action_request": {
                        "schema_version": "cognitive-action-request.v1",
                        "policy_id": "ci.control.wait",
                        "intent": {
                            "intent_id": "control.wait.v1",
                            "arguments": {"duration_ms": 1},
                        },
                        "success_criteria": [
                            {"kind": "intentional_noop", "condition": "waited"}
                        ],
                    },
                },
                mode="",
            )
            assert isinstance(proposal, GoalProposal)
            action = await coordinator.propose(
                proposal,
                idempotency_key="cross-process-control-wait",
                deadline=datetime.now(UTC) + timedelta(seconds=30),
                version_vector=VersionVector(),
            )
            completed = await coordinator.execute(action.action_id)
            assert completed.state == ActionExecutionState.SUCCEEDED
            assert completed.outcome is not None
            assert completed.outcome["evidence_refs"]
        finally:
            await wrong_client.close()
            await client.close()

    asyncio.run(scenario())


@pytest.mark.integration
def test_production_rest_and_usage_over_real_tcp() -> None:
    """The release gate must exercise production roles, not only legacy actions."""
    from harness.embodiment.contracts import BodyRestCommand, CognitiveUsage
    base_url = os.environ.get("NC_CROSS_PROCESS_MISTYAI_URL")
    token = os.environ.get("NC_CROSS_PROCESS_MISTYAI_TOKEN")
    accounting = os.environ.get("NC_CROSS_PROCESS_MISTYAI_ACCOUNTING_TOKEN")
    if not base_url or not token or not accounting:
        pytest.skip("set the three NC_CROSS_PROCESS_MISTYAI variables for the production TCP gate")

    async def scenario():
        client = MistyAIClient(base_url, token=token, accounting_token=accounting)
        wrong = MistyAIClient(base_url, token=token, accounting_token="wrong-accounting-token")
        provider = MistyAIBodyStateProvider(client)
        prefix = "launch-ci:" + uuid4().hex
        try:
            initial = await provider.snapshot()
            assert initial.physiology_mode == "production_v1"
            assert initial.episode_id and initial.sample_sequence is not None
            identity = {"body_instance_id": initial.body_instance_id, "episode_id": initial.episode_id}
            start = BodyRestCommand(command_id=prefix + ":start", **identity)
            first = await provider.start_rest(start)
            assert first.resting and first.evidence_refs
            assert await provider.start_rest(start) == first
            assert (await provider.snapshot()).resting is True
            usage = CognitiveUsage(inference_id=prefix + ":usage", model_class="main", cost_class="other",
                                   quantity=1, metering="call", **identity)
            with pytest.raises(MistyAIError) as unauthorized:
                await MistyAIBodyStateProvider(wrong).record_usage(usage)
            assert unauthorized.value.status_code == 401
            charged = await provider.record_usage(usage)
            assert charged.inference_id == usage.inference_id and charged.evidence_refs
            assert await provider.record_usage(usage) == charged
            ended = await provider.end_rest(BodyRestCommand(command_id=prefix + ":end", **identity))
            assert ended.resting is False
            assert (await provider.snapshot()).resting is False
        finally:
            await wrong.close()
            await client.close()
    asyncio.run(scenario())
