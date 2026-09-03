"""Deterministic in-process embodiment provider for contract and runtime tests."""

from __future__ import annotations

import hashlib
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from harness.embodiment.capabilities import (
    ACTION_EXECUTE,
    ACTION_RECONCILE,
    ACTION_SAFE_POINT,
    ACTION_VERIFY,
    EVIDENCE_REFERENCE,
    OBSERVATION_SNAPSHOT,
    PHYSIOLOGY_BODY_STATE,
    PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER,
    CapabilityDescriptor,
    CapabilityStatus,
    HealthStatus,
    ProviderCapabilities,
    ProviderHealth,
)
from harness.embodiment.contracts import (
    BodyStateSnapshot,
    EnergyState,
    EvidenceRef,
    PerceptionSnapshot,
)
from harness.runtime.models import ActionBackendResult, ActionExecution

Clock = Callable[[], datetime]
ScriptedOutcome = Literal["success", "failure", "unknown"]


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _default_world_states() -> list[dict[str, Any]]:
    return [{}]


class SimulatorConfig(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    body_instance_id: str = "sim-body-01"
    seed: int = 0
    observation_only: bool = False
    offline: bool = False
    safe_point: bool = True
    verify: bool = True
    reconcile: bool = True
    physiology: bool = False
    capacity_units: float = Field(default=100.0, gt=0)
    initial_units: float = Field(default=100.0, ge=0)
    terminal_threshold: float = Field(default=0.0, ge=0)
    basal_rate_units_per_minute: float = Field(default=0.0, ge=0)
    action_cost_units: float = Field(default=0.0, ge=0)
    scripted_outcomes: dict[str, ScriptedOutcome] = Field(default_factory=dict)
    world_states: list[dict[str, Any]] = Field(default_factory=_default_world_states)


class DeterministicEmbodimentSimulator:
    """One provider implementing all ports without HTTP or MistyAI transport DTOs."""

    provider_id = "simulator"

    def __init__(self, config: SimulatorConfig | None = None, *, clock: Clock | None = None) -> None:
        self.config = config or SimulatorConfig()
        if self.config.initial_units > self.config.capacity_units:
            raise ValueError("initial energy cannot exceed capacity")
        if self.config.terminal_threshold > self.config.capacity_units:
            raise ValueError("terminal threshold cannot exceed capacity")
        self.clock = clock or _utc_now
        self.random = random.Random(self.config.seed)
        self._offline = self.config.offline
        self._provider_revision = 1
        self._observation_revision = 0
        self._body_revision = 0
        self._world_index = 0
        self._available_units = self.config.initial_units
        self._last_energy_at = self.clock()
        self._results: dict[str, ActionBackendResult] = {}
        self._capability_status: dict[str, CapabilityStatus] = {}

    def set_offline(self, value: bool) -> None:
        self._offline = value

    def set_capability_status(self, capability: str, status: CapabilityStatus) -> None:
        self._capability_status[capability] = status
        self._provider_revision += 1

    async def capabilities(self) -> ProviderCapabilities:
        names = [OBSERVATION_SNAPSHOT, EVIDENCE_REFERENCE]
        if not self.config.observation_only:
            names.append(ACTION_EXECUTE)
            if self.config.safe_point:
                names.append(ACTION_SAFE_POINT)
            if self.config.verify:
                names.append(ACTION_VERIFY)
            if self.config.reconcile:
                names.append(ACTION_RECONCILE)
        if self.config.physiology:
            names.extend([PHYSIOLOGY_BODY_STATE, PHYSIOLOGY_VIRTUAL_RESOURCE_LEDGER])
        return ProviderCapabilities(
            provider_id=self.provider_id,
            provider_kind="in-process-simulator",
            provider_revision=str(self._provider_revision),
            body_instance_id=self.config.body_instance_id,
            capabilities=[
                CapabilityDescriptor(
                    capability=name,
                    status=self._capability_status.get(name, CapabilityStatus.AVAILABLE),
                    implementation_version="simulator-v1",
                )
                for name in names
            ],
            observed_at=self.clock(),
        )

    async def health(self) -> ProviderHealth:
        capabilities = await self.capabilities()
        overall = HealthStatus.UNAVAILABLE if self._offline else HealthStatus.HEALTHY
        return ProviderHealth(
            provider_id=self.provider_id,
            body_instance_id=self.config.body_instance_id,
            status=overall,
            capability_health={
                item.capability: (
                    HealthStatus.UNAVAILABLE
                    if self._offline or item.status == CapabilityStatus.UNAVAILABLE
                    else HealthStatus.DEGRADED
                    if item.status == CapabilityStatus.DEGRADED
                    else HealthStatus.HEALTHY
                )
                for item in capabilities.capabilities
            },
            observed_at=self.clock(),
            detail={"provider_revision": self._provider_revision},
        )

    async def observe(self, *, now: datetime | None = None) -> PerceptionSnapshot:
        if self._offline:
            raise ConnectionError("simulator is offline")
        self._observation_revision += 1
        states = self.config.world_states or [{}]
        state = states[min(self._world_index, len(states) - 1)]
        self._world_index += 1
        captured_at = self.clock()
        evidence = self._evidence(
            "observation",
            {"revision": self._observation_revision, "world": state},
            captured_at,
        )
        return PerceptionSnapshot(
            provider_id=self.provider_id,
            body_instance_id=self.config.body_instance_id,
            revision=self._observation_revision,
            captured_at=captured_at,
            summary=f"simulated observation revision {self._observation_revision}",
            confidence=1.0,
            freshness_seconds=max(0.0, ((now or captured_at) - captured_at).total_seconds()),
            evidence_refs=[evidence],
            extensions={"simulator.world.v1": state},
        )

    async def execute(self, execution: ActionExecution) -> ActionBackendResult:
        if self._offline:
            return ActionBackendResult(success=False, summary="simulator offline", unknown=True)
        if self.config.observation_only:
            return ActionBackendResult(success=False, summary="simulator is observation-only")
        if self._terminal:
            return ActionBackendResult(success=False, summary="simulated body is terminal")
        intent_id = (
            execution.embodied_action.intent.intent_id
            if execution.embodied_action is not None
            else "unstructured"
        )
        outcome = self.config.scripted_outcomes.get(intent_id, "success")
        self._integrate_energy()
        self._available_units = max(
            0.0,
            self._available_units - self.config.action_cost_units,
        )
        moment = self.clock()
        evidence = self._evidence(
            "action",
            {"action_id": execution.action_id, "intent_id": intent_id, "outcome": outcome},
            moment,
        )
        result = ActionBackendResult(
            success=outcome == "success",
            summary=f"simulated action {outcome}",
            evidence_refs=[evidence],
            safe_point=f"{execution.action_id}:complete" if outcome != "unknown" else None,
            unknown=outcome == "unknown",
            retry_safe=outcome == "failure",
        )
        self._results[execution.action_id] = result
        return result

    async def request_safe_point(self, execution: ActionExecution) -> ActionBackendResult:
        if self._offline:
            return ActionBackendResult(success=False, summary="simulator offline", unknown=True)
        return ActionBackendResult(
            success=False,
            summary="simulated action stopped at a safe point",
            safe_point=f"{execution.action_id}:safe",
            continuation={"next_step": execution.current_step},
            retry_safe=True,
        )

    async def verify(self, execution: ActionExecution) -> ActionBackendResult:
        if self._offline:
            return ActionBackendResult(success=False, summary="verification unavailable", unknown=True)
        result = self._results.get(execution.action_id)
        if result is None or result.unknown:
            return ActionBackendResult(success=False, summary="result cannot be proven", unknown=True)
        evidence = self._evidence(
            "verification",
            {"action_id": execution.action_id, "success": result.success},
            self.clock(),
        )
        return result.model_copy(update={"evidence_refs": [evidence]})

    async def reconcile(self, execution: ActionExecution) -> ActionBackendResult:
        return await self.verify(execution)

    async def snapshot(self) -> BodyStateSnapshot:
        if self._offline:
            raise ConnectionError("simulator is offline")
        if not self.config.physiology:
            raise RuntimeError("simulated physiology is disabled")
        self._integrate_energy()
        self._body_revision += 1
        moment = self.clock()
        energy = self._energy_state()
        evidence = self._evidence(
            "body-state",
            {"revision": self._body_revision, "energy": energy.model_dump(mode="json")},
            moment,
        )
        return BodyStateSnapshot(
            provider_id=self.provider_id,
            body_instance_id=self.config.body_instance_id,
            revision=self._body_revision,
            captured_at=moment,
            energy=energy,
            evidence_refs=[evidence],
        )

    @property
    def _terminal(self) -> bool:
        return self._available_units <= self.config.terminal_threshold

    def _integrate_energy(self) -> None:
        moment = self.clock()
        elapsed_minutes = max(0.0, (moment - self._last_energy_at).total_seconds() / 60.0)
        self._available_units = max(
            0.0,
            self._available_units - self.config.basal_rate_units_per_minute * elapsed_minutes,
        )
        self._last_energy_at = moment

    def _energy_state(self) -> EnergyState:
        basal = self.config.basal_rate_units_per_minute
        terminal = self._terminal
        return EnergyState(
            capacity_units=self.config.capacity_units,
            available_units=self._available_units,
            available_ratio=self._available_units / self.config.capacity_units,
            delta_units_per_minute=-basal,
            charging=False,
            estimated_time_to_empty_seconds=(
                int(
                    max(0.0, self._available_units - self.config.terminal_threshold)
                    / basal
                    * 60
                )
                if basal > 0 and not terminal
                else None
            ),
            terminal=terminal,
        )

    def _evidence(self, kind: str, payload: dict[str, Any], moment: datetime) -> EvidenceRef:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        return EvidenceRef(
            provider_id=self.provider_id,
            ref=f"simulator://evidence/{kind}/{digest}",
            digest=digest,
            media_type="application/json",
            created_at=moment,
        )
