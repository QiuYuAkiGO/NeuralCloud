import json

import pytest

from gui.presets import load_builtin
from tools.freeze_production_baseline import freeze, sha
from harness.memory.production_migration import inspect_snapshot, migrate_snapshot


def test_checked_baseline_and_migration_leave_original_intact(tmp_path):
    original = tmp_path / "misty.json"
    original.write_text(load_builtin("misty").model_dump_json(), encoding="utf-8")
    initial = sha(original)
    with pytest.raises(ValueError, match="quiesced"):
        freeze(preset=original, destination=tmp_path / "snapshot")
    result = freeze(preset=original, destination=tmp_path / "snapshot", writers_stopped=True)
    inventory = inspect_snapshot(result["manifest"])
    assert inventory["preset_id"] == "misty"
    manifest = json.loads((tmp_path / "snapshot" / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_fingerprints"][str(original)] == initial
    report = migrate_snapshot(result["manifest"], tmp_path / "new.sqlite3")
    assert report["complete"] and report["source_unchanged"]
    assert sha(original) == initial
    with pytest.raises(FileExistsError):
        freeze(preset=original, destination=tmp_path / "snapshot", writers_stopped=True)


def test_production_profile_keeps_original_and_research_hash():
    from pathlib import Path
    from harness.research.manifest import load_experiment_manifest
    from tools.production_profile import build
    source = Path("presets/runtime/shared-misty-v1.json")
    old = load_experiment_manifest(source)
    before = source.read_bytes()
    changed = build(source, body_instance_id="body-explicit")
    assert changed.production.autonomous_attempts_per_hour == 12
    assert changed.embodiment.required_for_runtime is False
    assert changed.embodiment.body_instance_id == "body-explicit"
    assert changed.homeostatic_expression.mode == "off"
    assert not changed.goal_regulation.enabled and changed.grounded_agency is None
    assert source.read_bytes() == before
    assert load_experiment_manifest(source).content_hash() == old.content_hash()
