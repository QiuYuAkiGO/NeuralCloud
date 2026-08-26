from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import TypeAdapter, ValidationError

from harness.contracts.decisions import (
    CharacterDecision,
    DeliveryEvidence,
    PreparedResponse,
    ReplyDecision,
)
from harness.contracts.envelope import (
    DomainCommandEnvelope,
    DomainCommandResult,
    VersionVector,
    canonical_payload_hash,
)
from harness.research.manifest import ExperimentManifest, load_experiment_manifest

NOW = datetime(2026, 8, 25, 9, 0, tzinfo=UTC)


def _manifest_payload() -> dict[str, object]:
    return {
        "experiment_id": "homeostasis-pilot-p01-c2",
        "revision": 1,
        "control_mode": "study_runtime",
        "participant_ref": "pseudonym:p01",
        "condition": "llm_top3",
        "persona_version": "persona_social_v1",
        "homeostatic_profile": "functional-social-v1",
        "heartbeat_seconds": 60,
        "policy_horizon": 3,
        "random_seed": 48117,
        "initial_snapshot_ref": "snapshot://pilot/base/social",
        "model_version": "rp-30b-a3b-v3",
        "prompt_version": "homeostasis-v1",
        "schema_version": "homeostasis-state-v1",
        "a_version": "A-v1",
        "b_version": "B-v1",
        "c_version": "C-v1",
        "precision_version": "precision-v1",
        "coupling_version": "coupling-v1",
        "priority_policy": "agent_first",
        "environment": "authorized_real_workspace",
        "authorized_data_scope_ref": "consent://p01/v1",
        "high_impact_gate": "independent_confirmation",
        "retention": "full_no_auto_delete",
        "watchdog_action": "pause_preserve_state",
    }


def test_command_envelope_hash_deadline_and_version_vector() -> None:
    payload = {"text": "你好", "session_id": "sess_1"}
    envelope = DomainCommandEnvelope[dict[str, str]](
        command_type="PrepareResponse",
        event_id="evt_1",
        receive_seq=4,
        correlation_id="corr_1",
        runtime_instance_id="runtime_1",
        manifest_revision=1,
        idempotency_key="prepare:evt_1",
        issued_at=NOW,
        deadline=NOW + timedelta(seconds=30),
        expected_revision=7,
        version_vector=VersionVector(cognitive_memory=7, homeostasis_state=11),
        payload=payload,
    )

    assert envelope.effective_request_hash() == canonical_payload_hash(envelope.request_fingerprint())
    assert envelope.version_vector is not None
    assert envelope.version_vector.flatten()["homeostasis.state"] == 11
    assert not envelope.is_expired(now=NOW + timedelta(seconds=10))
    assert envelope.is_expired(now=NOW + timedelta(seconds=30))
    assert envelope.is_expired(now=NOW + timedelta(seconds=31))


def test_command_envelope_rejects_payload_hash_mismatch_and_naive_clock() -> None:
    with pytest.raises(ValidationError, match="request_hash"):
        DomainCommandEnvelope[dict[str, str]](
            command_type="ApplyEvent",
            event_id="evt_1",
            receive_seq=1,
            correlation_id="corr_1",
            runtime_instance_id="runtime_1",
            manifest_revision=1,
            idempotency_key="apply:evt_1",
            issued_at=NOW,
            deadline=NOW + timedelta(seconds=30),
            payload={"value": "x"},
            request_hash="0" * 64,
        )

    envelope = DomainCommandEnvelope[dict[str, str]](
        command_type="ApplyEvent",
        event_id="evt_1",
        receive_seq=1,
        correlation_id="corr_1",
        runtime_instance_id="runtime_1",
        manifest_revision=1,
        idempotency_key="apply:evt_1",
        issued_at=NOW,
        deadline=NOW + timedelta(seconds=30),
        payload={"value": "x"},
    )
    with pytest.raises(ValueError, match="timezone-aware"):
        envelope.is_expired(now=datetime(2026, 8, 25, 9, 0))


def test_command_hash_survives_journal_json_round_trip() -> None:
    envelope = DomainCommandEnvelope[dict[str, object]](
        command_type="PrepareResponse",
        event_id="evt_json",
        receive_seq=2,
        correlation_id="corr_json",
        runtime_instance_id="runtime_1",
        manifest_revision=1,
        idempotency_key="prepare:evt_json",
        issued_at=NOW,
        deadline=NOW + timedelta(seconds=30),
        payload={"source_time": NOW, "nested": {"when": NOW}},
    )
    restored = DomainCommandEnvelope[dict[str, object]].model_validate(
        envelope.model_dump(mode="json")
    )
    assert restored.effective_request_hash() == envelope.effective_request_hash()


def test_domain_command_result_requires_revision_cursor_and_error() -> None:
    request_hash = canonical_payload_hash({"event": "evt_1"})
    result = DomainCommandResult[dict[str, int]](
        command_id="cmd_1",
        command_type="ApplyEvent",
        status="applied",
        request_hash=request_hash,
        committed_revision=8,
        acknowledged_event_id="evt_1",
        acknowledged_receive_seq=3,
        result={"revision": 8},
        completed_at=NOW,
    )
    assert result.committed_revision == 8

    with pytest.raises(ValidationError, match="committed_revision"):
        DomainCommandResult[dict[str, int]](
            command_id="cmd_2",
            command_type="ApplyEvent",
            status="applied",
            request_hash=request_hash,
            acknowledged_receive_seq=3,
            completed_at=NOW,
        )
    with pytest.raises(ValidationError, match="require error"):
        DomainCommandResult[dict[str, int]](
            command_id="cmd_3",
            command_type="ApplyEvent",
            status="conflict",
            request_hash=request_hash,
            completed_at=NOW,
        )


def test_character_decision_is_discriminated_and_prepared_is_uncommitted() -> None:
    adapter = TypeAdapter(CharacterDecision)
    decision = adapter.validate_python({"kind": "reply", "text": "嗯。", "segments": ["嗯。"]})
    assert isinstance(decision, ReplyDecision)

    prepared = PreparedResponse(
        event_id="evt_1",
        correlation_id="corr_1",
        session_id="sess_1",
        decision=decision,
        read_versions=VersionVector(cognitive_session=4, homeostasis_state=9),
        prepared_at=NOW,
        expires_at=NOW + timedelta(minutes=1),
        model_version="mock",
        prompt_version="memory-v1",
    )
    evidence = DeliveryEvidence(
        prepare_id=prepared.prepare_id,
        delivered_at=NOW + timedelta(seconds=1),
        channel="chat",
        success=True,
        delivered_segments=["嗯。"],
    )
    assert evidence.prepare_id == prepared.prepare_id


def test_manifest_is_frozen_hashable_and_loadable(tmp_path) -> None:
    payload = _manifest_payload()
    manifest = ExperimentManifest.model_validate(payload)
    same = ExperimentManifest.model_validate(dict(reversed(list(payload.items()))))

    assert manifest.manifest_ref == "manifest://homeostasis-pilot-p01-c2/r1"
    assert manifest.content_hash() == same.content_hash()
    with pytest.raises(ValidationError, match="frozen"):
        manifest.revision = 2

    path = tmp_path / "manifest.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    assert load_experiment_manifest(path) == manifest


def test_study_manifest_requires_authorization_fields() -> None:
    payload = _manifest_payload()
    payload["authorized_data_scope_ref"] = None
    with pytest.raises(ValidationError, match="authorized_data_scope_ref"):
        ExperimentManifest.model_validate(payload)

    payload["control_mode"] = "development_debug"
    manifest = ExperimentManifest.model_validate(payload)
    assert manifest.authorized_data_scope_ref is None
