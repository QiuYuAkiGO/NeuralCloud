"""Deterministic security compilation, separate from Homeostasis policy choice."""

from __future__ import annotations

from collections.abc import Iterable

from pydantic import ValidationError

from harness.cognitive.action_planner import (
    ActionDecisionPlanner, StructuredActionRequest, _PlanningRejected,
)
from harness.cognitive.decision_parser import (
    EXPERIMENTAL_INTENT_PROFILES, ExperimentalDecisionParseError, ExperimentalDecisionParser,
)
from harness.contracts.decisions import DeferDecision, GoalProposal, NoActionDecision
from harness.contracts.experimental_decisions import (
    EXPERIMENTAL_INTENT_IDS, ExperimentalActDecision, ExperimentalDecision,
    ExperimentalDeferDecision, ExperimentalNoActionDecision, ExperimentalObserveDecision,
    ExperimentalWaitDecision,
)
from harness.embodiment.contracts import ActionIntent, EmbodiedActionSpec, IntentionalNoopCriterion, ScenarioCriterion
from harness.research.grounded_agency.hashes import content_hash


CompiledExperimentalDecision = GoalProposal | DeferDecision | NoActionDecision


def available_experimental_intents(
    manifest_intents: Iterable[str], provider_intents: Iterable[str],
) -> tuple[str, ...]:
    return tuple(sorted(
        set(manifest_intents) & set(provider_intents) & EXPERIMENTAL_INTENT_IDS & EXPERIMENTAL_INTENT_PROFILES.keys()
    ))


class ExperimentalDecisionCompiler:
    def compile(
        self,
        decision: ExperimentalDecision,
        *,
        manifest_intents: Iterable[str],
        provider_intents: Iterable[str],
        decision_id: str | None = None,
    ) -> CompiledExperimentalDecision:
        allowed = available_experimental_intents(manifest_intents, provider_intents)
        try:
            # Revalidate even constructed/copied model instances at the trust boundary.
            decision = ExperimentalDecisionParser(allowed).parse(decision.model_dump(mode="json"))
            if isinstance(decision, ExperimentalNoActionDecision):
                return NoActionDecision(reason="model selected no_action")
            if isinstance(decision, ExperimentalDeferDecision):
                return DeferDecision(
                    reason="model selected defer", required_evidence=list(decision.missing_information),
                )
            if isinstance(decision, ExperimentalWaitDecision):
                act = ExperimentalActDecision(
                    intent=ActionIntent(intent_id="control.wait.v1", arguments={"duration_ms": decision.duration_ms}),
                    success_criteria=[IntentionalNoopCriterion(condition="waited")], confidence=decision.confidence,
                )
            elif isinstance(decision, ExperimentalObserveDecision):
                act = ExperimentalActDecision(
                    intent=ActionIntent(intent_id="control.observe.v1", arguments={}),
                    success_criteria=[IntentionalNoopCriterion(condition="observed")], confidence=decision.confidence,
                )
            else:
                act = decision
            if act.intent.intent_id not in allowed:
                return DeferDecision(
                    reason="model decision rejected: intent unavailable in manifest/provider intersection",
                    required_evidence=[f"authorized provider capability: {act.intent.intent_id}"],
                )
            profile = EXPERIMENTAL_INTENT_PROFILES[act.intent.intent_id]
            arguments = profile.arguments_model.model_validate(act.intent.arguments, strict=True).model_dump(mode="json")
            # Reuse target-binding checks, never the planner or its policy lookup.
            request = StructuredActionRequest(
                policy_id="model_structured", intent=act.intent, success_criteria=list(act.success_criteria),
            )
            ActionDecisionPlanner._validate_criteria(request, profile, arguments)
            scenario_binding = {
                "task.execute.v1": ("option_id", "task_completed"),
                "station.use.v1": ("station_id", "station_used"),
            }.get(act.intent.intent_id)
            if scenario_binding is not None:
                target_field, expected_condition = scenario_binding
                if any(
                    not isinstance(item, ScenarioCriterion)
                    or item.condition != expected_condition
                    or item.target_id != arguments[target_field]
                    for item in act.success_criteria
                ):
                    raise _PlanningRejected(
                        "scenario criterion does not identify the requested action target", "target-bound scenario criterion",
                    )
            goal_id = decision_id or "ga_goal_" + content_hash({
                "decision": decision.model_dump(mode="json"), "available_intents": allowed,
            })[:32]
            return GoalProposal(
                goal_id=goal_id,
                policy_id="model_structured",
                goal=f"Execute {act.intent.intent_id}",
                success_criteria=[ActionDecisionPlanner._criterion_summary(item) for item in act.success_criteria],
                permission_class=profile.permission_class,
                reversible=profile.reversible,
                verification_method="provider_typed_criteria.v1",
                required_approvals=list(profile.required_approvals),
                embodied_action=EmbodiedActionSpec(
                    intent=ActionIntent(intent_id=act.intent.intent_id, arguments=arguments),
                    success_criteria=list(act.success_criteria),
                ),
            )
        except (ExperimentalDecisionParseError, ValidationError, _PlanningRejected) as exc:
            reason = exc.reason if isinstance(exc, _PlanningRejected) else "strict decision validation failed"
            return DeferDecision(
                reason=f"model decision rejected: {reason}",
                required_evidence=["valid authorized typed decision"],
            )


__all__ = ["CompiledExperimentalDecision", "ExperimentalDecisionCompiler", "available_experimental_intents"]
