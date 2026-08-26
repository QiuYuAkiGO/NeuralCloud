"""Simple process-owned single-instance lock file."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path


class InstanceLockError(RuntimeError):
    pass


class SingleInstanceLock:
    def __init__(self, path: Path | str, *, runtime_instance_id: str) -> None:
        self.path = Path(path)
        self.runtime_instance_id = runtime_instance_id
        self._owned = False

    @property
    def owned(self) -> bool:
        return self._owned

    def acquire(self) -> None:
        if self._owned:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "pid": os.getpid(),
                "runtime_instance_id": self.runtime_instance_id,
                "acquired_at": datetime.now(UTC).isoformat(),
            },
            sort_keys=True,
        ).encode("utf-8")
        try:
            descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError as exc:
            if not self._remove_stale_lock():
                raise InstanceLockError(f"runtime instance lock already exists: {self.path}") from exc
            try:
                descriptor = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            except FileExistsError as retry_exc:
                raise InstanceLockError(f"runtime instance lock already exists: {self.path}") from retry_exc
        try:
            os.write(descriptor, payload)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        self._owned = True

    def _remove_stale_lock(self) -> bool:
        try:
            original = self.path.read_bytes()
            payload = json.loads(original.decode("utf-8"))
            pid = int(payload["pid"])
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            pid = -1
            original = b""
        if pid > 0:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                pass
            except PermissionError:
                return False
            except OSError:
                pass
            else:
                return False
        try:
            if original and self.path.read_bytes() != original:
                return False
            self.path.unlink()
            return True
        except OSError:
            return False

    def release(self) -> None:
        if not self._owned:
            return
        try:
            self.path.unlink(missing_ok=True)
        finally:
            self._owned = False

    def __enter__(self) -> SingleInstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.release()
