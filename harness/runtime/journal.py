"""Hash-chained append-only JSONL journal used as Runtime's fact source."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from harness.runtime.models import RUNTIME_JOURNAL_SCHEMA_VERSION


class JournalError(RuntimeError):
    pass


class JournalCorruptionError(JournalError):
    pass


class JournalUnavailableError(JournalError):
    pass


class RuntimeJournalRecord(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    schema_version: Literal["runtime-journal-v1"] = RUNTIME_JOURNAL_SCHEMA_VERSION
    record_seq: int = Field(ge=1)
    record_type: str = Field(min_length=1, max_length=96)
    runtime_instance_id: str = Field(min_length=1, max_length=128)
    character_id: str = Field(min_length=1, max_length=128)
    manifest_revision: int = Field(ge=1)
    recorded_at: AwareDatetime
    event_id: str | None = None
    receive_seq: int | None = Field(default=None, ge=1)
    process_seq: int | None = Field(default=None, ge=1)
    correlation_id: str | None = None
    command_id: str | None = None
    idempotency_key: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)
    prev_hash: str = Field(pattern=r"^(?:[0-9a-f]{64})?$")
    checksum: str = Field(pattern=r"^[0-9a-f]{64}$")

    def hash_payload(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude={"checksum"})

    def verify_checksum(self) -> bool:
        return self.checksum == _payload_checksum(self.hash_payload())


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _payload_checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()


class RuntimeJournal:
    """A durable journal with a small bounded emergency buffer.

    ``path`` may be a JSONL filename or a directory. Directory paths use
    ``runtime-000001.jsonl`` so segment rotation can be added without changing
    recovery semantics.
    """

    def __init__(
        self,
        path: Path | str,
        *,
        runtime_instance_id: str,
        character_id: str,
        manifest_revision: int,
        fsync: bool = True,
        emergency_buffer_size: int = 128,
        max_segment_bytes: int = 64 * 1024 * 1024,
    ) -> None:
        raw_path = Path(path)
        self._directory_mode = not bool(raw_path.suffix)
        self._journal_dir = raw_path if self._directory_mode else raw_path.parent
        self.path = raw_path if not self._directory_mode else raw_path / "runtime-000001.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.runtime_instance_id = runtime_instance_id
        self.character_id = character_id
        self.manifest_revision = manifest_revision
        self.fsync = fsync
        self.emergency_buffer_size = emergency_buffer_size
        if max_segment_bytes < 1024:
            raise ValueError("max_segment_bytes must be at least 1024")
        self.max_segment_bytes = max_segment_bytes
        self._lock = threading.RLock()
        self._records: list[RuntimeJournalRecord] = []
        self._buffer: list[RuntimeJournalRecord] = []
        self._degraded = False
        self.last_append_at: datetime | None = None
        self.truncated_tail_bytes = 0
        self._recover()

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def buffered_records(self) -> int:
        return len(self._buffer)

    @property
    def last_record_seq(self) -> int:
        return self._records[-1].record_seq if self._records else 0

    @property
    def last_hash(self) -> str:
        return self._records[-1].checksum if self._records else ""

    def records(self) -> list[RuntimeJournalRecord]:
        with self._lock:
            return list(self._records)

    def append(
        self,
        record_type: str,
        *,
        event_id: str | None = None,
        receive_seq: int | None = None,
        process_seq: int | None = None,
        correlation_id: str | None = None,
        command_id: str | None = None,
        idempotency_key: str | None = None,
        data: dict[str, Any] | None = None,
        recorded_at: datetime | None = None,
    ) -> RuntimeJournalRecord:
        with self._lock:
            payload: dict[str, Any] = {
                "schema_version": RUNTIME_JOURNAL_SCHEMA_VERSION,
                "record_seq": self.last_record_seq + 1,
                "record_type": record_type,
                "runtime_instance_id": self.runtime_instance_id,
                "character_id": self.character_id,
                "manifest_revision": self.manifest_revision,
                "recorded_at": recorded_at or datetime.now(UTC),
                "event_id": event_id,
                "receive_seq": receive_seq,
                "process_seq": process_seq,
                "correlation_id": correlation_id,
                "command_id": command_id,
                "idempotency_key": idempotency_key,
                "data": data or {},
                "prev_hash": self.last_hash,
            }
            json_payload = RuntimeJournalRecord.model_validate(
                {**payload, "checksum": "0" * 64}
            ).model_dump(mode="json", exclude={"checksum"})
            record = RuntimeJournalRecord.model_validate(
                {**payload, "checksum": _payload_checksum(json_payload)}
            )
            self._records.append(record)

            if self._degraded:
                self._buffer_record(record)
                return record
            try:
                self._write_record(record)
            except OSError as exc:
                self._degraded = True
                self._buffer_record(record)
                raise JournalUnavailableError(f"journal append failed: {exc}") from exc
            self.last_append_at = record.recorded_at
            return record

    def try_recover_writes(self) -> bool:
        with self._lock:
            if not self._degraded:
                return True
            try:
                for record in self._buffer:
                    self._write_record(record)
            except OSError:
                return False
            if self._buffer:
                self.last_append_at = self._buffer[-1].recorded_at
            self._buffer.clear()
            self._degraded = False
            return True

    def flush(self) -> None:
        with self._lock:
            if self._degraded and not self.try_recover_writes():
                raise JournalUnavailableError("journal remains unavailable")
            with self.path.open("ab") as handle:
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())

    def _buffer_record(self, record: RuntimeJournalRecord) -> None:
        if len(self._buffer) >= self.emergency_buffer_size:
            raise JournalUnavailableError("journal emergency buffer is full")
        self._buffer.append(record)

    def _write_record(self, record: RuntimeJournalRecord) -> None:
        line = _canonical_json(record.model_dump(mode="json")) + "\n"
        self._rotate_if_needed(len(line.encode("utf-8")))
        start_size = self.path.stat().st_size if self.path.exists() else 0
        try:
            with self.path.open("ab") as handle:
                handle.write(line.encode("utf-8"))
                handle.flush()
                if self.fsync:
                    os.fsync(handle.fileno())
        except OSError:
            # Never leave a partially appended line ahead of the emergency buffer.
            try:
                with self.path.open("r+b") as handle:
                    handle.truncate(start_size)
                    handle.flush()
                    if self.fsync:
                        os.fsync(handle.fileno())
            except OSError:
                pass
            raise

    def _rotate_if_needed(self, incoming_bytes: int) -> None:
        if not self._directory_mode or not self.path.exists():
            return
        if self.path.stat().st_size == 0:
            return
        if self.path.stat().st_size + incoming_bytes <= self.max_segment_bytes:
            return
        segments = self._segment_paths()
        next_index = max(int(segment.stem.rsplit("-", 1)[-1]) for segment in segments) + 1
        self.path = self._journal_dir / f"runtime-{next_index:06d}.jsonl"
        self.path.touch(exist_ok=False)

    def _segment_paths(self) -> list[Path]:
        if not self._directory_mode:
            return [self.path] if self.path.exists() else []
        return sorted(self._journal_dir.glob("runtime-*.jsonl"))

    def _recover(self) -> None:
        paths = self._segment_paths()
        if not paths:
            self.path.touch()
            return
        recovered: list[RuntimeJournalRecord] = []
        previous_hash = ""
        for path_index, segment_path in enumerate(paths):
            self.path = segment_path
            raw = segment_path.read_bytes()
            chunks = raw.splitlines(keepends=True)
            offset = 0
            for index, chunk in enumerate(chunks):
                is_final_line = path_index == len(paths) - 1 and index == len(chunks) - 1
                complete_line = chunk.endswith(b"\n") or chunk.endswith(b"\r")
                if is_final_line and not complete_line:
                    self._truncate_tail(offset, len(raw) - offset)
                    break
                if not complete_line:
                    raise JournalCorruptionError(
                        f"incomplete record in non-final segment {segment_path.name}"
                    )
                try:
                    payload = json.loads(chunk.decode("utf-8"))
                    record = RuntimeJournalRecord.model_validate(payload)
                except Exception as exc:
                    raise JournalCorruptionError(
                        f"invalid journal record in {segment_path.name} at byte {offset}"
                    ) from exc
                if record.record_seq != len(recovered) + 1:
                    raise JournalCorruptionError(f"non-monotonic record sequence at {record.record_seq}")
                if record.prev_hash != previous_hash:
                    raise JournalCorruptionError(f"broken journal hash chain at record {record.record_seq}")
                if not record.verify_checksum():
                    raise JournalCorruptionError(f"checksum mismatch at record {record.record_seq}")
                recovered.append(record)
                previous_hash = record.checksum
                offset += len(chunk)
        self.path = paths[-1]
        self._records = recovered
        if recovered:
            self.last_append_at = recovered[-1].recorded_at

    def _truncate_tail(self, offset: int, byte_count: int) -> None:
        with self.path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self.truncated_tail_bytes = byte_count
