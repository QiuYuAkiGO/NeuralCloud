from __future__ import annotations

import asyncio

from fastapi.testclient import TestClient
import pytest

from harness.config import Settings
from harness.homeostasis import HomeostasisEngine, ProfileRevisionStore, load_profile
from harness.memory.service import build_inmemory_service
from harness.pipeline.pipeline import InferencePipeline
from harness.research.manifest import ExperimentManifest
from harness.runtime import (
    ControlPlane,
    DomainCommandError,
    DomainSnapshot,
    PassiveHomeostasisAdapter,
    PipelineCognitiveAdapter,
    RuntimeSupervisor,
    create_control_app,
)
from tests.factories import vaelith


def _manifest(*, study: bool = False) -> ExperimentManifest:
    payload = {
        "experiment_id": "api-test",
        "revision": 1,
        "control_mode": "study_runtime" if study else "development_debug",
        "condition": "llm_top3",
        "persona_version": "persona_misty_v020",
        "homeostatic_profile": "functional-proxy-v1@2",
        "heartbeat_seconds": 60,
        "policy_horizon": 3,
        "random_seed": 7,
        "initial_snapshot_ref": "snapshot://functional-proxy-v1/initial",
        "model_version": "mock",
        "prompt_version": "memory-v1",
        "schema_version": "homeostasis-state-v1",
        "a_version": "A-v1",
        "b_version": "B-v1",
        "c_version": "C-v1",
        "precision_version": "precision-v1",
        "coupling_version": "coupling-v2-realized-delta",
        "priority_policy": "runtime-v1",
        "environment": "test",
        "retention": "test",
        "watchdog_action": "pause_preserve_state",
    }
    if study:
        payload.update(
            participant_ref="pseudonym:test",
            authorized_data_scope_ref="consent://test/v1",
            high_impact_gate="independent_confirmation",
        )
    return ExperimentManifest.model_validate(payload)


def _app(tmp_path, *, study: bool = False):
    settings = Settings(
        parser_llm_enabled=False,
        planner_llm_enabled=False,
        tier_llm_enabled=False,
        validator_sample_rate=0,
        continuation_enabled=False,
        call_log_enabled=False,
    )
    memory = build_inmemory_service(settings=settings)
    memory.bootstrap_persona(vaelith())
    cognitive = PipelineCognitiveAdapter(
        InferencePipeline(memory, settings=settings),
        state_path=tmp_path / "cognitive.json",
    )
    profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
    homeostasis = HomeostasisEngine(profile, state_dir=tmp_path / "homeostasis")
    supervisor = RuntimeSupervisor(
        manifest=_manifest(study=study),
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / "runtime",
    )
    control = ControlPlane(supervisor, token="control-secret")
    revisions = ProfileRevisionStore(
        profile_dir=tmp_path / "profiles",
        manifest_dir=tmp_path / "manifests",
        pending_path=tmp_path / "pending.json",
    )
    return create_control_app(control, profile_store=revisions)


def test_composite_snapshot_profile_validation_and_revision(tmp_path) -> None:
    with TestClient(_app(tmp_path)) as client:
        assert client.get("/v1/snapshot").status_code == 401
        headers = {"Authorization": "Bearer control-secret"}
        snapshot = client.get("/v1/snapshot", headers=headers).json()
        assert snapshot["homeostasis"]["payload"]["profile"]["profile_id"] == "functional-proxy-v1"
        profile = snapshot["homeostasis"]["payload"]["profile"]
        validated = client.post(
            "/v1/homeostasis/profile/validate",
            headers=headers,
            json={"profile": profile},
        )
        assert validated.status_code == 200
        profile["dimensions"]["energy_reserve"]["baseline_band"] = [0.57, 0.79]
        revision = client.post(
            "/v1/homeostasis/profile/revisions",
            headers=headers,
            json={"base_revision": 3, "profile": profile},
        )
        assert revision.status_code == 200
        assert revision.json()["profile_ref"] == "functional-proxy-v1@4"
        assert revision.json()["restart_required"] is True


def test_study_runtime_rejects_profile_revision(tmp_path) -> None:
    with TestClient(_app(tmp_path, study=True)) as client:
        headers = {"Authorization": "Bearer control-secret"}
        profile = client.get("/v1/homeostasis/profile", headers=headers).json()["profile"]
        response = client.post(
            "/v1/homeostasis/profile/revisions",
            headers=headers,
            json={"base_revision": 3, "profile": profile},
        )
        assert response.status_code == 403


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("homeostatic_profile", "other-profile@2"),
        ("schema_version", "other-state-v1"),
        ("persona_version", "other-persona"),
        ("a_version", "A-other"),
        ("b_version", "B-other"),
        ("c_version", "C-other"),
        ("precision_version", "precision-other"),
        ("coupling_version", "coupling-v1"),
    ],
)
def test_runtime_rejects_manifest_profile_contract_mismatch(
    tmp_path,
    field: str,
    invalid_value: str,
) -> None:
    settings = Settings(
        parser_llm_enabled=False,
        planner_llm_enabled=False,
        tier_llm_enabled=False,
        validator_sample_rate=0,
        continuation_enabled=False,
        call_log_enabled=False,
    )
    memory = build_inmemory_service(settings=settings)
    memory.bootstrap_persona(vaelith())
    cognitive = PipelineCognitiveAdapter(
        InferencePipeline(memory, settings=settings),
        state_path=tmp_path / f"cognitive-mismatch-{field}.json",
    )
    profile = load_profile("presets/homeostasis/functional-proxy-v1.json")
    homeostasis = HomeostasisEngine(
        profile,
        state_dir=tmp_path / f"homeostasis-mismatch-{field}",
    )
    manifest = _manifest().model_copy(update={field: invalid_value})
    supervisor = RuntimeSupervisor(
        manifest=manifest,
        cognitive=cognitive,
        homeostasis=homeostasis,
        state_dir=tmp_path / f"runtime-mismatch-{field}",
    )

    async def scenario() -> None:
        try:
            with pytest.raises(DomainCommandError, match=field):
                await supervisor._check_services()
        finally:
            await cognitive.close()
            await homeostasis.close()

    asyncio.run(scenario())


def test_only_explicit_passive_profile_may_omit_profile_payload(tmp_path) -> None:
    missing_profile = DomainSnapshot(service="homeostasis", revision=0, payload={})
    active = RuntimeSupervisor(
        manifest=_manifest(),
        cognitive=object(),  # type: ignore[arg-type]
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "active-missing-profile",
    )
    with pytest.raises(DomainCommandError, match="missing its profile"):
        active._validate_homeostasis_contract(missing_profile)

    passive = RuntimeSupervisor(
        manifest=_manifest().model_copy(update={"homeostatic_profile": "passive-v1"}),
        cognitive=object(),  # type: ignore[arg-type]
        homeostasis=PassiveHomeostasisAdapter(),
        state_dir=tmp_path / "passive-missing-profile",
    )
    passive._validate_homeostasis_contract(missing_profile)
