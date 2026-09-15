"""Production goal opt-in preserves existing manifests and research isolation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.regulation.config import RegulationConfig
from harness.research.manifest import ExperimentManifest, load_experiment_manifest


ROOT = Path(__file__).resolve().parents[1]
PRESETS = ROOT / "presets"


def shared_payload():
    return json.loads((PRESETS / "runtime/shared-misty-v1.json").read_text(encoding="utf-8"))


def test_regulation_production_preset_loads_through_manifest_validation():
    manifest = load_experiment_manifest(PRESETS / "runtime/shared-misty-regulation-v1.json")
    assert manifest.goal_regulation.enabled
    assert manifest.goal_regulation.autonomous_enabled
    assert manifest.autonomous_capabilities.enabled
    assert manifest.grounded_agency is None
    assert manifest.goal_regulation.maximum_model_calls_per_hour == 12
    assert manifest.goal_regulation.minimum_reconsideration_interval >= 60
    assert manifest.goal_regulation.stability_seconds >= 180
    assert json.loads(manifest.canonical_json())["goal_regulation"]["enabled"] is True


def test_existing_manifest_keeps_default_disabled_extension_out_of_canonical_hash():
    payload = shared_payload()
    assert "goal_regulation" not in payload
    old = ExperimentManifest.model_validate(payload)
    explicit_default = ExperimentManifest.model_validate(
        {**payload, "goal_regulation": RegulationConfig().model_dump(mode="json")}
    )
    assert old.goal_regulation == RegulationConfig()
    assert not old.goal_regulation.enabled
    assert "goal_regulation" not in json.loads(old.canonical_json())
    assert old.canonical_json() == explicit_default.canonical_json()
    assert old.content_hash() == explicit_default.content_hash()


@pytest.mark.parametrize("capabilities", [None, {}, {"enabled": False}])
def test_goal_regulation_requires_explicitly_enabled_shared_capabilities(capabilities):
    payload = shared_payload()
    payload["goal_regulation"] = {"enabled": True}
    if capabilities is None:
        payload.pop("autonomous_capabilities", None)
    else:
        payload["autonomous_capabilities"] = capabilities
    with pytest.raises(ValidationError, match="goal regulation requires shared autonomous capabilities"):
        ExperimentManifest.model_validate(payload)


def test_goal_regulation_rejects_otherwise_valid_grounded_research_manifest():
    research = load_experiment_manifest(PRESETS / "research/grounded-agency/no-body-v1.json")
    assert research.grounded_agency is not None
    payload = research.model_dump(mode="json")
    payload["goal_regulation"] = {"enabled": True}
    payload["autonomous_capabilities"] = {"enabled": True}
    with pytest.raises(ValidationError, match="production goal regulation and grounded research require separate manifests"):
        ExperimentManifest.model_validate(payload)


@pytest.mark.parametrize("enabled", ["true", 1])
def test_goal_opt_in_requires_boolean_in_real_manifest_parser(enabled):
    payload = shared_payload()
    payload["goal_regulation"] = {"enabled": enabled}
    with pytest.raises(ValidationError, match="goal_regulation.enabled"):
        ExperimentManifest.model_validate(payload)
