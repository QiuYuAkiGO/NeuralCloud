from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest

from harness.contracts.envelope import DomainCommandEnvelope, DomainCommandResult
from harness.homeostasis import DIMENSIONS, HomeostasisEngine, ProfileRevisionStore, load_profile
from harness.homeostasis.ledger import (
    CommandLedger,
    CommandLedgerConflictError,
    LegacyMigrationError,
    StaleSnapshotError,
)
from harness.homeostasis.models import (
    HomeostaticProfile,
    HomeostaticTransitionTrace,
    initial_snapshot,
)
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
        deadline=at + timedelta(days=3650),
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


def _appraisal_command(
    *,
    sequence: int,
    expected_revision: int,
    impacts: dict[str, float],
    at: datetime = NOW,
) -> DomainCommandEnvelope[dict]:
    command = _command(sequence=sequence, expected_revision=expected_revision, at=at)
    return command.model_copy(
        update={
            "payload": {
                "event": {
                    "event_type": "perception",
                    "source": "perception",
                    "source_time": at,
                    "payload": {
                        "appraisal": {
                            "source": "runtime",
                            "precision": 1.0,
                            "suggested_impacts": impacts,
                        }
                    },
                }
            }
        }
    )


def _heartbeat_command(
    *,
    sequence: int,
    expected_revision: int,
    at: datetime,
) -> DomainCommandEnvelope[dict]:
    command = _command(sequence=sequence, expected_revision=expected_revision, at=at)
    return command.model_copy(
        update={
            "command_type": "ApplyHeartbeat",
            "idempotency_key": f"homeostasis:heartbeat:{sequence}",
            "payload": {
                "event": {
                    "event_type": "heartbeat",
                    "source": "scheduler",
                    "source_time": at,
                    "payload": {},
                }
            },
        }
    )


def test_profile_and_initial_snapshot_are_complete(tmp_path) -> None:
    profile = _profile()
    engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
    assert tuple(engine.snapshot.dimensions) == DIMENSIONS
    assert engine.snapshot.profile_revision == profile.revision
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


def test_coupling_uses_realised_source_delta_at_hard_bound(tmp_path) -> None:
    async def scenario() -> None:
        profile = _profile()
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        load = engine.snapshot.dimensions["cognitive_load"]
        load = load.model_copy(
            update={
                "value": 0.94,
                "velocity": 0.0,
                "posterior": engine._posterior(0.94, load.target_band),
            }
        )
        engine.snapshot = engine.snapshot.model_copy(
            update={"dimensions": {**engine.snapshot.dimensions, "cognitive_load": load}}
        )

        first = await engine.apply_event(
            _appraisal_command(
                sequence=1,
                expected_revision=0,
                impacts={"cognitive_load": 0.10},
            )
        )
        first_trace = first.result["trace"]
        assert first_trace["applied_impacts"]["cognitive_load"] == pytest.approx(0.01)
        assert first_trace["coupling_impacts"]["energy_reserve"] == pytest.approx(-0.0012)
        assert first_trace["coupling_impacts"]["safety_predictability"] == pytest.approx(-0.0008)

        energy = engine.snapshot.dimensions["energy_reserve"].value
        safety = engine.snapshot.dimensions["safety_predictability"].value
        second = await engine.apply_event(
            _appraisal_command(
                sequence=2,
                expected_revision=1,
                impacts={"cognitive_load": 0.10},
            )
        )
        second_trace = second.result["trace"]
        assert second_trace["applied_impacts"]["cognitive_load"] == pytest.approx(0.0)
        assert second_trace["coupling_impacts"]["energy_reserve"] == pytest.approx(0.0)
        assert second_trace["coupling_impacts"]["safety_predictability"] == pytest.approx(0.0)
        assert engine.snapshot.dimensions["energy_reserve"].value == pytest.approx(energy)
        assert engine.snapshot.dimensions["safety_predictability"].value == pytest.approx(safety)

    asyncio.run(scenario())


def test_legacy_profile_keeps_requested_delta_coupling_semantics(tmp_path) -> None:
    async def scenario() -> None:
        payload = _profile().model_dump(mode="json")
        payload.pop("coupling_semantics")
        payload["coupling_matrix_version"] = "coupling-v1"
        profile = HomeostaticProfile.model_validate(payload)
        assert profile.coupling_semantics == "requested_delta_v1"
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        load = engine.snapshot.dimensions["cognitive_load"]
        upper = profile.dimensions["cognitive_load"].hard_bounds[1]
        load = load.model_copy(
            update={
                "value": upper,
                "velocity": 0.0,
                "posterior": engine._posterior(upper, load.target_band),
            }
        )
        engine.snapshot = engine.snapshot.model_copy(
            update={"dimensions": {**engine.snapshot.dimensions, "cognitive_load": load}}
        )

        result = await engine.apply_event(
            _appraisal_command(
                sequence=1,
                expected_revision=0,
                impacts={"cognitive_load": 0.10},
            )
        )
        trace = result.result["trace"]
        assert trace["coupling_impacts"]["energy_reserve"] == pytest.approx(-0.012)
        assert trace["coupling_impacts"]["safety_predictability"] == pytest.approx(-0.008)

    asyncio.run(scenario())


@pytest.mark.parametrize(("success", "direction"), [(True, 1), (False, -1)])
def test_nested_verified_outcome_controls_appraisal(
    tmp_path,
    success: bool,
    direction: int,
) -> None:
    async def scenario() -> None:
        engine = HomeostasisEngine(_profile(), state_dir=tmp_path, now=NOW)
        before_competence = engine.snapshot.dimensions["competence"].value
        before_safety = engine.snapshot.dimensions["safety_predictability"].value
        command = _command().model_copy(
            update={
                "command_type": "ApplyOutcome",
                "idempotency_key": "homeostasis:outcome:1",
                "payload": {
                    "action": {
                        "state": "SUCCEEDED" if success else "FAILED",
                        "outcome": {"success": success},
                    },
                    "evidence": {"success": success},
                },
            }
        )
        result = await engine.apply_outcome(command)
        trace = result.result["trace"]
        assert trace["source"] == "verified_outcome"
        assert (engine.snapshot.dimensions["competence"].value - before_competence) * direction > 0
        assert (engine.snapshot.dimensions["safety_predictability"].value - before_safety) * direction > 0

    asyncio.run(scenario())


def test_sustained_hard_bound_trips_watchdog_after_configured_heartbeats(tmp_path) -> None:
    async def scenario() -> None:
        payload = _profile().model_dump(mode="json")
        payload["watchdog_boundary_heartbeats"] = 2
        payload["dimensions"]["cognitive_load"]["drift_per_hour"] = 0.0
        profile = HomeostaticProfile.model_validate(payload)
        engine = HomeostasisEngine(profile, state_dir=tmp_path, now=NOW)
        load = engine.snapshot.dimensions["cognitive_load"]
        upper = profile.dimensions["cognitive_load"].hard_bounds[1]
        load = load.model_copy(
            update={
                "value": upper,
                "velocity": 0.0,
                "posterior": engine._posterior(upper, load.target_band),
            }
        )
        engine.snapshot = engine.snapshot.model_copy(
            update={"dimensions": {**engine.snapshot.dimensions, "cognitive_load": load}}
        )

        await engine.apply_heartbeat(
            _heartbeat_command(
                sequence=1,
                expected_revision=0,
                at=NOW + timedelta(minutes=1),
            )
        )
        assert "cognitive_load:hard_bound_sustained" not in engine.snapshot.watchdog
        engine = HomeostasisEngine(
            profile,
            state_dir=tmp_path,
            now=NOW + timedelta(minutes=1, seconds=1),
        )
        assert engine._boundary_heartbeat_streaks["cognitive_load"] == 1
        await engine.apply_heartbeat(
            _heartbeat_command(
                sequence=2,
                expected_revision=1,
                at=NOW + timedelta(minutes=2),
            )
        )
        assert "cognitive_load:hard_bound_sustained" in engine.snapshot.watchdog
        assert (await engine.health()).healthy is False

        await engine.apply_event(
            _appraisal_command(
                sequence=3,
                expected_revision=2,
                impacts={"cognitive_load": -0.10},
                at=NOW + timedelta(minutes=2, seconds=1),
            )
        )
        assert engine._boundary_heartbeat_streaks["cognitive_load"] == 0
        assert "cognitive_load:hard_bound_sustained" not in engine.snapshot.watchdog
        assert (await engine.health()).healthy is True

    asyncio.run(scenario())


def test_sqlite_ledger_bounds_cache_and_replays_evicted_command(tmp_path) -> None:
    async def scenario() -> None:
        profile = _profile()
        engine = HomeostasisEngine(
            profile,
            state_dir=tmp_path,
            now=NOW,
            ledger_cache_size=2,
        )
        for sequence in range(1, 4):
            await engine.apply_event(
                _appraisal_command(
                    sequence=sequence,
                    expected_revision=sequence - 1,
                    impacts={"social_connection": 0.01},
                    at=NOW + timedelta(seconds=sequence),
                )
            )
        assert engine._command_ledger.cached_entries == 2
        assert engine.ledger_store_path.is_file()
        await engine.close()
        stale = engine.snapshot.model_copy(update={"revision": 0})
        engine.state_path.write_text(stale.model_dump_json(indent=2), encoding="utf-8")

        restored = HomeostasisEngine(
            profile,
            state_dir=tmp_path,
            now=NOW + timedelta(hours=1),
            ledger_cache_size=2,
        )
        assert restored.snapshot.revision == 3
        replay = await restored.apply_event(
            _appraisal_command(
                sequence=1,
                expected_revision=0,
                impacts={"social_connection": 0.01},
                at=NOW + timedelta(seconds=1),
            )
        )
        assert replay.status == "idempotent_replay"
        assert restored.snapshot.revision == 3
        assert restored._command_ledger.cached_entries == 2

    asyncio.run(scenario())


def test_legacy_json_ledger_is_imported_without_rewriting_source(tmp_path) -> None:
    legacy = tmp_path / "command-ledger.json"
    snapshot = initial_snapshot(_profile(), now=NOW).model_copy(update={"revision": 1})
    (tmp_path / "snapshot.json").write_text(
        snapshot.model_dump_json(indent=2),
        encoding="utf-8",
    )
    trace = HomeostaticTransitionTrace(
        revision=1,
        command_type="ApplyEvent",
        event_id="evt-legacy",
        receive_seq=1,
        occurred_at=NOW,
        source="legacy-test",
        precision=1.0,
        suggested_impacts={},
        applied_impacts={},
        coupling_impacts={},
    ).model_dump(mode="json")
    result = DomainCommandResult[dict](
        command_id="cmd-legacy",
        command_type="ApplyEvent",
        status="applied",
        request_hash="a" * 64,
        committed_revision=1,
        acknowledged_event_id="evt-legacy",
        acknowledged_receive_seq=1,
        result={"revision": 1, "trace": trace},
        completed_at=NOW,
    ).model_dump(mode="json")
    legacy_payload = {
        "ApplyEvent\0legacy-key": {
            "request_hash": "a" * 64,
            "result": result,
        }
    }
    original = json.dumps(legacy_payload, ensure_ascii=False, indent=2) + "\n"
    legacy.write_text(original, encoding="utf-8")

    ledger = CommandLedger(tmp_path, cache_size=1)

    assert ledger.get("ApplyEvent\0legacy-key") == legacy_payload["ApplyEvent\0legacy-key"]
    assert ledger.load_trace(1) == trace
    assert legacy.read_text(encoding="utf-8") == original
    assert (tmp_path / "command-ledger.sqlite3").is_file()


def test_sqlite_commit_failure_does_not_advance_memory_or_idempotency(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine = HomeostasisEngine(_profile(), state_dir=tmp_path, now=NOW)
        original = CommandLedger._write_snapshot
        attempts = 0

        def fail_once(*args, **kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise sqlite3.OperationalError("injected snapshot failure")
            return original(*args, **kwargs)

        monkeypatch.setattr(CommandLedger, "_write_snapshot", staticmethod(fail_once))
        command = _command()
        with pytest.raises(sqlite3.OperationalError, match="injected snapshot failure"):
            await engine.apply_event(command)

        assert engine.snapshot.revision == 0
        assert engine._command_ledger.get(
            f"{command.command_type}\0{command.idempotency_key}"
        ) is None

        result = await engine.apply_event(command)
        assert result.status == "applied"
        assert engine.snapshot.revision == 1

    asyncio.run(scenario())


def test_sqlite_ledger_rejects_key_overwrite_and_snapshot_rollback(tmp_path) -> None:
    ledger = CommandLedger(tmp_path)
    first_snapshot = initial_snapshot(_profile(), now=NOW).model_copy(
        update={"revision": 1}
    )
    first_trace = HomeostaticTransitionTrace(
        revision=1,
        command_type="ApplyEvent",
        event_id="evt-first",
        receive_seq=1,
        occurred_at=NOW,
        source="test",
        precision=1.0,
        suggested_impacts={},
        applied_impacts={},
        coupling_impacts={},
    ).model_dump(mode="json")
    first_result = DomainCommandResult[dict](
        command_id="cmd-first",
        command_type="ApplyEvent",
        status="applied",
        request_hash="a" * 64,
        committed_revision=1,
        acknowledged_event_id="evt-first",
        acknowledged_receive_seq=1,
        result={"revision": 1, "trace": first_trace},
        completed_at=NOW,
    )
    ledger.commit(
        "ApplyEvent\0immutable-key",
        request_hash="a" * 64,
        result=first_result.model_dump(mode="json"),
        snapshot=first_snapshot.model_dump(mode="json"),
        engine_state={},
        trace=first_trace,
    )

    second_snapshot = first_snapshot.model_copy(update={"revision": 2})
    second_trace = {**first_trace, "revision": 2, "event_id": "evt-second"}
    second_result = first_result.model_copy(
        update={
            "command_id": "cmd-second",
            "request_hash": "b" * 64,
            "committed_revision": 2,
            "result": {"revision": 2, "trace": second_trace},
        }
    )
    with pytest.raises(CommandLedgerConflictError, match="idempotency key"):
        ledger.commit(
            "ApplyEvent\0immutable-key",
            request_hash="b" * 64,
            result=second_result.model_dump(mode="json"),
            snapshot=second_snapshot.model_dump(mode="json"),
            engine_state={},
            trace=second_trace,
        )
    assert ledger.load_snapshot()["revision"] == 1

    stale = first_snapshot.model_copy(update={"revision": 0})
    with pytest.raises(StaleSnapshotError, match="stale revision"):
        ledger.save_snapshot(stale.model_dump(mode="json"), engine_state={})
    assert ledger.load_snapshot()["revision"] == 1


def test_corrupt_legacy_migration_is_not_marked_complete(tmp_path) -> None:
    legacy = tmp_path / "command-ledger.json"
    legacy.write_text("{not-json", encoding="utf-8")

    with pytest.raises(LegacyMigrationError, match="cannot read legacy"):
        CommandLedger(tmp_path)

    with sqlite3.connect(tmp_path / "command-ledger.sqlite3") as connection:
        migrated = connection.execute(
            "SELECT 1 FROM metadata WHERE key = ?",
            (CommandLedger._MIGRATION_KEY,),
        ).fetchone()
    assert migrated is None

    legacy.write_text("{}", encoding="utf-8")
    CommandLedger(tmp_path)
    with sqlite3.connect(tmp_path / "command-ledger.sqlite3") as connection:
        migrated = connection.execute(
            "SELECT 1 FROM metadata WHERE key = ?",
            (CommandLedger._MIGRATION_KEY,),
        ).fetchone()
    assert migrated == (1,)


def test_legacy_result_cannot_advance_beyond_authoritative_snapshot(tmp_path) -> None:
    snapshot = initial_snapshot(_profile(), now=NOW).model_copy(update={"revision": 1})
    (tmp_path / "snapshot.json").write_text(
        snapshot.model_dump_json(indent=2),
        encoding="utf-8",
    )
    result = DomainCommandResult[dict](
        command_id="cmd-ahead",
        command_type="ApplyEvent",
        status="applied",
        request_hash="a" * 64,
        committed_revision=2,
        acknowledged_event_id="evt-ahead",
        acknowledged_receive_seq=2,
        result={"revision": 2},
        completed_at=NOW,
    )
    (tmp_path / "command-ledger.json").write_text(
        json.dumps(
            {
                "ApplyEvent\0ahead": {
                    "request_hash": "a" * 64,
                    "result": result.model_dump(mode="json"),
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(LegacyMigrationError, match="ahead of the authoritative"):
        CommandLedger(tmp_path)


def test_trace_remains_durable_when_jsonl_projection_fails(
    tmp_path,
    monkeypatch,
) -> None:
    async def scenario() -> None:
        engine = HomeostasisEngine(_profile(), state_dir=tmp_path, now=NOW)
        engine.trace_path.touch()
        original_projection = engine._append_trace

        def fail_projection(*_args, **_kwargs):
            raise OSError("injected trace projection failure")

        monkeypatch.setattr(engine, "_append_trace", fail_projection)
        result = await engine.apply_event(_command())

        assert result.status == "applied"
        assert result.result is not None
        assert engine._command_ledger.load_trace(1) == result.result["trace"]
        health = await engine.health()
        assert health.healthy is False
        assert "injected trace projection failure" in health.detail["projection_errors"][
            "trace"
        ]

        monkeypatch.setattr(engine, "_append_trace", original_projection)
        second = await engine.apply_event(
            _command(
                sequence=2,
                expected_revision=1,
                at=NOW + timedelta(seconds=1),
            )
        )
        assert second.status == "applied"
        traces = [
            json.loads(line)
            for line in engine.trace_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [trace["revision"] for trace in traces] == [1, 2]
        assert (await engine.health()).healthy is True

        engine.trace_path.unlink()
        restored = HomeostasisEngine(
            _profile(),
            state_dir=tmp_path,
            now=NOW + timedelta(seconds=2),
        )
        repaired = [
            json.loads(line)
            for line in restored.trace_path.read_text(encoding="utf-8").splitlines()
        ]
        assert [trace["revision"] for trace in repaired] == [1, 2]
        assert (await restored.health()).healthy is True

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
        base_revision=profile.revision,
    )
    assert staged["restart_required"] is True
    assert store.pending() == staged
    saved = load_profile(staged["profile_path"])
    assert saved.revision == profile.revision + 1
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
        candidate["revision"] = profile.revision + 1
        candidate["dimensions"]["social_connection"]["baseline_band"] = [0.58, 0.76]
        revised = HomeostaticProfile.model_validate(candidate)
        restored = HomeostasisEngine(revised, state_dir=tmp_path, now=NOW + timedelta(days=3))
        assert restored.snapshot.profile_revision == revised.revision
        assert restored.snapshot.dimensions["social_connection"].target_band == (0.58, 0.76)
        assert restored.snapshot.dimensions["social_connection"].value == pytest.approx(
            engine.snapshot.dimensions["social_connection"].value
        )
        await restored.close()
        reloaded = HomeostasisEngine(
            revised,
            state_dir=tmp_path,
            now=NOW + timedelta(days=3, seconds=1),
        )
        assert reloaded.snapshot == restored.snapshot

    asyncio.run(scenario())
