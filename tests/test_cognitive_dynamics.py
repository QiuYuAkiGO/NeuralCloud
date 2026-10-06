from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, datetime, timedelta

import pytest

from harness.homeostasis.dynamics import AttentionAssessment, CognitiveExposure, DynamicsConfig, DynamicsState, advance, appraise, expose
from harness.homeostasis.engine import HomeostasisEngine
from harness.homeostasis.profiles import load_profile
from harness.contracts.envelope import DomainCommandEnvelope


NOW = datetime(2026, 10, 6, tzinfo=UTC)
CONFIG = DynamicsConfig(mode="active")


def state(**changes):
    values = {"cognitive_load": .95, "novelty": .95, "safety_predictability": .66,
              "competence": .62, "autonomy": .68, "social_connection": .62}
    result = DynamicsState.migrate(values, values, NOW)
    return result.model_copy(update=changes)


def receipt(identity="one", **changes):
    return CognitiveExposure(exposure_id=identity, context_id="group:1044946257", attended_at=NOW,
        input_tokens=1000, content_hash=hashlib.sha256(identity.encode()).hexdigest(), **changes)


def command(engine, sequence, at, payload, *, heartbeat=False):
    return DomainCommandEnvelope(command_type="ApplyHeartbeat" if heartbeat else "ApplyEvent",
        event_id=f"test-{sequence}", receive_seq=sequence, correlation_id="test", runtime_instance_id="test",
        manifest_revision=1, idempotency_key=f"test-{sequence}", issued_at=at, deadline=at+timedelta(days=1),
        expected_revision=engine.snapshot.revision,
        payload={"event": {"event_type": "heartbeat" if heartbeat else "perception", "source_time": at,
                            "source": "runtime", "payload": payload}})


def active_profile(mode="active"):
    old = load_profile("presets/homeostasis/qq-social-drive-v1.json")
    return old.model_copy(update={"cognitive_dynamics": DynamicsConfig(mode=mode)})


def test_rest_has_distinct_fast_and_slow_recovery_without_psychological_rewards():
    initial = state(fatigue=.9)
    rested = advance(initial, NOW+timedelta(minutes=5), CONFIG)
    assert rested.load == pytest.approx(.26054, abs=.00001)
    assert rested.fatigue == pytest.approx(.70092, abs=.00001)
    assert rested.baselines == initial.baselines
    assert initial.load == .95  # advance is transactional, not an in-place mutation
    assert advance(initial, NOW+timedelta(hours=24), CONFIG).load == pytest.approx(.1)


def test_migration_does_not_invent_a_future_relationship_gain():
    values = state().values() | {"social_connection":.22}
    migrated = DynamicsState.migrate(values,{key:.7 for key in values},NOW)
    assert migrated.latent_confidence == 0
    assert advance(migrated,NOW+timedelta(days=2),CONFIG).values()["social_connection"] == .22


def test_clock_frequency_does_not_change_dynamics_even_with_active_work():
    initial = expose(state(), receipt(), CONFIG)
    one = advance(initial, NOW+timedelta(hours=1), CONFIG)
    many = initial
    for seconds in range(5, 3601, 5):
        many = advance(many, NOW+timedelta(seconds=seconds), CONFIG)
    for name in ("load", "fatigue", "novelty", "boredom"):
        assert getattr(one, name) == pytest.approx(getattr(many, name), abs=1e-9)
    assert one.recovered_since == pytest.approx(many.recovered_since, abs=timedelta(microseconds=5))


def test_habituation_is_topic_specific_recovers_and_never_discounted_work():
    current = state()
    first = None
    for index in range(10):
        current = expose(current, receipt(str(index), assessment=AttentionAssessment(
            topic="repeated", unexpectedness=1, meaning=.5, confidence=1)), CONFIG)
        first = current.work[-1].novelty if first is None else first
    assert current.work[-1].novelty < first/5
    assert sum(w.units for w in current.work) == 10000
    fresh = expose(current, receipt("fresh", assessment=AttentionAssessment(
        topic="different", unexpectedness=1, meaning=.5, confidence=1)), CONFIG)
    assert fresh.work[-1].novelty == 1
    later_receipt = receipt("later", assessment=AttentionAssessment(topic="repeated", unexpectedness=1, meaning=.5, confidence=1))
    later_receipt = later_receipt.model_copy(update={"attended_at": NOW+timedelta(hours=6)})
    later = expose(current, later_receipt, CONFIG)
    assert later.work[-1].novelty > .65
    repeated = expose(current, receipt("same-body", assessment=AttentionAssessment(
        topic="different", unexpectedness=1, meaning=1, confidence=1)), CONFIG, repeated_content=True)
    assert repeated.work[-1].novelty == 0


def test_busy_boredom_and_voluntary_rest_are_distinct():
    active = expose(state(), receipt(assessment=AttentionAssessment(
        topic="noise", unexpectedness=1, meaning=0, confidence=1)), CONFIG)
    resting = expose(state(), receipt(assessment=AttentionAssessment(
        topic="noise", unexpectedness=1, meaning=0, confidence=1, wants_engagement=False)), CONFIG)
    assert advance(active, NOW+timedelta(seconds=60), CONFIG).boredom > .08
    assert advance(resting, NOW+timedelta(seconds=60), CONFIG).boredom == 0
    assert advance(active, NOW+timedelta(minutes=20), CONFIG).boredom > .8
    assert advance(resting, NOW+timedelta(minutes=20), CONFIG).boredom == 0


def test_absolute_budgets_cannot_be_farmed_by_alternating_polarity():
    current = state()
    for index in range(100):
        current = appraise(current, NOW, {"autonomy": 1 if index % 2 else -1}, 1, CONFIG,
                           eligible_dimensions={"autonomy"}, verified_outcome=True)
    assert sum(c.short for c in current.changes) <= .15+1e-12
    assert sum(c.baseline for c in current.changes) <= .02+1e-12
    report = appraise(state(), NOW, {"competence": 1}, 1, CONFIG, eligible_dimensions={"competence"})
    assert report.baselines["competence"] == .62
    assert report.responses["competence"] == .05


def test_receipt_dedup_restart_and_quiet_recovery(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(active_profile(), state_dir=tmp_path, clock=lambda: NOW)
        payload = {"cognitive_exposure": receipt().model_dump(mode="json")}
        result = await engine.apply_event(command(engine, 1, NOW, payload))
        assert result.status == "applied"
        assert engine.snapshot.schema_version == "homeostasis-state-v3"
        assert engine.snapshot.interoceptive_summary.schema_version == "homeostatic-interoception-v2"
        await engine.apply_event(command(engine, 2, NOW, payload))
        assert engine._dynamics.exposures == 1
        await engine.close()
        now = NOW+timedelta(hours=24)
        resumed = HomeostasisEngine(active_profile(), state_dir=tmp_path, clock=lambda: now)
        result = await resumed.apply_heartbeat(command(resumed, 3, now, {}, heartbeat=True))
        assert result.status == "applied"
        assert resumed.snapshot.dimensions["cognitive_load"].value == pytest.approx(.1)
        assert resumed.snapshot.dimensions["energy_reserve"].value == engine.snapshot.dimensions["energy_reserve"].value
        assert resumed.snapshot.dimensions["energy_reserve"].precision < .01
        assert not resumed.snapshot.watchdog
        assert not any(n.dimension == "cognitive_load" and n.direction == "low" for n in resumed.snapshot.interoceptive_summary.dominant_needs)
        await resumed.close()
    asyncio.run(scenario())


def test_raw_qq_fact_has_no_psychological_credit_or_global_pause(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(active_profile(), state_dir=tmp_path, clock=lambda: NOW)
        before = {k: v.value for k, v in engine.snapshot.dimensions.items()}
        for index in range(8):
            fact = {"fact_id": str(index), "group_id": "1060979317", "kind": "verified_message",
                    "occurred_at": NOW.isoformat(), "verification": "qq_protocol_verified", "evidence_refs": [f"fact:{index}"]}
            assert (await engine.apply_event(command(engine, index+1, NOW, {"qq_fact": fact}))).status == "applied"
        assert {k: v.value for k, v in engine.snapshot.dimensions.items()} == before
        assert (await engine.health()).healthy
        await engine.close()
    asyncio.run(scenario())


def test_shadow_calculates_without_changing_authoritative_values(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(active_profile("shadow"), state_dir=tmp_path, clock=lambda: NOW)
        before = {k: v.value for k, v in engine.snapshot.dimensions.items()}
        result = await engine.apply_event(command(engine, 1, NOW, {"cognitive_exposure": receipt().model_dump(mode="json")}))
        assert result.status == "applied"
        assert {k: v.value for k, v in engine.snapshot.dimensions.items()} == before
        assert engine.snapshot.dynamics["mode"] == "shadow"
        assert engine._dynamics.exposures == 1
        await engine.close()
    asyncio.run(scenario())
