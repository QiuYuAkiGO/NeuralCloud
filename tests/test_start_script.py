from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("pwsh") or shutil.which("powershell")


@pytest.mark.skipif(POWERSHELL is None, reason="PowerShell is unavailable")
@pytest.mark.parametrize(
    ("provider", "expected_error"),
    [
        ("none", "required embodiment manifest cannot use provider none"),
        ("simulator", "required embodiment provider mismatch"),
    ],
)
def test_required_misty_manifest_rejects_provider_override_before_launch(
    provider: str,
    expected_error: str,
) -> None:
    environment = os.environ.copy()
    environment.pop("NC_MISTYAI_TOKEN", None)
    environment.pop("NC_MISTYAI_TOKEN_FILE", None)
    result = subprocess.run(
        [
            str(POWERSHELL),
            "-NoProfile",
            "-NonInteractive",
            "-File",
            str(ROOT / "start.ps1"),
            "-ManifestPath",
            str(ROOT / "presets" / "runtime" / "grounded-mistyai-v1.json"),
            "-EmbodimentProvider",
            provider,
        ],
        cwd=ROOT,
        env=environment,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )

    assert result.returncode == 1
    assert expected_error in result.stdout.lower()
    assert "Starting " not in result.stdout
