"""Stable, transport-neutral embodiment ports."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from harness.embodiment.capabilities import ProviderCapabilities, ProviderHealth
from harness.embodiment.contracts import BodyStateSnapshot, PerceptionSnapshot
from harness.runtime.models import ActionBackendResult, ActionExecution


class ActionBackend(Protocol):
    async def execute(self, execution: ActionExecution) -> ActionBackendResult: ...

    async def request_safe_point(self, execution: ActionExecution) -> ActionBackendResult: ...

    async def verify(self, execution: ActionExecution) -> ActionBackendResult: ...

    async def reconcile(self, execution: ActionExecution) -> ActionBackendResult: ...


class PerceptionBackend(Protocol):
    async def observe(self, *, now: datetime | None = None) -> PerceptionSnapshot: ...


class BodyStateProvider(Protocol):
    async def snapshot(self) -> BodyStateSnapshot: ...


class CapabilityProvider(Protocol):
    async def capabilities(self) -> ProviderCapabilities: ...

    async def health(self) -> ProviderHealth: ...
