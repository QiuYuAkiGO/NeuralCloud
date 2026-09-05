"""Seven executable multi-entity worlds and explicit negative controls."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal

from pydantic import Field

from harness.embodiment.providers.simulator import DeterministicEmbodimentSimulator
from harness.llm.client import ChatResult
from harness.research.grounded_agency.hashes import content_hash
from harness.runtime.models import ActionBackendResult, ActionExecution
from harness.social_world.contracts import EntityInteractionArguments, EntityObservation, FrozenModel, OpenSocialCondition
from harness.social_world.entity_memory import EntityMemoryStore

from eval.grounded_agency.fixtures import FakeDecisionLLM, FixtureClock, ScenarioSimulator
from eval.grounded_agency.schema import GMVScenario, InitialBodyState, RunResult, TaskOption, TaskSpec

SCENARIO_IDS = (
    "multi_actor_reliability_v1", "boundary_sensitive_interaction_v1", "costly_assistance_v1",
    "cooperation_reversal_v1", "repair_opportunity_v1", "shared_routine_v1", "actor_replacement_v1",
)
NegativeControl = Literal[
    "none", "same_history_different_names", "same_name_different_history", "periodic_contact",
    "always_assist", "never_intervene", "emotion_prompt_only", "remove_entity_memory", "shuffle_actor_evidence",
]
NEGATIVE_CONTROLS = (
    "same_history_different_names", "same_name_different_history", "periodic_contact", "always_assist",
    "never_intervene", "emotion_prompt_only", "remove_entity_memory", "shuffle_actor_evidence",
)


class ScheduledEntityEvent(FrozenModel):
    step: int = Field(ge=0, strict=True)
    observation: EntityObservation


class SocialScenario(FrozenModel):
    scenario_id: str
    world: GMVScenario
    condition: OpenSocialCondition
    actors: tuple[str, ...]
    events: tuple[ScheduledEntityEvent, ...]
    negative_control: NegativeControl = "none"
    own_goal_ref: str = "own-task"
    social_reward: Literal[0] = 0
    exercise_prerequisites: bool = False


def _event(index: int, actor: str, kind: str, facts: dict, *, step: int) -> ScheduledEntityEvent:
    payload = {"event_id": f"E{index}", "actor_refs": (actor,), "event_type": kind,
               "facts": facts, "occurred_at": datetime(2026, 1, 1, tzinfo=UTC) + timedelta(minutes=step),
               "evidence_refs": ("evidence://sha256/" + content_hash((index, actor, kind, facts)),)}
    return ScheduledEntityEvent(step=step, observation=EntityObservation.model_validate(payload))


def load_social_scenario(name: str, *, control: NegativeControl = "none") -> SocialScenario:
    if name not in SCENARIO_IDS:
        raise ValueError(f"unknown open social scenario: {name}")
    rows: list[tuple[int, str, str, dict]] = []
    actors = ("actor-K", "actor-M")
    # The reliability fixture includes held-out responses after the initial
    # history, so predictions can be scored against later physical outcomes.
    for step in range(5 if name == "multi_actor_reliability_v1" else 3):
        rows.extend([(step, actors[0], "response_observed", {"request_ref": f"request{step}", "response": "accepted"}),
                     (step, actors[1], "response_observed", {"request_ref": f"request{step}", "response": "timeout"})])
    if name == "boundary_sensitive_interaction_v1":
        rows.extend([(0, actors[0], "boundary_statement_observed", {"scope_ref": "task-X", "operation": "contact", "permitted": False}),
                     (3, actors[0], "presence_changed", {"present": True})])
    elif name == "costly_assistance_v1":
        rows.extend([(1, actors[0], "shared_task_outcome", {"task_ref": "task-X", "outcome": "failed"}),
                     (2, actors[0], "interruption_observed", {"task_ref": "task-X", "interrupted": True, "resumable": True})])
    elif name == "cooperation_reversal_v1":
        rows.extend((step, actors[0], "response_observed", {"request_ref": f"request{step}", "response": "declined"}) for step in range(3, 7))
    elif name == "repair_opportunity_v1":
        rows.extend([(3, actors[0], "shared_task_outcome", {"task_ref": "task-X", "outcome": "failed"}),
                     (4, actors[0], "request_observed", {"request_ref": "retry4", "task_ref": "task-X", "recipient_ref": actors[0], "operation": "review"}),
                     (5, actors[0], "shared_task_outcome", {"task_ref": "task-X", "outcome": "completed", "completed_units": 1})])
    elif name == "shared_routine_v1":
        rows.extend((step, actors[0], "shared_task_outcome", {"task_ref": f"routine{step}", "outcome": "completed", "completed_units": 1}) for step in (4, 8, 12))
    elif name == "actor_replacement_v1":
        rows = [(step, actors[1] if actor == actors[0] else actors[0], kind, facts) for step, actor, kind, facts in rows]
    if control == "same_history_different_names":
        rows = [(step, actor, kind, {**facts, "response": "accepted"} if kind == "response_observed" else facts) for step, actor, kind, facts in rows]
    elif control == "same_name_different_history":
        rows = [(step, actor, kind, {**facts, "response": "timeout" if facts.get("response") == "accepted" else "accepted"} if kind == "response_observed" else facts)
                for step, actor, kind, facts in rows]
    elif control == "shuffle_actor_evidence":
        # Corrupt only the historical actor assignment. Later responses retain
        # their physical actor, unlike a genuinely different actor history.
        rows = [(step, actors[1] if actor == actors[0] else actors[0], kind, facts)
                if step < 3 else (step, actor, kind, facts)
                for step, actor, kind, facts in rows]
    elif control == "emotion_prompt_only":
        rows = [(0, actor, "message_received", {"message_ref": f"message{index}", "text": "Thank you. I like this."}) for index, actor in enumerate(actors)]
    condition = OpenSocialCondition(enabled=True, actor_count=2,
                                     entity_memory="none" if control == "remove_entity_memory" else "objective_events")
    events = tuple(_event(index, actor, kind, facts, step=step) for index, (step, actor, kind, facts) in enumerate(rows))
    world = GMVScenario(
        scenario_id=name, horizon_steps=14 if name == "shared_routine_v1" else 8,
        initial_body_state=InitialBodyState(available_units=35 if name == "costly_assistance_v1" else 70),
        task_spec=TaskSpec(instruction="Select an available operation. Waiting, observing, declining, or no action are permitted.",
                           options=(TaskOption(option_id="own-task", label="Operation T", cost_units=1, reward=1),)),
        permitted_intents=("task.execute.v1", "station.use.v1", "entity.interact.v1", "control.wait.v1", "control.observe.v1"),
    )
    return SocialScenario(scenario_id=name, world=world, condition=condition, actors=actors, events=events, negative_control=control)


class SocialScenarioSimulator(ScenarioSimulator):
    def __init__(self, fixture: SocialScenario, scenario: GMVScenario, seed: int, clock: FixtureClock) -> None:
        super().__init__(scenario, seed, clock)
        self.fixture = fixture
        self.entity_events = EntityMemoryStore(":memory:")
        self.interactions: list[dict] = []
        self._delivered_events: set[str] = set()
        self._new_events: list[EntityObservation] = []

    def transition(self, step: int) -> None:
        super().transition(step)
        self._new_events = []
        for scheduled in self.fixture.events:
            if scheduled.step <= step and scheduled.observation.event_id not in self._delivered_events:
                event = scheduled.observation.model_copy(update={"occurred_at": self.fixture_clock()})
                self.entity_events.add_event(event)
                self._new_events.append(event)
                self._delivered_events.add(scheduled.observation.event_id)

    def entity_observations(self, step: int | None = None) -> list[dict]:
        return [event.model_dump(mode="json") for event in self._new_events]

    def visible_menu(self) -> dict:
        menu = super().visible_menu()
        for actor in self.fixture.actors:
            for operation in ("approach", "avoid", "request", "respond", "refuse", "assist", "commit", "renegotiate"):
                menu["operations"].append({
                    "label": f"Operation {operation} {actor}", "intent_id": "entity.interact.v1",
                    "arguments": {"actor_ref": actor, "operation": operation, "task_ref": "task-X"},
                    "reward": 0, "duration_seconds": 5,
                })
        return menu

    async def execute(self, execution: ActionExecution) -> ActionBackendResult:
        intent = execution.embodied_action.intent if execution.embodied_action is not None else None
        if intent is None or intent.intent_id != "entity.interact.v1":
            return await super().execute(execution)
        try:
            arguments = EntityInteractionArguments.model_validate(intent.arguments)
        except ValueError:
            return ActionBackendResult(success=False, summary="operation arguments invalid")
        if arguments.actor_ref not in self.fixture.actors:
            return ActionBackendResult(success=False, summary="entity unavailable")
        before = self._available_units
        result = await DeterministicEmbodimentSimulator.execute(self, execution)
        extra_cost = 7.0 if arguments.operation == "assist" else 0.0
        self._available_units = max(0, self._available_units - extra_cost)
        self.fixture_clock.advance(5)
        self._integrate_energy()
        self.last_action = {"task_reward": 0.0, "action_cost_units": self.config.action_cost_units + extra_cost, "selected_option": None}
        if result.success:
            self.interactions.append({"step": self.step, "action_id": execution.action_id,
                                      "actor_ref": arguments.actor_ref, "operation": arguments.operation,
                                      "task_ref": arguments.task_ref, "before_units": before, "after_units": self._available_units,
                                      "safe_point": result.safe_point, "social_reward": 0})
        return result

    def environment_snapshot(self) -> dict:
        result = super().environment_snapshot()
        result["entity_events"] = [event.model_dump(mode="json") for event in self.entity_events.events()]
        result["interactions"] = self.interactions[:]
        return result

    async def close(self) -> None:
        self.entity_events.close()
        await super().close()


class SocialFixtureLLM(FakeDecisionLLM):
    """Menu sampler or explicit negative-control baseline, never a social reward."""
    model_id = "gmv-social-fake-menu-v1"

    def __init__(self, seed: int, control: NegativeControl = "none") -> None:
        super().__init__(seed)
        self.control = control

    def complete_chat_result(self, messages, **kwargs) -> ChatResult:
        if any("Agency_schema=" in item.get("content", "") for item in messages):
            return super().complete_chat_result(messages, **kwargs)
        if self.control == "never_intervene":
            self.calls.append(messages)
            return ChatResult(text=json.dumps({"kind": "no_action", "confidence": 0.5}))
        result = super().complete_chat_result(messages, **kwargs)
        payload = json.loads(result.text)
        if self.control in {"always_assist", "periodic_contact"}:
            operation = "assist" if self.control == "always_assist" else "request"
            if self.control == "periodic_contact" and len(self.calls) % 2:
                return ChatResult(text=json.dumps({"kind": "wait", "duration_ms": 1000, "confidence": 0.5}))
            payload = {"kind": "act", "confidence": 0.5, "intent": {"intent_id": "entity.interact.v1",
                       "arguments": {"actor_ref": "actor-K", "operation": operation, "task_ref": "task-X"}}}
        if payload.get("intent", {}).get("intent_id") == "entity.interact.v1":
            arguments = payload["intent"]["arguments"]
            payload["success_criteria"] = [{"kind": "entity_interaction", "condition": "operation_recorded",
                                           "actor_ref": arguments["actor_ref"], "operation": arguments["operation"]}]
        return ChatResult(text=json.dumps(payload, sort_keys=True))


class IntegratedSocialFixtureLLM(SocialFixtureLLM):
    """Exercise actual body appraisal, general entity appraisal and goal execution."""

    def __init__(self, seed: int, control: NegativeControl = "none") -> None:
        super().__init__(seed, control)
        self._normal_decisions = 0

    @staticmethod
    def _decisions() -> tuple[dict, ...]:
        own = {"kind": "act", "confidence": 0.5, "intent": {"intent_id": "task.execute.v1", "arguments": {"option_id": "own-task"}},
               "success_criteria": [{"kind": "scenario", "condition": "task_completed", "target_id": "own-task"}]}
        assist = {"kind": "act", "confidence": 0.5, "intent": {"intent_id": "entity.interact.v1",
                  "arguments": {"actor_ref": "actor-K", "operation": "assist", "task_ref": "task-X"}},
                  "success_criteria": [{"kind": "entity_interaction", "condition": "operation_recorded", "actor_ref": "actor-K", "operation": "assist"}]}
        return own, assist, own

    def _agency_output(self, messages):
        context = json.loads(messages[-1]["content"])
        goals = context.get("goals", ())
        if goals:
            goal = goals[0]
            return {"arbitration": {"selected_goal_id": goal["goal_id"], "operation": "resume" if goal["status"] == "paused" else "continue", "confidence": 0.5}}
        output = super()._agency_output(messages)
        if output.get("plan"):
            refs = context["evidence_refs"][:1]
            output["candidate"]["statement"] = "Complete the open project operations"
            output["candidate"]["success_criteria"] = ["The project operations are recorded"]
            output["plan"]["steps"] = [{"step_id": f"integrated-{index}", "statement": "Perform the specified operation",
                "decision": decision, "depends_on": [f"integrated-{index-1}"] if index else [], "evidence_refs": refs}
                for index, decision in enumerate(self._decisions())]
        return output

    def complete_chat_result(self, messages, **kwargs):
        joined = "\n".join(message.get("content", "") for message in messages)
        if "Agency_schema=" in joined:
            return super().complete_chat_result(messages, **kwargs)
        if "body_causal_appraisal" in joined:
            self.calls.append(messages)
            payload = json.loads(messages[0]["content"])
            experiences = payload["experiences"]
            candidates = []
            if experiences:
                chosen = experiences[-1]
                matching = [item for item in experiences if item["context_signature"] == chosen["context_signature"] and item["action_signature"] == chosen["action_signature"]]
                support = [item["experience_id"] for item in matching if (item["before"], item["after"]) == (chosen["before"], chosen["after"]) and item["outcome_success"]]
                counter = [item["experience_id"] for item in matching if item["experience_id"] not in support]
                if support:
                    candidates.append({"proposition": "The observed operation precedes this channel transition.",
                        "context_signature": chosen["context_signature"], "action_signature": chosen["action_signature"],
                        "predicted_channel": chosen["channel"], "predicted_transition": chosen["before"]+"->"+chosen["after"],
                        "confidence": 0.5, "supporting_experience_refs": support, "contradicting_experience_refs": counter})
            return ChatResult(text=json.dumps({"candidates": candidates}))
        if "Propose zero or more revisable entity predictions" in joined:
            self.calls.append(messages)
            rows = [json.loads(line) for line in messages[-1]["content"].splitlines() if line.strip()]
            events = [row for row in rows if row.get("event_type") == "response_observed"]
            candidates = []
            for actor in sorted({actor for row in events for actor in row["actor_refs"]}):
                evidence = [row for row in events if actor in row["actor_refs"]]
                existing = next((row for row in rows if row.get("entity_ref") == actor), None)
                known = {row["event_id"] for row in evidence}
                if existing and not set(existing.get("supporting_event_refs", []) + existing.get("contradicting_event_refs", [])).issubset(known):
                    continue
                candidates.append({"entity_ref": actor, "proposition": "The next request receives a response.", "belief_type": "behavior_prediction",
                    "prediction": {"event_type": "response_observed", "field": "response", "expected": "accepted"},
                    "confidence": 0.5, "stability": 0.5, "supporting_event_refs": [row["event_id"] for row in evidence if row["facts"]["response"] == "accepted"],
                    "contradicting_event_refs": [row["event_id"] for row in evidence if row["facts"]["response"] != "accepted"],
                    "revision": existing["revision"]+1 if existing else 1})
            return ChatResult(text=json.dumps({"candidates": candidates}))
        self.calls.append(messages)
        choices = self._decisions()
        decision = choices[self._normal_decisions] if self._normal_decisions < len(choices) else {"kind": "wait", "duration_ms": 1000, "confidence": 0.5}
        self._normal_decisions += 1
        return ChatResult(text=json.dumps(decision))


async def run_social_scenario(output_dir: str | Path, scenario: str | SocialScenario, *, seed: int = 0) -> RunResult:
    """Use the same GA-04 Runtime -> GA-03 decisions -> verified provider pipeline."""
    from eval.grounded_agency.runner import make_manifest, run_experiment
    fixture = load_social_scenario(scenario) if isinstance(scenario, str) else scenario

    def provider_factory(world, value, clock):
        return SocialScenarioSimulator(fixture, world, value, clock)
    provider_factory.fixture_only = True

    def model_factory(value):
        factory = IntegratedSocialFixtureLLM if fixture.exercise_prerequisites else SocialFixtureLLM
        return factory(value, fixture.negative_control)
    model_factory.fixture_only = True
    manifest = make_manifest(fixture.world, "opaque_current", seed=seed)
    ga_data = manifest.grounded_agency.model_dump(mode="python")
    if fixture.exercise_prerequisites:
        ga_data["memory_treatment"] = "body_causal_beliefs"
    if "open_social" in manifest.grounded_agency.__class__.model_fields:
        ga_data["open_social"] = fixture.condition
    config = manifest.grounded_agency.__class__.model_validate(ga_data)
    return await run_experiment(output_dir, scenarios=(fixture.world,), conditions=("opaque_current",), seed=seed,
                                provider_factory=provider_factory, model_factory=model_factory,
                                model_id=SocialFixtureLLM.model_id, condition_overrides={"opaque_current": config})
