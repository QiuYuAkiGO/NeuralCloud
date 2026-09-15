"""JSONL trajectory/operation recorder. Schema version is stamped on every line."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from harness.config import SCHEMA_VERSION, Settings, get_settings


class Tracer(Protocol):
    def record(self, kind: str, payload: dict[str, Any]) -> None: ...


class NullTracer:
    def record(self, kind: str, payload: dict[str, Any]) -> None:
        return None


class JsonlTracer:
    def __init__(
        self,
        path: Path | None = None,
        *,
        settings: Settings | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        directory = path.parent if path else self.settings.trace_dir
        directory.mkdir(parents=True, exist_ok=True)
        self.path = path or (directory / "memory.jsonl")
        self._lock = threading.Lock()

    def record(self, kind: str, payload: dict[str, Any]) -> None:
        record = {
            "ts": datetime.now(UTC).isoformat(),
            "kind": kind,
            "schema_version": self.settings.schema_version or SCHEMA_VERSION,
            "model_version": self.settings.model_version,
            "prompt_version": self.settings.prompt_version,
            **payload,
        }
        line = json.dumps(record, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line)
