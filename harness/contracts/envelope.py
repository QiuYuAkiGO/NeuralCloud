"""Service envelopes and revision vectors for idempotent domain commands."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from typing import Any, Generic, Literal, TypeVar

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field, model_validator
from pydantic_core import to_jsonable_python

from harness.core.ids import new_id

CONTRACT_SCHEMA_VERSION = "service-contract-v1"

PayloadT = TypeVar("PayloadT")
ResultT = TypeVar("ResultT")


def canonical_payload_hash(value: Any) -> str:
    """Return a stable SHA-256 for request replay/conflict detection."""

    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    else:
        # Arbitrary command payloads may contain datetimes, enums or nested
        # Pydantic values. Normalize them exactly as the HTTP/journal JSON form
        # does so a recovered envelope retains the same request hash.
        value = to_jsonable_python(value)
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class VersionVector(BaseModel):
    """Revisions actually read while producing a decision or action plan."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    runtime_goal: int = Field(default=0, ge=0)
    runtime_action: int = Field(default=0, ge=0)
    cognitive_world: int = Field(default=0, ge=0)
    cognitive_relationship: int = Field(default=0, ge=0)
    cognitive_memory: int = Field(default=0, ge=0)
    cognitive_session: int = Field(default=0, ge=0)
    cognitive_maintenance: int = Field(default=0, ge=0)
    homeostasis_state: int = Field(default=0, ge=0)

    def flatten(self) -> dict[str, int]:
        return {
            "runtime.goal": self.runtime_goal,
            "runtime.action": self.runtime_action,
            "cognitive.world": self.cognitive_world,
            "cognitive.relationship": self.cognitive_relationship,
            "cognitive.memory": self.cognitive_memory,
            "cognitive.session": self.cognitive_session,
            "cognitive.maintenance": self.cognitive_maintenance,
            "homeostasis.state": self.homeostasis_state,
        }


class DomainCommandEnvelope(BaseModel, Generic[PayloadT]):
    """Metadata that every Runtime-to-domain command must carry."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["service-contract-v1"] = "service-contract-v1"
    command_id: str = Field(default_factory=lambda: new_id("command"), min_length=1)
    command_type: str = Field(min_length=1, max_length=96)
    event_id: str = Field(min_length=1, max_length=128)
    receive_seq: int = Field(ge=1)
    correlation_id: str = Field(min_length=1, max_length=128)
    character_id: str = Field(default="character", min_length=1, max_length=128)
    runtime_instance_id: str = Field(min_length=1, max_length=128)
    manifest_revision: int = Field(ge=1)
    idempotency_key: str = Field(min_length=1, max_length=256)
    deadline: AwareDatetime
    issued_at: AwareDatetime
    expected_revision: int | None = Field(default=None, ge=0)
    version_vector: VersionVector | None = None
    payload: PayloadT
    request_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def _validate_deadline_and_hash(self) -> DomainCommandEnvelope[PayloadT]:
        if self.deadline <= self.issued_at:
            raise ValueError("deadline must be later than issued_at")
        expected = canonical_payload_hash(self.request_fingerprint())
        if self.request_hash is not None and self.request_hash != expected:
            raise ValueError("request_hash does not match command request")
        return self

    def request_fingerprint(self) -> dict[str, Any]:
        """Stable request fields used to detect idempotency-key reuse conflicts."""

        payload: Any = self.payload
        if isinstance(payload, BaseModel):
            payload = payload.model_dump(mode="json")
        return {
            "schema_version": self.schema_version,
            "command_type": self.command_type,
            "event_id": self.event_id,
            "receive_seq": self.receive_seq,
            "correlation_id": self.correlation_id,
            "character_id": self.character_id,
            "runtime_instance_id": self.runtime_instance_id,
            "manifest_revision": self.manifest_revision,
            "expected_revision": self.expected_revision,
            "version_vector": None if self.version_vector is None else self.version_vector.model_dump(mode="json"),
            "payload": payload,
        }

    def effective_request_hash(self) -> str:
        return self.request_hash or canonical_payload_hash(self.request_fingerprint())

    def is_expired(self, *, now: datetime) -> bool:
        if now.tzinfo is None:
            raise ValueError("now must be timezone-aware")
        return now >= self.deadline


CommandResultStatus = Literal[
    "applied",
    "idempotent_replay",
    "rejected",
    "conflict",
    "failed",
]


class DomainCommandResult(BaseModel, Generic[ResultT]):
    """Stable response persisted by a domain command ledger."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["service-contract-v1"] = "service-contract-v1"
    command_id: str = Field(min_length=1)
    command_type: str = Field(min_length=1)
    status: CommandResultStatus
    request_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    committed_revision: int | None = Field(default=None, ge=0)
    acknowledged_event_id: str | None = None
    acknowledged_receive_seq: int | None = Field(default=None, ge=1)
    idempotency_hit: bool = False
    result: ResultT | None = None
    error: str | None = None
    completed_at: AwareDatetime

    @model_validator(mode="after")
    def _validate_status(self) -> DomainCommandResult[ResultT]:
        successful = self.status in {"applied", "idempotent_replay"}
        if successful and self.committed_revision is None:
            raise ValueError("successful command results require committed_revision")
        if successful and self.acknowledged_receive_seq is None:
            raise ValueError("successful command results require acknowledged_receive_seq")
        if self.status == "idempotent_replay" and not self.idempotency_hit:
            raise ValueError("idempotent_replay must set idempotency_hit")
        if not successful and not self.error:
            raise ValueError("non-success command results require error")
        return self
