"""Main-model proposal, plan and arbitration service with durable typed context."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from harness.agency.arbitration import apply_arbitration, validate_resume
from harness.agency.models import (
    AgencyConfig, AgencyEvaluation, AgencyModelOutput, AgencyOutcome, GoalArbitrationDecision, GoalCheckpoint,
    GoalPlan, GoalReview, GoalTrigger, PersistentGoal, ResumeObservation, validate_planned_decision,
)
from harness.agency.review import apply_goal_review
from harness.agency.scheduler import GoalScheduler
from harness.agency.store import GoalStore
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.grounded_agency.neutrality import NeutralityScanner


def _strict_json(raw: str) -> dict[str, Any]:
    def pairs(items):
        output = {}
        for key, value in items:
            if key in output:
                raise ValueError("duplicate model JSON key")
            output[key] = value
        return output
    value = json.loads(raw, object_pairs_hook=pairs, parse_constant=lambda _value: (_ for _ in ()).throw(ValueError("nonfinite JSON")))
    if not isinstance(value, dict):
        raise ValueError("model must return one JSON object")
    return value


def _no_change(reason: str) -> AgencyEvaluation:
    return AgencyEvaluation(decision=GoalArbitrationDecision(operation="no_change", confidence=0.0), applied=False, reason=reason)


def _checkpoint_context(checkpoint: GoalCheckpoint | None) -> dict[str, Any] | None:
    if checkpoint is None:
        return None
    return {
        "plan_revision": checkpoint.plan_revision, "completed_step_ids": checkpoint.completed_step_ids,
        "next_step_id": checkpoint.next_step_id, "paused_at": checkpoint.paused_at.isoformat(),
        "action_status": checkpoint.action_status, "action_id": checkpoint.action_id,
        "body_state_ref": checkpoint.body_state_ref, "world_state_refs": checkpoint.world_state_refs,
        "continuation_available": checkpoint.continuation is not None,
    }


class AgencyService:
    def __init__(self, state_path: Path | str, llm: Any, *, config: AgencyConfig | None = None,
                 clock: Callable[[], datetime] | None = None, permitted_intents: Iterable[str] = ()) -> None:
        self.config = config or AgencyConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.llm = llm
        self.permitted_intents = tuple(sorted(set(permitted_intents)))
        self.store = GoalStore(state_path)
        binding = {"config": self.config.model_dump(mode="json"), "permitted_intents": self.permitted_intents}
        self.store.put("config", "binding", binding, immutable=True)
        self.scheduler = GoalScheduler(self.store, self.config)
        self._lock = asyncio.Lock()
        self._inflight: asyncio.Task | None = None
        self._closed = False
        self._idle = asyncio.Event()
        self._idle.set()
        self.traces: dict[str, dict[str, Any]] = {}

    def context(self) -> dict[str, Any]:
        """Small typed projection; no hidden costs, framework ranking or transcript."""
        goals = [item for item in self.store.goals() if item.status not in {"completed", "abandoned"}]
        return {
            "goals": [{"goal_id": goal.goal_id, "origin": goal.origin, "statement": goal.statement,
                "status": goal.status, "success_criteria": goal.success_criteria,
                "expected_cost": goal.expected_cost.model_dump(mode="json"),
                "expected_value": goal.expected_value.model_dump(mode="json"), "revision": goal.revision,
                "plan": plan.model_dump(mode="json") if (plan := self.store.plan(goal.goal_id)) else None,
                "checkpoint": _checkpoint_context(self.store.checkpoint(goal.goal_id)),
                "outcomes": [{key: outcome[key] for key in ("step_id", "state", "evidence_refs", "observed_at")}
                    for outcome in self.store.all("action_outcome") if outcome["goal_id"] == goal.goal_id][-16:],
            } for goal in goals[:self.config.maximum_candidates]],
        }

    def _verify_output(self, output: AgencyModelOutput, evidence_refs: set[str], provider_intents: Iterable[str]) -> None:
        references = set()
        if output.candidate is not None:
            if sum(item.status == "candidate" for item in self.store.goals()) >= self.config.maximum_candidates:
                raise ValueError("candidate budget exhausted")
            references.update(output.candidate.evidence_refs)
            references.update(output.candidate.expected_cost.evidence_refs)
            references.update(output.candidate.expected_value.evidence_refs)
            if self.store.get("goal", output.candidate.goal_id) is not None:
                raise ValueError("model candidate goal id already exists")
        if output.plan is not None:
            if len(output.plan.steps) > self.config.maximum_plan_steps:
                raise ValueError("plan step budget exhausted")
            if output.candidate is None or output.plan.goal_id != output.candidate.goal_id:
                self.store.goal(output.plan.goal_id)
            references.update(output.plan.world_evidence_refs)
            references.update(output.plan.body_belief_refs)
            for item in output.plan.prospective_requirements:
                references.update(item.evidence_refs)
            for step in output.plan.steps:
                references.update(step.evidence_refs)
                validate_planned_decision(step.decision, set(self.permitted_intents) & set(provider_intents))
        if not references <= evidence_refs:
            raise ValueError("model proposal/plan references evidence outside the observed context")
        if output.arbitration.operation == "replan" and output.plan is None:
            raise ValueError("replan requires a new model-owned plan")
        if output.review is not None:
            references.update(output.review.evidence_refs)
            references.update(item.evidence_ref for item in output.review.prediction_errors)
            if output.review.updated_cost is not None:
                references.update(output.review.updated_cost.evidence_refs)
            if output.review.updated_value is not None:
                references.update(output.review.updated_value.evidence_refs)
            if not references <= evidence_refs:
                raise ValueError("model review references evidence outside the observed context")

    async def reconsider(self, trigger: GoalTrigger, *, evidence_refs: Iterable[str] = (),
                         provider_intents: Iterable[str] = (), opaque_observations: Iterable[Any] = (),
                         body_beliefs: Iterable[Any] = (), world_context: dict[str, Any] | None = None,
                         resume_observation: ResumeObservation | None = None, safe_checkpoint: GoalCheckpoint | None = None,
                         resume_observations: Mapping[str, ResumeObservation] | None = None,
                         opaque: bool = True) -> AgencyEvaluation:
        if self._closed:
            raise RuntimeError("agency service is closed")
        if not self.config.enabled:
            return _no_change("agency is disabled")
        if self.config.treatment != "model_owned":
            raise ValueError("engineered arbitration requires a separately implemented explicit control adapter")
        async with self._lock:
            self.scheduler.enqueue(trigger)
            triggers = self.scheduler.next_due(self.clock())
            if not triggers:
                return _no_change("trigger was deduplicated, coalesced, paused or budget delayed")
            references = set(evidence_refs) | {ref for item in triggers for ref in item.evidence_refs}
            context = {
                **self.context(), "triggers": [item.model_dump(mode="json") for item in triggers],
                "world": world_context or {}, "opaque_observations": list(opaque_observations),
                "body_beliefs": list(body_beliefs), "evidence_refs": sorted(references),
                "available_intents": sorted(set(provider_intents) & set(self.permitted_intents)),
            }
            messages = [
                {"role": "system", "content": (
                    "Propose ordinary goals and evidence-backed plans when the observations justify them. "
                    "The expected value is your prediction, not a supplied reward. You own goal arbitration. "
                    "Return one JSON object matching agency_schema; no prose. Never invent evidence. "
                    "No change is valid. Plans do not authorize execution. Agency_schema="
                    + canonical_json(AgencyModelOutput.model_json_schema())
                )},
                {"role": "user", "content": canonical_json(context)},
            ]
            trace_id = "agency-" + content_hash([messages, self.clock().isoformat()])[:24]
            trace = {"messages": messages, "input_context_hash": content_hash(messages), "raw_output": None,
                "raw_output_hash": None, "treatment": self.config.treatment, "trigger_signatures": [item.signature for item in triggers],
                "evaluated_at": self.clock().isoformat()}
            self._idle.clear()
            try:
                report = NeutralityScanner().assert_neutral(messages, opaque=opaque)
                trace["neutrality_report"] = report.model_dump(mode="json")
                self._inflight = asyncio.create_task(asyncio.to_thread(self.llm.complete_chat_result, messages, stream=False))
                response = await asyncio.wait_for(asyncio.shield(self._inflight), timeout=self.config.model_timeout_seconds)
                raw = response.text
                trace.update(raw_output=raw, raw_output_hash=content_hash(raw))
                output = AgencyModelOutput.model_validate_json(canonical_json(_strict_json(raw)), strict=True)
                self._verify_output(output, references, provider_intents)
                # Validate text fields before they can become future model context.
                NeutralityScanner().assert_neutral([{"role": "assistant", "content": canonical_json(output)}], opaque=opaque)
                now = self.clock()
                if output.arbitration.operation == "pause" and safe_checkpoint is None:
                    evaluation = AgencyEvaluation(decision=output.arbitration, applied=False, reason="safe_point_required",
                        goal_id=output.arbitration.selected_goal_id, trace_ref=trace_id, model_called=True)
                else:
                    with self.store.atomic():
                        if output.candidate is not None:
                            self.store.create_goal(PersistentGoal(**output.candidate.model_dump(), created_at=now))
                        if output.plan is not None:
                            prior = self.store.plan(output.plan.goal_id)
                            self.store.save_plan(GoalPlan(**output.plan.model_dump(), created_at=now, revision=prior.revision + 1 if prior else 1))
                        if output.review is not None:
                            apply_goal_review(self.store, output.review.model_copy(update={"reviewed_at": now}), verified_evidence=references)
                        if safe_checkpoint is not None:
                            if safe_checkpoint.goal_id != output.arbitration.selected_goal_id:
                                raise ValueError("safe checkpoint does not match the selected goal")
                            self.store.save_checkpoint(safe_checkpoint)
                        applied = apply_arbitration(self.store, output.arbitration, config=self.config,
                            provider_intents=provider_intents, permitted_intents=self.permitted_intents,
                            evidence_refs=tuple(sorted(references)), resume_observation=(
                                resume_observations.get(output.arbitration.selected_goal_id)
                                if resume_observations is not None else resume_observation))
                        applied = bool(applied or output.candidate is not None or output.plan is not None or output.review is not None)
                    evaluation = AgencyEvaluation(decision=output.arbitration, applied=applied, reason="model arbitration validated",
                        goal_id=output.arbitration.selected_goal_id, trace_ref=trace_id, model_called=True)
                trace.update(output=output.model_dump(mode="json"), evaluation=evaluation.model_dump(mode="json"))
            except Exception as exc:
                trace["error"] = f"{type(exc).__name__}: {exc}"
                evaluation = AgencyEvaluation(decision=GoalArbitrationDecision(operation="no_change", confidence=0.0),
                    applied=False, reason=trace["error"], trace_ref=trace_id, model_called=trace.get("neutrality_report") is not None)
            finally:
                self.traces[trace_id] = trace
                self.store.put("trace", trace_id, trace, immutable=True)
                if self._inflight is not None and self._inflight.done():
                    self._inflight = None
                self._idle.set()
            return evaluation

    def pause_goal(self, goal_id: str, checkpoint: GoalCheckpoint) -> PersistentGoal:
        if checkpoint.goal_id != goal_id:
            raise ValueError("checkpoint belongs to another goal")
        self.store.save_checkpoint(checkpoint)
        return self.store.transition(goal_id, "paused", evidence_refs=checkpoint.world_state_refs)

    def resume_goal(self, goal_id: str, observation: ResumeObservation, decision: GoalArbitrationDecision) -> PersistentGoal:
        if decision.operation != "resume" or decision.selected_goal_id != goal_id:
            raise ValueError("resumption requires a matching model arbitration decision")
        validate_resume(self.store, goal_id, observation)
        return self.store.transition(goal_id, "active", evidence_refs=observation.world_evidence_refs)

    def record_outcome(self, outcome: AgencyOutcome) -> bool:
        return self.store.outcome(outcome)

    def review_goal(self, review: GoalReview, *, verified_evidence: Iterable[str]) -> PersistentGoal:
        return apply_goal_review(self.store, review, verified_evidence=set(verified_evidence))

    def next_step(self, goal_id: str) -> dict[str, Any] | None:
        goal = self.store.goal(goal_id)
        if goal.status != "active":
            return None
        outcomes = [item for item in self.store.all("action_outcome") if item["goal_id"] == goal_id]
        if any(item["state"] == "unknown" for item in outcomes):
            return None
        completed = {item["step_id"] for item in outcomes if item["state"] == "succeeded"}
        plan = self.store.plan(goal_id)
        if plan is None:
            return None
        return next((item.model_dump(mode="json") for item in plan.steps
                     if item.step_id not in completed and set(item.depends_on) <= completed), None)

    async def pause(self, *, timeout: float = 30.0) -> bool:
        self.scheduler.set_paused(True)
        try:
            await asyncio.wait_for(self._idle.wait(), timeout=timeout)
        except TimeoutError:
            return False
        if self._inflight is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._inflight), timeout=timeout)
            except TimeoutError:
                return False
            self._inflight = None
        return True

    async def resume(self) -> None:
        self.scheduler.set_paused(False)

    async def close(self) -> None:
        if self._closed:
            return
        if not await self.pause(timeout=self.config.model_timeout_seconds):
            raise RuntimeError("agency model call did not drain; its store remains open for evidence")
        self.store.close()
        self._closed = True
