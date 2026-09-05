"""Runtime-owned objective experience boundary for the opt-in research route."""

from __future__ import annotations

from typing import Any

from harness.research.grounded_agency.hashes import content_hash
from harness.research.interoception.contracts import OpaqueInteroceptiveObservation


class ResearchMemoryBridge:
    def __init__(self, service: Any, *, context_key: str) -> None:
        self.service = service
        self.context_key = context_key
        self.decisions: dict[str, dict[str, Any]] = {}

    def remember(self, decision_id: str, trace: dict[str, Any]) -> None:
        self.decisions[decision_id] = trace

    @staticmethod
    def outcome_reference(decision_id: str, *, success: bool, verified: bool) -> str:
        return content_hash({"decision_id": decision_id, "success": success, "verified": verified})

    async def observe(self, observations) -> None:
        for observation in observations:
            await self.service.observe(observation)

    def context(self, observations):
        if not observations:
            return None
        return self.service.visible_context(
            observations[-1], context_hash=content_hash(self.context_key), context_key=self.context_key,
        )

    def complete(self, decision_id: str, *, evidence: Any, completed_at, success: bool, verified: bool) -> bool:
        from pydantic import TypeAdapter

        from harness.contracts.experimental_decisions import ExperimentalDecision

        trace = self.decisions.get(decision_id)
        if (trace is None or trace.get("success") is False or not trace.get("visible_observations")
                or trace.get("decision", {}).get("kind") not in {"act", "observe", "wait", "defer", "no_action"}):
            return False
        if isinstance(evidence, dict) and evidence.get("kind") in {"defer", "no_action"}:
            if trace["decision"]["kind"] != evidence["kind"]:
                return False
        decision = TypeAdapter(ExperimentalDecision).validate_python(trace["decision"])
        before = OpaqueInteroceptiveObservation.model_validate(trace["visible_observations"][-1])
        return self.service.record_decision(
            decision_id=decision_id, episode_id=trace["research_episode_id"],
            context_hash=trace["input_context_hash"], context_key=self.context_key,
            observation_before=before, decision=decision,
            action_outcome_refs=(self.outcome_reference(decision_id, success=success, verified=verified),),
            outcome_success=success, outcome_verified=verified, completed_at=completed_at,
        )
