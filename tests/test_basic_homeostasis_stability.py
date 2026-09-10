"""Accelerated domain acceptance for the eight-hour basic-function milestone.

These tests advance an explicit clock at the production 60-second heartbeat
cadence. Every step uses the public command API and real SQLite persistence;
neither state integration nor watchdogs are disabled. They are not a substitute
for an eight-hour process/desktop soak or a calibrated physiology experiment.
"""

from __future__ import annotations

import asyncio
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import DIMENSIONS, HomeostasisEngine, load_profile


START = datetime(2026, 9, 9, 0, 0, tzinfo=UTC)
PROFILE = Path(__file__).resolve().parents[1] / "presets/homeostasis/functional-proxy-v1.json"
EIGHT_HOURS = 8 * 60


def _command(engine, at, kind, payload):
    sequence = engine.snapshot.acknowledged_receive_seq + 1
    return DomainCommandEnvelope[dict](
        command_type=kind,
        event_id=f"basic-stability-{sequence}",
        receive_seq=sequence,
        correlation_id=f"basic-stability-{sequence}",
        runtime_instance_id="basic-stability-runtime",
        manifest_revision=1,
        idempotency_key=f"basic-stability:{kind}:{sequence}",
        expected_revision=engine.snapshot.revision,
        issued_at=at,
        deadline=at + timedelta(minutes=1),
        payload=payload,
    )


async def _heartbeat(engine, at):
    command = _command(engine, at, "ApplyHeartbeat", {
        "event": {"event_type": "heartbeat", "source": "scheduler", "source_time": at, "payload": {}},
    })
    result = await engine.apply_heartbeat(command)
    assert result.status == "applied", result.error
    return command


async def _event(engine, at, impacts):
    command = _command(engine, at, "ApplyEvent", {
        "event": {
            "event_type": "fixture_event", "source_time": at,
            "payload": {"appraisal": {
                "source": "runtime", "precision": 1.0, "suggested_impacts": impacts,
            }},
        },
    })
    result = await engine.apply_event(command)
    assert result.status == "applied", result.error


async def _observe(engine, at, values):
    command = _command(engine, at, "AssimilateObservation", {
        "observation": {
            "source": "system_sensor", "precision": 1.0,
            "observed_values": values, "observed_at": at,
            "evidence_refs": [{"provider_id": "deterministic-test", "ref": f"fixture://{at.isoformat()}"}],
        },
    })
    result = await engine.assimilate_observation(command)
    assert result.status == "applied", result.error
    return result


def _assert_finite_bounded(engine):
    for name, factor in engine.snapshot.dimensions.items():
        lower, upper = engine.profile.dimensions[name].hard_bounds
        assert math.isfinite(factor.value)
        assert lower <= factor.value <= upper
        assert all(math.isfinite(value) and 0 <= value <= 1 for value in factor.posterior.values())
        assert sum(factor.posterior.values()) == pytest.approx(1.0)
    assert engine.snapshot.interoceptive_summary.snapshot_revision == engine.snapshot.revision


def _distance_from_center(engine):
    return sum(
        abs(factor.value - sum(engine.profile.dimensions[name].baseline_band) / 2)
        for name, factor in engine.snapshot.dimensions.items()
    )


@pytest.mark.parametrize("perturbed", [False, True], ids=["nominal", "recovering"])
def test_functional_proxy_remains_healthy_for_eight_hours_with_restart(tmp_path, perturbed):
    async def scenario():
        clock = [START]
        profile = load_profile(PROFILE)
        engine = HomeostasisEngine(profile, state_dir=tmp_path, clock=lambda: clock[0], ledger_cache_size=16)
        assert profile.mode == "functional_proxy"
        if perturbed:
            await _event(engine, START, {
                name: (0.06 if index % 2 else -0.06) for index, name in enumerate(DIMENSIONS)
            })
        initial_distance = _distance_from_center(engine)
        changed = False
        for minute in range(1, EIGHT_HOURS + 1):
            clock[0] = START + timedelta(minutes=minute)
            before = {name: factor.value for name, factor in engine.snapshot.dimensions.items()}
            last_command = await _heartbeat(engine, clock[0])
            changed |= any(engine.snapshot.dimensions[name].value != value for name, value in before.items())
            _assert_finite_bounded(engine)
            assert (await engine.health()).healthy
            assert engine.snapshot.interoceptive_summary.salience == "nominal"
            assert engine._command_ledger.cached_entries <= 16
            if minute == EIGHT_HOURS // 2:
                saved = engine.snapshot.model_dump(mode="json")
                await engine.close()
                engine = HomeostasisEngine(profile, state_dir=tmp_path, clock=lambda: clock[0], ledger_cache_size=16)
                assert engine.snapshot.model_dump(mode="json") == saved
                replay = await engine.apply_heartbeat(last_command)
                assert replay.status == "idempotent_replay"
                assert engine.snapshot.model_dump(mode="json") == saved
        if perturbed:
            assert changed, "The perturbed state must actually integrate; a frozen state cannot pass."
            assert _distance_from_center(engine) < initial_distance * 0.01
        else:
            assert _distance_from_center(engine) < 1e-9
        await engine.close()
        assert sum(1 for _ in engine.trace_path.open(encoding="utf-8")) == EIGHT_HOURS + int(perturbed)

    asyncio.run(scenario())


@pytest.mark.parametrize("noise", ["empty_event", "body_observation"])
def test_unrelated_events_do_not_starve_heartbeat_recovery(tmp_path, noise):
    async def scenario():
        clock = [START]
        profile = load_profile(PROFILE)
        control = HomeostasisEngine(profile, state_dir=tmp_path / "control", clock=lambda: clock[0])
        observed = HomeostasisEngine(profile, state_dir=tmp_path / "observed", clock=lambda: clock[0])
        for engine in (control, observed):
            await _event(engine, START, {"social_connection": -0.1})
            await _heartbeat(engine, START)
        for minute in range(1, 11):
            clock[0] = START + timedelta(minutes=minute, seconds=-1)
            if noise == "empty_event":
                await _event(observed, clock[0], {})
            else:
                await _observe(observed, clock[0], {"energy_reserve": 0.4})
            # The intermediate event must not steal 59 of 60 seconds from an
            # unrelated dimension that received no new observation or impact.
            clock[0] += timedelta(seconds=1)
            await _heartbeat(control, clock[0])
            await _heartbeat(observed, clock[0])
            assert observed.snapshot.dimensions["social_connection"].value == pytest.approx(
                control.snapshot.dimensions["social_connection"].value, abs=1e-10,
            )
            if minute == 5:
                await observed.close()
                observed = HomeostasisEngine(profile, state_dir=tmp_path / "observed", clock=lambda: clock[0])
        await control.close()
        await observed.close()

    asyncio.run(scenario())


def test_real_profile_recovers_from_hard_boundaries_without_disabling_watchdog(tmp_path):
    async def scenario():
        clock = [START]
        profile = load_profile(PROFILE)
        engine = HomeostasisEngine(profile, state_dir=tmp_path, clock=lambda: clock[0], ledger_cache_size=16)
        assert profile.watchdog_boundary_heartbeats == 3
        # A one-time, explicit fixture observation establishes stressed initial
        # conditions. All later movement must come from the unchanged engine.
        await _observe(engine, START, {
            name: profile.dimensions[name].hard_bounds[index % 2]
            for index, name in enumerate(DIMENSIONS)
        })
        initial_distance = _distance_from_center(engine)
        for minute in range(1, EIGHT_HOURS + 1):
            clock[0] = START + timedelta(minutes=minute)
            await _heartbeat(engine, clock[0])
            _assert_finite_bounded(engine)
            assert (await engine.health()).healthy
            for name, factor in engine.snapshot.dimensions.items():
                lower, upper = profile.dimensions[name].hard_bounds
                assert lower < factor.value < upper
        assert _distance_from_center(engine) < initial_distance * 0.5
        await engine.close()

    asyncio.run(scenario())


def test_grounded_depletion_is_not_reported_as_eight_hour_idle_stability(tmp_path):
    async def scenario():
        clock = [START]
        profile = load_profile(PROFILE)
        engine = HomeostasisEngine(profile, state_dir=tmp_path, clock=lambda: clock[0], ledger_cache_size=16)
        for minute in range(EIGHT_HOURS + 1):
            clock[0] = START + timedelta(minutes=minute)
            measured = 0.8 * (1 - minute / EIGHT_HOURS)
            result = await _observe(engine, clock[0], {"energy_reserve": measured})
            # Sensor evidence remains authoritative even though the functional
            # proxy's heartbeat normally drifts toward its healthy target band.
            expected = max(profile.dimensions["energy_reserve"].hard_bounds[0], measured)
            assert engine.snapshot.dimensions["energy_reserve"].value == pytest.approx(expected)
            assert result.result["trace"]["observed_values"]["energy_reserve"] == pytest.approx(measured)
            # A heartbeat immediately after capture cannot invent one minute of
            # replenishment, and depleted-body evidence must retain protection.
            await _heartbeat(engine, clock[0])
            assert engine.snapshot.dimensions["energy_reserve"].value == pytest.approx(expected)
            _assert_finite_bounded(engine)
        assert "energy_reserve:hard_bound_sustained" in engine.snapshot.watchdog
        assert not (await engine.health()).healthy
        assert engine.snapshot.interoceptive_summary.salience == "critical"
        await engine.close()

    asyncio.run(scenario())
