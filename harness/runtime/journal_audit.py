"""Read-only journal diagnostics. Deliberately has no Runtime control port."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from contextlib import suppress
from datetime import UTC, datetime
from pathlib import Path

from harness.files import replace_path
from harness.runtime.journal import JournalCorruptionError, RuntimeJournal

log = logging.getLogger(__name__)


class JournalAudit:
    def __init__(self, journal, path, *, bytes_per_second=16 * 1024 * 1024, duty_cycle=0.1, interval=86400):
        if bytes_per_second <= 0 or not 0 < duty_cycle <= 1 or interval <= 0:
            raise ValueError("invalid journal audit resource limits")
        self.journal, self.path = journal, Path(path)
        self.bytes_per_second, self.duty_cycle, self.interval = bytes_per_second, duty_cycle, interval
        self._status = {"state": "not_started"}
        self._task = None

    def status(self):
        return dict(self._status)

    def start(self, *, delay=0):
        if self._task is None:
            self._status["state"] = "scheduled"
            self._task = asyncio.create_task(self._run(delay), name="journal-readonly-audit")

    async def close(self):
        if self._task is not None:
            self._task.cancel()
            with suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    def _save(self):
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(self._status, handle, ensure_ascii=False)
                handle.flush()
                os.fsync(handle.fileno())
            replace_path(temporary, self.path)
        except Exception:
            log.exception("journal audit diagnostic write failed; result=%s", self._status)

    def _batches(self, paths, boundary, anchor):
        previous, sequence, batch_bytes, count = "", 0, 0, 0
        for path in paths:
            with path.open("rb") as handle:
                while sequence < boundary:
                    offset = handle.tell()
                    chunk = handle.readline()
                    if not chunk:
                        break
                    self._status.update(path=str(path), offset=offset, record_seq=sequence + 1)
                    record = RuntimeJournal._decode_record(chunk, path, offset)
                    RuntimeJournal._verify_record(record, sequence + 1, previous)
                    if anchor and record.record_seq == anchor["seq"] and record.checksum != anchor["checksum"]:
                        raise JournalCorruptionError(f"audit checkpoint mismatch at {record.record_seq}")
                    previous, sequence = record.checksum, record.record_seq
                    batch_bytes += len(chunk)
                    count += 1
                    self._status["verified_seq"] = sequence
                    if count == 1000:
                        yield batch_bytes
                        batch_bytes, count = 0, 0
            if sequence == boundary:
                break
        if sequence != boundary:
            raise JournalCorruptionError(f"missing journal record {sequence + 1}")
        if count:
            yield batch_bytes

    async def run_once(self):
        with self.journal._lock:
            boundary = self.journal._disk_last_record_seq
            paths = self.journal._segment_paths()
            store = self.journal.recovery_store
            anchor = dict(store.anchor) if store and store.anchor else None
        self._status = dict(
            state="running", boundary_seq=boundary, verified_seq=0, started_at=datetime.now(UTC).isoformat()
        )
        scanner = self._batches(paths, boundary, anchor)
        try:
            while True:
                started = time.perf_counter()
                work = asyncio.create_task(asyncio.to_thread(next, scanner, None))
                try:
                    size = await asyncio.shield(work)
                except asyncio.CancelledError:
                    # Let the bounded read release its handle before closing
                    # the generator. Cancellation never reaches the journal.
                    with suppress(Exception):
                        await work
                    raise
                elapsed = time.perf_counter() - started
                if size is None:
                    break
                self._save()
                await asyncio.sleep(max(size / self.bytes_per_second - elapsed, elapsed * (1 / self.duty_cycle - 1), 0))
            self._status["state"] = "passed"
        except asyncio.CancelledError:
            self._status["state"] = "cancelled"
            raise
        except JournalCorruptionError as exc:
            self._status.update(state="corrupt", error=str(exc), error_type=type(exc).__name__)
            log.error("journal audit found corruption: %s", self._status)
        except Exception as exc:
            self._status.update(state="audit_error", error=str(exc), error_type=type(exc).__name__)
            log.exception("journal audit failed")
        finally:
            scanner.close()
            self._status["finished_at"] = datetime.now(UTC).isoformat()
            self._save()
        return self.status()

    async def _run(self, delay=0):
        if delay:
            await asyncio.sleep(delay)
        retries = 0
        while True:
            try:
                result = await self.run_once()
                if result["state"] == "audit_error":
                    delay = (30, 120, 600)[min(retries, 2)]
                    retries += 1
                else:
                    retries, delay = 0, self.interval
            except asyncio.CancelledError:
                raise
            except Exception:
                # Even failures in reporting/scheduling stay diagnostic-only.
                log.exception("journal audit task failed")
                delay = 600
            await asyncio.sleep(delay)
