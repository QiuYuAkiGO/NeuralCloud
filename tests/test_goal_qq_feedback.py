"""Goal regulation distinguishes a verified QQ interaction from supportive valence."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.appraisal import PsychologicalAppraisal
from harness.homeostasis.models import QQHomeostaticFact


NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)
PROFILE = "presets/homeostasis/qq-social-drive-v1.json"


def envelope(engine, command_type, payload):
    sequence = engine.snapshot.acknowledged_receive_seq + 1
    return DomainCommandEnvelope[dict](
        command_type=command_type, event_id=f"goal-qq-event-{sequence}", receive_seq=sequence,
        correlation_id="goal-qq-feedback", runtime_instance_id="isolated-goal-qq-test",
        manifest_revision=1, idempotency_key=f"goal-qq-command-{sequence}",
        expected_revision=engine.snapshot.revision, issued_at=NOW,
        deadline=NOW + timedelta(seconds=60), payload=payload,
    )


def apply_fact_command(engine, kind, *, regulation_feedback):
    fact = QQHomeostaticFact(
        fact_id=f"verified-{kind}", group_id="10001", kind=kind,
        occurred_at=NOW, verification="qq_ui_verified",
        evidence_refs=[f"fixture://qq/{kind}"],
        related_attempt_id="verified-own-send" if kind == "verified_interaction" else None,
    )
    payload = {"event": {"event_type": "qq_fact", "source": "scheduler", "source_time": NOW,
        "payload": {"qq_fact": fact.model_dump(mode="json")}}}
    if regulation_feedback:
        payload["regulation_feedback"] = True
    return envelope(engine, "ApplyEvent", payload), fact


@pytest.mark.parametrize("regulation_feedback", [True, False])
def test_linked_interaction_does_not_imply_support_in_goal_mode_and_preserves_legacy(tmp_path, regulation_feedback):
    async def scenario():
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, now=NOW, clock=lambda: NOW)
        try:
            before = engine.snapshot.dimensions["social_connection"].value
            command, fact = apply_fact_command(engine, "verified_interaction", regulation_feedback=regulation_feedback)
            result = await engine.apply_event(command)
            assert result.status == "applied" and result.result["qq_fact"]["applied"] is True
            trace = result.result["trace"]
            assert trace["evidence_refs"] == fact.evidence_refs
            assert trace["applied_impacts"]["novelty"] > 0
            after = engine.snapshot.dimensions["social_connection"].value
            if regulation_feedback:
                assert after == before
                assert trace["suggested_impacts"].get("social_connection", 0) == 0
                assert trace["applied_impacts"]["social_connection"] == 0
            else:
                assert after > before
                assert trace["suggested_impacts"]["social_connection"] > 0
        finally:
            await engine.close()

    asyncio.run(scenario())


def test_negative_interpretation_can_lower_connection_after_verified_interaction(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, now=NOW, clock=lambda: NOW)
        try:
            fact_command, fact = apply_fact_command(engine, "verified_interaction", regulation_feedback=True)
            fact_result = await engine.apply_event(fact_command)
            assert fact_result.status == "applied"
            before = engine.snapshot.model_copy(deep=True)
            appraisal = PsychologicalAppraisal(
                episode_id="goal-qq-episode", appraisal_id="negative-reply-review",
                basis_snapshot_revision=before.revision, observed_event_refs=fact.evidence_refs,
                appraisal_kind="event_interpretation", suggested_impacts={"social_connection": -0.1},
                model_confidence=1.0, created_at=NOW, expires_at=NOW + timedelta(minutes=5),
            )
            result = await engine.apply_appraisal(envelope(engine, "ApplyAppraisal", {
                "episode_id": appraisal.episode_id, "appraisal": appraisal.model_dump(mode="json"),
                "verified_evidence_refs": fact.evidence_refs,
            }))
            assert result.status == "applied"
            assert result.result["trace"]["source"] == "psychological:event_interpretation"
            assert result.result["trace"]["evidence_refs"] == fact.evidence_refs
            assert result.result["trace"]["applied_impacts"]["social_connection"] < 0
            assert engine.snapshot.dimensions["social_connection"].value < before.dimensions["social_connection"].value
            assert engine.snapshot.dimensions["energy_reserve"] == before.dimensions["energy_reserve"]
        finally:
            await engine.close()

    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["send_attempt", "no_response"])
def test_goal_send_and_silence_add_no_fixed_reward_or_penalty(tmp_path, kind):
    async def scenario():
        engine = HomeostasisEngine(load_profile(PROFILE), state_dir=tmp_path, now=NOW, clock=lambda: NOW)
        try:
            before = {key: factor.value for key, factor in engine.snapshot.dimensions.items()}
            activity_at = engine._qq_activity_at
            command, _ = apply_fact_command(engine, kind, regulation_feedback=True)
            result = await engine.apply_event(command)
            assert result.status == "applied" and result.result["qq_fact"]["applied"] is True
            assert result.result["trace"]["suggested_impacts"] == {}
            assert all(value == 0 for value in result.result["trace"]["applied_impacts"].values())
            assert {key: factor.value for key, factor in engine.snapshot.dimensions.items()} == before
            assert engine._qq_activity_at == activity_at
        finally:
            await engine.close()

    asyncio.run(scenario())
