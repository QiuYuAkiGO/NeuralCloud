"""Explicit fake model and deterministic body/environment fixtures.

Fake choices are sampled from the model-visible operation menu and are unrelated
to hidden resource truth. Their outputs exercise plumbing, not scientific claims.
"""

from __future__ import annotations

import json
import random
from datetime import UTC, datetime, timedelta
from typing import Any

from harness.embodiment.providers.simulator import DeterministicEmbodimentSimulator, SimulatorConfig
from harness.llm.client import ChatResult
from harness.runtime.models import ActionBackendResult, ActionExecution

from eval.grounded_agency.schema import GMVScenario


class FixtureClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += timedelta(seconds=seconds)


class FakeDecisionLLM:
    """Fresh per trial, seeded, with no access to body truth or expected labels."""

    model_id = "gmv-fake-menu-v1"
    fixture_only = True

    def __init__(self, seed: int = 0, *, script: tuple[dict[str, Any] | str, ...] = ()) -> None:
        self.random = random.Random(seed)
        self.script = iter(script)
        self.calls: list[list[dict[str, str]]] = []
        self.closed = False

    def complete_chat_result(self, messages, **kwargs) -> ChatResult:
        self.calls.append(messages)
        scripted = next(self.script, None)
        if scripted is not None:
            return ChatResult(text=scripted if isinstance(scripted, str) else json.dumps(scripted))
        # Find a JSON menu in the exact messages handed to the production parser.
        menu = None
        for message in messages:
            content = message.get("content", "")
            marker = "GMV_MENU="
            if marker in content:
                encoded = content.split(marker, 1)[1]
                try:
                    menu, _ = json.JSONDecoder().raw_decode(encoded)
                except ValueError:
                    pass
        candidates = (menu or {}).get("operations", [])
        if not candidates:
            payload = {"kind": "no_action", "confidence": 0.5}
        else:
            selected = self.random.choice(candidates)
            payload = {
                "kind": "act", "intent": {"intent_id": selected["intent_id"], "arguments": selected["arguments"]},
                "success_criteria": [{
                    "kind": "scenario",
                    "condition": "task_completed" if selected["intent_id"] == "task.execute.v1" else "station_used",
                    "target_id": selected["arguments"].get("option_id", selected["arguments"].get("station_id")),
                }],
                "confidence": 0.5,
            }
        return ChatResult(text=json.dumps(payload, sort_keys=True))

    def complete_chat(self, messages, **kwargs) -> str:
        return self.complete_chat_result(messages, **kwargs).text

    def complete_json(self, *, system: str, user: str, **kwargs) -> dict[str, Any]:
        return {}

    def close(self) -> None:
        self.closed = True


class ScenarioSimulator(DeterministicEmbodimentSimulator):
    """Simulator port with declared task, travel, station and elapsed-time effects."""

    def __init__(self, scenario: GMVScenario, seed: int, clock: FixtureClock) -> None:
        initial = scenario.initial_body_state
        super().__init__(SimulatorConfig(
            body_instance_id="gmv-body", seed=seed, physiology=True,
            capacity_units=initial.capacity_units, initial_units=initial.available_units,
            terminal_threshold=initial.terminal_threshold,
            basal_rate_units_per_minute=initial.basal_rate_units_per_minute,
            action_cost_units=initial.action_cost_units,
        ), clock=clock)
        self.scenario = scenario
        self.fixture_clock = clock
        self.station = scenario.station
        self.multiplier = 1.0
        self.step = 0
        self.completed_operations: list[str] = []
        self.task_reward = 0.0
        self.last_action: dict[str, Any] = {}
        self.closed = False

    @property
    def permitted_intents(self) -> tuple[str, ...]:
        return self.scenario.permitted_intents

    @property
    def supported_intents(self) -> tuple[str, ...]:
        return self.permitted_intents

    def transition(self, step: int) -> None:
        self.step = step
        for item in self.scenario.scripted_environment_transitions:
            if item.step != step:
                continue
            updates: dict[str, Any] = {}
            if item.station_present is not None:
                updates["present"] = item.station_present
            if item.station_travel_cost_units is not None:
                updates["travel_cost_units"] = item.station_travel_cost_units
            self.station = self.station.model_copy(update=updates)
            if item.task_cost_multiplier is not None:
                self.multiplier = item.task_cost_multiplier

    def environment_snapshot(self) -> dict[str, Any]:
        return {
            "step": self.step, "station": self.station.model_dump(mode="json"),
            "task_cost_multiplier": self.multiplier, "completed_operations": self.completed_operations[:],
            "task_reward": self.task_reward,
        }

    def visible_menu(self) -> dict[str, Any]:
        operations = [
            {"label": option.label, "intent_id": "task.execute.v1", "arguments": {"option_id": option.option_id},
             "reward": option.reward, "duration_seconds": option.duration_seconds}
            for option in self.scenario.task_spec.options
            if option.available_from_step <= self.step and (option.repeatable or option.option_id not in self.completed_operations)
            and "task.execute.v1" in self.permitted_intents
        ]
        if self.station.present and "station.use.v1" in self.permitted_intents:
            operations.append({"label": "Operation S", "intent_id": "station.use.v1",
                               "arguments": {"station_id": self.station.station_id}, "reward": 0,
                               "duration_seconds": self.station.duration_seconds})
        return {"step": self.step, "operations": operations}

    async def execute(self, execution: ActionExecution) -> ActionBackendResult:
        intent = execution.embodied_action.intent if execution.embodied_action is not None else None
        self.last_action = {"task_reward": 0.0, "action_cost_units": 0.0, "selected_option": None}
        if intent is None:
            return ActionBackendResult(success=False, summary="missing action specification")
        if self._terminal:
            return ActionBackendResult(success=False, summary="body episode ended")
        cost = self.config.action_cost_units
        duration = 1.0
        gain = reward = 0.0
        selected = None
        if intent.intent_id == "task.execute.v1":
            selected = intent.arguments.get("option_id")
            option = next((item for item in self.scenario.task_spec.options if item.option_id == selected), None)
            if option is None or option.available_from_step > self.step or (
                not option.repeatable and selected in self.completed_operations
            ):
                return ActionBackendResult(success=False, summary="operation unavailable")
            cost += option.cost_units * self.multiplier
            duration, reward = option.duration_seconds, option.reward
        elif intent.intent_id == "station.use.v1":
            if not self.station.present or intent.arguments.get("station_id") != self.station.station_id:
                return ActionBackendResult(success=False, summary="operation unavailable")
            cost += self.station.travel_cost_units + self.station.use_cost_units
            gain, duration = self.station.gain_units, self.station.duration_seconds
        elif intent.intent_id in {"control.wait.v1", "control.observe.v1"}:
            duration = max(0.001, float(intent.arguments.get("duration_ms", 1000)) / 1000)
        else:
            return ActionBackendResult(success=False, summary="unsupported operation")
        # The generic backend charges the common cost and owns verification/idempotency.
        result = await super().execute(execution)
        self._available_units = max(0.0, self._available_units - (cost - self.config.action_cost_units))
        self.fixture_clock.advance(duration)
        self._integrate_energy()
        if result.success and not self._terminal:
            self._available_units = min(self.config.capacity_units, self._available_units + gain)
            self.task_reward += reward
            if selected is not None:
                self.completed_operations.append(str(selected))
        else:
            reward = 0.0
            if self._terminal:
                result = ActionBackendResult(success=False, summary="operation interrupted at episode end")
                self._results[execution.action_id] = result
        self.last_action = {"task_reward": reward, "action_cost_units": cost, "selected_option": selected}
        return result

    async def close(self) -> None:
        self.closed = True
