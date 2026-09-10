"""Command replay uses sequence metadata, not scans or retained payload copies."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from harness.contracts.decisions import PreparedResponse, ReplyDecision
from harness.contracts.envelope import DomainCommandEnvelope, VersionVector
from harness.research.manifest import load_experiment_manifest
from harness.runtime.journal import RuntimeJournal
from harness.runtime.models import RuntimeEvent, RuntimeEventPriority, RuntimeEventType
from harness.runtime.supervisor import RuntimeSupervisor


START = datetime(2026, 9, 9, tzinfo=UTC)


def _journal(path):
    return RuntimeJournal(
        path, runtime_instance_id="old-runtime", character_id="misty", manifest_revision=1,
        fsync=False, recent_record_limit=8, max_segment_bytes=4096,
    )


def _runtime(tmp_path, journal):
    manifest = load_experiment_manifest(
        Path(__file__).resolve().parents[1] / "presets/runtime/development-demo.json"
    )
    # These tests exercise scheduling metadata only; no domain calls occur.
    return RuntimeSupervisor(
        manifest=manifest, cognitive=object(), homeostasis=object(),
        state_dir=tmp_path / "state", journal=journal, runtime_instance_id="new-runtime",
    )


def _event(sequence=1):
    return RuntimeEvent(
        event_id=f"event-{sequence}", receive_seq=sequence,
        correlation_id=f"correlation-{sequence}", event_type=RuntimeEventType.USER_MESSAGE,
        priority=RuntimeEventPriority.USER, source="user", source_time=START, received_at=START,
        idempotency_key=f"user:{sequence}", manifest_revision=1, payload={"text": "你好"},
    )


def _envelope(event, *, command_type="PrepareResponse", key=None, command_id="command-old"):
    return DomainCommandEnvelope[dict](
        command_id=command_id, command_type=command_type, event_id=event.event_id,
        receive_seq=event.receive_seq, correlation_id=event.correlation_id,
        runtime_instance_id="old-runtime", manifest_revision=1,
        idempotency_key=key or f"cognitive:prepare:{event.event_id}",
        issued_at=START - timedelta(days=1), deadline=START - timedelta(hours=23),
        expected_revision=7, version_vector=VersionVector(cognitive_memory=7),
        payload={"original": "authorized request", "large_body": "x" * 2048},
    )


def _intent(journal, event, envelope):
    return journal.append(
        "domain_command_intent", event_id=event.event_id,
        command_id=envelope.command_id, idempotency_key=envelope.idempotency_key,
        data={"service": "cognitive", "envelope": envelope.model_dump(mode="json")},
    )


def _noise(journal, count=70):
    for index in range(count):
        journal.append("irrelevant", data={"index": index, "payload": "n" * 128})


def _forbid_history_scans(monkeypatch, journal):
    def forbidden(*args, **kwargs):
        raise AssertionError("Command scheduling must not traverse full journal history.")

    monkeypatch.setattr(journal, "iter_records", forbidden)
    monkeypatch.setattr(journal, "records", forbidden)


def _result_payload(event, *, valid=True):
    prepared_at = START - timedelta(days=1)
    prepared = PreparedResponse(
        event_id=event.event_id, correlation_id=event.correlation_id, session_id="session",
        decision=ReplyDecision(text="你好"), read_versions=VersionVector(cognitive_memory=7),
        prepared_at=prepared_at,
        expires_at=START + timedelta(days=3650) if valid else START - timedelta(hours=1),
        model_version="fixture", prompt_version="fixture",
    )
    return {"service": "cognitive", "result": {"result": prepared.model_dump(mode="json")}}


def test_restart_replays_evicted_intent_without_new_payload_or_identity(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    event = _event()
    original = _envelope(event, command_type="ApplyEvent", key="homeostasis:event:old")
    _intent(journal, event, original)
    # A later envelope-less diagnostic intent must not hide the real command.
    journal.append("domain_command_intent", idempotency_key=original.idempotency_key, data={})
    _noise(journal)
    resumed_journal = _journal(tmp_path / "journal")
    runtime = _runtime(tmp_path, resumed_journal)
    _forbid_history_scans(monkeypatch, resumed_journal)
    replay = runtime._make_command(
        event, command_type="ApplyEvent", idempotency_key=original.idempotency_key,
        expected_revision=999, payload={"must_not_replace": "original request"},
    )
    assert replay.command_id == original.command_id
    assert replay.runtime_instance_id == original.runtime_instance_id
    assert replay.payload == original.payload
    assert replay.expected_revision == 7
    assert replay.version_vector == original.version_vector
    assert replay.effective_request_hash() == original.effective_request_hash()
    assert replay.issued_at > original.issued_at
    assert replay.deadline > replay.issued_at
    assert runtime._command_intent_seqs == {original.idempotency_key: 1}


def test_new_commands_and_prepare_keys_do_not_scan_existing_history(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    _noise(journal, 1000)
    runtime = _runtime(tmp_path, journal)
    _forbid_history_scans(monkeypatch, journal)
    for sequence in range(1, 101):
        event = _event(sequence)
        key = runtime._prepare_idempotency_key(event)
        assert key == f"cognitive:prepare:{event.event_id}"
        command = runtime._make_command(
            event, command_type="PrepareResponse", idempotency_key=key,
            payload={"text": str(sequence)}, expected_revision=sequence,
        )
        runtime._append_journal(
            "domain_command_intent", event=event, command_id=command.command_id,
            idempotency_key=key, data={"service": "cognitive", "envelope": command.model_dump(mode="json")},
        )
        assert runtime._prepare_idempotency_key(event) == key
    assert len(runtime._command_intent_seqs) == 100
    assert all(isinstance(value, int) for value in runtime._command_intent_seqs.values())
    assert all(
        isinstance(value, tuple) and all(isinstance(item, int) for item in value)
        for value in runtime._prepare_intent_seqs.values()
    )
    assert runtime._command_index_cursor == journal.last_record_seq


@pytest.mark.parametrize("outcome", ["missing", "valid", "expired", "malformed", "late"])
def test_prepare_replay_and_reconciliation_keep_historical_semantics(tmp_path, monkeypatch, outcome):
    journal = _journal(tmp_path / "journal")
    event = _event()
    envelope = _envelope(event)
    _intent(journal, event, envelope)
    _intent(journal, event, envelope)  # Replay attempts count as separate intents.
    if outcome != "missing":
        journal.append(
            "domain_command_late_result" if outcome == "late" else "domain_command_result",
            command_id=envelope.command_id,
            data={} if outcome == "malformed" else _result_payload(event, valid=outcome != "expired"),
        )
    # A result for another command must not supersede this command's result.
    journal.append("domain_command_late_result", command_id="another-command", data={})
    _noise(journal)
    runtime = _runtime(tmp_path, _journal(tmp_path / "journal"))
    _forbid_history_scans(monkeypatch, runtime.journal)
    expected = envelope.idempotency_key
    if outcome in {"expired", "malformed", "late"}:
        expected += ":reconcile:2"
    assert runtime._prepare_idempotency_key(event) == expected
    assert runtime._prepare_intent_seqs[event.event_id] == (2, 2)


def test_incremental_results_and_reprepare_attempts_update_sequence_metadata(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    runtime = _runtime(tmp_path, journal)
    _forbid_history_scans(monkeypatch, journal)
    event = _event()
    envelope = _envelope(event)
    # Recovery tools can append directly; catch-up must read only new pages.
    _intent(journal, event, envelope)
    assert runtime._prepare_idempotency_key(event) == envelope.idempotency_key
    journal.append("domain_command_result", command_id=envelope.command_id, data=_result_payload(event))
    assert runtime._prepare_idempotency_key(event) == envelope.idempotency_key
    journal.append("domain_command_late_result", command_id=envelope.command_id, data=_result_payload(event))
    retry_key = f"{envelope.idempotency_key}:reconcile:1"
    assert runtime._prepare_idempotency_key(event) == retry_key
    retry = runtime._make_command(
        event, command_type="PrepareResponse", idempotency_key=retry_key, payload={}, expected_revision=8,
    )
    assert retry.command_id != envelope.command_id
    runtime._append_journal(
        "domain_command_intent", event=event, command_id=retry.command_id,
        idempotency_key=retry_key, data={"service": "cognitive", "envelope": retry.model_dump(mode="json")},
    )
    assert runtime._prepare_idempotency_key(event) == retry_key
    runtime._append_journal(
        "domain_command_result", event=event, command_id=retry.command_id,
        data=_result_payload(event, valid=False),
    )
    assert runtime._prepare_idempotency_key(event) == f"{envelope.idempotency_key}:reconcile:2"
    assert all(isinstance(seq, int) for seq in runtime._command_result_seqs.values())


def test_failed_write_buffered_intent_is_indexed_before_retry(tmp_path, monkeypatch):
    journal = _journal(tmp_path / "journal")
    runtime = _runtime(tmp_path, journal)
    _forbid_history_scans(monkeypatch, journal)
    event = _event()
    original = _envelope(event)
    original_write = journal._write_record

    def unavailable(record):
        raise OSError("injected journal outage")

    monkeypatch.setattr(journal, "_write_record", unavailable)
    assert runtime._append_journal(
        "domain_command_intent", event=event, command_id=original.command_id,
        idempotency_key=original.idempotency_key,
        data={"service": "cognitive", "envelope": original.model_dump(mode="json")},
    ) is None
    assert runtime._command_index_cursor == journal.last_record_seq == 1
    replay = runtime._make_command(
        event, command_type="PrepareResponse", idempotency_key=original.idempotency_key,
        expected_revision=100, payload={"new": "must not replace buffered intent"},
    )
    assert replay.command_id == original.command_id
    assert replay.effective_request_hash() == original.effective_request_hash()
    monkeypatch.setattr(journal, "_write_record", original_write)
    assert journal.try_recover_writes()
    assert runtime._prepare_idempotency_key(event) == original.idempotency_key
