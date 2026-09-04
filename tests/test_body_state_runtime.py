from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness.embodiment.capabilities import PHYSIOLOGY_BODY_STATE, CapabilityRequirement
from harness.embodiment.contracts import BodyStateSnapshot, EnergyState, EvidenceRef
from harness.embodiment.guards import BodyStateGuard
from harness.embodiment.manager import EmbodimentManager
from harness.embodiment.providers.simulator import (
    DeterministicEmbodimentSimulator,
    SimulatorConfig,
)
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.research.manifest import EmbodimentCondition
from harness.runtime import RuntimeEventPriority, RuntimeEventType, RuntimeLifecycle, RuntimeSupervisor
from tests.test_runtime_supervisor import _manifest, _pipeline_adapter


def _body(
    ratio: float,
    revision: int,
    captured_at: datetime,
    *,
    body_instance_id: str = "body-1",
    terminal: bool = False,
) -> BodyStateSnapshot:
    digest = f"{revision:064x}"
    return BodyStateSnapshot(
        provider_id="fixture",
        body_instance_id=body_instance_id,
        revision=revision,
        captured_at=captured_at,
        energy=EnergyState(
            capacity_units=100,
            available_units=ratio * 100,
            available_ratio=ratio,
            delta_units_per_minute=-0.1,
            terminal=terminal,
        ),
        evidence_refs=[
            EvidenceRef(
                provider_id="fixture",
                ref=f"fixture://body-state/{revision}",
                digest=digest,
                media_type="application/json",
                created_at=captured_at,
            )
        ],
    )


def _embodied_supervisor(tmp_path, *, mode: str):
    moment = datetime.now(UTC)
    condition = EmbodimentCondition(
        mode=mode,
        body_instance_id="body-1",
        capability_requirements=[
            CapabilityRequirement(capability=PHYSIOLOGY_BODY_STATE, required=True)
        ],
        replan_hysteresis=0.05,
        minimum_replan_interval_seconds=300,
    )
    profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
    manifest = _manifest().model_copy(
        update={
            "embodiment": condition,
            "persona_version": profile.persona_version,
            "homeostatic_profile": profile.profile_ref,
            "schema_version": "homeostasis-state-v1",
            "a_version": profile.observation_model_version,
            "b_version": profile.transition_model_version,
            "c_version": profile.preference_model_version,
            "precision_version": profile.precision_profile_version,
            "coupling_version": profile.coupling_matrix_version,
        }
    )
    simulator = DeterministicEmbodimentSimulator(
        SimulatorConfig(body_instance_id="body-1", physiology=True),
        clock=lambda: moment,
    )
    manager = EmbodimentManager(condition, simulator)
    _memory, _pipeline, cognitive = _pipeline_adapter(tmp_path)
    homeostasis = HomeostasisEngine(
        profile,
        state_dir=tmp_path / "homeostasis",
        now=moment,
    )
    supervisor = RuntimeSupervisor(
        manifest=manifest,
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime",
        embodiment_manager=manager,
    )
    return supervisor, homeostasis, moment


async def _dispatch_body(supervisor: RuntimeSupervisor, snapshot: BodyStateSnapshot):
    accepted = await supervisor.submit_event(
        event_type=RuntimeEventType.BODY_STATE,
        priority=RuntimeEventPriority.PERCEPTION,
        source="fixture",
        source_time=snapshot.captured_at,
        payload={"body_state": snapshot.model_dump(mode="json")},
    )
    assert accepted is not None
    queued = await supervisor.queue.get(timeout=0)
    assert queued is not None
    return await supervisor.dispatch(queued)


@pytest.mark.parametrize("mode", ["observe", "grounded"])
def test_observe_does_not_assimilate_and_grounded_updates_only_energy(tmp_path, mode: str) -> None:
    async def scenario() -> None:
        supervisor, homeostasis, moment = _embodied_supervisor(tmp_path, mode=mode)
        await supervisor.boot_and_recover()
        before_revision = homeostasis.snapshot.revision
        before = {name: factor.value for name, factor in homeostasis.snapshot.dimensions.items()}
        result = await _dispatch_body(supervisor, _body(0.25, 1, moment))
        assert result.status == "completed"
        if mode == "observe":
            assert homeostasis.snapshot.revision == before_revision
            assert {name: factor.value for name, factor in homeostasis.snapshot.dimensions.items()} == before
        else:
            assert homeostasis.snapshot.revision == before_revision + 1
            assert homeostasis.snapshot.dimensions["energy_reserve"].value != before["energy_reserve"]
            for dimension, value in before.items():
                if dimension != "energy_reserve":
                    assert homeostasis.snapshot.dimensions[dimension].value == value

            await supervisor.submit_user_message("hello", session_id="body-context")
            message = await supervisor.queue.get(timeout=0)
            assert message is not None
            await supervisor.dispatch(message)
            prepare_intents = [
                record
                for record in supervisor.journal.records()
                if record.record_type == "domain_command_intent"
                and (record.data.get("envelope") or {}).get("command_type") == "PrepareResponse"
            ]
            payload = prepare_intents[-1].data["envelope"]["payload"]["interoception"]
            assert payload["resource_level"] == 0.25
            assert "evidence_refs" not in payload
            assert "provider" not in str(payload).lower()
            assert "threshold" not in str(payload).lower()
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_active_reconsideration_debounce_and_terminal_preserve_runtime(tmp_path) -> None:
    async def scenario() -> None:
        supervisor, _homeostasis, moment = _embodied_supervisor(tmp_path, mode="active")
        await supervisor.boot_and_recover()
        await _dispatch_body(supervisor, _body(0.65, 1, moment))
        assert supervisor.queue.empty()

        await _dispatch_body(supervisor, _body(0.40, 2, moment + timedelta(seconds=1)))
        reconsideration = await supervisor.queue.get(timeout=0)
        assert reconsideration is not None
        assert reconsideration.event_type == RuntimeEventType.GOAL_RECONSIDERATION

        await _dispatch_body(supervisor, _body(0.65, 3, moment + timedelta(seconds=2)))
        assert supervisor.queue.empty(), "minimum replan interval must debounce a second crossing"

        await _dispatch_body(
            supervisor,
            _body(0.0, 4, moment + timedelta(seconds=3), terminal=True),
        )
        queued_types = {event.event_type for event in supervisor.queue.snapshot()}
        assert RuntimeEventType.AGENCY_TERMINAL in queued_types
        assert RuntimeEventType.GOAL_RECONSIDERATION in queued_types
        assert supervisor.status().embodiment is not None
        assert supervisor.status().embodiment.state == "terminal"
        assert supervisor.lifecycle == RuntimeLifecycle.RUNNING
        assert supervisor.status().health.cognitive_healthy is True
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())


def test_body_state_guard_rejects_wrong_instance_stale_future_and_rollback() -> None:
    class Provider:
        def __init__(self, snapshots: list[BodyStateSnapshot]) -> None:
            self.snapshots = snapshots

        async def snapshot(self) -> BodyStateSnapshot:
            return self.snapshots.pop(0)

    async def submit(**_kwargs):
        return None

    async def scenario() -> None:
        now = datetime.now(UTC)
        wrong = BodyStateGuard(
            Provider([_body(0.5, 1, now, body_instance_id="wrong")]),
            submit,
            expected_body_instance_id="body-1",
            clock=lambda: now,
        )
        with pytest.raises(ValueError, match="different body instance"):
            await wrong.capture_once()

        stale = BodyStateGuard(
            Provider([_body(0.5, 1, now - timedelta(seconds=20))]),
            submit,
            max_age_seconds=10,
            clock=lambda: now,
        )
        with pytest.raises(ValueError, match="stale"):
            await stale.capture_once()

        future = BodyStateGuard(
            Provider([_body(0.5, 1, now + timedelta(seconds=2))]),
            submit,
            clock=lambda: now,
        )
        with pytest.raises(ValueError, match="future"):
            await future.capture_once()

        rollback = BodyStateGuard(
            Provider([_body(0.5, 2, now), _body(0.5, 1, now)]),
            submit,
            clock=lambda: now,
        )
        await rollback.capture_once()
        with pytest.raises(ValueError, match="backwards"):
            await rollback.capture_once()

    asyncio.run(scenario())


def test_required_offline_provider_pauses_without_faulting_runtime(tmp_path) -> None:
    async def scenario() -> None:
        condition = EmbodimentCondition(
            mode="grounded",
            required_for_runtime=True,
            body_instance_id="body-1",
            capability_requirements=[
                CapabilityRequirement(capability=PHYSIOLOGY_BODY_STATE, required=True)
            ],
        )
        profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
        manifest = _manifest().model_copy(
            update={
                "embodiment": condition,
                "persona_version": profile.persona_version,
                "homeostatic_profile": profile.profile_ref,
                "schema_version": "homeostasis-state-v1",
                "a_version": profile.observation_model_version,
                "b_version": profile.transition_model_version,
                "c_version": profile.preference_model_version,
                "precision_version": profile.precision_profile_version,
                "coupling_version": profile.coupling_matrix_version,
            }
        )
        simulator = DeterministicEmbodimentSimulator(
            SimulatorConfig(body_instance_id="body-1", physiology=True, offline=True)
        )
        manager = EmbodimentManager(condition, simulator)
        _memory, _pipeline, cognitive = _pipeline_adapter(tmp_path)
        supervisor = RuntimeSupervisor(
            manifest=manifest,
            cognitive=cognitive,
            homeostasis=HomeostasisEngine(
                profile,
                state_dir=tmp_path / "homeostasis",
            ),
            state_dir=tmp_path / "runtime",
            embodiment_manager=manager,
        )
        await supervisor.boot_and_recover()
        assert supervisor.lifecycle == RuntimeLifecycle.PAUSED
        assert supervisor.status().embodiment is not None
        assert supervisor.status().embodiment.state == "unavailable"
        assert supervisor.status().health.cognitive_healthy is True
        await supervisor.stop(reason="test complete")

    asyncio.run(scenario())
