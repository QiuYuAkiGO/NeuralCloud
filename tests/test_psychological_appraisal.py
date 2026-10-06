"""Psychological suggestions cannot certify evidence or manufacture rewards."""

from __future__ import annotations

import asyncio
import json
import sqlite3
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from harness.contracts.envelope import DomainCommandEnvelope
from harness.homeostasis import HomeostasisEngine, load_profile
from harness.homeostasis.appraisal import PsychologicalAppraisal
from harness.homeostasis.models import HomeostaticProfile

NOW = datetime(2026, 9, 12, 12, tzinfo=UTC)


@pytest.mark.parametrize("case", ["heartbeat", "event", "too_old", "future", "missing_time", "no_opt_in", "missing_trace"])
def test_short_appraisal_can_rebase_only_over_proven_heartbeats(tmp_path, case):
    from tests.test_homeostasis_engine import _heartbeat_command, _command

    async def scenario():
        later = NOW + timedelta(seconds=10)
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW, clock=lambda: later)
        try:
            advance = _heartbeat_command(sequence=1, expected_revision=0, at=later)
            if case == "event":
                advance = _command(sequence=1, expected_revision=0, at=later)
            applied = await (engine.apply_event(advance) if case == "event" else engine.apply_heartbeat(advance))
            assert applied.status == "applied"
            if case == "missing_trace":
                with sqlite3.connect(engine._command_ledger.path) as db:
                    db.execute("DELETE FROM transition_traces WHERE revision=1")
            payload = {"allow_heartbeat_rebase": case != "no_opt_in", "basis_snapshot_at": NOW}
            if case == "too_old":
                payload["basis_snapshot_at"] = later - timedelta(seconds=61)
            elif case == "future":
                payload["basis_snapshot_at"] = later + timedelta(seconds=1)
            elif case == "missing_time":
                payload.pop("basis_snapshot_at")
            before = engine.snapshot.model_copy(deep=True)
            request = command(at=later, payload_updates=payload)
            result = await engine.apply_appraisal(request)
            if case == "heartbeat":
                assert result.status == "applied"
                assert result.result["appraisal"]["heartbeat_rebased"] is True
                assert result.result["appraisal"]["basis_snapshot_revision"] == 0
                assert result.result["appraisal"]["applied_to_revision"] == 1
                assert engine.snapshot.dimensions["energy_reserve"] == before.dimensions["energy_reserve"]
                revision = engine.snapshot.revision
                assert (await engine.apply_appraisal(request)).status == "idempotent_replay"
                assert engine.snapshot.revision == revision
            else:
                assert result.status == "conflict" and engine.snapshot == before
        finally:
            await engine.close()
    asyncio.run(scenario())


def appraisal(**updates):
    payload = {
        "episode_id": "regulation-one",
        "appraisal_id": "appraisal-one",
        "basis_snapshot_revision": 0,
        "observed_event_refs": ["evidence://board/receipt-one", "event://user/one"],
        "appraisal_kind": "self_report",
        "suggested_impacts": {"cognitive_load": -0.1},
        "model_confidence": 0.8,
        "created_at": NOW,
        "expires_at": NOW + timedelta(minutes=5),
    }
    payload.update(updates)
    return PsychologicalAppraisal.model_validate(payload)


def command(value=None, *, sequence=1, at=NOW, payload_updates=None):
    value = value or appraisal()
    payload = {
        "episode_id": value.episode_id,
        "appraisal": value.model_dump(mode="json"),
        "verified_evidence_refs": list(value.observed_event_refs),
    }
    payload.update(payload_updates or {})
    return DomainCommandEnvelope[dict](
        command_type="ApplyAppraisal",
        event_id=f"appraisal-event-{sequence}",
        receive_seq=sequence,
        correlation_id="goal-correlation",
        runtime_instance_id="runtime-test",
        manifest_revision=1,
        idempotency_key=f"appraisal-command-{sequence}",
        issued_at=at,
        deadline=at + timedelta(minutes=1),
        expected_revision=value.basis_snapshot_revision,
        payload=payload,
    )


def profile(*, precision=0.35):
    payload = load_profile("presets/homeostasis/functional-proxy-v1.json").model_dump(mode="json")
    payload["source_precision"]["llm_appraisal"] = precision
    return HomeostaticProfile.model_validate(payload)


@pytest.mark.parametrize("updates", [
    {"suggested_impacts": {"energy_reserve": 0.1}},
    {"suggested_impacts": {"energy": 0.1}},
    {"suggested_impacts": {"competence": float("nan")}},
    {"suggested_impacts": {"competence": float("inf")}},
    {"suggested_impacts": {"competence": True}},
    {"suggested_impacts": {"competence": "0.1"}},
    {"suggested_impacts": {"competence": 1.01}},
    {"suggested_impacts": {}},
    {"source": "system_sensor"},
    {"precision": 1.0},
    {"verified_evidence_refs": ["invented"]},
    {"model_confidence": float("nan")},
    {"model_confidence": True},
    {"model_confidence": "1.0"},
    {"basis_snapshot_revision": True},
    {"episode_id": " "},
    {"observed_event_refs": []},
    {"observed_event_refs": ["one", "one"]},
    {"observed_event_refs": [" "]},
    {"created_at": NOW.replace(tzinfo=None)},
    {"expires_at": NOW},
])
def test_model_contract_rejects_non_psychological_or_self_certifying_input(updates):
    with pytest.raises(ValidationError):
        appraisal(**updates)


@pytest.mark.parametrize("kind", ["self_report", "event_interpretation"])
@pytest.mark.parametrize("configured_precision", [0.2, 0.95])
def test_appraisal_is_bounded_at_server_precision_and_preserves_energy(tmp_path, kind, configured_precision):
    async def scenario():
        engine = HomeostasisEngine(profile(precision=configured_precision), state_dir=tmp_path, now=NOW)
        before = engine.snapshot.model_copy(deep=True)
        value = appraisal(appraisal_kind=kind, suggested_impacts={"cognitive_load": -1.0})
        result = await engine.apply_appraisal(command(value))
        assert result.status == "applied"
        precision = min(configured_precision, 0.35) * value.model_confidence
        trace = result.result["trace"]
        assert trace["source"] == f"psychological:{kind}"
        assert trace["precision"] == pytest.approx(precision)
        assert trace["episode_id"] == value.episode_id
        assert trace["evidence_refs"] == value.observed_event_refs
        assert trace["values_before"]["cognitive_load"] == before.dimensions["cognitive_load"].value
        delta = -engine.profile.dimensions["cognitive_load"].max_event_delta * precision
        assert trace["applied_impacts"]["cognitive_load"] == pytest.approx(delta)
        assert trace["values_after"]["cognitive_load"] == pytest.approx(before.dimensions["cognitive_load"].value + delta)
        assert engine.snapshot.dimensions["energy_reserve"] == before.dimensions["energy_reserve"]
        assert trace["applied_impacts"]["energy_reserve"] == 0
        assert trace["coupling_impacts"]["energy_reserve"] == 0
        assert result.result["appraisal"]["effective_precision"] == pytest.approx(precision)
        assert result.result["interoceptive_summary"]["snapshot_revision"] == 1
        # Original references survive both the trace and factor projection.
        assert engine.snapshot.dimensions["cognitive_load"].dominant_evidence[:2] == value.observed_event_refs
        await engine.close()
        traces = [json.loads(line) for line in (tmp_path / "transitions.jsonl").read_text().splitlines()]
        assert traces[-1]["source"] == f"psychological:{kind}"
        assert traces[-1]["evidence_refs"] == value.observed_event_refs

    asyncio.run(scenario())


@pytest.mark.parametrize("confidence", [0.0, 1.0])
def test_zero_suggestion_or_confidence_adds_no_outcome_bonus_or_heartbeat_drift(tmp_path, confidence):
    async def scenario():
        later = NOW + timedelta(minutes=3)
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW, clock=lambda: later)
        before = {name: factor.value for name, factor in engine.snapshot.dimensions.items()}
        previous_heartbeat = engine.snapshot.last_heartbeat_at
        value = appraisal(
            suggested_impacts={"competence": 0.0 if confidence else 0.5},
            model_confidence=confidence,
            created_at=later,
            expires_at=later + timedelta(minutes=1),
        )
        result = await engine.apply_appraisal(command(value, at=later))
        assert result.status == "applied"
        assert {name: factor.value for name, factor in engine.snapshot.dimensions.items()} == before
        assert all(delta == 0 for delta in result.result["trace"]["applied_impacts"].values())
        assert engine.snapshot.last_heartbeat_at == previous_heartbeat

    asyncio.run(scenario())


@pytest.mark.parametrize("case", ["unverified", "episode", "expired", "future", "basis", "extra", "energy"])
def test_invalid_binding_time_or_revision_cannot_change_state_or_consume_evidence(tmp_path, case):
    async def scenario():
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        value = appraisal()
        payload_updates = {}
        if case == "unverified":
            payload_updates["verified_evidence_refs"] = [value.observed_event_refs[0]]
        elif case == "episode":
            payload_updates["episode_id"] = "different-episode"
        elif case == "expired":
            value = appraisal(created_at=NOW - timedelta(minutes=1), expires_at=NOW)
        elif case == "future":
            value = appraisal(created_at=NOW + timedelta(seconds=1))
        elif case == "basis":
            value = appraisal(basis_snapshot_revision=1)
        elif case == "extra":
            payload_updates["event"] = {"source": "user", "success": True, "text": "谢谢你的支持"}
        elif case == "energy":
            payload_updates["appraisal"] = {**value.model_dump(mode="json"), "suggested_impacts": {"energy_reserve": 0.9}}
        before = engine.snapshot.model_copy(deep=True)
        request = command(value, payload_updates=payload_updates)
        if case == "basis":
            # Exercise the appraisal's own revision binding independently of
            # the envelope's optional compare-and-set field.
            request = request.model_copy(update={"expected_revision": 0})
        result = await engine.apply_appraisal(request)
        assert result.status in {"rejected", "conflict"}
        assert engine.snapshot == before
        assert not engine._command_ledger.appraisal_is_committed(
            episode_id=value.episode_id, appraisal_id=value.appraisal_id, evidence_set_hash=value.evidence_set_hash
        )
        # A rejected suggestion never poisons its delivery key or evidence claim.
        valid = await engine.apply_appraisal(command())
        assert valid.status == "applied"

    asyncio.run(scenario())


def test_same_command_replays_after_expiry_and_restart_without_second_gain(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        original = command()
        applied = await engine.apply_appraisal(original)
        before = engine.snapshot.model_copy(deep=True)
        replay = await engine.apply_appraisal(original)
        assert replay.status == "idempotent_replay"
        assert replay.result == applied.result
        assert engine.snapshot == before
        await engine.close()
        restored = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW + timedelta(hours=1))
        replay = await restored.apply_appraisal(original)
        assert replay.status == "idempotent_replay"
        assert restored.snapshot.revision == 1
        assert replay.result["appraisal"] == applied.result["appraisal"]

    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["id_and_kind", "reference_order", "same_id_new_evidence", "subset", "append_ref"])
def test_episode_semantic_dedup_survives_new_ids_kinds_and_restart(tmp_path, change):
    async def scenario():
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        value = appraisal()
        await engine.apply_appraisal(command(value))
        await engine.close()
        restored = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        updates = {"basis_snapshot_revision": 1, "appraisal_id": "second-appraisal"}
        if change == "id_and_kind":
            updates["appraisal_kind"] = "event_interpretation"
        elif change == "reference_order":
            updates["observed_event_refs"] = list(reversed(value.observed_event_refs))
        elif change == "subset":
            updates["observed_event_refs"] = value.observed_event_refs[:1]
        elif change == "append_ref":
            updates["observed_event_refs"] = [*value.observed_event_refs, "event://new-ref-added-to-old-facts"]
        else:
            updates["appraisal_id"] = value.appraisal_id
            updates["observed_event_refs"] = ["evidence://new-actual-event"]
        before = restored.snapshot.model_copy(deep=True)
        duplicate = await restored.apply_appraisal(command(appraisal(**updates), sequence=2))
        assert duplicate.status == "conflict"
        assert restored.snapshot == before
        assert len(list(restored._command_ledger.iter_traces())) == 1

    asyncio.run(scenario())


def test_distinct_verified_event_set_in_same_episode_can_be_appraised(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        await engine.apply_appraisal(command())
        second = appraisal(
            appraisal_id="appraisal-two", basis_snapshot_revision=1,
            observed_event_refs=["event://user/two"],
            suggested_impacts={"social_connection": -0.1},
            appraisal_kind="event_interpretation",
        )
        result = await engine.apply_appraisal(command(second, sequence=2))
        assert result.status == "applied"
        assert result.committed_revision == 2
        assert result.result["trace"]["applied_impacts"]["social_connection"] < 0

    asyncio.run(scenario())


def test_two_stale_engines_cannot_commit_different_appraisals_at_one_revision(tmp_path):
    async def scenario():
        first = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        stale = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        applied = await first.apply_appraisal(command())
        assert applied.status == "applied"
        new_evidence = appraisal(
            appraisal_id="second-appraisal", observed_event_refs=["event://separate-event"]
        )
        conflict = await stale.apply_appraisal(command(new_evidence, sequence=2))
        assert conflict.status == "conflict"
        assert stale.snapshot.revision == 0
        assert stale._command_ledger.get("ApplyAppraisal\0appraisal-command-2") is None
        recovered = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        assert recovered.snapshot.revision == 1
        assert len(list(recovered._command_ledger.iter_traces())) == 1
        assert not recovered._command_ledger.appraisal_is_committed(
            episode_id=new_evidence.episode_id, appraisal_id=new_evidence.appraisal_id,
            evidence_set_hash=new_evidence.evidence_set_hash,
        )

    asyncio.run(scenario())


@pytest.mark.parametrize("collision", ["set", "reference"])
def test_semantic_claim_and_state_commit_are_atomic_on_a_database_conflict(tmp_path, monkeypatch, collision):
    async def scenario():
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        original_commit = engine._command_ledger.commit
        value = appraisal()

        def competing_claim(*args, **kwargs):
            # Another writer claims the same semantic evidence after the
            # engine's read, before its transaction. The unique index must
            # prevent state/trace/idempotency from committing separately.
            connection = sqlite3.connect(engine.ledger_store_path)
            with connection:
                if collision == "set":
                    connection.execute(
                        "INSERT INTO psychological_appraisals VALUES (?, ?, ?, ?)",
                        (value.episode_id, "competing-appraisal", value.evidence_set_hash, 1),
                    )
                else:
                    connection.execute(
                        "INSERT INTO psychological_appraisal_evidence VALUES (?, ?, ?, ?)",
                        (value.episode_id, value.observed_event_refs[0], "competing-appraisal", 1),
                    )
            connection.close()
            original_commit(*args, **kwargs)

        monkeypatch.setattr(engine._command_ledger, "commit", competing_claim)
        before = engine.snapshot.model_copy(deep=True)
        result = await engine.apply_appraisal(command(value))
        assert result.status == "conflict"
        assert engine.snapshot == before
        assert engine._command_ledger.get("ApplyAppraisal\0appraisal-command-1") is None
        assert list(engine._command_ledger.iter_traces()) == []
        stored = engine._command_ledger.load_snapshot()
        assert stored is None or stored["revision"] == 0

    asyncio.run(scenario())


def test_earlier_exact_set_ledger_backfills_single_reference_claims(tmp_path):
    async def scenario():
        engine = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        value = appraisal()
        await engine.apply_appraisal(command(value))
        await engine.close()
        # Reproduce the earlier schema: its exact-set rows and traces exist,
        # but the new per-reference claim table has not been installed yet.
        connection = sqlite3.connect(engine.ledger_store_path)
        with connection:
            connection.execute("DROP TABLE psychological_appraisal_evidence")
        connection.close()
        recovered = HomeostasisEngine(profile(), state_dir=tmp_path, now=NOW)
        attempted = appraisal(
            appraisal_id="post-upgrade", basis_snapshot_revision=1,
            observed_event_refs=[value.observed_event_refs[0], "event://new-fact"],
        )
        result = await recovered.apply_appraisal(command(attempted, sequence=2))
        assert result.status == "conflict"
        assert recovered.snapshot.revision == 1

    asyncio.run(scenario())
