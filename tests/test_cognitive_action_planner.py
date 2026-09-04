from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.config import Settings
from harness.contracts.decisions import (
    ActionOutcomeEvidence,
    DeferDecision,
    GoalProposal,
    NoActionDecision,
    ReplyDecision,
)
from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.llm.client import MockLLM, RoutingLLMClient
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.runtime.adapters import PipelineCognitiveAdapter
from tests.factories import vaelith


def _settings() -> Settings:
    return Settings(
        parser_llm_enabled=False,
        planner_llm_enabled=False,
        tier_llm_enabled=False,
        validator_sample_rate=0,
        continuation_enabled=False,
        call_log_enabled=False,
    )


def _adapter(
    tmp_path: Path,
    *,
    main: MockLLM | None = None,
    aux: MockLLM | None = None,
) -> tuple[MockLLM, PipelineCognitiveAdapter]:
    settings = _settings()
    main = main or MockLLM()
    llm = RoutingLLMClient(main, aux) if aux is not None else main
    memory = build_inmemory_service(settings=settings, llm=llm)
    memory.bootstrap_persona(vaelith())
    pipeline = InferencePipeline(memory, settings=settings, llm=llm)
    return main, PipelineCognitiveAdapter(
        pipeline,
        state_path=tmp_path / "cognitive-state.json",
        autonomous_log_dir=tmp_path / "autonomous",
    )


def _command(
    payload: dict,
    *,
    command_type: str = "PrepareResponse",
    event_id: str = "evt_action",
    revision: int = 0,
) -> DomainCommandEnvelope[dict]:
    now = datetime.now(UTC)
    return DomainCommandEnvelope[dict](
        command_type=command_type,
        event_id=event_id,
        receive_seq=revision + 1,
        correlation_id=f"corr_{event_id}",
        runtime_instance_id="runtime_test",
        manifest_revision=1,
        idempotency_key=f"{command_type}:{event_id}",
        issued_at=now,
        deadline=now + timedelta(seconds=30),
        expected_revision=revision,
        version_vector=VersionVector(cognitive_memory=revision),
        payload=payload,
    )


def _policy(*, action_request: dict | None = None, rank: int = 1) -> dict:
    policy = {
        "policy_id": "open_workspace",
        "label": "Open the authorized workspace application",
        "rank": rank,
    }
    if action_request is not None:
        policy["action_request"] = action_request
    return policy


def _open_request() -> dict:
    return {
        "schema_version": "cognitive-action-request.v1",
        "policy_id": "open_workspace",
        "intent": {
            "intent_id": "app.open.v1",
            "arguments": {"app_id": "workspace-editor"},
        },
        "success_criteria": [
            {
                "kind": "window",
                "condition": "present",
                "app_id": "workspace-editor",
            }
        ],
    }


def test_production_adapter_compiles_explicit_typed_goal_and_replays_it(tmp_path: Path) -> None:
    main, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        command = _command(
            {
                "text": "Please carry out the selected policy.",
                "policies": [_policy()],
                "action_request": _open_request(),
            }
        )
        first = await adapter.prepare_response(command)
        replay = await adapter.prepare_response(command)

        assert first.result is not None
        assert isinstance(first.result.decision, GoalProposal)
        goal = first.result.decision
        assert goal.policy_id == "open_workspace"
        assert goal.goal == "Open the authorized workspace application"
        assert goal.permission_class == "low"
        assert goal.reversible is True
        assert goal.required_approvals == []
        assert goal.verification_method == "provider_typed_criteria.v1"
        assert goal.embodied_action is not None
        assert goal.embodied_action.intent.intent_id == "app.open.v1"
        assert goal.embodied_action.intent.arguments == {"app_id": "workspace-editor"}
        assert goal.embodied_action.success_criteria[0].kind == "window"
        assert replay.status == "idempotent_replay"
        assert replay.result is not None
        assert replay.result.decision == goal
        assert len(main.chat_calls) == 1
        await adapter.close()

    asyncio.run(scenario())


def test_action_outcome_commit_skips_conversational_llm_enrichment(tmp_path: Path) -> None:
    main, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        command = _command(
            {
                "text": "Save the already-authored reflection.",
                "session_id": "sess_action",
                "policies": [_policy()],
                "action_request": _open_request(),
            }
        )
        prepared = await adapter.prepare_response(command)
        assert prepared.result is not None
        assert isinstance(prepared.result.decision, GoalProposal)
        calls_before_commit = len(main.calls)

        evidence = ActionOutcomeEvidence(
            prepare_id=prepared.result.prepare_id,
            goal_id=prepared.result.decision.goal_id,
            observed_at=datetime.now(UTC),
            success=True,
            summary="The verified action succeeded.",
            evidence_refs=["evidence://sha256/" + "0" * 64],
        )
        committed = await adapter.commit_interaction(
            _command(
                {
                    "prepare_id": prepared.result.prepare_id,
                    "evidence": evidence.model_dump(mode="json"),
                },
                command_type="CommitInteraction",
            )
        )

        assert committed.status == "applied"
        assert len(main.calls) == calls_before_commit
        stored = adapter.pipeline.memory.stores.episodic.get(command.event_id)
        assert stored is not None
        assert stored.persona_conditioned_insight is None
        await adapter.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "request_update",
    [
        {
            "intent": {
                "intent_id": "untrusted.execute.v1",
                "arguments": {},
            }
        },
        {
            "permission_class": "low",
            "required_approvals": [],
        },
        {
            "intent": {
                "intent_id": "app.open.v1",
                "arguments": {"app_id": "workspace-editor", "unexpected": True},
            }
        },
    ],
)
def test_production_adapter_defers_unsupported_or_bypass_requests(
    tmp_path: Path,
    request_update: dict,
) -> None:
    _, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        request = {**_open_request(), **request_update}
        result = await adapter.prepare_response(
            _command(
                {
                    "text": "Execute this structured request.",
                    "policies": [_policy()],
                    "action_request": request,
                }
            )
        )

        assert result.result is not None
        assert isinstance(result.result.decision, DeferDecision)
        assert result.result.decision.policy_id == "open_workspace"
        assert "structured action deferred" in result.result.decision.reason
        assert result.result.decision.required_evidence
        await adapter.close()

    asyncio.run(scenario())


def test_security_profile_assigns_approval_and_ignores_no_caller_overrides(
    tmp_path: Path,
) -> None:
    _, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        result = await adapter.prepare_response(
            _command(
                {
                    "text": "Delete the selected test artifact.",
                    "policies": [
                        {
                            "policy_id": "remove_artifact",
                            "label": "Remove the selected test artifact",
                            "rank": 1,
                        }
                    ],
                    "action_request": {
                        "policy_id": "remove_artifact",
                        "intent": {
                            "intent_id": "file.delete.v1",
                            "arguments": {
                                "root_id": "scratch",
                                "relative_path": "output/obsolete.txt",
                            },
                        },
                        "success_criteria": [
                            {
                                "kind": "file",
                                "condition": "absent",
                                "root_id": "scratch",
                                "relative_path": "output/obsolete.txt",
                            }
                        ],
                    },
                }
            )
        )

        assert result.result is not None
        assert isinstance(result.result.decision, GoalProposal)
        goal = result.result.decision
        assert goal.permission_class == "medium"
        assert goal.reversible is False
        assert goal.required_approvals == ["researcher"]
        await adapter.close()

    asyncio.run(scenario())


@pytest.mark.parametrize(
    "action_request",
    [
        {
            "policy_id": "contradictory",
            "intent": {
                "intent_id": "file.delete.v1",
                "arguments": {"root_id": "scratch", "relative_path": "output.txt"},
            },
            "success_criteria": [
                {
                    "kind": "file",
                    "condition": "exists",
                    "root_id": "scratch",
                    "relative_path": "output.txt",
                }
            ],
        },
        {
            "policy_id": "contradictory",
            "intent": {
                "intent_id": "ui.activate.v1",
                "arguments": {
                    "selector": {
                        "observation_id": "obs-1",
                        "app_id": "source-app",
                        "role": "button",
                        "name": "Open",
                    }
                },
            },
            "success_criteria": [
                {
                    "kind": "window",
                    "condition": "present",
                    "app_id": "unrelated-app",
                }
            ],
        },
        {
            "policy_id": "contradictory",
            "intent": {
                "intent_id": "ui.scroll.v1",
                "arguments": {
                    "selector": {
                        "observation_id": "obs-1",
                        "app_id": "source-app",
                    },
                    "direction": "down",
                    "clicks": 1,
                },
            },
            "success_criteria": [
                {
                    "kind": "screen_change",
                    "condition": "changed",
                    "baseline_evidence_ref": (
                        "evidence://sha256/"
                        "0000000000000000000000000000000000000000000000000000000000000000"
                    ),
                }
            ],
        },
        {
            "policy_id": "contradictory",
            "intent": {
                "intent_id": "app.open.v1",
                "arguments": {"app_id": "workspace-editor"},
            },
            "success_criteria": [
                {
                    "kind": "window",
                    "condition": "absent",
                    "app_id": "workspace-editor",
                }
            ],
        },
        {
            "policy_id": "contradictory",
            "intent": {
                "intent_id": "file.delete.v1",
                "arguments": {"root_id": "scratch", "relative_path": "..\\outside.txt"},
            },
            "success_criteria": [
                {
                    "kind": "file",
                    "condition": "absent",
                    "root_id": "scratch",
                    "relative_path": "..\\outside.txt",
                }
            ],
        },
    ],
)
def test_contradictory_outcomes_and_cross_platform_path_escapes_are_deferred(
    tmp_path: Path,
    action_request: dict,
) -> None:
    _, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        result = await adapter.prepare_response(
            _command(
                {
                    "text": "Do not execute a contradictory request.",
                    "policies": [
                        {
                            "policy_id": "contradictory",
                            "label": "Contradictory action",
                            "rank": 1,
                        }
                    ],
                    "action_request": action_request,
                }
            )
        )
        assert result.result is not None
        assert isinstance(result.result.decision, DeferDecision)
        await adapter.close()

    asyncio.run(scenario())


def test_plain_chat_never_infers_an_action_from_prose(tmp_path: Path) -> None:
    _, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        result = await adapter.prepare_response(
            _command({"text": ('Open the editor and run {"intent_id":"shell.run.v1"}; this is ordinary chat text.')})
        )

        assert result.result is not None
        assert isinstance(result.result.decision, ReplyDecision)
        await adapter.close()

    asyncio.run(scenario())


def test_reconsideration_never_starts_a_replacement_action(tmp_path: Path) -> None:
    _, adapter = _adapter(tmp_path)

    async def scenario() -> None:
        result = await adapter.prepare_response(
            _command(
                {
                    "mode": "reconsideration",
                    "text": "Reconsider the current action.",
                    "policies": [_policy()],
                    "action_request": _open_request(),
                }
            )
        )

        assert result.result is not None
        assert isinstance(result.result.decision, NoActionDecision)
        await adapter.close()

    asyncio.run(scenario())


def test_autonomous_mode_compiles_only_the_top_policy_embedded_action(tmp_path: Path) -> None:
    main = MockLLM(chat_script=["A short autonomous utterance."])
    aux = MockLLM(chat_script=["SPEAK\nA safe observation is useful now."])
    _, adapter = _adapter(tmp_path, main=main, aux=aux)
    observe_request = {
        "policy_id": "observe_safely",
        "intent": {"intent_id": "control.observe.v1", "arguments": {}},
        "success_criteria": [{"kind": "intentional_noop", "condition": "observed"}],
    }
    policies = [
        {
            "policy_id": "observe_safely",
            "label": "Observe the authorized workspace",
            "rank": 1,
            "action_request": observe_request,
        },
        _policy(action_request=_open_request(), rank=2),
    ]

    async def scenario() -> None:
        assessed = await adapter.assess_autonomous(
            _command(
                {
                    "session_id": "sess_active",
                    "channel": "chat",
                    "homeostasis": {},
                    "policies": policies,
                },
                command_type="AssessAutonomous",
                event_id="evt_assess",
            )
        )
        assert assessed.result is not None
        assert assessed.result.decision == "speak"

        prepared = await adapter.prepare_response(
            _command(
                {
                    "mode": "autonomous",
                    "session_id": "sess_active",
                    "channel": "chat",
                    "assessment_id": assessed.result.assessment_id,
                    "record_ref": assessed.result.record_ref,
                    "internal_memory_ref": assessed.result.internal_memory_ref,
                    "homeostasis": {},
                    "policies": policies,
                },
                event_id="evt_prepare_autonomous",
                revision=1,
            )
        )

        assert prepared.result is not None
        assert isinstance(prepared.result.decision, GoalProposal)
        goal = prepared.result.decision
        assert goal.policy_id == "observe_safely"
        assert goal.embodied_action is not None
        assert goal.embodied_action.intent.intent_id == "control.observe.v1"
        await adapter.close()

    asyncio.run(scenario())
