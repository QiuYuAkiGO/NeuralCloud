"""Frozen, hashable ExperimentManifest used as the study/runtime authority."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, ConfigDict, Field, model_validator

ControlMode = Literal["development_debug", "study_runtime"]
SelectionCondition = Literal["argmin_efe", "llm_top3", "llm_open"]


class ExperimentManifest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    experiment_id: str = Field(min_length=1, max_length=128)
    revision: int = Field(ge=1)
    control_mode: ControlMode = "study_runtime"
    participant_ref: str | None = None
    condition: SelectionCondition
    persona_version: str = Field(min_length=1)
    homeostatic_profile: str = Field(min_length=1)
    heartbeat_seconds: int = Field(default=60, ge=1, le=3600)
    policy_horizon: int = Field(default=3, ge=1, le=3)
    random_seed: int = Field(ge=0)
    initial_snapshot_ref: str = Field(min_length=1)
    model_version: str = Field(min_length=1)
    prompt_version: str = Field(min_length=1)
    schema_version: str = Field(min_length=1)
    a_version: str = Field(min_length=1)
    b_version: str = Field(min_length=1)
    c_version: str = Field(min_length=1)
    precision_version: str = Field(min_length=1)
    coupling_version: str = Field(min_length=1)
    priority_policy: str = Field(min_length=1)
    environment: str = Field(min_length=1)
    authorized_data_scope_ref: str | None = None
    high_impact_gate: str | None = None
    retention: str = Field(min_length=1)
    watchdog_action: str = Field(min_length=1)
    timezone: str = Field(default="Asia/Shanghai", min_length=1)

    @model_validator(mode="after")
    def _validate_study_authority(self) -> ExperimentManifest:
        try:
            ZoneInfo(self.timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"unknown manifest timezone: {self.timezone}") from exc
        if self.control_mode == "study_runtime":
            required = {
                "participant_ref": self.participant_ref,
                "authorized_data_scope_ref": self.authorized_data_scope_ref,
                "high_impact_gate": self.high_impact_gate,
            }
            missing = [name for name, value in required.items() if not value]
            if missing:
                raise ValueError(f"study_runtime manifest missing: {', '.join(missing)}")
        return self

    @property
    def manifest_ref(self) -> str:
        return f"manifest://{self.experiment_id}/r{self.revision}"

    def canonical_json(self) -> str:
        return json.dumps(
            self.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.canonical_json().encode("utf-8")).hexdigest()


def load_experiment_manifest(path: Path) -> ExperimentManifest:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return ExperimentManifest.model_validate(payload)
