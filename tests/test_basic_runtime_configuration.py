from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from harness.research.manifest import ExperimentManifest, load_experiment_manifest
from harness.runtime import PassiveHomeostasisAdapter, RuntimeEventPriority, RuntimeEventType, RuntimeSupervisor
from tests.test_runtime_supervisor import _event, _manifest, _pipeline_adapter


def test_milestone_preset_and_legacy_manifest_hash_contract():
    legacy = _manifest()
    assert "command_timeout_seconds" not in json.loads(legacy.canonical_json())
    assert "basic_terminal" not in json.loads(legacy.canonical_json())
    preset = load_experiment_manifest(Path("presets/runtime/basic-runtime-v1.json"))
    assert preset.command_timeout_seconds == 120 and preset.basic_terminal.enabled
    assert preset.embodiment.mode == "grounded" and preset.homeostatic_expression.mode == "full"
    for value in (0, 301, float("nan"), float("inf")):
        with pytest.raises(ValidationError):
            ExperimentManifest.model_validate({**legacy.model_dump(), "command_timeout_seconds": value})
    with pytest.raises(ValidationError):
        preset.model_copy(update={"embodiment": {"mode": "off"}})


def test_runtime_transport_window_uses_frozen_manifest_budget(tmp_path):
    _, pipeline, cognitive = _pipeline_adapter(tmp_path)
    manifest = ExperimentManifest.model_validate({**_manifest().model_dump(), "command_timeout_seconds": 120})
    runtime = RuntimeSupervisor(manifest=manifest, cognitive=cognitive, homeostasis=PassiveHomeostasisAdapter(), state_dir=tmp_path / "runtime")
    command = runtime._make_command(
        _event(1, RuntimeEventType.USER_MESSAGE, RuntimeEventPriority.USER), command_type="PrepareResponse",
        idempotency_key="test:120-second-budget", expected_revision=0, payload={"text": "hello"},
    )
    assert (command.deadline - command.issued_at).total_seconds() == 120
    pipeline.memory.close()
