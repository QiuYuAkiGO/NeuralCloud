"""Demo bootstrap: factory Vaelith lives in presets/builtin/vaelith.json."""

from __future__ import annotations

from datetime import datetime

from gui.presets import DEFAULT_PRESET_ID, DEFAULT_SESSION_ID, hydrate_memory, load_builtin
from harness.memory.service import MemoryService

__all__ = ["DEFAULT_PRESET_ID", "DEFAULT_SESSION_ID", "bootstrap_demo"]


def bootstrap_demo(service: MemoryService, *, now: datetime | None = None) -> None:
    del now
    hydrate_memory(service, load_builtin(DEFAULT_PRESET_ID))
