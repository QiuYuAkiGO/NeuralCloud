from __future__ import annotations

import sqlite3
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from harness.contracts.body import BodyStateSnapshot as WireBodyState
from harness.embodiment.contracts import BodyStateSnapshot
from harness.research.grounded_agency.manifest import GroundedAgencyCondition
from harness.research.interoception import OpaqueInteroceptionStore

NOW = datetime(2026, 9, 5, tzinfo=timezone.utc)


def condition(treatment: str = "opaque_current", delay: str = "none", noise: str = "none") -> GroundedAgencyCondition:
    return GroundedAgencyCondition.model_validate({
        "protocol_version": "grounded-agency-v1", "condition_id": "test-v1", "interoception_treatment": treatment,
        "memory_treatment": "none", "action_selection": "legacy_reply", "report_channel": "normal",
        "semantic_homeostasis": False, "homeostatic_policy_ranking": False, "derived_emotion_visible": False,
        "observer_social_presence": "absent",
        "sensor_transform": {"transform_id": "test-v1", "seed": 17, "channel_alias": "Q7", "encoding": "ordinal_symbols",
                             "direction": "direct", "noise_profile": noise, "delay_profile": delay},
        "artifact_identity": {"neuralcloud_commit": "a" * 40, "model_id": "test", "prompt_digest": "b" * 64, "persona_digest": "c" * 64},
    })


def body(revision: int, *, value: float = 0.5, captured_at: datetime | None = None) -> BodyStateSnapshot:
    return BodyStateSnapshot.model_validate({
        "provider_id": "sim", "body_instance_id": "body-1", "revision": revision,
        "captured_at": captured_at or NOW + timedelta(seconds=revision),
        "energy": {"capacity_units": 100, "available_units": 100 * value, "available_ratio": value,
                   "delta_units_per_minute": 0},
        "evidence_refs": [{"provider_id": "sim", "ref": "evidence://sha256/" + "d" * 64}],
    })


def test_current_and_history_release_only_due_samples_and_bound_history(tmp_path: Path):
    for treatment, expected in (("opaque_current", [8]), ("opaque_history", [6, 7, 8])):
        with OpaqueInteroceptionStore(tmp_path / treatment, condition(treatment, "fixed:10"), history_limit=3) as store:
            for revision in range(1, 9):
                store.ingest(body(revision), episode_id="a")
            assert store.observations(now=NOW + timedelta(seconds=10)) == ()
            assert [o.sample_sequence for o in store.observations(now=NOW + timedelta(seconds=13))] == (
                [3] if treatment == "opaque_current" else [1, 2, 3]
            )
            assert [o.sample_sequence for o in store.observations(now=NOW + timedelta(seconds=18))] == expected


def test_restart_restores_mapping_sequence_and_idempotence(tmp_path: Path):
    path = tmp_path / "opaque.sqlite"
    config = condition("opaque_history", "fixed:2", "normal:0.05")
    with OpaqueInteroceptionStore(path, config) as store:
        first = store.ingest(body(1), episode_id="a")
        second = store.ingest(body(2, value=0.4), episode_id="a")
        records = store.records()
    with OpaqueInteroceptionStore(path, config) as restored:
        assert restored.ingest(body(1), episode_id="a") == first
        assert restored.ingest(body(2, value=0.4), episode_id="a") == second
        assert restored.records() == records
        assert restored.ingest(body(3, value=0.3), episode_id="a").observation.sample_sequence == 3
        assert len(restored.envelopes()) == 3


def test_episode_reset_does_not_mix_prior_history(tmp_path: Path):
    with OpaqueInteroceptionStore(tmp_path / "journal", condition("opaque_history", "fixed:2")) as store:
        first = store.ingest(body(1), episode_id="a")
        second = store.ingest(body(1, captured_at=NOW + timedelta(seconds=10)), episode_id="b")
        assert first.observation.transform_ref != second.observation.transform_ref
        assert store.observations(now=NOW + timedelta(seconds=11)) == ()
        assert store.observations(now=NOW + timedelta(seconds=12)) == (second.observation,)


def test_conflicting_and_stale_revision_cannot_allocate_new_sequence(tmp_path: Path):
    with OpaqueInteroceptionStore(tmp_path / "journal", condition()) as store:
        store.ingest(body(2))
        with pytest.raises(ValueError, match="conflicting"):
            store.ingest(body(2, value=0.7))
        with pytest.raises(ValueError, match="increase"):
            store.ingest(body(1))
        with pytest.raises(ValueError, match="backwards"):
            store.ingest(body(3, captured_at=NOW))
        assert store.ingest(body(3)).observation.sample_sequence == 2


def test_condition_drift_rejected_at_open(tmp_path: Path):
    path = tmp_path / "journal"
    OpaqueInteroceptionStore(path, condition()).close()
    with pytest.raises(ValueError, match="drift"):
        OpaqueInteroceptionStore(path, condition(delay="fixed:1"))
    with pytest.raises(ValueError, match="drift"):
        OpaqueInteroceptionStore(path, condition(), bin_count=5)


def test_wire_contract_episode_and_evidence_stay_private(tmp_path: Path):
    # This frozen research wire contract is independent of the extensible
    # production provider DTO; never construct it from the latter's dump.
    source = {"schema_version": "body-state-v1", "body_instance_id": "body-1", "episode_id": "wire-a",
        "revision": 1, "captured_at": (NOW + timedelta(seconds=1)).isoformat(),
        "energy": {"capacity_units": 100, "available_units": 50, "available_ratio": .5,
            "delta_units_per_minute": 0}, "evidence_refs": ["evidence://sha256/" + "e" * 64]}
    wire = WireBodyState.model_validate(source)
    with OpaqueInteroceptionStore(tmp_path / "journal", condition()) as store:
        envelope = store.ingest(wire)
        assert envelope.truth_ref.episode_id == "wire-a"
        assert envelope.truth_ref.evidence_refs == tuple(wire.evidence_refs)
        assert envelope.journal_payload()["visibility"] == "runtime_only"
        visible = store.observations(now=NOW + timedelta(seconds=2))[0].model_dump_json()
        for name in ("body_instance_id", "body_revision", "evidence", "truth_hash", "available_ratio", "terminal", "seed"):
            assert name not in visible
        with pytest.raises(ValueError, match="contradicts"):
            store.ingest(wire, episode_id="other")


def test_pre_extension_provider_journal_replays_without_rewriting_truth_hash(tmp_path):
    from harness.research.grounded_agency.hashes import content_hash

    path = tmp_path / "legacy.sqlite"
    legacy = body(1).model_dump(mode="json", include={
        "schema_version", "provider_id", "body_instance_id", "revision", "captured_at", "energy", "evidence_refs"})
    old_hash = content_hash(legacy)
    with OpaqueInteroceptionStore(path, condition()) as store:
        envelope = store.ingest(BodyStateSnapshot.model_validate(legacy), episode_id="a")
    # Simulate the exact journal emitted before the provider added production
    # fields: only its old source hash differs; transform/evidence stay intact.
    saved = envelope.model_dump(mode="json")
    saved["truth_ref"]["truth_hash"] = old_hash
    with sqlite3.connect(path) as db:
        db.execute("UPDATE opaque_samples SET envelope_json=?", (json.dumps(saved),))
    with OpaqueInteroceptionStore(path, condition()) as store:
        replay = store.ingest(BodyStateSnapshot.model_validate(legacy), episode_id="a")
        assert replay.truth_ref.truth_hash == old_hash and len(store.envelopes()) == 1
        with pytest.raises(ValueError, match="conflicting truth"):
            store.ingest(BodyStateSnapshot.model_validate({**legacy, "resting": True}), episode_id="a")
        with pytest.raises(ValueError, match="conflicting truth"):
            store.ingest(body(1, value=.4), episode_id="a")


def test_persisted_mapping_tampering_fails_replay(tmp_path: Path):
    path = tmp_path / "journal"
    with OpaqueInteroceptionStore(path, condition()) as store:
        store.ingest(body(1))
    with sqlite3.connect(path) as db:
        db.execute("UPDATE opaque_samples SET source_value = 0.0")
    with pytest.raises(ValueError, match="replay"):
        OpaqueInteroceptionStore(path, condition())


def test_two_connections_share_atomic_sequence_and_deduplication(tmp_path: Path):
    path = tmp_path / "journal"
    with OpaqueInteroceptionStore(path, condition()) as a, OpaqueInteroceptionStore(path, condition()) as b:
        first = a.ingest(body(1))
        assert b.ingest(body(1)) == first
        assert b.ingest(body(2)).observation.sample_sequence == 2
        assert a.ingest(body(3)).observation.sample_sequence == 3


def test_sensor_intervention_keeps_true_snapshot_hash_and_replays(tmp_path: Path):
    from harness.research.grounded_agency.hashes import content_hash

    path = tmp_path / "intervention.sqlite"
    truth = body(1, value=0.1)
    with OpaqueInteroceptionStore(path, condition()) as store:
        changed = store.ingest(truth, sensor_value=0.8)
        assert changed.truth_ref.truth_hash == content_hash(truth)
        with pytest.raises(ValueError, match="conflicting"):
            store.ingest(truth, sensor_value=0.1)
    with OpaqueInteroceptionStore(path, condition()) as restored:
        assert restored.ingest(truth, sensor_value=0.8) == changed
    with OpaqueInteroceptionStore(tmp_path / "control.sqlite", condition()) as control:
        actual = control.ingest(truth)
        assert actual.truth_ref.truth_hash == changed.truth_ref.truth_hash
        assert actual.observation.reading != changed.observation.reading
