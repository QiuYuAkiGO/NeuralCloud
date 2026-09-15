"""Derived Runtime checkpoints written with atomic replacement."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from harness.contracts.envelope import VersionVector
from harness.runtime.models import AgencyState, RUNTIME_CHECKPOINT_SCHEMA_VERSION, RuntimeLifecycle


class RuntimeCheckpoint(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["runtime-checkpoint-v1"] = RUNTIME_CHECKPOINT_SCHEMA_VERSION
    character_id: str
    runtime_instance_id: str
    manifest_revision: int = Field(ge=1)
    created_at: AwareDatetime = Field(default_factory=lambda: datetime.now(UTC))
    journal_record_seq: int = Field(ge=0)
    journal_hash: str
    receive_seq: int = Field(ge=0)
    process_seq: int = Field(ge=0)
    lifecycle: RuntimeLifecycle
    lifecycle_reason: str
    version_vector: VersionVector = Field(default_factory=VersionVector)
    pending_event_ids: list[str] = Field(default_factory=list)
    prepared_response_ids: list[str] = Field(default_factory=list)
    pending_action_ids: list[str] = Field(default_factory=list)
    maintenance_refs: list[str] = Field(default_factory=list)
    cognitive_acknowledged_receive_seq: int = Field(default=0, ge=0)
    homeostasis_acknowledged_receive_seq: int = Field(default=0, ge=0)
    agency_state: AgencyState = AgencyState.AVAILABLE
    body_episode_id: str | None = None
    body_revision: int | None = Field(default=None, ge=1)
    body_captured_at: AwareDatetime | None = None
    interoceptive_state: dict[str, object] | None = None


class CheckpointStore:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def load(self) -> RuntimeCheckpoint | None:
        if not self.path.exists():
            return None
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return RuntimeCheckpoint.model_validate(payload)
        except (OSError, ValueError):
            return None

    def write(self, checkpoint: RuntimeCheckpoint) -> None:
        temporary = self.path.with_name(f".{self.path.name}.tmp")
        encoded = json.dumps(
            checkpoint.model_dump(mode="json"),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.path)
