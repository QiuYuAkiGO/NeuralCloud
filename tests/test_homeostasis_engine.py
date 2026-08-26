from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import DIMENSIONS, HomeostasisEngine, ProfileRevisionStore, load_profile
from harness.homeostasis.models import HomeostaticProfile
from harness.research.manifest import ExperimentManifest

NOW = datetime(2026, 8, 25, 12, 0, tzinfo=UTC)


def _profile() -> HomeostaticProfile:
    return load_profile("presets/homeostasis/functional-proxy-v1.json")


def _command(*, sequence: int = 1, expected_revision: int = 0, at: datetime = NOW):
    return DomainCommandEnvelope[dict](
        command_type="ApplyEvent",
        event_id=f"evt_{sequence}",
        receive_seq=sequence,
        correlation_id=f"corr_{sequence}",
        runtime_instance_id="runtime_test",
        manifest_revision=1,
        idempotency_key=f"homeostasis:event:{sequence}",
        issued_at=at,
        deadline=at + timedelta(seconds=30),
        expected_revision=expected_revision,
        payload={
            "event": {
                "event_type": "user_message",
                "source": "user",
                "source_time": at,
                "payload": {
                    "text": "谢谢你的支持",
                    "appraisal": {
                        "source": "user_statement",
                        "precision": 0.75,
                        "suggested_impacts": {"social_connection": 0.08},
                    },
                },
            }
        },
    )


def test_profile_and_initial_snapshot_are_complete(tmp_path) -> None:
    profile = _profile()
    engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
    assert tuple(engine.snapshot.dimensions) == DIMENSIONS
    assert engine.snapshot.profile_revision == 1
    assert len(engine.snapshot.candidates) == 3
    for factor in engine.snapshot.dimensions.values():
        assert sum(factor.posterior.values()) == pytest.approx(1.0)


def test_event_is_bounded_coupled_idempotent_and_replayable(tmp_path) -> None:
    async def scenario() -> None:
        profile = _profile()
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        before = engine.snapshot.dimensions["social_connection"].value
        result = await engine.apply_event(_command())
        assert result.status == "applied"
        assert result.committed_revision == 1
        assert engine.snapshot.dimensions["social_connection"].value > before
        assert engine.snapshot.dimensions["safety_predictability"].velocity > 0
        replay = await engine.apply_event(_command())
        assert replay.status == "idempotent_replay"
        assert engine.snapshot.revision == 1
        await engine.close()

        restored = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW + timedelta(hours=1))
        assert restored.snapshot.revision == 1
        replay_after_restart = await restored.apply_event(_command())
        assert replay_after_restart.status == "idempotent_replay"

    asyncio.run(scenario())


def test_heartbeat_caps_downtime_drift(tmp_path) -> None:
    async def scenario() -> None:
        profile = _profile()
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        initial = engine.snapshot.dimensions["energy_reserve"]
        lowered = initial.model_copy(update={"value": 0.2})
        engine.snapshot = engine.snapshot.model_copy(
            update={"dimensions": {**engine.snapshot.dimensions, "energy_reserve": lowered}}
        )
        command = _command(at=NOW + timedelta(days=2))
        command = command.model_copy(
            update={
                "command_type": "ApplyHeartbeat",
                "idempotency_key": "homeostasis:heartbeat:1",
                "payload": {
                    "event": {
                        "event_type": "heartbeat",
                        "source": "scheduler",
                        "source_time": NOW + timedelta(days=2),
                        "payload": {},
                    }
                },
            }
        )
        await engine.apply_heartbeat(command)
        changed = engine.snapshot.dimensions["energy_reserve"].value - 0.2
        maximum = profile.dimensions["energy_reserve"].drift_per_hour * profile.max_heartbeat_step_seconds / 3600
        assert changed <= maximum + 1e-8

    asyncio.run(scenario())


def test_profile_revision_is_immutable_and_staged(tmp_path) -> None:
    manifest = ExperimentManifest.model_validate_json(
        open("presets/runtime/development-demo.json", encoding="utf-8").read()
    )
    profile = _profile()
    store = ProfileRevisionStore(
        profile_dir=tmp_path / "profiles",
        manifest_dir=tmp_path / "manifests",
        pending_path=tmp_path / "pending.json",
    )
    candidate = profile.model_dump(mode="json")
    candidate["dimensions"]["energy_reserve"]["baseline_band"] = [0.58, 0.8]
    staged = store.stage(
        current_profile=profile,
        current_manifest=manifest,
        candidate=candidate,
        base_revision=1,
    )
    assert staged["restart_required"] is True
    assert store.pending() == staged
    saved = load_profile(staged["profile_path"])
    assert saved.revision == 2
    with pytest.raises(ValueError, match="revision conflict"):
        store.stage(
            current_profile=profile,
            current_manifest=manifest,
            candidate=candidate,
            base_revision=0,
        )


def test_new_profile_revision_maps_existing_snapshot_without_downtime_drift(tmp_path) -> None:
    async def scenario() -> None:
        profile = _profile()
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        await engine.apply_event(_command())
        await engine.close()
        candidate = profile.model_dump(mode="json")
        candidate["revision"] = 2
        candidate["dimensions"]["social_connection"]["baseline_band"] = [0.58, 0.76]
        revised = HomeostaticProfile.model_validate(candidate)
        restored = HomeostasisEngine(revised, state_dir=tmp_path, now=NOW + timedelta(days=3))
        assert restored.snapshot.profile_revision == 2
        assert restored.snapshot.dimensions["social_connection"].target_band == (0.58, 0.76)
        assert restored.snapshot.dimensions["social_connection"].value == pytest.approx(
            engine.snapshot.dimensions["social_connection"].value
        )

    asyncio.run(scenario())
