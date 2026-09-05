"""Connect persistent plans to verified Runtime actions without selecting actions."""

from __future__ import annotations

from harness.agency.models import AgencyOutcome, GoalCheckpoint, GoalTrigger, ResumeObservation
from harness.core.inference import ContextSegment
from harness.research.grounded_agency.hashes import canonical_json, content_hash
from harness.research.interoception.renderer import render_opaque_interoception
from harness.runtime.models import ActionExecutionState, RuntimeEventType


def _belief_revision(runtime) -> str:
    memory = runtime._research_memory
    beliefs = memory.service.store.beliefs() if memory is not None else ()
    return content_hash([(item.belief_id, item.revision) for item in beliefs])


def _world(runtime) -> dict:
    return runtime._agency_service.store.get("runtime", "world") or {"hash": content_hash({}), "text": ""}


def observe(runtime, observations) -> None:
    service = runtime._agency_service
    if service is None or not observations:
        return
    observation = observations[-1]
    prior = service.store.get("runtime", "sensor")
    current = {"reading": observation.reading, "sequence": observation.sample_sequence,
               "transform": observation.transform_ref}
    if prior == current:
        return
    service.store.put("runtime", "sensor", current)
    if prior is not None and prior["reading"] != observation.reading:
        service.scheduler.enqueue(GoalTrigger(
            kind="opaque_change", evidence_refs=(content_hash(observation),),
            signature=content_hash([prior, current]), change_symbol=observation.change,
            observed_at=runtime._research_clock(),
        ))


def heartbeat_due(runtime) -> bool:
    service = runtime._agency_service
    if service is None:
        return False
    now = runtime._research_clock()
    for goal in service.store.goals():
        if goal.status in {"active", "paused"} and goal.target_horizon is not None and goal.target_horizon <= now:
            service.scheduler.enqueue(GoalTrigger(
                kind="deadline", goal_id=goal.goal_id, evidence_refs=goal.evidence_refs,
                signature=content_hash([goal.goal_id, goal.revision, goal.target_horizon.isoformat()]), observed_at=now,
            ))
    return service.scheduler.pending_count > 0


def record_action(runtime, event_id, action) -> None:
    service = runtime._agency_service
    binding = runtime._agency_decision_bindings.get(event_id)
    if service is None or binding is None or not action.terminal:
        return
    state = "succeeded" if action.state == ActionExecutionState.SUCCEEDED else (
        "failed" if action.state == ActionExecutionState.FAILED else "unknown"
    )
    action_ref = "goal-action-" + content_hash(event_id)[:32]
    prior = service.store.get("action_outcome", action_ref)
    if prior is not None and prior["state"] == state:
        return
    outcome_ref = content_hash([event_id, state, binding])
    outcome = AgencyOutcome(
        outcome_id="outcome-" + content_hash([event_id, action.state.value])[:32],
        goal_id=binding["goal_id"], step_id=binding["step_id"], action_id=action_ref,
        state=state, evidence_refs=(outcome_ref,), observed_at=runtime._research_clock(),
    )
    if service.record_outcome(outcome):
        service.scheduler.enqueue(GoalTrigger(
            kind="action_terminal", evidence_refs=outcome.evidence_refs,
            signature=content_hash(outcome), goal_id=binding["goal_id"], observed_at=runtime._research_clock(),
        ))
        runtime._append_journal("persistent_goal_outcome", data={
            **outcome.model_dump(mode="json"), "raw_action_id": action.action_id, "raw_outcome": action.outcome,
        })


def checkpoint_goals(runtime, *, goal_id: str | None = None) -> None:
    service = runtime._agency_service
    if service is None:
        return
    current = runtime.action_coordinator.current
    if current is not None and not current.terminal:
        raise RuntimeError("persistent goals require an action safe point before checkpointing")
    if current is not None:
        record_action(runtime, current.goal_id, current)
    samples = runtime._opaque_store.observations(now=runtime._research_clock()) if runtime._opaque_store else ()
    for goal in service.store.goals():
        if goal.status != "active" or (goal_id is not None and goal.goal_id != goal_id):
            continue
        plan = service.store.plan(goal.goal_id)
        if plan is None:
            continue
        completed = tuple(item["step_id"] for item in service.store.all("action_outcome")
                          if item["goal_id"] == goal.goal_id and item["state"] == "succeeded")
        next_step = service.next_step(goal.goal_id)
        binding = runtime._agency_decision_bindings.get(current.goal_id, {}) if current else {}
        bound_action = current if binding.get("goal_id") == goal.goal_id else None
        checkpoint = GoalCheckpoint(
            goal_id=goal.goal_id, plan_revision=plan.revision, completed_step_ids=completed,
            next_step_id=next_step["step_id"] if next_step else None,
            continuation={"provider_continuation": bound_action.continuation if bound_action else None,
                          "belief_revision_hash": _belief_revision(runtime)},
            body_state_ref=content_hash(samples[-1]) if samples else content_hash("no_observation"),
            world_state_refs=(_world(runtime)["hash"],), paused_at=runtime._research_clock(),
            safe_point=(bound_action.safe_point or "terminal") if bound_action else "no_inflight_action",
            action_id="goal-action-" + content_hash(bound_action.goal_id)[:32] if bound_action else None,
            action_status="none" if bound_action is None else (
                "succeeded" if bound_action.state == ActionExecutionState.SUCCEEDED else
                "stopped" if bound_action.state == ActionExecutionState.FAILED else "unknown"
            ),
        )
        service.pause_goal(goal.goal_id, checkpoint)
        runtime._append_journal("persistent_goal_checkpoint", data=checkpoint.model_dump(mode="json"))


async def reconsider(runtime, event, observations, segments, provider_intents):
    service = runtime._agency_service
    if service is None:
        return None, None
    now = runtime._research_clock()
    if event.event_type == RuntimeEventType.USER_MESSAGE and runtime.action_coordinator.current is not None:
        await runtime.action_coordinator.request_preemption(reason="explicit user interruption")
        if not await runtime._drain_action_tasks(timeout=runtime.maintenance_drain_seconds):
            raise RuntimeError("user interruption did not reach an action safe point")
        checkpoint_goals(runtime)
    if event.event_type == RuntimeEventType.ENVIRONMENT_CHANGE:
        text = str(event.payload.get("text") or "")
        objective = [item.text for item in segments if item.kind == "entity_observation"]
        signature = runtime._research_world_signature() if runtime._research_world_signature else text
        service.store.put("runtime", "world", {"hash": content_hash([signature, objective]), "text": text})
    world = _world(runtime)
    observation_refs = tuple(content_hash(item) for item in observations)
    source_refs = tuple(ref for segment in segments for ref in segment.source_ids)
    trigger_kind = {
        RuntimeEventType.USER_MESSAGE: "user_interrupt", RuntimeEventType.ENVIRONMENT_CHANGE: "environment_change",
        RuntimeEventType.GOAL_RECONSIDERATION: "deadline", RuntimeEventType.IDLE: "environment_change",
        RuntimeEventType.HEARTBEAT: "deadline",
    }[event.event_type]
    explicit_trigger = event.payload.get("agency_trigger")
    if explicit_trigger in {"recovery", "safe_point", "deadline"}:
        trigger_kind = explicit_trigger
    evidence_refs = tuple(dict.fromkeys((world["hash"], *observation_refs, *source_refs)))
    paused = [service.store.checkpoint(goal.goal_id) for goal in service.store.goals() if goal.status == "paused"]
    paused = [item for item in paused if item is not None]
    resume_observation = ResumeObservation(
        observed_at=now, world_evidence_refs=(world["hash"],),
        body_state_ref=content_hash(observations[-1]) if observations else content_hash("no_observation"),
        provider_intents=tuple(provider_intents),
        assumptions_hold=all(world["hash"] in item.world_state_refs for item in paused),
        continuation_usable=all(item.action_status != "unknown" for item in paused),
        body_allows_next_step=bool(provider_intents),
        belief_revision_matches=all((item.continuation or {}).get("belief_revision_hash") == _belief_revision(runtime) for item in paused),
    ) if paused else None
    result = await service.reconsider(
        GoalTrigger(kind=trigger_kind, evidence_refs=(world["hash"],), observed_at=now,
                    signature=content_hash([trigger_kind, world["hash"], str(event.payload.get("text") or "")])),
        evidence_refs=evidence_refs, provider_intents=provider_intents,
        opaque_observations=({"observations": render_opaque_interoception(observations)},) if observations else (),
        body_beliefs=tuple({"text": segment.text, "source_refs": segment.source_ids} for segment in segments if segment.kind == "body_memory"),
        world_context={"observed": world["text"], "task": str(event.payload.get("text") or ""),
                       "entity_context": [segment.text for segment in segments if segment.kind.startswith("entity_")]},
        resume_observation=resume_observation, opaque=runtime.manifest.grounded_agency.is_opaque,
    )
    if result.trace_ref is not None:
        runtime._append_journal("persistent_goal_arbitration", event=event, data=service.traces[result.trace_ref])
    if result.decision.operation == "pause":
        await runtime.action_coordinator.request_preemption(reason="model requested goal pause")
        if not await runtime._drain_action_tasks(timeout=runtime.maintenance_drain_seconds):
            raise RuntimeError("goal pause did not reach a safe point")
        checkpoint_goals(runtime, goal_id=result.decision.selected_goal_id)
    context = service.context()
    if not context["goals"]:
        return result, None
    return result, ContextSegment(
        kind="persistent_goals", text=canonical_json(context),
        source_ids=[f'{goal["goal_id"]}:r{goal["revision"]}' for goal in context["goals"]],
        why="model_owned_plans_and_checkpoints",
    )


def bind_decision(runtime, event_id, trace, arbitration) -> None:
    if arbitration is None or arbitration.goal_id is None:
        return
    step = runtime._agency_service.next_step(arbitration.goal_id)
    if step is None or trace.get("success") is False:
        return
    chosen = trace.get("decision", {})
    planned = step["decision"]
    keys = ("kind", "intent", "duration_ms", "observation_scope")
    if all(chosen.get(key) == planned.get(key) for key in keys):
        binding = {"goal_id": arbitration.goal_id, "step_id": step["step_id"]}
        runtime._agency_decision_bindings[event_id] = binding
        runtime._append_journal("persistent_goal_decision_bound", data={"decision_id": event_id, **binding})


def unresolved_plan_reason(runtime, chosen: dict, arbitration) -> str | None:
    """A fresh action ID must not bypass an unresolved plan's execution guard."""
    service = runtime._agency_service
    if service is None:
        return None

    def signature(decision):
        intent = decision.get("intent") or {}
        arguments = {key: value for key, value in intent.get("arguments", {}).items() if key != "evidence_refs"}
        return content_hash([decision.get("kind"), intent.get("intent_id"), arguments,
                             decision.get("duration_ms"), decision.get("observation_scope")])

    target = signature(chosen)
    outcomes = service.store.all("action_outcome")
    selected = service.store.goal(arbitration.goal_id) if arbitration is not None and arbitration.goal_id else None
    for goal in service.store.goals():
        plan = service.store.plan(goal.goal_id)
        if plan is None:
            continue
        unknown = {item["step_id"] for item in outcomes if item["goal_id"] == goal.goal_id and item["state"] == "unknown"}
        completed = {item["step_id"] for item in outcomes if item["goal_id"] == goal.goal_id and item["state"] == "succeeded"}
        for step in plan.steps:
            if signature(step.decision) != target:
                continue
            if step.step_id in unknown:
                return "matching planned action has an unresolved outcome; reconciliation is required"
            if goal.status in {"paused", "blocked"} and step.step_id not in completed:
                if selected is None or selected.goal_id != goal.goal_id or selected.status != "active":
                    return "matching plan requires a validated model-owned resume or replan"
    return None
