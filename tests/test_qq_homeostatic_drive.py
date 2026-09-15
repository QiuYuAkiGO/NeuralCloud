"""Offline social-need dynamics and durable evidence accounting; no QQ I/O."""

import asyncio
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.models import HomeostaticProfile, QQHomeostaticFact
from harness.homeostasis.ledger import CommandLedgerConflictError


START = datetime(2026, 9, 12, tzinfo=UTC)
PROFILE = "presets/homeostasis/qq-social-drive-v1.json"
DEFAULT = "presets/homeostasis/functional-proxy-v1.json"


def command(engine, at, *, fact=None, resting=False, heartbeat=False):
    sequence = engine.snapshot.acknowledged_receive_seq + 1
    payload = {"qq_drive_context": {"resting": resting}} if heartbeat else {"qq_fact": fact}
    return DomainCommandEnvelope[dict](
        command_type="ApplyHeartbeat" if heartbeat else "ApplyEvent",
        event_id=f"qq-drive-{sequence}", receive_seq=sequence, correlation_id=f"qq-drive-{sequence}",
        runtime_instance_id="isolated-drive-test", manifest_revision=1,
        idempotency_key=f"drive:{sequence}", expected_revision=engine.snapshot.revision,
        issued_at=at, deadline=at + timedelta(seconds=60),
        payload={"event": {"event_type": "heartbeat" if heartbeat else "qq_fact",
                           "source": "scheduler", "source_time": at, "payload": payload}},
    )


def fact(kind="verified_interaction", *, identity="observation-1", at=START, **extra):
    value = {"schema_version": "qq-homeostatic-fact-v1", "fact_id": identity, "group_id": "10001",
             "kind": kind, "occurred_at": at.isoformat(), "verification": "qq_ui_verified",
             "evidence_refs": [f"fixture://{identity}"]}
    if kind == "verified_interaction":
        value["related_attempt_id"] = "actual-own-send-receipt"
    return {**value, **extra}


def values(engine):
    return {key: factor.value for key, factor in engine.snapshot.dimensions.items()}


async def tick(engine, clock, *, resting=False, seconds=60):
    clock[0] += timedelta(seconds=seconds)
    result = await engine.apply_heartbeat(command(engine, clock[0], heartbeat=True, resting=resting))
    assert result.status == "applied"
    return result


def test_new_profile_opt_in_preserves_original_persona_and_all_default_parameters():
    old = json.loads(Path(DEFAULT).read_text(encoding="utf-8"))
    new = json.loads(Path(PROFILE).read_text(encoding="utf-8"))
    assert "qq_social_drive" not in old
    assert load_profile(DEFAULT).qq_social_drive is None
    assert load_profile(PROFILE).qq_social_drive.enabled
    assert {k: v for k, v in new.items() if k not in {"profile_id", "revision", "qq_social_drive"}} == {
        k: v for k, v in old.items() if k not in {"profile_id", "revision"}
    }
    raw = dict(new, qq_social_drive={})
    assert not HomeostaticProfile.model_validate(raw).qq_social_drive.enabled
    assert "boredom" not in load_profile(PROFILE).dimensions


@pytest.mark.parametrize("mutation", [
    {"social_floor": 0.05}, {"novelty_floor": 0.35},
    {"social_depletion_per_hour": 0.02}, {"novelty_depletion_per_hour": float("nan")},
])
def test_profile_rejects_runaway_bounds_or_depletion_that_cannot_create_need(mutation):
    raw = load_profile(PROFILE).model_dump(mode="json")
    raw["qq_social_drive"].update(mutation)
    with pytest.raises(ValidationError):
        HomeostaticProfile.model_validate(raw)


def test_eight_hours_silence_only_opt_in_profile_develops_bounded_low_needs(tmp_path):
    async def run():
        clock = [START]
        active = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path / "active", clock=lambda: clock[0])
        legacy = HomeostasisEngine(load_profile(DEFAULT), state_dir=tmp_path / "legacy", clock=lambda: clock[0])
        legacy_initial = values(legacy)
        first = {}
        for minute in range(1, 481):
            await tick(active, clock)
            await legacy.apply_heartbeat(command(legacy, clock[0], heartbeat=True))
            summary = active.snapshot.interoceptive_summary
            first.setdefault(summary.salience, minute)
            assert not active.snapshot.watchdog
            assert legacy.snapshot.interoceptive_summary.salience == "nominal"
        assert 45 <= first["noticeable"] < first["marked"] <= 360
        assert values(legacy) == legacy_initial
        summary = active.snapshot.interoceptive_summary
        assert summary.salience == "marked"
        assert {(need.dimension, need.direction) for need in summary.dominant_needs} >= {
            ("social_connection", "low"), ("novelty", "low")}
        assert {"connect", "explore"}.issubset(summary.policy_refs)
        drive = active.profile.qq_social_drive
        assert active.snapshot.dimensions["social_connection"].value >= drive.social_floor - 0.001
        assert active.snapshot.dimensions["novelty"].value >= drive.novelty_floor - 0.001
        before = values(active)
        for _ in range(30):
            await tick(active, clock)
        assert abs(values(active)["social_connection"] - before["social_connection"]) < 0.001
        assert abs(values(active)["novelty"] - before["novelty"]) < 0.001
        await active.close()
        await legacy.close()
    asyncio.run(run())


def test_long_offline_gap_does_not_instantly_integrate_hours_of_need(tmp_path):
    async def run():
        clock = [START]
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, clock=lambda: clock[0])
        before = values(engine)
        await tick(engine, clock, seconds=8 * 3600)
        assert before["social_connection"] - values(engine)["social_connection"] <= 0.14 * 90 / 3600 + 1e-9
        assert engine.snapshot.interoceptive_summary.salience == "nominal"
        await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize("condition", ["rest", "low_energy", "high_load"])
def test_rest_and_resource_pressure_suppress_idle_drive_without_disabling_recovery(tmp_path, condition):
    async def run():
        clock = [START]
        profile = load_profile(PROFILE)
        active = HomeostasisEngine(profile, state_dir=tmp_path / "active", clock=lambda: clock[0])
        inactive_profile = profile.model_copy(update={"qq_social_drive": profile.qq_social_drive.model_copy(update={"enabled": False})})
        control = HomeostasisEngine(inactive_profile, state_dir=tmp_path / "control", clock=lambda: clock[0])
        factors = dict(active.snapshot.dimensions)
        for dimension, value in {"social_connection": 0.3, "novelty": 0.2,
                                 **({"energy_reserve": 0.3} if condition == "low_energy" else {}),
                                 **({"cognitive_load": 0.8} if condition == "high_load" else {})}.items():
            factors[dimension] = factors[dimension].model_copy(update={"value": value})
        active.snapshot = active.snapshot.model_copy(update={"dimensions": factors})
        control.snapshot = active.snapshot.model_copy(deep=True)
        await tick(active, clock, seconds=900, resting=condition == "rest")
        await control.apply_heartbeat(command(control, clock[0], heartbeat=True))
        assert values(active) == values(control)
        assert values(active)["social_connection"] > 0.3
        assert values(active)["novelty"] > 0.2
        await active.close()
        await control.close()
    asyncio.run(run())


@pytest.mark.parametrize("kind", ["send_attempt", "no_response"])
def test_sending_and_silence_do_not_create_social_reward_punishment_or_idle_reset(tmp_path, kind):
    async def run():
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, now=START)
        before = values(engine)
        activity = engine._qq_activity_at
        result = await engine.apply_event(command(engine, START, fact=fact(kind)))
        assert result.status == "applied" and result.result["qq_fact"]["applied"]
        assert values(engine) == before and engine._qq_activity_at == activity
        assert result.result["trace"]["suggested_impacts"] == {}
        await engine.close()
    asyncio.run(run())


def test_new_message_is_stimulation_not_proof_of_interaction_and_does_not_repeat(tmp_path):
    async def run():
        clock = [START]
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, clock=lambda: clock[0])
        clock[0] += timedelta(minutes=2)
        item = fact("verified_message", at=clock[0])
        before = values(engine)
        first = await engine.apply_event(command(engine, clock[0], fact=item))
        assert first.status == "applied"
        assert values(engine)["social_connection"] == before["social_connection"]
        assert values(engine)["novelty"] > before["novelty"]
        applied = values(engine)
        original_activity = engine._qq_activity_at
        clock[0] += timedelta(minutes=2)
        duplicate = await engine.apply_event(command(engine, clock[0], fact=item))
        assert duplicate.result["qq_fact"]["applied"] is False
        assert values(engine) == applied and engine._qq_activity_at == original_activity
        await engine.close()
        restored = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, clock=lambda: clock[0])
        duplicate = await restored.apply_event(command(restored, clock[0], fact=item))
        assert duplicate.result["qq_fact"]["applied"] is False
        assert values(restored) == applied and restored._qq_activity_at == original_activity
        for _ in range(3):
            await tick(restored, clock)
        assert values(restored)["social_connection"] == applied["social_connection"]
        await restored.close()
    asyncio.run(run())


def test_verified_interaction_and_experience_restore_low_need_with_actual_sources(tmp_path):
    async def run():
        clock = [START]
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, clock=lambda: clock[0])
        for _ in range(240):
            await tick(engine, clock)
        before = values(engine)
        item = fact(at=clock[0])
        original = command(engine, clock[0], fact=item)
        interaction = await engine.apply_event(original)
        after = values(engine)
        assert after["social_connection"] > before["social_connection"]
        assert after["novelty"] > before["novelty"]
        assert interaction.result["trace"]["evidence_refs"] == item["evidence_refs"]
        assert next(need for need in engine.snapshot.interoceptive_summary.dominant_needs
                    if need.dimension == "social_connection").trend == "recovering"
        replay = await engine.apply_event(original)
        assert replay.status == "idempotent_replay" and values(engine) == after
        novelty = await engine.apply_event(command(engine, clock[0], fact=fact("verified_novelty", identity="experience-1", at=clock[0])))
        assert novelty.status == "applied" and values(engine)["novelty"] > after["novelty"]
        await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize("fault", ["missing_link", "missing_evidence", "unverified", "text", "old", "future", "appraisal"])
def test_unbound_or_stale_facts_are_rejected_without_state_or_fact_write(tmp_path, fault):
    async def run():
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, now=START)
        item = fact()
        if fault == "missing_link":
            item.pop("related_attempt_id")
        elif fault == "missing_evidence":
            item["evidence_refs"] = []
        elif fault == "unverified":
            item["verification"] = "model_guessed"
        elif fault == "text":
            item["text"] = "not-a-supported-field"
        elif fault in {"old", "future"}:
            item["occurred_at"] = (START + timedelta(seconds=-301 if fault == "old" else 6)).isoformat()
        cmd = command(engine, START, fact=item)
        if fault == "appraisal":
            cmd.payload["event"]["payload"]["appraisal"] = {"suggested_impacts": {"social_connection": 1}}
        result = await engine.apply_event(cmd)
        assert result.status == "rejected" and engine.snapshot.revision == 0
        assert engine._command_ledger.qq_fact_hash("qq:10001:observation-1") is None
        await engine.close()
    asyncio.run(run())


def test_fact_content_conflict_and_transaction_failure_cannot_double_reward(tmp_path, monkeypatch):
    async def run():
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, now=START)
        item = fact()
        await engine.apply_event(command(engine, START, fact=item))
        before = engine.snapshot.model_copy(deep=True)
        changed = {**item, "kind": "verified_novelty"}
        conflict = await engine.apply_event(command(engine, START, fact=changed))
        assert conflict.status == "conflict" and engine.snapshot == before
        # Simulate a stale pre-read: the transactional unique key must reject
        # the second appraisal and roll back snapshot, command and trace.
        monkeypatch.setattr(engine._command_ledger, "qq_fact_hash", lambda key: None)
        with pytest.raises(CommandLedgerConflictError):
            await engine.apply_event(command(engine, START, fact=item))
        assert engine.snapshot == before
        assert engine._command_ledger.load_snapshot() == before.model_dump(mode="json")
        await engine.close()
    asyncio.run(run())


def test_disabled_profile_rejects_qq_fact_instead_of_silently_reinterpreting_it(tmp_path):
    async def run():
        engine = HomeostasisEngine(load_profile(DEFAULT), state_dir=tmp_path, now=START)
        result = await engine.apply_event(command(engine, START, fact=fact()))
        assert result.status == "rejected" and engine.snapshot.revision == 0
        await engine.close()
    asyncio.run(run())


@pytest.mark.parametrize("dimension,low_policy,high_policy", [
    ("social_connection", "connect", "quiet"), ("novelty", "explore", "familiar")])
def test_policy_direction_never_increases_already_high_social_or_novelty(tmp_path, dimension, low_policy, high_policy):
    engine = HomeostasisEngine(load_profile(DEFAULT), state_dir=tmp_path, now=START)
    for value, expected, sign in [(0.1, low_policy, 1), (0.94, high_policy, -1)]:
        factors = dict(engine.snapshot.dimensions)
        factors[dimension] = factors[dimension].model_copy(update={"value": value})
        policy = next(item for item in engine._rank_policies(factors) if item.source_drive == dimension)
        assert policy.policy_id == expected and policy.expected_impacts[dimension] * sign > 0
    asyncio.run(engine.close())


def test_fact_hash_is_stable_for_evidence_order_but_not_changed_identity():
    original = QQHomeostaticFact.model_validate(fact(evidence_refs=["fixture://a", "fixture://b"]))
    reordered = QQHomeostaticFact.model_validate(fact(evidence_refs=["fixture://b", "fixture://a"]))
    assert original.fact_key == reordered.fact_key and original.fact_hash == reordered.fact_hash
    assert original.fact_hash != original.model_copy(update={"related_attempt_id": "other-receipt"}).fact_hash
