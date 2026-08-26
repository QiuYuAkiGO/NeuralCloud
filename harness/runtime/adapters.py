"""Transport-neutral domain service ports and in-process reference adapters."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from harness.contracts.decisions import (
    ActionOutcomeEvidence,
    CommitEvidence,
    DecisionEvidence,
    DeliveryEvidence,
    NoActionDecision,
    PreparedResponse,
    ReplyDecision,
)
from harness.contracts.envelope import (
    CONTRACT_SCHEMA_VERSION,
    DomainCommandEnvelope,
    DomainCommandResult,
    VersionVector,
)
from harness.core.inference import FinalResponse, SessionTurn
from harness.core.models import InteractionEvent
from harness.pipeline.pipeline import InferencePipeline


class DomainSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: str = CONTRACT_SCHEMA_VERSION
    service: str
    revision: int = Field(ge=0)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    acknowledged_event_id: str | None = None
    component_revisions: dict[str, int] = Field(default_factory=dict)
    payload: dict[str, Any] = Field(default_factory=dict)


class ServiceHealth(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    service: str
    healthy: bool = True
    schema_version: str = CONTRACT_SCHEMA_VERSION
    revision: int = Field(default=0, ge=0)
    acknowledged_receive_seq: int = Field(default=0, ge=0)
    detail: dict[str, Any] = Field(default_factory=dict)


class CognitiveServiceClient(Protocol):
    async def health(self) -> ServiceHealth: ...

    async def get_snapshot(self) -> DomainSnapshot: ...

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]: ...

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def pause_maintenance(self, *, timeout: float) -> bool: ...

    async def resume_maintenance(self) -> None: ...

    async def ui_snapshot(self) -> dict[str, Any]: ...

    async def control(self, operation: str, payload: dict[str, Any] | None = None) -> dict[str, Any]: ...

    async def close(self) -> None: ...


class HomeostasisServiceClient(Protocol):
    async def health(self) -> ServiceHealth: ...

    async def get_snapshot(self) -> DomainSnapshot: ...

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]: ...

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]: ...

    async def close(self) -> None: ...


class _PreparedEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event: InteractionEvent
    final: FinalResponse
    prepared: PreparedResponse


def _utc_now() -> datetime:
    return datetime.now(UTC)


class PipelineCognitiveAdapter:
    """Idempotent PrepareResponse/CommitInteraction boundary for M2 Pipeline.

    The optional state file persists the command ledger, prepared results,
    revisions, acknowledged cursor and Session projection. Memory durability is
    still owned by the configured ``MemoryService`` backend.
    """

    def __init__(
        self,
        pipeline: InferencePipeline,
        *,
        character_id: str = "character",
        state_path: Path | str | None = None,
    ) -> None:
        self.pipeline = pipeline
        self.character_id = character_id
        self.state_path = Path(state_path) if state_path is not None else None
        self._lock = asyncio.Lock()
        self._revision = 0
        self._cursor = 0
        self._event_id: str | None = None
        self._component_revisions = {
            "world": 0,
            "relationship": 0,
            "memory": 0,
            "session": 0,
            "maintenance": 0,
        }
        self._ledger: dict[tuple[str, str], DomainCommandResult[Any]] = {}
        self._request_hashes: dict[tuple[str, str], str] = {}
        self._prepared: dict[str, _PreparedEntry] = {}
        self._maintenance_paused = False
        self._load_state()

    async def health(self) -> ServiceHealth:
        async with self._lock:
            return ServiceHealth(
                service="cognitive",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                detail={"maintenance_paused": self._maintenance_paused},
            )

    async def get_snapshot(self) -> DomainSnapshot:
        async with self._lock:
            return DomainSnapshot(
                service="cognitive",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                acknowledged_event_id=self._event_id,
                component_revisions=dict(self._component_revisions),
                payload={"prepared_count": len(self._prepared)},
            )

    async def prepare_response(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[PreparedResponse]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                self._persist_state()
                return DomainCommandResult[PreparedResponse].model_validate(replay.model_dump(mode="json"))
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[PreparedResponse].model_validate(rejected.model_dump(mode="json"))

            payload = command.payload
            observer = getattr(self.pipeline, "observer", None)
            bind = getattr(observer, "bind", None)
            if callable(bind):
                bind(str(payload.get("call_id") or command.correlation_id))
            source_time = payload.get("source_time") or command.issued_at
            autonomous = payload.get("mode") == "autonomous"
            event = InteractionEvent(
                event_id=command.event_id,
                timestamp=source_time,
                channel=str(payload.get("channel") or "chat"),
                speaker=str(payload.get("speaker") or ("runtime" if autonomous else "user")),
                modality="text",
                raw_text=str(payload.get("text") or ("（自主状态评估）" if autonomous else "")),
                session_id=str(payload.get("session_id") or "sess_default"),
            )
            try:
                final = await asyncio.to_thread(
                    self.pipeline.prepare,
                    event,
                    now=command.issued_at,
                    call_id=payload.get("call_id"),
                )
                now = _utc_now()
                read_versions = command.version_vector or self._version_vector()
                decision = (
                    ReplyDecision(
                        text=final.text,
                        segments=list(final.segments) or [final.text],
                        channel=str(payload.get("channel") or "chat"),
                    )
                    if final.text.strip()
                    else NoActionDecision(reason="cognitive pipeline produced no reply")
                )
                prepared = PreparedResponse(
                    event_id=command.event_id,
                    correlation_id=command.correlation_id,
                    session_id=event.session_id,
                    decision=decision,
                    read_versions=read_versions,
                    prepared_at=now,
                    expires_at=command.deadline,
                    model_version=self.pipeline.settings.model_version,
                    prompt_version=self.pipeline.settings.prompt_version,
                )
            except Exception as exc:
                result = self._failure(command, str(exc))
                self._remember(command, result)
                self._persist_state()
                return DomainCommandResult[PreparedResponse].model_validate(result.model_dump(mode="json"))

            self._prepared[prepared.prepare_id] = _PreparedEntry(
                event=event,
                final=final,
                prepared=prepared,
            )
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[PreparedResponse](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=command.receive_seq,
                result=prepared,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            self._persist_state()
            return result

    async def commit_interaction(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            replay = self._lookup_replay(command)
            if replay is not None:
                self._persist_state()
                return DomainCommandResult[dict[str, Any]].model_validate(replay.model_dump(mode="json"))
            rejected = self._preflight(command)
            if rejected is not None:
                return DomainCommandResult[dict[str, Any]].model_validate(rejected.model_dump(mode="json"))

            prepare_id = str(command.payload.get("prepare_id") or "")
            entry = self._prepared.get(prepare_id)
            if entry is None:
                result = self._failure(command, f"unknown prepare_id: {prepare_id}", status="rejected")
                self._remember(command, result)
                return result
            try:
                evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence = TypeAdapter(
                    CommitEvidence
                ).validate_python(command.payload.get("evidence"))
            except Exception as exc:
                result = self._failure(command, f"invalid commit evidence: {exc}", status="rejected")
                self._remember(command, result)
                return result
            if evidence.prepare_id != prepare_id:
                result = self._failure(command, "evidence prepare_id mismatch", status="rejected")
                self._remember(command, result)
                return result

            self._commit_entry(entry, evidence)
            self._revision += 1
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            self._component_revisions["session"] = self._revision
            self._component_revisions["memory"] = self._revision
            payload = {
                "prepare_id": prepare_id,
                "evidence_kind": evidence.evidence_kind,
                "revision": self._revision,
            }
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=command.effective_request_hash(),
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result=payload,
                completed_at=_utc_now(),
            )
            self._remember(command, result)
            self._persist_state()
            return result

    async def pause_maintenance(self, *, timeout: float) -> bool:
        self._maintenance_paused = True
        drain = getattr(self.pipeline.memory, "drain_maintenance", None)
        if not callable(drain):
            return True
        try:
            if inspect.iscoroutinefunction(drain):
                result = await asyncio.wait_for(drain(timeout=timeout), timeout=timeout)
            else:
                result = await asyncio.wait_for(
                    asyncio.to_thread(drain, timeout=timeout),
                    timeout=timeout,
                )
            return bool(result)
        except Exception:
            return False

    async def resume_maintenance(self) -> None:
        self._maintenance_paused = False
        resume = getattr(self.pipeline.memory, "resume_maintenance", None)
        if callable(resume):
            if inspect.iscoroutinefunction(resume):
                await resume()
            else:
                await asyncio.to_thread(resume)

    async def close(self) -> None:
        self._persist_state()
        close = getattr(self.pipeline.memory, "close", None)
        if callable(close):
            await asyncio.to_thread(close)

    def _commit_entry(
        self,
        entry: _PreparedEntry,
        evidence: DeliveryEvidence | ActionOutcomeEvidence | DecisionEvidence,
    ) -> None:
        event = entry.event
        session = self.pipeline.session_store
        if not session.contains_event(event.session_id, event.event_id):
            session.append(
                event.session_id,
                "user",
                event.raw_text,
                speaker=event.speaker,
                event_id=event.event_id,
                timestamp=event.timestamp,
            )
        if self.pipeline.memory.stores.episodic.get(event.event_id) is None:
            self.pipeline.memory.record_event(event, generate_insight=True, curate=True, now=event.timestamp)

        if isinstance(evidence, DeliveryEvidence) and evidence.success:
            for index, segment in enumerate(evidence.delivered_segments, start=1):
                segment_event_id = f"{entry.final.response_id}:s{index}"
                if not session.contains_event(event.session_id, segment_event_id):
                    session.append(
                        event.session_id,
                        "character",
                        segment,
                        speaker=self.pipeline._speaker_label("character"),
                        event_id=segment_event_id,
                        timestamp=evidence.delivered_at,
                    )
            reply_event_id = f"{entry.final.response_id}:delivered"
            reply = InteractionEvent(
                event_id=reply_event_id,
                timestamp=evidence.delivered_at,
                channel=event.channel,
                speaker="character",
                modality=event.modality,
                raw_text="\n\n".join(evidence.delivered_segments),
                session_id=event.session_id,
                evidence=[evidence.external_ref or f"delivery:{evidence.delivery_id}"],
            )
            if self.pipeline.memory.stores.episodic.get(reply_event_id) is None:
                self.pipeline.memory.record_event(
                    reply,
                    generate_insight=False,
                    curate=False,
                    now=evidence.delivered_at,
                )

    def _lookup_replay(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any] | None:
        key = (command.command_type, command.idempotency_key)
        previous = self._ledger.get(key)
        if previous is None:
            return None
        if self._request_hashes[key] != command.effective_request_hash():
            return self._failure(command, "idempotency key reused with different request", status="conflict")
        return previous.model_copy(
            update={"status": "idempotent_replay", "idempotency_hit": True}
        )

    def _preflight(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[Any] | None:
        if command.is_expired(now=_utc_now()):
            return self._failure(command, "command deadline expired", status="rejected")
        if command.expected_revision is not None and command.expected_revision != self._revision:
            return self._failure(
                command,
                f"expected revision {command.expected_revision}, current {self._revision}",
                status="conflict",
            )
        return None

    def _failure(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        error: str,
        *,
        status: Literal["failed", "rejected", "conflict"] = "failed",
    ) -> DomainCommandResult[Any]:
        return DomainCommandResult[Any](
            command_id=command.command_id,
            command_type=command.command_type,
            status=status,
            request_hash=command.effective_request_hash(),
            error=error,
            completed_at=_utc_now(),
        )

    def _remember(
        self,
        command: DomainCommandEnvelope[dict[str, Any]],
        result: DomainCommandResult[Any],
    ) -> None:
        key = (command.command_type, command.idempotency_key)
        self._ledger[key] = result
        self._request_hashes[key] = command.effective_request_hash()

    def _version_vector(self) -> VersionVector:
        return VersionVector(
            cognitive_world=self._component_revisions["world"],
            cognitive_relationship=self._component_revisions["relationship"],
            cognitive_memory=self._component_revisions["memory"],
            cognitive_session=self._component_revisions["session"],
            cognitive_maintenance=self._component_revisions["maintenance"],
        )

    def _persist_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": self._revision,
            "cursor": self._cursor,
            "event_id": self._event_id,
            "component_revisions": self._component_revisions,
            "request_hashes": {f"{key[0]}\0{key[1]}": value for key, value in self._request_hashes.items()},
            "ledger": {
                f"{key[0]}\0{key[1]}": value.model_dump(mode="json") for key, value in self._ledger.items()
            },
            "prepared": {key: value.model_dump(mode="json") for key, value in self._prepared.items()},
            "sessions": {
                key: [turn.model_dump(mode="json") for turn in turns]
                for key, turns in self.pipeline.session_store.snapshot().items()
            },
        }
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._revision = int(payload.get("revision", 0))
        self._cursor = int(payload.get("cursor", 0))
        self._event_id = payload.get("event_id")
        self._component_revisions.update(payload.get("component_revisions") or {})
        for encoded, request_hash in (payload.get("request_hashes") or {}).items():
            self._request_hashes[tuple(encoded.split("\0", 1))] = request_hash
        for encoded, result in (payload.get("ledger") or {}).items():
            self._ledger[tuple(encoded.split("\0", 1))] = DomainCommandResult[Any].model_validate(result)
        self._prepared = {
            key: _PreparedEntry.model_validate(value)
            for key, value in (payload.get("prepared") or {}).items()
        }
        sessions = {
            key: [SessionTurn.model_validate(turn) for turn in turns]
            for key, turns in (payload.get("sessions") or {}).items()
        }
        self.pipeline.session_store.restore(sessions)


class PassiveHomeostasisAdapter:
    """Passive monotonic reference service used until the V2 engine is supplied."""

    def __init__(
        self,
        *,
        policies: list[dict[str, Any]] | None = None,
        state_path: Path | str | None = None,
    ) -> None:
        self._revision = 0
        self._cursor = 0
        self._event_id: str | None = None
        self._policies = list(policies or [])
        self.state_path = Path(state_path) if state_path is not None else None
        self._ledger: dict[tuple[str, str], tuple[str, DomainCommandResult[dict[str, Any]]]] = {}
        self._lock = asyncio.Lock()
        self._load_state()

    async def health(self) -> ServiceHealth:
        async with self._lock:
            return ServiceHealth(
                service="homeostasis",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
            )

    async def get_snapshot(self) -> DomainSnapshot:
        async with self._lock:
            return DomainSnapshot(
                service="homeostasis",
                revision=self._revision,
                acknowledged_receive_seq=self._cursor,
                acknowledged_event_id=self._event_id,
                component_revisions={"state": self._revision},
                payload={"policies": self._policies},
            )

    async def apply_event(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_heartbeat(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def apply_outcome(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        return await self._apply(command)

    async def propose_policies(self, *, version_vector: VersionVector) -> list[dict[str, Any]]:
        return list(self._policies)

    async def close(self) -> None:
        self._persist_state()

    async def _apply(
        self, command: DomainCommandEnvelope[dict[str, Any]]
    ) -> DomainCommandResult[dict[str, Any]]:
        async with self._lock:
            key = (command.command_type, command.idempotency_key)
            request_hash = command.effective_request_hash()
            previous = self._ledger.get(key)
            if previous is not None:
                if previous[0] != request_hash:
                    return DomainCommandResult[dict[str, Any]](
                        command_id=command.command_id,
                        command_type=command.command_type,
                        status="conflict",
                        request_hash=request_hash,
                        error="idempotency key reused with different request",
                        completed_at=_utc_now(),
                    )
                replay = previous[1].model_copy(
                    update={"status": "idempotent_replay", "idempotency_hit": True}
                )
                self._persist_state()
                return replay
            if command.is_expired(now=_utc_now()):
                return DomainCommandResult[dict[str, Any]](
                    command_id=command.command_id,
                    command_type=command.command_type,
                    status="rejected",
                    request_hash=request_hash,
                    error="command deadline expired",
                    completed_at=_utc_now(),
                )
            if command.expected_revision is not None and command.expected_revision != self._revision:
                return DomainCommandResult[dict[str, Any]](
                    command_id=command.command_id,
                    command_type=command.command_type,
                    status="conflict",
                    request_hash=request_hash,
                    error=f"expected revision {command.expected_revision}, current {self._revision}",
                    completed_at=_utc_now(),
                )
            self._revision += 1
            self._cursor = max(self._cursor, command.receive_seq)
            self._event_id = command.event_id
            result = DomainCommandResult[dict[str, Any]](
                command_id=command.command_id,
                command_type=command.command_type,
                status="applied",
                request_hash=request_hash,
                committed_revision=self._revision,
                acknowledged_event_id=command.event_id,
                acknowledged_receive_seq=self._cursor,
                result={"revision": self._revision, "policies": list(self._policies)},
                completed_at=_utc_now(),
            )
            self._ledger[key] = (request_hash, result)
            self._persist_state()
            return result

    def _persist_state(self) -> None:
        if self.state_path is None:
            return
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "revision": self._revision,
            "cursor": self._cursor,
            "event_id": self._event_id,
            "policies": self._policies,
            "ledger": {
                f"{key[0]}\0{key[1]}": {
                    "request_hash": request_hash,
                    "result": result.model_dump(mode="json"),
                }
                for key, (request_hash, result) in self._ledger.items()
            },
        }
        temporary = self.state_path.with_name(f".{self.state_path.name}.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, self.state_path)

    def _load_state(self) -> None:
        if self.state_path is None or not self.state_path.exists():
            return
        payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        self._revision = int(payload.get("revision", 0))
        self._cursor = int(payload.get("cursor", 0))
        self._event_id = payload.get("event_id")
        if "policies" in payload:
            self._policies = list(payload["policies"])
        for encoded, entry in (payload.get("ledger") or {}).items():
            key = tuple(encoded.split("\0", 1))
            self._ledger[key] = (
                entry["request_hash"],
                DomainCommandResult[dict[str, Any]].model_validate(entry["result"]),
            )
