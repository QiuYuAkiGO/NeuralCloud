"""Stable, transport-neutral embodiment ports."""

from __future__ import annotations

from datetime import datetime
from collections.abc import Callable
from typing import Protocol

from harness.embodiment.capabilities import EmbodimentStatus, ProviderCapabilities, ProviderHealth
from harness.embodiment.web_contracts import WebResult, WebSearchArguments
from harness.embodiment.server_contracts import ServerCommandArguments, ServerCommandResult
from harness.embodiment.contracts import BodyStateSnapshot, PerceptionSnapshot
from harness.embodiment.qq_contracts import (
    QQCommunicationStatus, QQDeliveryResult, QQReadRequest, QQReadResult,
    QQReconcileRequest, QQSendRequest,
)
from harness.runtime.models import ActionBackendResult, ActionExecution
from harness.embodiment.board_contracts import (
    BoardAppendRequest, BoardReadRequest, BoardReadResult, BoardReceipt,
    BoardReconcileRequest, BoardReconcileResult, BoardStatus,
)
from harness.embodiment.voice_contracts import (
    VoiceJob, VoiceProfile, VoiceRenderRequest, VoiceStatus,
)


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


class WebInformationPort(Protocol):
    """Negotiated search/extraction; Runtime resolves model source references."""

    async def status(self) -> EmbodimentStatus: ...

    async def search(self, request: WebSearchArguments) -> WebResult: ...

    async def read(self, urls: list[str]) -> WebResult: ...

    async def close(self) -> None: ...


class ServerExecutionPort(Protocol):
    """Negotiated remote command execution; Runtime owns classification and receipts."""

    async def status(self) -> EmbodimentStatus: ...

    async def run(self, request: ServerCommandArguments, *, deadline: datetime | None = None) -> ServerCommandResult: ...

    async def close(self) -> None: ...


class QQCommunicationPort(Protocol):
    """All operations enter negotiated driver gates, never a second GUI actor."""

    async def status(self) -> QQCommunicationStatus: ...

    async def read(self, request: QQReadRequest) -> QQReadResult: ...

    async def send(self, request: QQSendRequest) -> QQDeliveryResult: ...

    async def reconcile(self, request: QQReconcileRequest) -> QQDeliveryResult: ...


class BoardExpressionPort(Protocol):
    """Private expression with negotiated append and durable read-back evidence."""

    async def status(self) -> BoardStatus: ...

    async def read(self, request: BoardReadRequest) -> BoardReadResult: ...

    async def append(self, request: BoardAppendRequest) -> BoardReceipt: ...

    async def reconcile(self, request: BoardReconcileRequest) -> BoardReconcileResult: ...


class VoiceAssetPort(Protocol):
    """Fixed-profile voice generation and asset evidence, independent of playback."""

    async def activate(self) -> VoiceStatus: ...

    async def status(self) -> VoiceStatus: ...

    async def profiles(self) -> list[VoiceProfile]: ...

    async def profile(self) -> VoiceProfile: ...

    async def render(self, request: VoiceRenderRequest, *, before_mutation: Callable[[], None] | None = None) -> VoiceJob: ...

    async def job(self, job_id: str) -> VoiceJob: ...

    async def cancel(self, job_id: str, *, before_mutation: Callable[[], None] | None = None) -> VoiceJob: ...

    async def asset(self, digest: str) -> tuple[bytes, str]: ...

    def restore_binding(self, job_id: str, input_sha256: str) -> None: ...

    async def close(self) -> None: ...
