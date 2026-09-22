"""Hash-chained append-only JSONL journal used as Runtime's fact source."""

from __future__ import annotations

import hashlib
import json
import os
import threading
from bisect import bisect_right
from collections import deque
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NamedTuple

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from harness.runtime.models import RUNTIME_JOURNAL_SCHEMA_VERSION


class JournalError(RuntimeError):
    pass


class JournalCorruptionError(JournalError):
    pass


class JournalUnavailableError(JournalError):
    pass


class _RecordLocation(NamedTuple):
    record_seq: int
    path: Path
    offset: int
    prev_hash: str


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
    ``runtime-000001.jsonl`` segments. All history remains on disk, while only
    a bounded recent payload tail and sparse seek metadata remain resident.
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
        recent_record_limit: int = 512,
        index_stride: int = 128,
        recovery_store=None,
        recovery_mode: str = "strict",
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
        if emergency_buffer_size < 1 or recent_record_limit < 1 or index_stride < 1:
            raise ValueError("journal buffer, recent record limit and index stride must be positive")
        if max_segment_bytes < 1024:
            raise ValueError("max_segment_bytes must be at least 1024")
        self.max_segment_bytes = max_segment_bytes
        self._lock = threading.RLock()
        self._records: deque[RuntimeJournalRecord] = deque(maxlen=recent_record_limit)
        self._index_stride = index_stride
        self._index: list[_RecordLocation] = []
        self._index_seqs: list[int] = []
        self._paths: list[Path] = []
        self._path_positions: dict[Path, int] = {}
        self._disk_last_record_seq = 0
        self._pending_truncation: tuple[Path, int] | None = None
        self._buffer: list[RuntimeJournalRecord] = []
        self._degraded = False
        self.last_append_at: datetime | None = None
        self.truncated_tail_bytes = 0
        self.recovery_store = recovery_store
        self.recovered_records = 0
        if recovery_store is not None:
            recovery_store.journal = self
        anchor = None
        if recovery_store is not None and recovery_mode == "fast" and recovery_store.anchor:
            try:
                anchor = self._validate_anchor(recovery_store.anchor)
            except (OSError, ValueError, KeyError, TypeError, JournalCorruptionError) as exc:
                recovery_store.fallback_reason = f"invalid recovery anchor: {exc}"
        if anchor is None:
            if recovery_store is not None:
                if recovery_mode == "fast" and not recovery_store.fallback_reason:
                    recovery_store.fallback_reason = "no compatible recovery anchor"
                recovery_store.reset()
            self._recover()
        else:
            recovery_store.mode = "fast"
            self._recover(anchor=anchor)

    @property
    def degraded(self) -> bool:
        return self._degraded

    @property
    def buffered_records(self) -> int:
        return len(self._buffer)

    @property
    def retained_records(self) -> int:
        """Number of payloads in the bounded recent tail (not total history)."""
        return len(self._records)

    @property
    def last_record_seq(self) -> int:
        return self._records[-1].record_seq if self._records else 0

    @property
    def last_hash(self) -> str:
        return self._records[-1].checksum if self._records else ""

    def records(self) -> list[RuntimeJournalRecord]:
        """Compatibility API: explicitly materialize all history when requested."""
        return list(self.iter_records())

    def iter_records(self, *, reverse: bool = False) -> Iterator[RuntimeJournalRecord]:
        """Iterate a stable prefix with bounded pages, newest first if requested.

        The iterator holds no lock across a yield. Concurrent appends are not
        included once iteration starts, and callers can stop a reverse search
        after a recent match without reading the complete historical payload.
        """
        with self._lock:
            boundary = self.last_record_seq
        page_size = 512
        if reverse:
            end = boundary
            while end:
                start = max(0, end - page_size)
                yield from reversed(self.records_after(start, limit=end - start))
                end = start
        else:
            cursor = 0
            while cursor < boundary:
                page = self.records_after(cursor, limit=min(page_size, boundary - cursor))
                if not page:
                    raise JournalCorruptionError(f"missing journal records after {cursor}")
                yield from page
                cursor = page[-1].record_seq

    def records_after(self, record_seq: int, *, limit: int = 500) -> list[RuntimeJournalRecord]:
        """Read a bounded page from any historical cursor, including old segments."""

        if record_seq < 0:
            raise ValueError("record_seq must be non-negative")
        page_size = max(1, min(5000, int(limit)))
        with self._lock:
            end = min(self.last_record_seq, record_seq + page_size)
            if record_seq >= end:
                return []
            if self._records and record_seq + 1 >= self._records[0].record_seq:
                return [record for record in self._records if record_seq < record.record_seq <= end]
            result = self._read_disk_page(record_seq + 1, min(end, self._disk_last_record_seq))
            result.extend(record for record in self._buffer if record_seq < record.record_seq <= end)
            if len(result) != end - record_seq:
                raise JournalCorruptionError(f"missing journal records after {record_seq}")
            return result

    def _read_disk_page(self, first: int, last: int) -> list[RuntimeJournalRecord]:
        if first > last:
            return []
        slot = bisect_right(self._index_seqs, first) - 1
        if slot >= 0:
            anchor = self._index[slot]
        elif self.recovery_store is not None:
            row = self.recovery_store.locate(first)
            if row is None:
                raise JournalCorruptionError(f"missing seek anchor for {first}")
            anchor = _RecordLocation(row[0], Path(row[1]), row[2], row[3])
        else:
            raise JournalCorruptionError(f"missing seek anchor for {first}")
        path_index = self._path_positions[anchor.path]
        offset = anchor.offset
        sequence = anchor.record_seq
        previous_hash = anchor.prev_hash
        page: list[RuntimeJournalRecord] = []
        while sequence <= last and path_index < len(self._paths):
            path = self._paths[path_index]
            with path.open("rb") as handle:
                handle.seek(offset)
                while sequence <= last:
                    position = handle.tell()
                    chunk = handle.readline()
                    if not chunk:
                        break
                    record = self._decode_record(chunk, path, position)
                    self._verify_record(record, sequence, previous_hash)
                    previous_hash = record.checksum
                    if sequence >= first:
                        page.append(record)
                    sequence += 1
            path_index += 1
            offset = 0
        if sequence <= last:
            raise JournalCorruptionError(f"missing journal record {sequence}")
        return page

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
            if self._degraded and len(self._buffer) >= self.emergency_buffer_size:
                # Rejected appends must not advance the in-memory hash chain.
                raise JournalUnavailableError("journal emergency buffer is full")
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
                self._repair_failed_append()
                while self._buffer:
                    record = self._buffer[0]
                    self._write_record(record)
                    self._buffer.pop(0)
                    self.last_append_at = record.recorded_at
            except OSError:
                return False
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
        self._repair_failed_append()
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
            self._pending_truncation = (self.path, start_size)
            try:
                self._repair_failed_append()
            except OSError:
                pass
            raise
        self._index_record(record, self.path, start_size, start_size + len(line.encode("utf-8")))

    def _repair_failed_append(self) -> None:
        if self._pending_truncation is None:
            return
        path, offset = self._pending_truncation
        with path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self._pending_truncation = None

    def _index_record(self, record: RuntimeJournalRecord, path: Path, offset: int, end: int | None = None) -> None:
        if path not in self._path_positions:
            self._path_positions[path] = len(self._paths)
            self._paths.append(path)
        if self.recovery_store is None and (offset == 0 or (record.record_seq - 1) % self._index_stride == 0):
            self._index.append(_RecordLocation(record.record_seq, path, offset, record.prev_hash))
            self._index_seqs.append(record.record_seq)
        self._disk_last_record_seq = record.record_seq
        if self.recovery_store is not None:
            # Compute the actual byte boundary (canonical appends and recovered
            # noncanonical JSON lines can have different encoded lengths).
            self.recovery_store.observe(record, path, offset, end)

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

    def _validate_anchor(self, anchor):
        paths = self._segment_paths()
        path = Path(anchor['path'])
        if path not in paths or anchor['seq'] != self.recovery_store.projector.cursor:
            raise ValueError("anchor is not in this journal")
        with paths[0].open('rb') as handle:
            genesis = self._decode_record(handle.readline(), paths[0], 0)
            self._verify_record(genesis, 1, '')
        if genesis.checksum != self.recovery_store.projector.state.get('journal_genesis'):
            raise ValueError("journal identity changed")
        with path.open('rb') as handle:
            handle.seek(anchor['offset'])
            record = self._decode_record(handle.readline(), path, anchor['offset'])
            if handle.tell() != anchor['end'] or record.record_seq != anchor['seq']:
                raise ValueError("anchor position changed")
            if record.checksum != anchor['checksum'] or not record.verify_checksum():
                raise ValueError("anchor checksum changed")
        return anchor, record

    def _recover(self, *, anchor=None) -> None:
        paths = self._segment_paths()
        if not paths:
            self.path.touch()
            return
        previous_hash = ""
        start_index, start_offset = 0, 0
        if anchor is not None:
            saved, record = anchor
            start_index = paths.index(Path(saved['path']))
            start_offset = saved['end']
            previous_hash = record.checksum
            self._disk_last_record_seq = record.record_seq
            self._records.append(record)
            self._paths = list(paths)
            self._path_positions = {path: i for i, path in enumerate(paths)}
        for path_index, segment_path in enumerate(paths):
            if path_index < start_index:
                continue
            self.path = segment_path
            with segment_path.open("rb") as handle:
                if path_index == start_index:
                    handle.seek(start_offset)
                while True:
                    offset = handle.tell()
                    chunk = handle.readline()
                    if not chunk:
                        break
                    complete_line = chunk.endswith(b"\n") or chunk.endswith(b"\r")
                    if not complete_line:
                        if path_index == len(paths) - 1:
                            self._truncate_tail(offset, len(chunk))
                            break
                        raise JournalCorruptionError(
                            f"incomplete record in non-final segment {segment_path.name}"
                        )
                    record = self._decode_record(chunk, segment_path, offset)
                    self._verify_record(record, self._disk_last_record_seq + 1, previous_hash)
                    self._records.append(record)
                    self._index_record(record, segment_path, offset, handle.tell())
                    self.recovered_records += 1
                    previous_hash = record.checksum
        self.path = paths[-1]
        if self._records:
            self.last_append_at = self._records[-1].recorded_at

    @staticmethod
    def _decode_record(chunk: bytes, path: Path, offset: int) -> RuntimeJournalRecord:
        if not (chunk.endswith(b"\n") or chunk.endswith(b"\r")):
            raise JournalCorruptionError(f"incomplete journal record in {path.name} at byte {offset}")
        try:
            return RuntimeJournalRecord.model_validate(json.loads(chunk.decode("utf-8")))
        except Exception as exc:
            raise JournalCorruptionError(f"invalid journal record in {path.name} at byte {offset}") from exc

    @staticmethod
    def _verify_record(record: RuntimeJournalRecord, expected_seq: int, previous_hash: str) -> None:
        if record.record_seq != expected_seq:
            raise JournalCorruptionError(f"non-monotonic record sequence at {record.record_seq}")
        if record.prev_hash != previous_hash:
            raise JournalCorruptionError(f"broken journal hash chain at record {record.record_seq}")
        if not record.verify_checksum():
            raise JournalCorruptionError(f"checksum mismatch at record {record.record_seq}")

    def _truncate_tail(self, offset: int, byte_count: int) -> None:
        with self.path.open("r+b") as handle:
            handle.truncate(offset)
            handle.flush()
            if self.fsync:
                os.fsync(handle.fileno())
        self.truncated_tail_bytes = byte_count
