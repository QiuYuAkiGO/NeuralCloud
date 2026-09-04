"""Versioned profile loading and immutable revision staging."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from harness.homeostasis.models import HomeostaticProfile
from harness.research.manifest import ExperimentManifest


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def load_profile(path: Path | str) -> HomeostaticProfile:
    target = Path(path)
    return HomeostaticProfile.model_validate(json.loads(target.read_text(encoding="utf-8")))


class ProfileRevisionStore:
    def __init__(
        self,
        *,
        profile_dir: Path | str,
        manifest_dir: Path | str,
        pending_path: Path | str,
    ) -> None:
        self.profile_dir = Path(profile_dir)
        self.manifest_dir = Path(manifest_dir)
        self.pending_path = Path(pending_path)

    def pending(self) -> dict[str, Any] | None:
        if not self.pending_path.is_file():
            return None
        try:
            value = json.loads(self.pending_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def stage(
        self,
        *,
        current_profile: HomeostaticProfile,
        current_manifest: ExperimentManifest,
        candidate: dict[str, Any],
        base_revision: int,
    ) -> dict[str, Any]:
        if current_manifest.control_mode != "development_debug":
            raise PermissionError("profile revisions are disabled in study_runtime mode")
        if base_revision != current_profile.revision:
            raise ValueError(
                f"profile revision conflict: expected {current_profile.revision}, got {base_revision}"
            )
        next_revision = current_profile.revision + 1
        payload = dict(candidate)
        payload.update(
            {
                "schema_version": "homeostasis-profile-v1",
                "profile_id": current_profile.profile_id,
                "revision": next_revision,
            }
        )
        profile = HomeostaticProfile.model_validate(payload)
        manifest_payload = current_manifest.model_dump(mode="json")
        manifest_payload.update(
            {
                "revision": current_manifest.revision + 1,
                "homeostatic_profile": profile.profile_ref,
                "persona_version": profile.persona_version,
                "a_version": profile.observation_model_version,
                "b_version": profile.transition_model_version,
                "c_version": profile.preference_model_version,
                "precision_version": profile.precision_profile_version,
                "coupling_version": profile.coupling_matrix_version,
            }
        )
        manifest = ExperimentManifest.model_validate(manifest_payload)
        profile_path = self.profile_dir / f"{profile.profile_id}-r{profile.revision}.json"
        manifest_path = self.manifest_dir / f"{manifest.experiment_id}-r{manifest.revision}.json"
        if profile_path.exists() or manifest_path.exists():
            raise FileExistsError("the requested immutable revision already exists")
        atomic_write_json(profile_path, profile.model_dump(mode="json"))
        atomic_write_json(manifest_path, manifest.model_dump(mode="json"))
        pending = {
            "profile_path": str(profile_path.resolve()),
            "manifest_path": str(manifest_path.resolve()),
            "profile_ref": profile.profile_ref,
            "manifest_ref": manifest.manifest_ref,
            "restart_required": True,
        }
        atomic_write_json(self.pending_path, pending)
        return pending
