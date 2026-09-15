"""Process and host resource sampling for the Runtime monitor projection."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict


ROLE_CMDLINE = {
    "cognitive": ("harness.cognitive", "harness.cognitive.service"),
    "homeostasis": ("harness.homeostasis", "harness.homeostasis.service"),
    "qq": ("harness.qq", "harness.qq.direct", "harness.qq.watcher"),
    "gui": ("gui.runtime_server", "gui.server", "gui.__main__"),
    "board": ("harness.board.service", "harness.board"),
}
_PYTHON_NAMES = {"python", "python.exe", "pythonw.exe", "python3", "python3.exe"}


class ResourceSample(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    observed_at: str
    role: str
    pid: int | None = None
    cpu_percent: float | None = None
    rss_bytes: int | None = None
    num_threads: int | None = None
    host_cpu_percent: float | None = None
    host_mem_percent: float | None = None
    available: bool = True
    detail: str | None = None


class ResourceSampler:
    """Sample this Runtime process and sibling NeuralCloud processes."""

    def __init__(
        self,
        *,
        pid_file: Path | str | None = None,
        state_dir: Path | str | None = None,
        self_role: str = "runtime",
    ) -> None:
        self.self_role = self_role
        self.state_dir = Path(state_dir) if state_dir is not None else None
        candidates = []
        if pid_file is not None:
            candidates.append(Path(pid_file))
        if self.state_dir is not None:
            candidates.append(self.state_dir.parent / ".neuralcloud.pids.json")
            candidates.append(self.state_dir / ".neuralcloud.pids.json")
        candidates.append(Path.cwd() / ".neuralcloud.pids.json")
        self.pid_candidates = candidates
        self._primed: set[int] = set()
        self._cmdline_cache: list[tuple[str, int]] = []
        self._cmdline_cache_at: float = 0.0

    def sample(self) -> list[ResourceSample]:
        observed_at = datetime.now(UTC).isoformat()
        try:
            import psutil
        except ImportError:
            return [ResourceSample(
                observed_at=observed_at, role="host", available=False, detail="psutil_unavailable",
            )]
        try:
            host_cpu = float(psutil.cpu_percent(interval=None))
            host_mem = float(psutil.virtual_memory().percent)
        except Exception as exc:
            return [ResourceSample(
                observed_at=observed_at, role="host", available=False, detail=type(exc).__name__,
            )]
        rows: list[ResourceSample] = []
        seen: set[int] = {os.getpid()}
        rows.append(self._process_row(
            psutil, os.getpid(), self.self_role, observed_at, host_cpu, host_mem,
        ))
        registry = self._registry_pids()
        for role, pid in registry:
            if pid in seen or pid <= 0:
                continue
            seen.add(pid)
            rows.append(self._process_row(psutil, pid, role, observed_at, host_cpu, host_mem))
        if not registry:
            for role, pid in self._cmdline_pids(psutil):
                if pid in seen:
                    continue
                seen.add(pid)
                rows.append(self._process_row(psutil, pid, role, observed_at, host_cpu, host_mem))
        return rows

    def _process_row(
        self, psutil: Any, pid: int, role: str, observed_at: str, host_cpu: float, host_mem: float,
    ) -> ResourceSample:
        try:
            process = psutil.Process(pid)
            if pid not in self._primed:
                process.cpu_percent(interval=None)
                self._primed.add(pid)
            cpu = float(process.cpu_percent(interval=None))
            memory = process.memory_info()
            threads = int(process.num_threads())
            return ResourceSample(
                observed_at=observed_at, role=role, pid=pid, cpu_percent=cpu,
                rss_bytes=int(memory.rss), num_threads=threads,
                host_cpu_percent=host_cpu, host_mem_percent=host_mem, available=True,
            )
        except Exception as exc:
            return ResourceSample(
                observed_at=observed_at, role=role, pid=pid, available=False,
                detail=type(exc).__name__, host_cpu_percent=host_cpu, host_mem_percent=host_mem,
            )

    def _registry_pids(self) -> list[tuple[str, int]]:
        found: list[tuple[str, int]] = []
        for path in self.pid_candidates:
            try:
                if not path.is_file() or path.is_symlink():
                    continue
                payload = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, json.JSONDecodeError):
                continue
            services = payload.get("services") if isinstance(payload, dict) else None
            if not isinstance(services, list):
                continue
            for item in services:
                if not isinstance(item, dict):
                    continue
                name = str(item.get("name") or "").strip().lower()
                try:
                    pid = int(item.get("pid"))
                except (TypeError, ValueError):
                    continue
                if name and pid > 0:
                    found.append((name, pid))
            if found:
                return found
        return found

    def _cmdline_pids(self, psutil: Any) -> list[tuple[str, int]]:
        now = time.monotonic()
        if self._cmdline_cache and now - self._cmdline_cache_at < 30:
            return list(self._cmdline_cache)
        found: list[tuple[str, int]] = []
        try:
            processes = list(psutil.process_iter(["pid", "name"]))
        except Exception:
            return found
        for process in processes:
            try:
                name = str(process.info.get("name") or "").lower()
                pid = int(process.info["pid"])
            except (TypeError, ValueError, KeyError):
                continue
            if name not in _PYTHON_NAMES:
                continue
            try:
                cmdline = " ".join(process.cmdline() or [])
            except Exception:
                continue
            for role, needles in ROLE_CMDLINE.items():
                if any(needle in cmdline for needle in needles):
                    found.append((role, pid))
                    break
        self._cmdline_cache = found
        self._cmdline_cache_at = now
        return list(found)
