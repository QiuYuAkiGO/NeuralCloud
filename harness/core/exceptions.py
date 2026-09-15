"""Harness-wide exceptions."""

from __future__ import annotations


class HarnessError(Exception):
    """Base error for the memory subsystem."""


class PersonaLockedError(HarnessError):
    """Persona Core is locked; AI may not mutate it."""


class EvidenceRequiredError(HarnessError):
    """Long-term memory writes require a non-empty evidence chain."""


class InvalidCuratorOpError(HarnessError):
    """Curator emitted an op that failed schema or safety checks."""


class MemoryNotFoundError(HarnessError):
    """Requested memory/state row does not exist."""


class LLMError(HarnessError):
    """Remote LLM call failed or returned an unusable payload."""
