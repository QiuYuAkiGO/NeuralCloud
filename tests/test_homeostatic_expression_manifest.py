from __future__ import annotations

from pathlib import Path

import pytest

from harness.research.manifest import ExperimentManifest, load_experiment_manifest


ROOT = Path(__file__).resolve().parents[1]


def _legacy_payload() -> dict[str, object]:
    return {
        "experiment_id": "legacy-compatible",
        "revision": 1,
        "control_mode": "development_debug",
        "condition": "llm_top3",
        "persona_version": "persona-v1",
        "homeostatic_profile": "passive-v1",
        "heartbeat_seconds": 60,
        "policy_horizon": 3,
        "random_seed": 7,
        "initial_snapshot_ref": "snapshot://test/initial",
        "model_version": "mock",
        "prompt_version": "memory-v1",
        "schema_version": "state-v1",
        "a_version": "A-v1",
        "b_version": "B-v1",
        "c_version": "C-v1",
        "precision_version": "precision-v1",
        "coupling_version": "coupling-v1",
        "priority_policy": "runtime-v1",
        "environment": "test",
        "retention": "test",
        "watchdog_action": "pause_preserve_state",
    }


def test_legacy_manifest_defaults_homeostatic_expression_off() -> None:
    manifest = ExperimentManifest.model_validate(_legacy_payload())

    assert manifest.homeostatic_expression.mode == "off"
    assert manifest.homeostatic_expression.marked_cooldown_seconds == 1800


def test_runtime_presets_enable_full_homeostatic_expression() -> None:
    for relative in (
        "presets/runtime/development-demo.json",
        "presets/runtime/grounded-mistyai-v1.json",
    ):
        manifest = load_experiment_manifest(ROOT / relative)

        assert manifest.homeostatic_expression.mode == "full"
        assert manifest.homeostatic_expression.marked_cooldown_seconds == 1800
        assert manifest.homeostatic_profile == "functional-proxy-v1@3"
        assert manifest.schema_version == "homeostasis-state-v2"
        assert manifest.prompt_version == "homeostatic-interoception-v1"


@pytest.mark.parametrize(
    ("mode", "overrides", "error"),
    [
        (
            "shadow",
            {"homeostatic_profile": "functional-proxy-v1@2"},
            "revision 3",
        ),
        (
            "responses",
            {"schema_version": "homeostasis-state-v1"},
            "homeostasis-state-v2",
        ),
        (
            "full",
            {"prompt_version": "memory-v1"},
            "homeostatic-interoception-v1",
        ),
    ],
)
def test_enabled_homeostatic_expression_rejects_legacy_contracts(
    mode: str,
    overrides: dict[str, object],
    error: str,
) -> None:
    payload = {
        **_legacy_payload(),
        "homeostatic_profile": "functional-proxy-v1@3",
        "schema_version": "homeostasis-state-v2",
        "prompt_version": "homeostatic-interoception-v1",
        "homeostatic_expression": {"mode": mode},
        **overrides,
    }

    with pytest.raises(ValueError, match=error):
        ExperimentManifest.model_validate(payload)
